"""Keep the H2 persistent-file slice on the sole object-storage path."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OWNED_WRITERS = (
    Path("app/services/people/hr/handbook_service.py"),
    Path("app/services/finance/rpt/report_instance.py"),
    Path("app/services/automation/document_generator.py"),
)
OBJECT_STORAGE_SEAMS = {
    OWNED_WRITERS[0]: "get_hr_handbook_upload",
    OWNED_WRITERS[1]: "get_generated_report_upload",
    OWNED_WRITERS[2]: "get_generated_docs_upload",
}


def _durable_local_writes(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "mkdir",
            "write_bytes",
            "write_text",
        }:
            offenders.append(node.func.attr)
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "open":
            continue
        mode: object = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for keyword in node.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                mode = keyword.value.value
        if isinstance(mode, str) and any(marker in mode for marker in "wax+"):
            offenders.append(f"open:{mode}")
    return tuple(offenders)


def test_persistent_file_writers_delegate_to_object_storage_only() -> None:
    for relative_path in OWNED_WRITERS:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert _durable_local_writes(source) == (), relative_path
        assert OBJECT_STORAGE_SEAMS[relative_path] in source


def test_local_write_detector_is_sensitive() -> None:
    planted = """
from pathlib import Path

Path('/app/uploads').mkdir(parents=True)
Path('/app/uploads/file').write_bytes(b'bytes')
with open('/app/reports/output.json', 'w') as handle:
    handle.write('{}')
"""

    assert _durable_local_writes(planted) == ("mkdir", "write_bytes", "open:w")
