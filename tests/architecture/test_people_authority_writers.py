"""Exact ratchet for ERP's remaining People-domain mutation authority.

This is a retirement ledger, not an allowlist.  Every detected writer remains
ERP authority until its domain cutover removes the path and lowers the TSV in
the same change.  Equality in both directions makes a new writer fail and
makes a retired writer fail until the evidence is updated deliberately.

The scan covers application, task/worker code, operator scripts and tools.
Migrations are excluded because they define storage rather than an online or
operator business writer.  Python structure is parsed; shell and SQL entry
points are scanned for explicit DML against the seven source relations.
"""

from __future__ import annotations

import ast
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY = REPO_ROOT / "docs" / "inventories" / "people-authority-writers.tsv"
RUNTIME_ROOTS = ("app", "scripts", "tools")
INVENTORY_FIELDS = (
    "path",
    "symbol",
    "entities",
    "evidence",
    "disposition",
    "retire_with",
    "next_owner",
)
DISPOSITIONS = {"disable_before_cutover", "retire_with_domain_cutover"}

MODEL_ENTITIES = {
    "Person": "party_person",
    "Department": "department",
    "Designation": "designation",
    "EmploymentType": "employment_type",
    "Employee": "employee",
    "Position": "position",
    "PositionAssignment": "position_assignment",
}
VARIABLE_ENTITIES = {
    "person": "party_person",
    "new_person": "party_person",
    "department": "department",
    "dept": "department",
    "designation": "designation",
    "desig": "designation",
    "employment_type": "employment_type",
    "emp_type": "employment_type",
    "employee": "employee",
    "emp": "employee",
    "position": "position",
    "pos": "position",
    "assignment": "position_assignment",
    "position_assignment": "position_assignment",
}
TARGET_FIELDS = {
    "party_person": {
        "first_name",
        "last_name",
        "display_name",
        "email",
        "is_active",
    },
    "department": {
        "department_code",
        "department_name",
        "description",
        "parent_department_id",
        "is_active",
        # Temporary migration input for the target position marker.
        "head_id",
    },
    "designation": {
        "designation_code",
        "designation_name",
        "description",
        "is_active",
    },
    "employment_type": {
        "type_code",
        "type_name",
        "description",
        "is_active",
    },
    "employee": {
        "person_id",
        "employee_code",
        "department_id",
        "designation_id",
        "employment_type_id",
        "date_of_joining",
        "date_of_leaving",
        "probation_end_date",
        "confirmation_date",
        "status",
    },
    "position": {
        "position_code",
        "position_name",
        "department_id",
        "designation_id",
        "parent_position_id",
        "vacancy_routing_policy",
        "is_active",
    },
    "position_assignment": {
        "employee_id",
        "position_id",
        "assignment_type",
        "start_date",
        "end_date",
    },
}
RELATION_ENTITIES = {
    "people": "party_person",
    "hr.department": "department",
    "hr.designation": "designation",
    "hr.employment_type": "employment_type",
    "hr.employee": "employee",
    "hr.position": "position",
    "hr.position_assignment": "position_assignment",
}
_RELATION_PATTERN = (
    r"(?:hr\.)?(?:employee|department|designation|employment_type|position|"
    r"position_assignment)|people"
)
_RAW_CREATE_OR_DELETE = re.compile(
    rf"\b(?:insert\s+into|delete\s+from)\s+(?P<relation>{_RELATION_PATTERN})\b",
    re.IGNORECASE,
)
_RAW_UPDATE = re.compile(
    rf"\bupdate\s+(?P<relation>{_RELATION_PATTERN})(?:\s+[a-z_][a-z0-9_]*)?"
    r"\s+set\s+(?P<set_clause>.*?)(?=\s+from\b|\s+where\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, order=True)
class Writer:
    path: str
    symbol: str
    entities: str
    evidence: str


EMPLOYMENT_TYPE_PROJECTOR = Writer(
    path="app/services/people/hr/employment_types.py",
    symbol="_EmploymentTypeProjector.project",
    entities="employment_type",
    evidence="attribute_assignment|constructor|session_add",
)


def _without_employment_type_projector(discovered: set[Writer]) -> set[Writer]:
    """Partition the exact-one derived writer from remaining ERP authority."""
    candidates = {
        writer
        for writer in discovered
        if "employment_type" in writer.entities.split("|")
    }
    assert candidates == {EMPLOYMENT_TYPE_PROJECTOR}, (
        "the legacy Employment Type table must have exactly one canonical "
        f"projector; found:\n{_render(candidates) or '-'}"
    )
    return discovered - candidates


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _base_name(node: ast.expr) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _entity_for_name(name: str | None, available_entities: set[str]) -> str | None:
    if not name:
        return None
    entity = VARIABLE_ENTITIES.get(name.lower().removeprefix("new_"))
    if entity in available_entities:
        return entity
    return None


def _entity_for_call(node: ast.expr, model_aliases: dict[str, str]) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    called = _call_name(node)
    if called in model_aliases:
        return model_aliases[called]
    if called in {"scalar", "scalars"} and node.args:
        for part in ast.walk(node.args[0]):
            if not isinstance(part, ast.Call) or _call_name(part) != "select":
                continue
            if part.args and isinstance(part.args[0], ast.Name):
                return model_aliases.get(part.args[0].id)
    if called in {"get", "get_or_404"} and node.args:
        target = node.args[0]
        if isinstance(target, ast.Name):
            return model_aliases.get(target.id)
    if called is None or "_" not in called:
        return None
    normalized = called.lower().strip("_")
    for name, entity in VARIABLE_ENTITIES.items():
        if normalized in {
            name,
            f"get_{name}",
            f"find_{name}",
            f"load_{name}",
            f"require_{name}",
            f"find_or_create_{name}",
            f"get_or_create_{name}",
        } and entity in set(model_aliases.values()):
            return entity
    return None


def _relation_entity(relation: str) -> str:
    normalized = relation.lower()
    if "." not in normalized and normalized != "people":
        normalized = f"hr.{normalized}"
    return RELATION_ENTITIES[normalized]


def _raw_dml_entities(source: str) -> set[str]:
    entities = {
        _relation_entity(match.group("relation"))
        for match in _RAW_CREATE_OR_DELETE.finditer(source)
    }
    for match in _RAW_UPDATE.finditer(source):
        entity = _relation_entity(match.group("relation"))
        set_clause = match.group("set_clause")
        if any(
            re.search(rf"\b{re.escape(field)}\s*=", set_clause, re.IGNORECASE)
            for field in TARGET_FIELDS[entity]
        ):
            entities.add(entity)
    return entities


def _sql_template(
    node: ast.expr, named_templates: dict[str, str] | None = None
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and named_templates is not None:
        return named_templates.get(node.id)
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else "<dynamic>"
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _sql_template(node.left, named_templates) or "<dynamic>"
        right = _sql_template(node.right, named_templates) or "<dynamic>"
        return left + right
    return None


def _chained_update_entity(node: ast.Call, model_aliases: dict[str, str]) -> str | None:
    for part in ast.walk(node.func):
        if not isinstance(part, ast.Call) or _call_name(part) != "update":
            continue
        if part.args and isinstance(part.args[0], ast.Name):
            return model_aliases.get(part.args[0].id)
    return None


def _is_target_import(module: str | None, model_name: str) -> bool:
    if model_name == "Person":
        return module == "app.models.person"
    return bool(
        model_name in MODEL_ENTITIES
        and module is not None
        and (
            module == "app.models.people.hr"
            or module.startswith("app.models.people.hr.")
        )
    )


class _PythonWriterVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.found: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.model_aliases: dict[str, str] = {}
        self.local_types: list[dict[str, str]] = []
        self.module_sql_templates: dict[str, str] = {}
        self.local_sql_templates: list[dict[str, str]] = []

    @property
    def symbol(self) -> str:
        return ".".join(self.scope) if self.scope else "<module>"

    def _record(self, entity: str, evidence: str) -> None:
        self.found[(self.symbol, entity)].add(evidence)

    @property
    def available_entities(self) -> set[str]:
        return set(self.model_aliases.values())

    def _local_entity(self, node: ast.expr) -> str | None:
        name = _base_name(node)
        if name is None or not self.local_types:
            return None
        return self.local_types[-1].get(name)

    @property
    def named_sql_templates(self) -> dict[str, str]:
        templates = dict(self.module_sql_templates)
        for local_templates in self.local_sql_templates:
            templates.update(local_templates)
        return templates

    def _bind_sql_template(self, name: str, value: ast.expr) -> None:
        template = _sql_template(value, self.named_sql_templates)
        target = (
            self.local_sql_templates[-1]
            if self.local_sql_templates
            else self.module_sql_templates
        )
        if template is None:
            if self.local_sql_templates:
                # A local binding shadows a same-named module SQL constant.
                target[name] = "<dynamic>"
            else:
                target.pop(name, None)
        else:
            target[name] = template

    def _bind_argument(self, argument: ast.arg) -> None:
        if not self.local_types:
            return
        self.local_sql_templates[-1][argument.arg] = "<dynamic>"
        entity = None
        if isinstance(argument.annotation, ast.Name):
            entity = self.model_aliases.get(argument.annotation.id)
        if entity is None:
            entity = _entity_for_name(argument.arg, self.available_entities)
        if entity is not None:
            self.local_types[-1][argument.arg] = entity

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        for imported in node.names:
            if _is_target_import(node.module, imported.name):
                self.model_aliases[imported.asname or imported.name] = MODEL_ENTITIES[
                    imported.name
                ]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.local_types.append({})
        self.local_sql_templates.append({})
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            self._bind_argument(argument)
        if node.args.vararg is not None:
            self._bind_argument(node.args.vararg)
        if node.args.kwarg is not None:
            self._bind_argument(node.args.kwarg)
        self.generic_visit(node)
        self.local_sql_templates.pop()
        self.local_types.pop()
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _call_name(node)
        if name in self.model_aliases:
            self._record(self.model_aliases[name], "constructor")

        if name in {"insert", "delete"} and node.args:
            target = node.args[0]
            if isinstance(target, ast.Name) and target.id in self.model_aliases:
                self._record(self.model_aliases[target.id], "sql_dml")
        if name == "values":
            entity = _chained_update_entity(node, self.model_aliases)
            fields = {keyword.arg for keyword in node.keywords}
            if entity is not None and (
                None in fields or bool(fields.intersection(TARGET_FIELDS[entity]))
            ):
                self._record(entity, "sql_dml")

        if (
            name in {"delete", "add", "add_all"}
            and isinstance(node.func, ast.Attribute)
            and _receiver_name(node.func.value) in {"db", "session"}
        ):
            for argument in node.args:
                entity = None
                if isinstance(argument, ast.Call):
                    called = _call_name(argument)
                    if called is not None:
                        entity = self.model_aliases.get(called)
                elif isinstance(argument, (ast.List, ast.Tuple, ast.Set)):
                    for item in argument.elts:
                        item_name = (
                            _call_name(item) if isinstance(item, ast.Call) else None
                        )
                        item_entity = (
                            self.model_aliases.get(item_name)
                            if item_name is not None
                            else self._local_entity(item)
                        )
                        if item_entity is not None:
                            self._record(item_entity, f"session_{name}")
                    continue
                else:
                    entity = self._local_entity(argument)
                if entity is not None:
                    self._record(entity, f"session_{name}")

        if name == "setattr" and node.args:
            entity = self._local_entity(node.args[0])
            field = (
                node.args[1].value
                if len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                else None
            )
            if entity is not None and (field is None or field in TARGET_FIELDS[entity]):
                self._record(entity, "setattr")

        if name in {"text", "execute", "exec_driver_sql"} and node.args:
            sql = _sql_template(node.args[0], self.named_sql_templates)
            if sql is not None:
                for entity in _raw_dml_entities(sql):
                    self._record(entity, "raw_sql_dml")
        self.generic_visit(node)

    def _attribute_targets(self, target: ast.expr) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._attribute_targets(item)
            return
        if not isinstance(target, ast.Attribute):
            return
        entity = self._local_entity(target)
        if entity is not None and target.attr in TARGET_FIELDS[entity]:
            self._record(entity, "attribute_assignment")

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            self._attribute_targets(target)
            if isinstance(target, ast.Name):
                self._bind_sql_template(target.id, node.value)
            if isinstance(target, ast.Name) and self.local_types:
                entity = _entity_for_call(node.value, self.model_aliases)
                if (
                    entity is None
                    and isinstance(node.value, ast.Call)
                    and (_call_name(node.value) or "").islower()
                ):
                    entity = _entity_for_name(target.id, self.available_entities)
                if entity is None:
                    self.local_types[-1].pop(target.id, None)
                else:
                    self.local_types[-1][target.id] = entity
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        self._attribute_targets(node.target)
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._bind_sql_template(node.target.id, node.value)
        if isinstance(node.target, ast.Name) and self.local_types:
            entity = None
            if isinstance(node.annotation, ast.Name):
                entity = self.model_aliases.get(node.annotation.id)
            if entity is None and node.value is not None:
                entity = _entity_for_call(node.value, self.model_aliases)
            if entity is not None:
                self.local_types[-1][node.target.id] = entity
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        self._attribute_targets(node.target)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        if isinstance(node.target, ast.Name) and self.local_types:
            entity = _entity_for_name(node.target.id, self.available_entities)
            if entity is not None:
                self.local_types[-1][node.target.id] = entity
        self.generic_visit(node)


def _scan_python_text(path: str, source: str) -> set[Writer]:
    visitor = _PythonWriterVisitor(path)
    visitor.visit(ast.parse(source, filename=path))
    by_symbol: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for (symbol, entity), evidence in visitor.found.items():
        by_symbol[symbol][entity].update(evidence)
    return {
        Writer(
            path=path,
            symbol=symbol,
            entities="|".join(sorted(entities)),
            evidence="|".join(
                sorted({kind for kinds in entities.values() for kind in kinds})
            ),
        )
        for symbol, entities in by_symbol.items()
    }


def _scan_text_entrypoint(path: str, source: str) -> set[Writer]:
    entities = _raw_dml_entities(source)
    if not entities:
        return set()
    return {
        Writer(
            path=path,
            symbol="<file>",
            entities="|".join(sorted(entities)),
            evidence="raw_sql_dml",
        )
    }


def _scan_repository() -> set[Writer]:
    found: set[Writer] = set()
    for root_name in RUNTIME_ROOTS:
        root = REPO_ROOT / root_name
        assert root.is_dir(), f"writer entry-point family disappeared: {root_name}"
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".sh", ".sql"}:
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            source = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                found.update(_scan_python_text(relative, source))
            else:
                found.update(_scan_text_entrypoint(relative, source))
    return found


def _load_inventory() -> set[Writer]:
    if not INVENTORY.exists():
        return set()
    with INVENTORY.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        assert reader.fieldnames == list(INVENTORY_FIELDS)
        rows = list(reader)
    assert all(row["disposition"] in DISPOSITIONS for row in rows)
    assert all(row["retire_with"] == "people" for row in rows)
    # `next_owner` is an AUTHORITY field, so it carries the assembly id
    # `asm-dotmac-erp` — not the product/repository slug `dotmac-erp` used for
    # consumers, and not the legacy source repository `dotmac_erp`. Corrected
    # 2026-08-19; the four names are set out in
    # `docs/architecture/people-replacement-boundary.md`.
    assert all(row["next_owner"] == "asm-dotmac-erp/dotmac-people" for row in rows)
    writers = {
        Writer(
            path=row["path"],
            symbol=row["symbol"],
            entities=row["entities"],
            evidence=row["evidence"],
        )
        for row in rows
    }
    assert len(writers) == len(rows), "People writer inventory contains duplicates"
    return writers


def _render(rows: set[Writer]) -> str:
    return "\n".join(
        f"{row.path}\t{row.symbol}\t{row.entities}\t{row.evidence}"
        for row in sorted(rows)
    )


def test_people_authority_writer_inventory_is_exact() -> None:
    discovered = _without_employment_type_projector(_scan_repository())
    recorded = _load_inventory()

    assert discovered == recorded, (
        "People authority writers changed. Additions are new ERP authority; "
        "removals are retired authority and must lower the checked-in ledger."
        f"\n\nUnrecorded:\n{_render(discovered - recorded) or '-'}"
        f"\n\nNo longer detected:\n{_render(recorded - discovered) or '-'}"
    )


def test_employment_type_projector_partition_is_two_directional() -> None:
    try:
        _without_employment_type_projector(set())
    except AssertionError:
        pass
    else:  # pragma: no cover - sensitivity canary
        raise AssertionError("a missing compatibility projector was accepted")

    second_writer = Writer(
        path="app/tasks/shadow_employment_type_projector.py",
        symbol="shadow_project",
        entities="employee|employment_type",
        evidence="attribute_assignment",
    )
    try:
        _without_employment_type_projector({EMPLOYMENT_TYPE_PROJECTOR, second_writer})
    except AssertionError:
        pass
    else:  # pragma: no cover - sensitivity canary
        raise AssertionError("a second compatibility projector was accepted")


def test_writer_detector_sensitivity_covers_every_mutation_shape() -> None:
    planted = _scan_python_text(
        "app/tasks/planted.py",
        """
from app.models.person import Person
from app.models.people.hr import Department, Designation, Employee, EmploymentType, Position, PositionAssignment

EMPLOYMENT_TYPE_SQL = "UPDATE hr.employment_type SET type_name = 'X'"

def create_one(db):
    db.add(Employee(employee_code="X"))

def update_one(db):
    db.execute(update(Department).values(department_name="X"))

def delete_one(db, person):
    db.delete(person)

def setattr_one(position):
    setattr(position, "position_name", "X")

def assign_one(assignment):
    assignment.end_date = None

def raw_one(db):
    db.execute(text(prefix + "UPDATE hr.designation SET designation_name = 'X'"))

def raw_named_constant(db):
    db.execute(text(EMPLOYMENT_TYPE_SQL))
""",
    )

    assert {(row.symbol, row.entities, row.evidence) for row in planted} == {
        ("create_one", "employee", "constructor|session_add"),
        ("update_one", "department", "sql_dml"),
        ("delete_one", "party_person", "session_delete"),
        ("setattr_one", "position", "setattr"),
        ("assign_one", "position_assignment", "attribute_assignment"),
        ("raw_one", "designation", "raw_sql_dml"),
        ("raw_named_constant", "employment_type", "raw_sql_dml"),
    }


def test_writer_detector_does_not_call_reads_writes() -> None:
    assert not _scan_python_text(
        "app/tasks/read_only.py",
        """
from app.models.people.hr import Employee

def read_only(db, organization_id):
    return db.scalars(select(Employee).where(Employee.organization_id == organization_id))
""",
    )
