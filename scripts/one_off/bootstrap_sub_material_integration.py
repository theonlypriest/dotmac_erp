"""Provision the least-privilege Sub material integration and status hook.

The webhook secret must be supplied through ``ERP_SUB_WEBHOOK_SECRET`` and is
never persisted by this script. The generated API token is printed once.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

LABEL = "dotmac-sub-material-integration"
SERVICE_EMAIL = "service-dotmac-sub-material@dotmac.io"
EVENT_NAME = "sub.material_request.status_changed"
SCOPES = [
    "sub:inventory:read",
    "sub:material:write",
    "sub:material:read",
    "sub:domain:write",
]


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--organization-id", required=True, type=UUID)
    parser.add_argument(
        "--callback-url",
        required=True,
        help="Sub callback containing its capability binding UUID",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rotate-key", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _args()
    if not args.callback_url.startswith("https://selfcare.dotmac.io/"):
        raise SystemExit("Callback must use the production selfcare HTTPS origin")
    if not args.apply:
        print(f"DRY RUN organization={args.organization_id}")
        print(f"Would grant only: {', '.join(SCOPES)}")
        print(f"Would configure signed callback: {args.callback_url}")
        return 0
    if not os.getenv("ERP_SUB_WEBHOOK_SECRET"):
        raise SystemExit("ERP_SUB_WEBHOOK_SECRET must be present")

    from sqlalchemy import select

    from app.db.session_context import session_for_org
    from app.models.auth import ApiKey
    from app.models.finance.platform.service_hook import (
        HookExecutionMode,
        HookHandlerType,
        ServiceHook,
    )
    from app.models.person import Person
    from app.services.auth import hash_api_key
    from app.services.feature_flag_service import FeatureFlagService
    from app.services.feature_flags import FEATURE_SERVICE_HOOKS

    raw_key: str | None = None
    with session_for_org(args.organization_id) as db:
        FeatureFlagService(db).toggle(
            args.organization_id,
            FEATURE_SERVICE_HOOKS,
            True,
            changed_by_id=None,
        )
        person = db.scalar(
            select(Person).where(
                Person.organization_id == args.organization_id,
                Person.email == SERVICE_EMAIL,
            )
        )
        if person is None:
            person = Person(
                organization_id=args.organization_id,
                first_name="DotMac Sub",
                last_name="Material Integration",
                display_name="DotMac Sub Material Integration",
                email=SERVICE_EMAIL,
                email_verified=True,
                is_active=True,
                marketing_opt_in=False,
                metadata_={
                    "identity_type": "service_account",
                    "interactive_login": False,
                },
            )
            db.add(person)
            db.flush()
        key = db.scalar(
            select(ApiKey).where(
                ApiKey.person_id == person.id,
                ApiKey.label == LABEL,
                ApiKey.is_active.is_(True),
            )
        )
        if key is not None and args.rotate_key:
            key.is_active = False
            key = None
        if key is None:
            raw_key = secrets.token_urlsafe(32)
            key = ApiKey(
                person_id=person.id,
                label=LABEL,
                key_hash=hash_api_key(raw_key),
                scopes=SCOPES,
                is_active=True,
            )
            db.add(key)
            db.flush()
        elif key.scopes != SCOPES:
            key.scopes = SCOPES
            db.flush()

        hook = db.scalar(
            select(ServiceHook).where(
                ServiceHook.organization_id == args.organization_id,
                ServiceHook.name == LABEL,
            )
        )
        config = {
            "url": args.callback_url,
            "method": "POST",
            "timeout_seconds": 15,
            "payload_only": True,
            "signing_secret_env": "ERP_SUB_WEBHOOK_SECRET",
        }
        if hook is None:
            hook = ServiceHook(
                organization_id=args.organization_id,
                event_name=EVENT_NAME,
                handler_type=HookHandlerType.WEBHOOK,
                execution_mode=HookExecutionMode.ASYNC,
                handler_config=config,
                conditions={},
                name=LABEL,
                description="Signed ERP material outcomes delivered only to DotMac Sub",
                is_active=True,
                max_retries=8,
                retry_backoff_seconds=30,
            )
            db.add(hook)
        else:
            hook.event_name = EVENT_NAME
            hook.handler_type = HookHandlerType.WEBHOOK
            hook.execution_mode = HookExecutionMode.ASYNC
            hook.handler_config = config
            hook.is_active = True
            hook.max_retries = 8
            hook.retry_backoff_seconds = 30
        db.commit()
        print(f"API key id: {key.id}")
        print(f"Service hook id: {hook.hook_id}")
        if raw_key:
            print("SUB ERP SERVICE TOKEN (shown once):")
            print(raw_key)
        else:
            print("Existing active token retained; use --rotate-key to replace it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
