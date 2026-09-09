"""One owner for the purchase order's received and invoiced amounts.

`ap.purchase_order` carried `amount_received` and `amount_invoiced` as stored
columns, and the authority on that row was split three ways:

* `GoodsReceiptService._update_po_status` recomputed `amount_received`
  ABSOLUTELY from the PO lines.
* `PurchaseOrderService.update_received_amount` INCREMENTED the same column by a
  caller-supplied delta.  Two writers, two different arithmetics, one column —
  the value was whichever ran last.
* `amount_invoiced` had **no** writer.  Every row held `0` forever, the purchase
  order detail screen rendered that zero as a financial fact, and the CRM
  supersede interlock trusted it as a safety check that therefore could not fire.
* `WorkflowActionExecutor._action_update_field` could `setattr` either column to
  any value, because `PURCHASE_ORDER` is in the automation entity registry and
  the action was unbounded.

Both facts are derivable from the receipt and invoice records that are already
authoritative, so they are derived rather than stored, by exactly one owner:
`app.services.finance.ap.purchase_order_amounts`.

## What this test enforces, and why each half matters

1. **The columns are gone from the model.**  This is the load-bearing one.  The
   standing rule is that authority transfers at the COLUMN level, so a row whose
   columns are owned by different authorities cannot be handed over as a unit.
   Deleting the columns makes the rule structural: there is no column for a
   second writer to take.  A test that merely counted writers would pass again
   the moment someone re-added the column with one writer and waited.

2. **Nothing under `app/` assigns either name to an attribute.**  This is what
   fails if a writer comes back — including a writer onto some other object that
   happens to reuse the name, which is deliberate: the point is that these two
   words name a derived fact in this codebase.

3. **The SUM has one definition.**  A hand-written
   `quantity_received * unit_price` sum outside the owner is a second definition
   that can drift from it, which is exactly how `balance_due` grew 75 copies.

4. **The invoiced-status vocabulary is total.**  `COUNTS_AS_INVOICED` and
   `NOT_YET_INVOICED` must partition `SupplierInvoiceStatus` exactly.  Adding a
   status to the enum without deciding which side it falls on then fails the
   build, instead of silently defaulting one way and quietly changing every
   supersede and cancel interlock.

5. **The automation engine cannot write them.**  The generic `setattr` action is
   gated by `field_authority_owner`, and the gate names both fields even though
   neither exists — so a re-added column is not writable by a workflow rule the
   moment it reappears.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP = REPO_ROOT / "app"

DERIVED_FIELDS = ("amount_received", "amount_invoiced")

OWNER_REL = "app/services/finance/ap/purchase_order_amounts.py"

# `GoodsReceiptService._update_po_status` sums `quantity_received * unit_price`
# over the PO lines to decide PARTIALLY_RECEIVED vs RECEIVED.  That is a STATUS
# decision over a local total, not the derived amount: it writes `po.status` and
# writes no amount anywhere.  Folding it into the owner would make the owner
# decide PO status, which is a different question with a different owner.
_STATUS_DECISION_NOT_AN_AMOUNT = {
    "app/services/finance/ap/goods_receipt.py",
}


def _python_files() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _attribute_writes(path: Path) -> list[tuple[int, str]]:
    """Line numbers where a derived field is assigned to an ATTRIBUTE.

    `po.amount_received = x`, `po.amount_received += x`, and the walrus/annotated
    forms.  Assignment to a bare local name is not flagged: a local named
    `amount_received` is a value being computed, not a fact being stamped onto a
    record.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []

    def record(target: ast.expr, lineno: int) -> None:
        if isinstance(target, ast.Attribute) and target.attr in DERIVED_FIELDS:
            hits.append((lineno, target.attr))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                record(elt, lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                record(target, node.lineno)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            record(node.target, node.lineno)

    return hits


def test_the_columns_are_not_on_the_purchase_order_row() -> None:
    """The model must not carry either field as a mapped column.

    Checked against the mapper rather than the file text, so a column added
    through a mixin, a `declared_attr` or a synonym is caught too.
    """
    from app.models.finance.ap.purchase_order import PurchaseOrder

    mapped = set(PurchaseOrder.__mapper__.columns.keys())
    present = sorted(f for f in DERIVED_FIELDS if f in mapped)
    assert not present, (
        f"{present} are back on ap.purchase_order. These are DERIVED facts owned "
        f"by {OWNER_REL}; storing them on the PO row splits authority over the "
        f"row between ERP AP and whoever writes the commitment columns. Derive "
        f"them, or give them their own narrowly scoped table keyed by po_id."
    )

    attrs = set(dir(PurchaseOrder))
    shadowed = sorted(f for f in DERIVED_FIELDS if f in attrs)
    assert not shadowed, (
        f"{shadowed} exist on PurchaseOrder as a property/hybrid. Even a "
        f"read-only accessor here re-creates the second definition this test "
        f"exists to prevent — call {OWNER_REL} instead."
    )


def test_nothing_writes_the_derived_amounts() -> None:
    """No code under `app/` may stamp either amount onto an object."""
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in _python_files():
        rel = _rel(path)
        if rel == OWNER_REL:
            continue
        hits = _attribute_writes(path)
        if hits:
            offenders[rel] = hits

    assert not offenders, (
        "Derived purchase-order amounts are being written:\n"
        + "\n".join(
            f"  {rel}: " + ", ".join(f"line {ln} ({name})" for ln, name in hits)
            for rel, hits in sorted(offenders.items())
        )
        + f"\n\nThese facts have one owner, {OWNER_REL}, and it does not store "
        "them — it derives them from the receipt and invoice records. A write "
        "here is a second authority."
    )


def test_the_owner_itself_stores_nothing() -> None:
    """The owner derives; it must not write the facts back onto a row either.

    A derivation that caches its answer onto the object is a stored value with
    extra steps, and the staleness comes back with it.
    """
    owner = REPO_ROOT / OWNER_REL
    hits = _attribute_writes(owner)
    assert not hits, (
        f"{OWNER_REL} assigns a derived amount onto an attribute at "
        f"{hits}. The owner derives these facts and returns them; it does not "
        "persist them."
    )


def test_the_received_sum_has_one_definition() -> None:
    """`quantity_received * unit_price` may be summed in one place."""
    pattern_files: list[str] = []
    for path in _python_files():
        rel = _rel(path)
        if rel == OWNER_REL or rel in _STATUS_DECISION_NOT_AN_AMOUNT:
            continue
        text = path.read_text(encoding="utf-8")
        if "quantity_received" in text and "unit_price" in text:
            # Only flag the two appearing in one multiplication.
            for line in text.splitlines():
                if "quantity_received" in line and "unit_price" in line and "*" in line:
                    pattern_files.append(rel)
                    break

    assert not pattern_files, (
        "The received-amount formula is written out in "
        f"{sorted(set(pattern_files))}. It has one definition, in {OWNER_REL}. "
        "Two copies of an arithmetic rule is how `balance_due` reached 75 "
        "hand-written subtractions."
    )


def test_every_supplier_invoice_status_is_classified() -> None:
    """The invoiced vocabulary partitions the status enum exactly."""
    from app.models.finance.ap.supplier_invoice import SupplierInvoiceStatus
    from app.services.finance.ap.purchase_order_amounts import (
        COUNTS_AS_INVOICED,
        NOT_YET_INVOICED,
    )

    all_statuses = set(SupplierInvoiceStatus)
    classified = COUNTS_AS_INVOICED | NOT_YET_INVOICED

    unclassified = sorted(s.value for s in all_statuses - classified)
    assert not unclassified, (
        f"SupplierInvoiceStatus {unclassified} is in neither COUNTS_AS_INVOICED "
        f"nor NOT_YET_INVOICED in {OWNER_REL}. Decide whether an invoice in that "
        "status is a real claim against a purchase order — the answer changes "
        "every supersede and cancel interlock, so it must not default."
    )

    overlap = sorted(s.value for s in COUNTS_AS_INVOICED & NOT_YET_INVOICED)
    assert not overlap, f"{overlap} is classified both ways in {OWNER_REL}."

    unknown = sorted(str(s) for s in classified - all_statuses)
    assert not unknown, (
        f"{unknown} is classified in {OWNER_REL} but is not a "
        "SupplierInvoiceStatus member — a renamed status left a dead entry."
    )


@pytest.mark.parametrize("field", DERIVED_FIELDS)
def test_the_automation_engine_may_not_write_them(field: str) -> None:
    """The generic workflow `setattr` action is gated for both fields."""
    from app.services.finance.automation.entity_registry import (
        field_authority_owner,
        protected_fields,
    )

    assert field in protected_fields("PURCHASE_ORDER"), (
        f"'{field}' is not in the PURCHASE_ORDER protected-field map. "
        "`_action_update_field` loads any registered entity and setattrs any "
        "named field, so an ungated field is writable by any workflow rule."
    )
    owner = field_authority_owner("PURCHASE_ORDER", field)
    assert owner and "purchase_order_amounts" in owner, (
        f"The protected-field entry for '{field}' must name its owner, so the "
        "refusal tells an operator where the write belongs."
    )


def test_the_automation_gate_still_bites() -> None:
    """A check over an empty rule set passes for the wrong reason.

    This asserts the gate lets ordinary fields through, so the test above is
    measuring a real restriction and not a function that returns an owner for
    everything.
    """
    from app.services.finance.automation.entity_registry import field_authority_owner

    assert field_authority_owner("PURCHASE_ORDER", "status") is None
    assert field_authority_owner("BILL", "amount_received") is None


def test_the_write_detector_still_bites(tmp_path: Path) -> None:
    """A scan that finds nothing passes whether or not it can find anything.

    `test_nothing_writes_the_derived_amounts` is green because there are no
    writers left. That is indistinguishable, from the outside, from an AST walk
    that quietly stopped matching — a rename of the fields, a `_python_files`
    glob that no longer reaches `app/`, a walk that misses `AugAssign`. So the
    detector is run against a file that IS an offender, in every syntactic shape
    a returning writer would plausibly take.
    """
    offender = tmp_path / "offender.py"
    offender.write_text(
        "\n".join(
            (
                "po.amount_received = total",  # plain assignment
                "po.amount_invoiced += delta",  # the incremental writer's shape
                "po.amount_received, po.status = total, RECEIVED",  # tuple target
                "amount_received = total",  # a LOCAL: must NOT be flagged
            )
        ),
        encoding="utf-8",
    )

    found = _attribute_writes(offender)
    names = sorted({name for _, name in found})

    assert names == ["amount_invoiced", "amount_received"], (
        f"The detector found {found} in a file with three deliberate writers. "
        "It has stopped detecting, which means the green result above means "
        "nothing."
    )
    assert len(found) == 3, (
        f"Expected exactly the three attribute writes, got {found}. A count of 4 "
        "means the bare local `amount_received = total` was flagged, which would "
        "make the real scan fire on any function that merely computes the value."
    )
