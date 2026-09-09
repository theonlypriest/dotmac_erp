#!/usr/bin/env python3
"""
Bootstrap Instance Script — Provision a new single-tenant DotMac ERP instance.

Creates a self-contained deployment directory with:
  - docker-compose.yml (unique container names, ports, volumes)
  - .env (generated secrets, org config)
  - bootstrap_db.py (run inside the container to seed org + admin user + RBAC)

Works for both local VPS (multiple instances on one server) and remote VPS
(copy the directory to another machine and run).

Usage:
    # Interactive mode (prompts for all values)
    python scripts/bootstrap_instance.py

    # Non-interactive mode
    python scripts/bootstrap_instance.py \
        --org-code ACME \
        --org-name "ACME Corp" \
        --sector-type PRIVATE \
        --framework IFRS \
        --currency USD \
        --admin-email admin@acme.com \
        --admin-username admin \
        --admin-password "secure-password-here" \
        --app-port 8010 \
        --db-port 5440 \
        --redis-port 6390

    # Remote VPS mode (generates a tarball you can scp to another server)
    python scripts/bootstrap_instance.py ... --package
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
import textwrap
import uuid

# ---------------------------------------------------------------------------
# Defaults & Constants
# ---------------------------------------------------------------------------
BASE_APP_PORT = 8010  # First instance starts here
BASE_DB_PORT = 5440  # First instance DB port
BASE_REDIS_PORT = 6390  # First instance Redis port
BASE_OPENBAO_PORT = 8210  # First instance OpenBao port
INSTANCES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "instances")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECTOR_CHOICES = ["PRIVATE", "PUBLIC", "NGO"]
FRAMEWORK_CHOICES = ["IFRS", "IPSAS", "BOTH"]


def generate_secret(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def generate_fernet_key() -> str:
    """Generate a Fernet key for TOTP encryption."""
    try:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()
    except ImportError:
        # Fallback: base64-encoded 32 bytes (valid Fernet key format)
        import base64

        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def find_next_ports(instances_dir: str) -> tuple[int, int, int, int]:
    """Scan existing instances to find next available port set."""
    app_port = BASE_APP_PORT
    db_port = BASE_DB_PORT
    redis_port = BASE_REDIS_PORT
    bao_port = BASE_OPENBAO_PORT

    if os.path.isdir(instances_dir):
        for name in os.listdir(instances_dir):
            env_path = os.path.join(instances_dir, name, ".env")
            if os.path.isfile(env_path):
                with open(env_path) as f:
                    for line in f:
                        if line.startswith("APP_PORT="):
                            used = int(line.strip().split("=", 1)[1])
                            app_port = max(app_port, used + 1)
                        elif line.startswith("DB_PORT="):
                            used = int(line.strip().split("=", 1)[1])
                            db_port = max(db_port, used + 1)
                        elif line.startswith("REDIS_PORT="):
                            used = int(line.strip().split("=", 1)[1])
                            redis_port = max(redis_port, used + 1)
                        elif line.startswith("OPENBAO_PORT="):
                            used = int(line.strip().split("=", 1)[1])
                            bao_port = max(bao_port, used + 1)

    return app_port, db_port, redis_port, bao_port


def prompt_if_missing(args: argparse.Namespace) -> argparse.Namespace:
    """Interactively prompt for any missing required values."""

    if not args.org_code:
        args.org_code = input("Organization code (short, e.g. ACME): ").strip().upper()
    if not args.org_name:
        args.org_name = input("Organization legal name: ").strip()
    if not args.sector_type:
        print(f"Sector type options: {', '.join(SECTOR_CHOICES)}")
        args.sector_type = input("Sector type [PRIVATE]: ").strip().upper() or "PRIVATE"
    if not args.framework:
        print(f"Accounting framework options: {', '.join(FRAMEWORK_CHOICES)}")
        args.framework = (
            input("Accounting framework [IFRS]: ").strip().upper() or "IFRS"
        )
    if not args.currency:
        args.currency = (
            input("Functional currency code [NGN]: ").strip().upper() or "NGN"
        )
    if not args.admin_email:
        args.admin_email = input("Admin email: ").strip()
    if not args.admin_username:
        default_user = args.admin_email.split("@")[0] if args.admin_email else "admin"
        args.admin_username = (
            input(f"Admin username [{default_user}]: ").strip() or default_user
        )
    if not args.admin_password:
        args.admin_password = input("Admin password: ").strip()

    # Validate
    if args.sector_type not in SECTOR_CHOICES:
        print(f"Error: sector_type must be one of {SECTOR_CHOICES}")
        sys.exit(1)
    if args.framework not in FRAMEWORK_CHOICES:
        print(f"Error: framework must be one of {FRAMEWORK_CHOICES}")
        sys.exit(1)

    return args


# ---------------------------------------------------------------------------
# File generators
# ---------------------------------------------------------------------------


def generate_env(
    org_id: str,
    org_code: str,
    org_name: str,
    sector_type: str,
    framework: str,
    currency: str,
    admin_email: str,
    admin_username: str,
    admin_password: str,
    app_port: int,
    db_port: int,
    redis_port: int,
    bao_port: int,
    app_url: str,
) -> str:
    pg_password = generate_secret(16)
    redis_password = generate_secret(16)
    jwt_secret = generate_secret(32)
    totp_key = generate_fernet_key()
    bao_token = generate_secret(16)
    db_name = f"dotmac_{org_code.lower()}"

    return textwrap.dedent(f"""\
        # =============================================================================
        # DotMac ERP Instance: {org_name} ({org_code})
        # Generated by bootstrap_instance.py
        # =============================================================================

        # Instance identity
        INSTANCE_ORG_CODE={org_code}
        DEFAULT_ORGANIZATION_ID={org_id}

        # Ports (unique per instance on same host)
        APP_PORT={app_port}
        DB_PORT={db_port}
        REDIS_PORT={redis_port}
        OPENBAO_PORT={bao_port}

        # Database
        POSTGRES_USER=postgres
        POSTGRES_PASSWORD={pg_password}
        POSTGRES_DB={db_name}
        DATABASE_URL=postgresql+psycopg://postgres:{pg_password}@db:5432/{db_name}

        # Redis
        REDIS_PASSWORD={redis_password}
        REDIS_URL=redis://:{redis_password}@redis:6379/0
        CELERY_BROKER_URL=redis://:{redis_password}@redis:6379/0
        CELERY_RESULT_BACKEND=redis://:{redis_password}@redis:6379/1
        CELERY_TIMEZONE=UTC

        # Authentication secrets
        JWT_SECRET={jwt_secret}
        JWT_ALGORITHM=HS256
        JWT_ACCESS_TTL_MINUTES=60
        JWT_REFRESH_TTL_DAYS=30
        TOTP_ISSUER=dotmac_erp
        TOTP_ENCRYPTION_KEY={totp_key}

        # Cookie Security
        REFRESH_COOKIE_NAME=refresh_token
        REFRESH_COOKIE_SECURE=true
        REFRESH_COOKIE_SAMESITE=strict
        REFRESH_COOKIE_DOMAIN=
        REFRESH_COOKIE_PATH=/

        # OpenBao
        OPENBAO_ADDR=http://openbao:8200
        OPENBAO_TOKEN={bao_token}
        OPENBAO_ALLOW_INSECURE=true

        # Branding
        BRAND_NAME={org_name}
        BRAND_TAGLINE=
        BRAND_LOGO_URL=
        APP_URL={app_url}
        DEFAULT_FUNCTIONAL_CURRENCY_CODE={currency}
        DEFAULT_PRESENTATION_CURRENCY_CODE={currency}

        # Build-only pointer. Set this to an absolute, readable local file that
        # contains the approved Forgejo package-read token. Never paste the
        # token value into this generated environment file.
        FORGEJO_TOKEN_FILE=

        # Gunicorn
        GUNICORN_WORKERS=2
        GUNICORN_LOG_LEVEL=info

        # ----- Bootstrap data (used by bootstrap_db.py, not by the app) -----
        BOOTSTRAP_ORG_CODE={org_code}
        BOOTSTRAP_ORG_NAME={org_name}
        BOOTSTRAP_SECTOR_TYPE={sector_type}
        BOOTSTRAP_FRAMEWORK={framework}
        BOOTSTRAP_CURRENCY={currency}
        BOOTSTRAP_ADMIN_EMAIL={admin_email}
        BOOTSTRAP_ADMIN_USERNAME={admin_username}
        BOOTSTRAP_ADMIN_PASSWORD={admin_password}
    """)


def generate_docker_compose(org_code: str) -> str:
    slug = org_code.lower()
    return textwrap.dedent(f"""\
        # DotMac ERP Instance: {org_code}
        # Generated by bootstrap_instance.py

        services:
          openbao:
            image: openbao/openbao:2
            container_name: dotmac_{slug}_openbao
            restart: unless-stopped
            cap_add:
              - IPC_LOCK
            environment:
              BAO_DEV_ROOT_TOKEN_ID: ${{OPENBAO_TOKEN}}
              BAO_DEV_LISTEN_ADDRESS: 0.0.0.0:8200
            ports:
              - "${{OPENBAO_PORT}}:8200"
            volumes:
              - openbao_data:/openbao/data
            command: server -dev
            healthcheck:
              test: ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:8200/v1/sys/health"]
              interval: 10s
              timeout: 5s
              retries: 3

          app:
            build:
              context: ${{APP_BUILD_CONTEXT:-./../..}}
              secrets:
                - forgejo_token
            container_name: dotmac_{slug}_app
            restart: unless-stopped
            ports:
              - "${{APP_PORT}}:8002"
            env_file:
              - .env
            environment:
              DATABASE_URL: postgresql+psycopg://postgres:${{POSTGRES_PASSWORD}}@db:5432/${{POSTGRES_DB}}
              REDIS_URL: redis://:${{REDIS_PASSWORD}}@redis:6379/0
              CELERY_BROKER_URL: redis://:${{REDIS_PASSWORD}}@redis:6379/0
              CELERY_RESULT_BACKEND: redis://:${{REDIS_PASSWORD}}@redis:6379/1
              OPENBAO_ADDR: http://openbao:8200
              OPENBAO_TOKEN: ${{OPENBAO_TOKEN}}
              OPENBAO_ALLOW_INSECURE: "true"
              GUNICORN_WORKERS: "${{GUNICORN_WORKERS:-2}}"
            depends_on:
              db:
                condition: service_healthy
              redis:
                condition: service_started
              openbao:
                condition: service_healthy
            volumes:
              - dotmac_logs:/var/log/dotmac
            healthcheck:
              test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8002/health')"]
              interval: 30s
              timeout: 10s
              retries: 3
              start_period: 40s
            command: ["gunicorn", "-c", "gunicorn.conf.py", "app.main:app"]

          worker:
            build:
              context: ${{APP_BUILD_CONTEXT:-./../..}}
              secrets:
                - forgejo_token
            container_name: dotmac_{slug}_worker
            restart: unless-stopped
            env_file:
              - .env
            environment:
              DATABASE_URL: postgresql+psycopg://postgres:${{POSTGRES_PASSWORD}}@db:5432/${{POSTGRES_DB}}
              REDIS_URL: redis://:${{REDIS_PASSWORD}}@redis:6379/0
              CELERY_BROKER_URL: redis://:${{REDIS_PASSWORD}}@redis:6379/0
              CELERY_RESULT_BACKEND: redis://:${{REDIS_PASSWORD}}@redis:6379/1
              OPENBAO_ADDR: http://openbao:8200
              OPENBAO_TOKEN: ${{OPENBAO_TOKEN}}
              OPENBAO_ALLOW_INSECURE: "true"
            depends_on:
              db:
                condition: service_healthy
              redis:
                condition: service_started
              openbao:
                condition: service_healthy
            command: ["celery", "-A", "app.celery_app", "worker", "-l", "info"]

          beat:
            build:
              context: ${{APP_BUILD_CONTEXT:-./../..}}
              secrets:
                - forgejo_token
            container_name: dotmac_{slug}_beat
            restart: unless-stopped
            env_file:
              - .env
            environment:
              DATABASE_URL: postgresql+psycopg://postgres:${{POSTGRES_PASSWORD}}@db:5432/${{POSTGRES_DB}}
              REDIS_URL: redis://:${{REDIS_PASSWORD}}@redis:6379/0
              CELERY_BROKER_URL: redis://:${{REDIS_PASSWORD}}@redis:6379/0
              CELERY_RESULT_BACKEND: redis://:${{REDIS_PASSWORD}}@redis:6379/1
              OPENBAO_ADDR: http://openbao:8200
              OPENBAO_TOKEN: ${{OPENBAO_TOKEN}}
              OPENBAO_ALLOW_INSECURE: "true"
            depends_on:
              db:
                condition: service_healthy
              redis:
                condition: service_started
              openbao:
                condition: service_healthy
            command: ["celery", "-A", "app.celery_app", "beat", "-l", "info"]

          db:
            image: postgres:16
            container_name: dotmac_{slug}_db
            restart: unless-stopped
            environment:
              POSTGRES_USER: ${{POSTGRES_USER:-postgres}}
              POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD}}
              POSTGRES_DB: ${{POSTGRES_DB}}
            ports:
              - "${{DB_PORT}}:5432"
            volumes:
              - db_data:/var/lib/postgresql/data
            healthcheck:
              test: ["CMD-SHELL", "pg_isready -U ${{POSTGRES_USER:-postgres}} -d ${{POSTGRES_DB}}"]
              interval: 10s
              timeout: 5s
              retries: 5

          redis:
            image: redis:7
            container_name: dotmac_{slug}_redis
            restart: unless-stopped
            command: ["redis-server", "--requirepass", "${{REDIS_PASSWORD}}"]
            ports:
              - "${{REDIS_PORT}}:6379"

        secrets:
          forgejo_token:
            file: ${{FORGEJO_TOKEN_FILE:?Set FORGEJO_TOKEN_FILE to an absolute readable local token file}}

        volumes:
          db_data:
          openbao_data:
          dotmac_logs:
    """)


def generate_bootstrap_db_script() -> str:
    """Python script that runs INSIDE the container to seed org + admin."""
    return textwrap.dedent('''\
        #!/usr/bin/env python3
        """
        Bootstrap Database — Seeds organization, admin user, and RBAC.

        Runs inside the app container after migrations:
            docker compose exec app python bootstrap_db.py
        """
        import os
        import sys

        from dotenv import load_dotenv

        load_dotenv()

        # Add project root to path (we're running from /app inside container)
        sys.path.insert(0, "/app")

        from app.db import SessionLocal
        from app.models.auth import AuthProvider, UserCredential
        from app.models.finance.core_org.organization import (
            AccountingFramework,
            Organization,
            SectorType,
        )
        from app.models.person import Person
        from app.models.rbac import Permission, PersonRole, Role, RolePermission
        from app.services.auth_flow import hash_password
        from app.services.tenant_projection import reconcile_organization_tenant
        from scripts.seed_rbac import DEFAULT_PERMISSIONS, DEFAULT_ROLES


        def main():
            org_id = os.environ["DEFAULT_ORGANIZATION_ID"]
            org_code = os.environ["BOOTSTRAP_ORG_CODE"]
            org_name = os.environ["BOOTSTRAP_ORG_NAME"]
            sector_type = os.environ.get("BOOTSTRAP_SECTOR_TYPE", "PRIVATE")
            framework = os.environ.get("BOOTSTRAP_FRAMEWORK", "IFRS")
            currency = os.environ.get("BOOTSTRAP_CURRENCY", "NGN")
            admin_email = os.environ["BOOTSTRAP_ADMIN_EMAIL"]
            admin_username = os.environ["BOOTSTRAP_ADMIN_USERNAME"]
            admin_password = os.environ["BOOTSTRAP_ADMIN_PASSWORD"]

            db = SessionLocal()
            try:
                # --- 1. Create Organization ---
                org = db.query(Organization).filter(
                    Organization.organization_code == org_code
                ).first()

                if not org:
                    from uuid import UUID
                    is_public = sector_type in ("PUBLIC", "NGO")

                    org = Organization(
                        organization_id=UUID(org_id),
                        organization_code=org_code,
                        legal_name=org_name,
                        sector_type=SectorType(sector_type),
                        accounting_framework=AccountingFramework(framework),
                        fund_accounting_enabled=is_public,
                        commitment_control_enabled=(sector_type == "PUBLIC"),
                        functional_currency_code=currency,
                        presentation_currency_code=currency,
                        fiscal_year_end_month=12,
                        fiscal_year_end_day=31,
                        is_active=True,
                    )
                    db.add(org)
                    db.flush()
                    print(f"  Created organization: {org_name} ({org_code})")
                    print(f"    ID:        {org_id}")
                    print(f"    Sector:    {sector_type}")
                    print(f"    Framework: {framework}")
                    print(f"    Currency:  {currency}")
                else:
                    print(f"  Organization exists: {org_code}")

                reconcile_organization_tenant(db, org)

                # --- 2. Seed RBAC (permissions + roles) ---
                print("  Setting up RBAC...")
                for key, description in DEFAULT_PERMISSIONS:
                    perm = db.query(Permission).filter(Permission.key == key).first()
                    if not perm:
                        perm = Permission(key=key, description=description, is_active=True)
                        db.add(perm)
                db.flush()
                print(f"    Permissions: {len(DEFAULT_PERMISSIONS)}")

                for name, description in DEFAULT_ROLES:
                    role = db.query(Role).filter(Role.name == name).first()
                    if not role:
                        role = Role(name=name, description=description, is_active=True)
                        db.add(role)
                db.flush()
                print(f"    Roles: {len(DEFAULT_ROLES)}")

                admin_role = db.query(Role).filter(Role.name == "admin").first()
                all_perms = db.query(Permission).all()
                for perm in all_perms:
                    link = (
                        db.query(RolePermission)
                        .filter(
                            RolePermission.role_id == admin_role.id,
                            RolePermission.permission_id == perm.id,
                        )
                        .first()
                    )
                    if not link:
                        db.add(RolePermission(role_id=admin_role.id, permission_id=perm.id))
                db.flush()
                print(f"    Admin role: {len(all_perms)} permissions")

                # --- 3. Create admin user ---
                person = db.query(Person).filter(Person.email == admin_email).first()
                if not person:
                    person = Person(
                        first_name="Admin",
                        last_name=org_code.title(),
                        email=admin_email,
                        organization_id=org.organization_id,
                    )
                    db.add(person)
                    db.flush()
                    print(f"  Created admin person: {admin_email}")
                else:
                    print(f"  Admin person exists: {admin_email}")

                credential = (
                    db.query(UserCredential)
                    .filter(
                        UserCredential.person_id == person.id,
                        UserCredential.provider == AuthProvider.local,
                    )
                    .first()
                )
                if not credential:
                    credential = UserCredential(
                        person_id=person.id,
                        provider=AuthProvider.local,
                        username=admin_username,
                        password_hash=hash_password(admin_password),
                        must_change_password=False,
                    )
                    db.add(credential)
                    db.flush()
                    print(f"  Created credential: {admin_username}")
                else:
                    print(f"  Credential exists: {credential.username}")

                # Assign admin role
                link = (
                    db.query(PersonRole)
                    .filter(
                        PersonRole.person_id == person.id,
                        PersonRole.role_id == admin_role.id,
                    )
                    .first()
                )
                if not link:
                    db.add(PersonRole(person_id=person.id, role_id=admin_role.id))

                db.commit()
                print("\\n=== Bootstrap complete ===")
                print(f"  Login at: {os.environ.get('APP_URL', 'http://localhost')}")
                print(f"  Username: {admin_username}")

            except Exception as e:
                db.rollback()
                print(f"Error: {e}")
                raise
            finally:
                db.close()


        if __name__ == "__main__":
            main()
    ''')


def generate_setup_script(org_code: str) -> str:
    """Shell script to bring up the instance and bootstrap the DB."""
    org_code.lower()
    return textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        # =====================================================================
        # DotMac ERP Instance Setup: {org_code}
        # =====================================================================
        # This script:
        #   1. Builds the Docker image (if not already built)
        #   2. Starts all containers
        #   3. Runs database migrations
        #   4. Seeds the organization, admin user, and RBAC
        # =====================================================================

        SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
        cd "$SCRIPT_DIR"

        echo ""
        echo "=== Setting up DotMac ERP instance: {org_code} ==="
        echo ""

        # The Dockerfile refuses private-package resolution without a BuildKit
        # secret. Resolve only its local file pointer from the caller or .env;
        # never source .env or read/echo the token value in this script.
        if [[ -z "${{FORGEJO_TOKEN_FILE:-}}" && -f .env ]]; then
            FORGEJO_TOKEN_FILE="$(sed -n 's/^FORGEJO_TOKEN_FILE=//p' .env | tail -n 1)"
        fi
        if [[ -z "${{FORGEJO_TOKEN_FILE:-}}" ]]; then
            echo "ERROR: set FORGEJO_TOKEN_FILE to an absolute file containing the approved Forgejo read token." >&2
            exit 2
        fi
        if [[ "$FORGEJO_TOKEN_FILE" != /* ]]; then
            echo "ERROR: FORGEJO_TOKEN_FILE must be an absolute path." >&2
            exit 2
        fi
        if [[ ! -f "$FORGEJO_TOKEN_FILE" || ! -r "$FORGEJO_TOKEN_FILE" || ! -s "$FORGEJO_TOKEN_FILE" ]]; then
            echo "ERROR: FORGEJO_TOKEN_FILE must identify a non-empty readable regular file." >&2
            exit 2
        fi
        export FORGEJO_TOKEN_FILE

        # Step 1: Start infrastructure (db, redis, openbao first)
        echo "[1/4] Starting infrastructure..."
        docker compose up -d db redis openbao
        echo "  Waiting for database to be healthy..."
        sleep 5

        # Step 2: Build and start app containers
        echo "[2/4] Building and starting app containers..."
        docker compose up -d --build app worker beat

        # Step 3: Run migrations
        echo "[3/4] Running database migrations..."
        docker compose exec app alembic upgrade head

        # Step 4: Bootstrap org + admin
        echo "[4/4] Bootstrapping organization and admin user..."
        docker compose exec app python bootstrap_db.py

        echo ""
        echo "=== Instance ready ==="
        APP_PORT=$(grep "^APP_PORT=" .env | cut -d= -f2)
        APP_URL=$(grep "^APP_URL=" .env | cut -d= -f2)
        echo "  URL:      ${{APP_URL:-http://localhost:$APP_PORT}}"
        echo "  Username: $(grep "^BOOTSTRAP_ADMIN_USERNAME=" .env | cut -d= -f2)"
        echo ""
        echo "To stop:   docker compose down"
        echo "To start:  docker compose up -d"
        echo "To logs:   docker compose logs -f app"
        echo ""
    """)


def generate_readme(org_code: str, org_name: str, app_port: int) -> str:
    slug = org_code.lower()
    return textwrap.dedent(f"""\
        # DotMac ERP — {org_name} ({org_code})

        Single-tenant instance provisioned by `bootstrap_instance.py`.

        ## Quick Start

        ```bash
        # First set FORGEJO_TOKEN_FILE in .env to an approved absolute local
        # token-file pointer; then build, migrate, and seed.
        ./setup.sh

        # Start / stop
        docker compose up -d
        docker compose down

        # View logs
        docker compose logs -f app
        ```

        ## Ports

        | Service   | Port  |
        |-----------|-------|
        | App (web) | {app_port} |

        ## Containers

        - `dotmac_{slug}_app` — Web application
        - `dotmac_{slug}_worker` — Celery background worker
        - `dotmac_{slug}_beat` — Celery beat scheduler
        - `dotmac_{slug}_db` — PostgreSQL database
        - `dotmac_{slug}_redis` — Redis cache/broker
        - `dotmac_{slug}_openbao` — Secrets management

        ## Deploying to a Remote VPS

        1. Copy this entire directory to the remote server
        2. Ensure Docker and Docker Compose are installed
        3. Set `APP_BUILD_CONTEXT` in `.env` to point to the app source
        4. Store the approved Forgejo package-read token outside this generated
           directory and set `FORGEJO_TOKEN_FILE` in `.env` to that file's
           absolute path. Never paste the token value into `.env` or Compose.
        5. Run `./setup.sh`; it fails before starting infrastructure when the
           pointer is absent, relative, empty, or unreadable

        ## Updating

        ```bash
        # Pull latest code, rebuild, and migrate
        docker compose build app worker beat
        docker compose up -d
        docker compose exec app alembic upgrade head
        ```
    """)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Provision a new single-tenant DotMac ERP instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--org-code", help="Short org code (e.g. ACME)")
    p.add_argument("--org-name", help="Legal organization name")
    p.add_argument(
        "--sector-type", choices=SECTOR_CHOICES, help="PRIVATE, PUBLIC, or NGO"
    )
    p.add_argument(
        "--framework", choices=FRAMEWORK_CHOICES, help="IFRS, IPSAS, or BOTH"
    )
    p.add_argument("--currency", help="Functional currency code (e.g. USD, NGN)")
    p.add_argument("--admin-email", help="Admin user email")
    p.add_argument("--admin-username", help="Admin login username")
    p.add_argument("--admin-password", help="Admin login password")
    p.add_argument("--app-port", type=int, help="Host port for the web app")
    p.add_argument("--db-port", type=int, help="Host port for PostgreSQL")
    p.add_argument("--redis-port", type=int, help="Host port for Redis")
    p.add_argument("--bao-port", type=int, help="Host port for OpenBao")
    p.add_argument("--app-url", help="Public URL (e.g. https://acme.erp.example.com)")
    p.add_argument("--output-dir", help="Where to create the instance directory")
    p.add_argument(
        "--package", action="store_true", help="Create a .tar.gz for remote deployment"
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args = prompt_if_missing(args)

    org_code = args.org_code.upper()
    slug = org_code.lower()

    # Resolve ports
    auto_app, auto_db, auto_redis, auto_bao = find_next_ports(INSTANCES_DIR)
    app_port = args.app_port or auto_app
    db_port = args.db_port or auto_db
    redis_port = args.redis_port or auto_redis
    bao_port = args.bao_port or auto_bao

    # Instance directory
    output_dir = args.output_dir or os.path.join(INSTANCES_DIR, slug)
    if os.path.exists(output_dir):
        print(f"\nError: Instance directory already exists: {output_dir}")
        print("  Remove it first or choose a different --org-code.")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    # Generate org UUID
    org_id = str(uuid.uuid4())

    # App URL
    app_url = args.app_url or f"http://localhost:{app_port}"

    # Write files
    print(f"\nProvisioning instance: {org_code}")
    print(f"  Directory: {output_dir}")
    print(f"  Org ID:    {org_id}")
    print(
        f"  Ports:     app={app_port}, db={db_port}, redis={redis_port}, openbao={bao_port}"
    )
    print()

    # .env
    env_content = generate_env(
        org_id=org_id,
        org_code=org_code,
        org_name=args.org_name,
        sector_type=args.sector_type,
        framework=args.framework,
        currency=args.currency,
        admin_email=args.admin_email,
        admin_username=args.admin_username,
        admin_password=args.admin_password,
        app_port=app_port,
        db_port=db_port,
        redis_port=redis_port,
        bao_port=bao_port,
        app_url=app_url,
    )
    with open(os.path.join(output_dir, ".env"), "w") as f:
        f.write(env_content)
    print("  Created .env")

    # docker-compose.yml
    with open(os.path.join(output_dir, "docker-compose.yml"), "w") as f:
        f.write(generate_docker_compose(org_code))
    print("  Created docker-compose.yml")

    # bootstrap_db.py
    with open(os.path.join(output_dir, "bootstrap_db.py"), "w") as f:
        f.write(generate_bootstrap_db_script())
    print("  Created bootstrap_db.py")

    # setup.sh
    setup_path = os.path.join(output_dir, "setup.sh")
    with open(setup_path, "w") as f:
        f.write(generate_setup_script(org_code))
    os.chmod(setup_path, 0o755)
    print("  Created setup.sh")

    # README.md
    with open(os.path.join(output_dir, "README.md"), "w") as f:
        f.write(generate_readme(org_code, args.org_name, app_port))
    print("  Created README.md")

    # Package for remote deployment
    if args.package:
        archive_name = f"dotmac-{slug}"
        archive_path = shutil.make_archive(
            os.path.join(INSTANCES_DIR, archive_name),
            "gztar",
            root_dir=INSTANCES_DIR,
            base_dir=slug,
        )
        print(f"\n  Packaged: {archive_path}")
        print(f"  Deploy:   scp {archive_path} user@remote:/opt/dotmac/")
        print(
            f"            ssh user@remote 'cd /opt/dotmac && tar xzf {archive_name}.tar.gz && cd {slug} && ./setup.sh'"
        )

    print(f"""
=== Instance provisioned ===

To start (on this server):
  cd {output_dir}
  ./setup.sh

To deploy to a remote VPS:
  1. scp -r {output_dir} user@remote:/opt/dotmac/{slug}
  2. ssh user@remote 'cd /opt/dotmac/{slug} && ./setup.sh'

The setup script will:
  - Build the Docker image
  - Start all 6 containers
  - Run database migrations
  - Create the organization, admin user, and RBAC roles
""")


if __name__ == "__main__":
    main()
