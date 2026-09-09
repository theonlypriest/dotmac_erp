import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

FILES_WITH_LOGGED_AMBIENT_READS = (
    "app/services/auth_flow.py",
    "app/services/nextcloud/client.py",
    "app/api/finance/payments.py",
    "app/services/finance/payments/web.py",
    "app/services/finance/payments/payment_service.py",
    "app/services/finance/payments/paystack_sync.py",
    "app/services/finance/payments/paystack_customer_sync.py",
)


def _resolve_value_calls_without_scope(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "resolve_value":
            continue
        if not any(keyword.arg == "organization_id" for keyword in node.keywords):
            missing.append(node.lineno)
    return missing


def test_logged_domain_settings_call_sites_state_their_scope() -> None:
    missing = {
        str(path.relative_to(ROOT)): lines
        for file_name in FILES_WITH_LOGGED_AMBIENT_READS
        if (lines := _resolve_value_calls_without_scope(path := ROOT / file_name))
    }

    assert missing == {}
