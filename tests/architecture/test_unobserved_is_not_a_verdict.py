"""A non-observation may not be recorded as a verdict.

``PaymentIntentStatus.FAILED`` and ``EXPIRED`` are claims about money: FAILED
says the payout did not happen, EXPIRED says nobody ever tried. Downstream
readers act on both — the claim reverts to APPROVED, the intent becomes
resettable, the operator is told to retry. Writing either one because a network
call did not come back is how a payout that may already have gone gets sent
again (ADR-0007, adopting `dotmac_starter_mt` ADR-0032).

## What this detects, precisely

An assignment of ``PaymentIntentStatus.FAILED`` or ``PaymentIntentStatus.EXPIRED``
to a ``.status`` attribute **lexically inside an ``except`` block that handles a
transport failure** — ``PaystackUnreachable``, ``httpx.RequestError``,
``httpx.TimeoutException``, ``httpx.ConnectError``, ``httpx.HTTPStatusError``,
``httpx.TransportError``, or a bare/broad ``except Exception``.

Scoped to ``app/``, ``scripts/`` and ``tools/``, and it is the SHAPE that is
caught, not one call site: a future author who adds a fourth way to fail a poll
and reaches for the reassuring status inside their own handler is caught by the
same rule.

## What it deliberately does NOT detect

Assignment reached indirectly — ``self._settle(intent)`` called from inside the
handler, where the write happens one frame away. A static scan cannot follow
that, and pretending otherwise would be worse than saying so here.

What keeps the check honest instead is the two-sided sensitivity proof at the
bottom, in the shape ``test_paid_status_single_owner.py`` uses: a planted
violation must be DETECTED, and the legitimate FAILED write in
``poll_transfer_status`` — where Paystack answered and refused, which is the one
case that has earned the word — must NOT be flagged. Together they mean the
sweep above passes because the code is right, not because the pattern stopped
matching anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ENUM = "PaymentIntentStatus"
COLUMN = "status"

#: Statuses that assert a fact about the money. INDETERMINATE is deliberately
#: absent — it is the one member that asserts nothing, and it is precisely what
#: an exception handler in this class is SUPPOSED to write.
VERDICT_MEMBERS = frozenset({"FAILED", "EXPIRED"})

#: Exception names that mean "we did not learn what happened".
TRANSPORT_HANDLERS = frozenset(
    {
        "PaystackUnreachable",
        "RequestError",
        "TimeoutException",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "TransportError",
        "HTTPStatusError",
        "HTTPError",
        # A broad handler catches the transport failures too, and is the
        # commonest way this defect actually arrives.
        "Exception",
        "BaseException",
    }
)

SCANNED_ROOTS = ("app", "scripts", "tools")
SKIPPED_PARTS = frozenset({"__pycache__", "archive"})

OWNER = "app/services/finance/payments/payment_service.py"
"""The module that legitimately writes both verdicts and non-verdicts."""

#: Sites that still offend, each with the premise under which it is tolerated.
#: Keyed by enclosing FUNCTION, never by line number — a line number exemption
#: silently moves onto whatever code drifts into that position.
#:
#: This is a ratchet, not a permission. `test_the_backlog_only_shrinks` fails
#: in BOTH directions: a new offender is a build break, and an entry that has
#: stopped offending must be deleted rather than left as decoration.
#: "Grandfathered" is not "reviewed and correct" (ADR-0018).
KNOWN_UNFIXED = {
    (
        "app/services/finance/payments/payment_service.py",
        "process_successful_payment",
    ): (
        "INBOUND collection, not an outbound transfer. Paystack has already "
        "CONFIRMED the customer's payment succeeded by the time this runs — "
        "the method is only called on a verified success — and "
        "what failed is our own recording of it (CustomerPayment creation, GL "
        "posting). So this is the same family of defect — an observed fact "
        "overwritten by a status that denies it — but the opposite direction, "
        "and INDETERMINATE would be the wrong fix because the outcome here IS "
        "observed. ADR-0007 is scoped to outbound payouts and deliberately "
        "does not touch the collection path; naming this site is how the gap "
        "stays visible instead of passing as clean."
    ),
}


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """Every exception name this handler catches. Empty set for a bare except."""
    if handler.type is None:
        return {"Exception"}  # bare `except:` catches everything

    names: set[str] = set()
    candidates = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    for node in candidates:
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)  # `httpx.RequestError` -> RequestError
    return names


def _is_verdict(node: ast.expr) -> bool:
    """True for ``PaymentIntentStatus.FAILED`` / ``.EXPIRED``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in VERDICT_MEMBERS
        and isinstance(node.value, ast.Name)
        and node.value.id == ENUM
    )


def _verdict_writes_in(node: ast.AST) -> list[int]:
    """Line numbers of ``<x>.status = PaymentIntentStatus.<VERDICT>`` under node."""
    found: list[int] = []
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Assign) or not _is_verdict(inner.value):
            continue
        if any(
            isinstance(target, ast.Attribute) and target.attr == COLUMN
            for target in inner.targets
        ):
            found.append(inner.lineno)
    return found


def _scan(node: ast.AST, function: str | None, hits: list[tuple[str | None, int]]):
    """Walk once, tracking the innermost enclosing function.

    ``ast.walk`` alone cannot say which function a handler is in, and the
    exemption below is keyed by function precisely so it cannot drift onto
    unrelated code.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _scan(child, child.name, hits)
            continue
        if isinstance(child, ast.ExceptHandler) and (
            _handler_names(child) & TRANSPORT_HANDLERS
        ):
            # `_verdict_writes_in` already covers the whole handler subtree,
            # so descending again would double-count a nested try/except.
            hits.extend((function, lineno) for lineno in _verdict_writes_in(child))
            continue
        _scan(child, function, hits)


def verdict_writes_in_transport_handlers(path: Path) -> list[tuple[str | None, int]]:
    """``(enclosing function, line)`` where a non-observation becomes a verdict."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    hits: list[tuple[str | None, int]] = []
    _scan(tree, None, hits)
    return sorted(hits, key=lambda hit: hit[1])


def verdicts_written_in_transport_handlers(path: Path) -> list[int]:
    """Line numbers only — the shape the sensitivity probes assert on."""
    return [lineno for _, lineno in verdict_writes_in_transport_handlers(path)]


def scanned_files():
    for root in SCANNED_ROOTS:
        directory = REPO_ROOT / root
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            if SKIPPED_PARTS & set(path.parts):
                continue
            yield path


# ===========================================================================
# The sweep
# ===========================================================================


def _offenders() -> dict[tuple[str, str | None], list[int]]:
    found: dict[tuple[str, str | None], list[int]] = {}
    for path in scanned_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for function, lineno in verdict_writes_in_transport_handlers(path):
            found.setdefault((relative, function), []).append(lineno)
    return found


def test_no_transport_handler_records_a_verdict() -> None:
    offenders = [
        f"{relative}:{lineno} (in {function})"
        for (relative, function), linenos in _offenders().items()
        if (relative, function) not in KNOWN_UNFIXED
        for lineno in linenos
    ]

    assert offenders == [], (
        "these sites record a verdict about money from a failure to observe "
        "it:\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nFAILED means Paystack said the payout did not happen; EXPIRED"
        "\nmeans nobody ever tried. Neither is what a timeout, a 5xx or a"
        "\nbroad `except` has learned. Use PaymentIntentStatus.INDETERMINATE"
        "\n(PaymentService._settle_indeterminate), which asserts nothing about"
        "\nthe money and is picked up by resolve_indeterminate_transfer."
        "\nSee docs/adr/0007-unobserved-is-not-failed.md."
    )


def test_the_backlog_only_shrinks() -> None:
    """Two-directional ratchet.

    An exemption that outlives the thing it exempts is worse than no exemption:
    it reads as a reviewed decision while monitoring nothing. So a listed site
    that no longer offends must be DELETED from the list, and that deletion is
    what this test forces.
    """
    offending = set(_offenders())
    stale = sorted(
        f"{relative}::{function}"
        for (relative, function) in KNOWN_UNFIXED
        if (relative, function) not in offending
    )
    assert stale == [], (
        "these entries in KNOWN_UNFIXED no longer offend:\n  "
        + "\n  ".join(stale)
        + "\n\nDelete them. A backlog that only ever grows is not a ratchet."
    )
    assert len(KNOWN_UNFIXED) == 1, (
        "KNOWN_UNFIXED changed size. Adding an entry needs a stated, "
        "reviewable premise in the dict itself; removing one needs this "
        "number lowered in the same change."
    )


def test_each_exemption_names_a_function_that_exists() -> None:
    """A premise about `process_successful_payment` means nothing once that
    function has been renamed."""
    for relative, function in KNOWN_UNFIXED:
        path = REPO_ROOT / relative
        assert path.is_file(), f"exempted file {relative} is gone"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert function in defined, (
            f"{relative} no longer defines {function}; the exemption is "
            "pointing at nothing and is silently covering whatever else is "
            "there now."
        )


# ===========================================================================
# Sensitivity proof, both sides
# ===========================================================================


def test_the_detector_fires_on_the_original_defect(tmp_path: Path) -> None:
    """Detection half: plant exactly the code this change removed."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import httpx\n"
        "def poll(intent, client):\n"
        "    try:\n"
        "        client.verify_transfer(intent.paystack_reference)\n"
        "    except httpx.RequestError:\n"
        "        intent.status = PaymentIntentStatus.FAILED\n"
    )
    assert verdicts_written_in_transport_handlers(probe) == [6]

    # ...and the corrected version of the same file is clean.
    probe.write_text(
        "import httpx\n"
        "def poll(intent, client):\n"
        "    try:\n"
        "        client.verify_transfer(intent.paystack_reference)\n"
        "    except httpx.RequestError:\n"
        "        intent.status = PaymentIntentStatus.INDETERMINATE\n"
    )
    assert verdicts_written_in_transport_handlers(probe) == []


@pytest.mark.parametrize(
    "handler",
    [
        "PaystackUnreachable as e",
        "httpx.TimeoutException",
        "httpx.HTTPStatusError as e",
        "Exception",
        "(PaystackUnreachable, ValueError)",
    ],
)
def test_the_detector_covers_every_way_of_catching_it(
    tmp_path: Path, handler: str
) -> None:
    """One spelling of the handler is not the rule; the shape is."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "def poll(intent, client):\n"
        "    try:\n"
        "        client.verify_transfer(intent.paystack_reference)\n"
        f"    except {handler}:\n"
        "        intent.status = PaymentIntentStatus.EXPIRED\n"
    )
    assert verdicts_written_in_transport_handlers(probe) == [5]


def test_a_bare_except_is_covered(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "def poll(intent, client):\n"
        "    try:\n"
        "        client.verify_transfer(intent.paystack_reference)\n"
        "    except:  # noqa: E722\n"
        "        intent.status = PaymentIntentStatus.FAILED\n"
    )
    assert verdicts_written_in_transport_handlers(probe) == [5]


def test_the_legitimate_failed_write_is_not_flagged(tmp_path: Path) -> None:
    """Specificity half. `poll_transfer_status` writes FAILED when Paystack
    ANSWERED that the transfer failed. That is the one place the word has been
    earned, it is not inside any handler, and a rule that swept it up would be
    telling the system never to record a real failure."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "def poll(intent, result):\n"
        "    if result.status == 'failed':\n"
        "        intent.status = PaymentIntentStatus.FAILED\n"
        "    elif result.status == 'expired':\n"
        "        intent.status = PaymentIntentStatus.EXPIRED\n"
    )
    assert verdicts_written_in_transport_handlers(probe) == []


def test_the_owner_still_writes_the_legitimate_verdict() -> None:
    """Liveness. The sweep must pass because the code is right, not because
    the owner stopped writing FAILED at all — which would be its own defect,
    since a transfer Paystack refused has to be recordable as refused.

    Asserted through the single-owner detector's own scan so a rename of the
    enum or the module fails here too.
    """
    from tests.architecture.test_payment_intent_status_single_owner import (
        writes_payment_intent_status,
    )

    owner = REPO_ROOT / OWNER
    assert owner.is_file(), f"the named owner {OWNER} is missing"

    source = owner.read_text(encoding="utf-8")
    assert f"{ENUM}.FAILED" in source, (
        "the owner no longer writes FAILED anywhere. An unobserved outcome is "
        "not a failure, but a failure Paystack reported still is one."
    )
    assert f"{ENUM}.INDETERMINATE" in source, (
        "the owner no longer writes INDETERMINATE — the vocabulary this rule "
        "depends on has been removed or renamed."
    )
    assert len(writes_payment_intent_status(owner)) >= 5


def test_the_vocabulary_and_the_exception_still_exist() -> None:
    """The failure message names an enum member, a method and an exception
    type; a rename must not leave it pointing at nothing."""
    import importlib

    intent_model = importlib.import_module("app.models.finance.payments.payment_intent")
    statuses = getattr(intent_model, ENUM)
    assert hasattr(statuses, "INDETERMINATE")
    for member in VERDICT_MEMBERS:
        assert hasattr(statuses, member), member
    assert hasattr(intent_model.PaymentIntent, "unresolved_since")

    client = importlib.import_module("app.services.finance.payments.paystack_client")
    assert issubclass(client.PaystackUnreachable, client.PaystackError), (
        "PaystackUnreachable must stay a PaystackError subclass, or every "
        "existing `except PaystackError` handler silently stops catching "
        "transport failures."
    )

    service = importlib.import_module("app.services.finance.payments.payment_service")
    assert callable(service.PaymentService._settle_indeterminate)
    assert callable(service.PaymentService.resolve_indeterminate_transfer)
    assert callable(service.PaymentService.find_indeterminate_transfer_intents)
    assert callable(service.is_unobserved)


def test_the_classifier_defaults_to_unobserved() -> None:
    """The rule that matters most is not the classification but its DEFAULT.

    `is_unobserved` asks whether Paystack answered, not whether the error looks
    like a network problem, so an exception type nobody has thought of yet
    lands on the safe side. A future author adding a new failure mode gets the
    correct behaviour without having read this file.
    """
    from app.services.finance.payments.payment_service import is_unobserved
    from app.services.finance.payments.paystack_client import (
        PaystackError,
        PaystackUnreachable,
    )

    assert is_unobserved(PaystackUnreachable("connect timeout")) is True
    assert is_unobserved(RuntimeError("something nobody anticipated")) is True
    assert is_unobserved(ValueError("a bug in our own posting code")) is True

    # ...and exactly one thing counts as an answer.
    assert is_unobserved(PaystackError("Transfer not found")) is False


@pytest.mark.parametrize("root", SCANNED_ROOTS)
def test_every_scanned_root_is_real(root: str) -> None:
    """A mis-globbed root would make the sweep pass over nothing."""
    assert (REPO_ROOT / root).is_dir(), (
        f"{root}/ does not exist — the sweep is scanning a directory that is "
        "not there and would pass for the wrong reason."
    )


def test_the_client_never_collapses_transport_into_a_verdict() -> None:
    """The other half of the fix, checked at its source.

    Every ``except httpx.RequestError`` in the Paystack client must raise
    ``PaystackUnreachable``. One site left raising plain ``PaystackError``
    would make a timeout indistinguishable from a refusal again, and no amount
    of care downstream could recover the difference.
    """
    client_path = REPO_ROOT / "app/services/finance/payments/paystack_client.py"
    tree = ast.parse(client_path.read_text(encoding="utf-8"), filename=str(client_path))

    offenders: list[int] = []
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if "RequestError" not in _handler_names(node):
            continue
        raises = [n for n in ast.walk(node) if isinstance(n, ast.Raise)]
        if not raises:
            continue  # a re-raise-free handler (the metrics wrapper) is fine
        checked += 1
        for raise_node in raises:
            exc = raise_node.exc
            if exc is None:
                continue  # bare `raise` re-raises the httpx error itself
            called = exc.func if isinstance(exc, ast.Call) else exc
            name = getattr(called, "id", None) or getattr(called, "attr", None)
            if name == "PaystackError":
                offenders.append(raise_node.lineno)

    assert offenders == [], (
        "these transport handlers raise a plain PaystackError, which means "
        f"'Paystack answered and refused':\n  {sorted(offenders)}\n"
        "Raise PaystackUnreachable instead."
    )
    assert checked >= 10, (
        f"only {checked} RequestError handlers found in the client — the scan "
        "has probably stopped matching and would pass over nothing."
    )
