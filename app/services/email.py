import logging
import os
import smtplib
import socket
import ssl
import threading
from collections.abc import Generator
from contextlib import contextmanager
from email import encoders
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TypedDict
from urllib.parse import quote
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.email_profile import EmailModule
from app.services.domain_settings import AMBIENT, _Ambient
from app.services.people.hr.invite_email import (
    default_employee_invite_email_template,
    render_employee_invite_email_template,
)

logger = logging.getLogger(__name__)


# SMTP Connection Pool configuration
SMTP_POOL_SIZE = int(os.getenv("SMTP_POOL_SIZE", "5"))
SMTP_POOL_TIMEOUT = int(os.getenv("SMTP_POOL_TIMEOUT", "30"))


class SMTPConfig(TypedDict):
    host: str
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    use_ssl: bool
    from_email: str
    from_name: str
    reply_to: str | None


class SMTPConnectionPool:
    """
    Thread-safe SMTP connection pool for efficient email sending.

    Maintains a pool of SMTP connections to avoid the overhead of
    creating new connections for each email.
    """

    def __init__(self, max_connections: int = SMTP_POOL_SIZE):
        self._pool: list[smtplib.SMTP | smtplib.SMTP_SSL] = []
        self._lock = threading.Lock()
        self._max_connections = max_connections
        self._config: dict | None = None
        self._config_hash: str | None = None

    def _get_config_hash(self, config: SMTPConfig) -> str:
        """Generate hash of config to detect changes."""
        return f"{config['host']}:{config['port']}:{config.get('username', '')}:{config['use_ssl']}:{config['use_tls']}"

    def _create_connection(self, config: SMTPConfig) -> smtplib.SMTP | smtplib.SMTP_SSL:
        """Create a new SMTP connection."""
        timeout = SMTP_POOL_TIMEOUT
        server: smtplib.SMTP | smtplib.SMTP_SSL
        if config["use_ssl"]:
            server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=timeout)
        else:
            server = smtplib.SMTP(config["host"], config["port"], timeout=timeout)
            if config["use_tls"]:
                server.starttls()

        if config["username"] and config["password"]:
            server.login(config["username"], config["password"])

        return server

    def _is_connection_alive(self, conn: smtplib.SMTP | smtplib.SMTP_SSL) -> bool:
        """Check if a connection is still alive."""
        try:
            status = conn.noop()[0]
            return status == 250
        except (smtplib.SMTPException, OSError):
            return False

    def _close_connection(self, conn: smtplib.SMTP | smtplib.SMTP_SSL) -> None:
        """Safely close a connection."""
        try:
            conn.quit()
        except (smtplib.SMTPException, OSError):
            try:
                conn.close()
            except (smtplib.SMTPException, OSError):
                pass

    @contextmanager
    def get_connection(
        self, config: SMTPConfig
    ) -> Generator[smtplib.SMTP | smtplib.SMTP_SSL, None, None]:
        """
        Get an SMTP connection from the pool.

        Creates a new connection if the pool is empty.
        Returns the connection to the pool after use.
        """
        config_hash = self._get_config_hash(config)
        conn: smtplib.SMTP | smtplib.SMTP_SSL | None = None

        with self._lock:
            # Clear pool if config changed
            if self._config_hash != config_hash:
                for old_conn in self._pool:
                    self._close_connection(old_conn)
                self._pool.clear()
                self._config_hash = config_hash

            # Try to get a connection from the pool
            while self._pool:
                candidate = self._pool.pop()
                if self._is_connection_alive(candidate):
                    conn = candidate
                    break
                else:
                    self._close_connection(candidate)

        # Create new connection if needed
        if conn is None:
            conn = self._create_connection(config)

        try:
            yield conn
            # Return connection to pool if still alive and pool not full
            with self._lock:
                if len(
                    self._pool
                ) < self._max_connections and self._is_connection_alive(conn):
                    self._pool.append(conn)
                    conn = None  # Don't close it
        finally:
            if conn is not None:
                self._close_connection(conn)

    def clear(self) -> None:
        """Clear all connections from the pool."""
        with self._lock:
            for conn in self._pool:
                self._close_connection(conn)
            self._pool.clear()
            self._config_hash = None


# Global connection pool instance
_smtp_pool = SMTPConnectionPool()


def person_can_receive_email(person: object | None) -> bool:
    """Return whether a person record is eligible for email delivery."""
    if person is None:
        return False

    email = getattr(person, "email", None)
    if not isinstance(email, str) or not email.strip():
        return False

    if getattr(person, "is_active", True) is False:
        return False

    try:
        from app.models.person import PersonStatus

        status = getattr(person, "status", None)
        if status is not None and status != PersonStatus.active:
            return False
    except (ImportError, AttributeError):
        logger.debug("Could not resolve person status while checking email eligibility")

    return True


def employee_can_receive_email(employee: object | None) -> bool:
    """Return whether an employee record is eligible for email delivery."""
    if employee is None:
        return False

    try:
        from app.models.people.hr.employee import EmployeeStatus

        inactive_statuses = {
            EmployeeStatus.RESIGNED,
            EmployeeStatus.TERMINATED,
            EmployeeStatus.RETIRED,
        }
        status = getattr(employee, "status", None)
        if status in inactive_statuses:
            return False
    except (ImportError, AttributeError):
        logger.debug(
            "Could not resolve employee status while checking email eligibility"
        )

    person = getattr(employee, "person", None)
    if person is not None:
        return person_can_receive_email(person)

    email = getattr(employee, "work_email", None) or getattr(
        employee, "personal_email", None
    )
    return isinstance(email, str) and bool(email.strip())


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def _env_int(name: str, default: int) -> int:
    raw = _env_value(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_value(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _get_db_setting(
    db: Session | None,
    key: str,
    *,
    organization_id: "UUID | None | _Ambient" = AMBIENT,
) -> object | None:
    """Get a setting value from the database."""
    if db is None:
        return None
    try:
        from app.services.settings_spec import resolve_value

        return resolve_value(
            db, SettingDomain.email, key, organization_id=organization_id
        )
    except (ImportError, AttributeError, KeyError) as exc:
        logger.debug("Could not resolve email setting %s: %s", key, exc)
        return None


def _get_smtp_config(
    db: Session | None = None,
    *,
    organization_id: "UUID | None | _Ambient" = AMBIENT,
) -> SMTPConfig:
    """Get SMTP config from database first, then fall back to environment variables."""
    # Try DB settings first, then env vars, then defaults
    host_raw = (
        _get_db_setting(db, "smtp_host", organization_id=organization_id)
        or _env_value("SMTP_HOST")
        or "localhost"
    )
    host = str(host_raw)
    port = _get_db_setting(
        db, "smtp_port", organization_id=organization_id
    ) or _env_int("SMTP_PORT", 587)
    username_raw = _get_db_setting(
        db, "smtp_username", organization_id=organization_id
    ) or _env_value("SMTP_USERNAME")
    username = str(username_raw) if username_raw is not None else None
    password_raw = _get_db_setting(
        db, "smtp_password", organization_id=organization_id
    ) or _env_value("SMTP_PASSWORD")
    password = str(password_raw) if password_raw is not None else None

    # Boolean settings
    use_tls_db = _get_db_setting(db, "smtp_use_tls", organization_id=organization_id)
    use_tls = use_tls_db if use_tls_db is not None else _env_bool("SMTP_USE_TLS", True)

    use_ssl_db = _get_db_setting(db, "smtp_use_ssl", organization_id=organization_id)
    use_ssl = use_ssl_db if use_ssl_db is not None else _env_bool("SMTP_USE_SSL", False)

    from_email_raw = (
        _get_db_setting(db, "smtp_from_email", organization_id=organization_id)
        or _env_value("SMTP_FROM_EMAIL")
        or "noreply@example.com"
    )
    from_email = str(from_email_raw)
    from_name_raw = (
        _get_db_setting(db, "smtp_from_name", organization_id=organization_id)
        or _env_value("SMTP_FROM_NAME")
        or "Dotmac ERP"
    )
    from_name = str(from_name_raw)
    reply_to_raw = _get_db_setting(
        db, "email_reply_to", organization_id=organization_id
    ) or _env_value("EMAIL_REPLY_TO")
    reply_to = str(reply_to_raw) if reply_to_raw is not None else None

    port_int = 587
    if port is not None:
        try:
            port_int = int(str(port))
        except (TypeError, ValueError):
            port_int = 587

    return {
        "host": host,
        "port": port_int,
        "username": username,
        "password": password,
        "use_tls": bool(use_tls),
        "use_ssl": bool(use_ssl),
        "from_email": from_email,
        "from_name": from_name,
        "reply_to": reply_to,
    }


def _get_module_smtp_config(
    db: Session | None,
    organization_id: UUID | None,
    module: EmailModule | None,
) -> SMTPConfig | None:
    if db is None or module is None:
        return None
    from app.services.email_profile import EmailProfileService

    profile = EmailProfileService(db).get_profile_for_module(organization_id, module)
    if profile:
        return profile.to_config_dict()
    return None


def validate_smtp_config(
    config: SMTPConfig, timeout_seconds: int = 10
) -> tuple[bool, str | None]:
    """Validate SMTP settings by attempting a connection and (optional) auth."""
    host = str(config.get("host") or "").strip()
    if not host:
        return False, "SMTP host is required."

    try:
        port = int(config.get("port") or 0)
    except (TypeError, ValueError):
        return False, "SMTP port must be a valid integer."
    if port <= 0:
        return False, "SMTP port must be a positive integer."

    use_tls = bool(config.get("use_tls"))
    use_ssl = bool(config.get("use_ssl"))
    if use_tls and use_ssl:
        return False, "SMTP TLS and SSL cannot both be enabled."

    username = config.get("username")
    password = config.get("password")
    if (username and not password) or (password and not username):
        return False, "SMTP username and password must both be set."

    server: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=timeout_seconds)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout_seconds)

        server.ehlo()
        if use_tls and not use_ssl:
            server.starttls()
            server.ehlo()

        if username and password:
            server.login(username, password)

        # NOOP ensures server accepts commands after connect/auth
        server.noop()
        return True, None
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP validation failed for host %s:%s: authentication failed", host, port
        )
        return False, "SMTP authentication failed. Check username and password."
    except smtplib.SMTPConnectError as exc:
        logger.error("SMTP validation failed for host %s:%s: %s", host, port, exc)
        return False, "SMTP connection failed. Check host/port and firewall."
    except (socket.gaierror, ssl.SSLError, OSError) as exc:
        logger.error("SMTP validation failed for host %s:%s: %s", host, port, exc)
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == 111:
            return False, "SMTP connection refused. Check host/port and firewall."
        return False, "Unable to connect to the SMTP server with the provided settings."
    except Exception as exc:
        logger.error("SMTP validation failed for host %s:%s: %s", host, port, exc)
        return False, "Unable to connect to the SMTP server with the provided settings."
    finally:
        if server:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError) as quit_exc:
                logger.debug("SMTP quit failed during validation: %s", quit_exc)
                try:
                    server.close()
                except (smtplib.SMTPException, OSError) as close_exc:
                    logger.debug(
                        "SMTP close also failed during validation: %s", close_exc
                    )


def send_email(
    db: Session | None,
    to_email: str,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    raise_on_error: bool = False,
    *,
    module: EmailModule | None = EmailModule.ADMIN,
    organization_id: UUID | None = None,
) -> bool:
    """
    Send an email using SMTP settings from database or environment.

    Args:
        db: Database session (optional, for retrieving SMTP settings)
        to_email: Recipient email address
        subject: Email subject
        body_html: HTML body content
        body_text: Plain text body content (optional)
        attachments: List of attachments as (filename, data, mime_type) tuples
            Example: [("payslip.pdf", pdf_bytes, "application/pdf")]
        module: Email module used for profile routing (defaults to ADMIN)
        organization_id: Organization scope for module routing (optional)
        raise_on_error: If True, re-raise exceptions instead of returning False.
                       Use this for async tasks that need to classify errors.

    Returns:
        True if email was sent successfully, False otherwise

    Raises:
        smtplib.SMTPException: If raise_on_error=True and sending fails
    """
    config = _get_module_smtp_config(db, organization_id, module) or _get_smtp_config(
        db
    )

    # Use mixed multipart to support both alternative content and attachments
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = f"{config['from_name']} <{config['from_email']}>"
    msg["To"] = to_email

    # Add Reply-To header if configured
    reply_to = config.get("reply_to")
    if reply_to:
        msg["Reply-To"] = reply_to

    # Create alternative part for text/html body
    msg_alternative = MIMEMultipart("alternative")
    if body_text:
        msg_alternative.attach(MIMEText(body_text, "plain"))
    msg_alternative.attach(MIMEText(body_html, "html"))
    msg.attach(msg_alternative)

    # Add attachments if provided
    if attachments:
        for filename, data, mime_type in attachments:
            maintype, subtype = (
                mime_type.split("/", 1)
                if "/" in mime_type
                else ("application", "octet-stream")
            )
            attachment_part: MIMEBase
            if maintype == "application" and subtype == "pdf":
                attachment_part = MIMEApplication(data, _subtype=subtype)
            else:
                attachment_part = MIMEBase(maintype, subtype)
                attachment_part.set_payload(data)
                encoders.encode_base64(attachment_part)
            attachment_part.add_header(
                "Content-Disposition",
                "attachment",
                filename=filename,
            )
            msg.attach(attachment_part)

    try:
        with _smtp_pool.get_connection(config) as server:
            server.sendmail(config["from_email"], to_email, msg.as_string())
        logger.info("Email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        if raise_on_error:
            raise
        return False


def send_password_reset_email(
    db: Session | None,
    to_email: str,
    reset_token: str,
    person_name: str | None = None,
    app_url: str | None = None,
    organization_id: UUID | None = None,
    next_url: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    email_template: dict[str, str] | None = None,
) -> bool:
    name = person_name or "there"
    env_app_url = _env_value("APP_URL")
    resolved_app_url = env_app_url or app_url or "http://localhost:8000"
    reset_link = f"{resolved_app_url.rstrip('/')}/reset-password?token={reset_token}"
    if next_url:
        reset_link = f"{reset_link}&next={quote(next_url, safe='/')}"
    rendered = render_employee_invite_email_template(
        email_template or default_employee_invite_email_template(),
        name=name,
        reset_link=reset_link,
    )
    return send_email(
        db,
        to_email,
        rendered["subject"],
        rendered["body_html"],
        rendered["body_text"],
        attachments=attachments,
        module=EmailModule.ADMIN,
        organization_id=organization_id,
    )


# Async email sending convenience function
def queue_email(
    to_email: str,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    *,
    module: EmailModule | None = EmailModule.ADMIN,
    organization_id: UUID | None = None,
) -> None:
    """
    Queue an email for async delivery via Celery.

    Use this for non-blocking email sends where immediate delivery is not required.
    For immediate delivery (e.g., password reset), use send_email() directly.

    Args:
        to_email: Recipient email address
        subject: Email subject
        body_html: HTML body content
        body_text: Plain text body content (optional)
        attachments: List of attachments as (filename, data, mime_type) tuples
    """
    from app.tasks.email import queue_email as _queue_email

    module_value = module.value if module else None
    _queue_email(
        to_email,
        subject,
        body_html,
        body_text,
        attachments,
        module=module_value,
        organization_id=str(organization_id) if organization_id else None,
    )


from app.services.setting_domain_declaration import ModuleSettingDomains  # noqa: E402

# Setting domain(s) this module owns — outbound mail transport and templates.
# Validated by `app.services.setting_domains` at startup and at every write;
# see that module for why ownership lives here rather than in a central list.
SETTING_DOMAINS = ModuleSettingDomains(setting_domains=("email",))
