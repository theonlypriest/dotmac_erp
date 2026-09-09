from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import scripts.probe_people_employment_type_activation as probe
from scripts.probe_people_employment_type_activation import ProbeExit, classify


@pytest.mark.parametrize(
    (
        "revision_recorded",
        "module_authority_present",
        "projection_fence_present",
        "expected",
    ),
    [
        # The activation committed: its revision row is the answer.
        (True, True, True, ProbeExit.ACTIVATED),
        (True, True, False, ProbeExit.ACTIVATED),
        # The one rollback-safe state: positive control seen, and neither the
        # revision row nor the activation's own projection fence exists.
        (False, True, False, ProbeExit.DEFINITELY_PRE_ACTIVATION),
        # Torn: the activation artifact exists without its revision row.
        (False, True, True, ProbeExit.AMBIGUOUS),
        # No positive control: an absent revision proves nothing at all.
        (False, False, False, ProbeExit.AMBIGUOUS),
        (False, False, True, ProbeExit.AMBIGUOUS),
        (True, False, False, ProbeExit.AMBIGUOUS),
        (True, False, True, ProbeExit.AMBIGUOUS),
    ],
)
def test_probe_allows_rollback_only_from_a_positive_pre_activation_state(
    revision_recorded: bool,
    module_authority_present: bool,
    projection_fence_present: bool,
    expected: ProbeExit,
) -> None:
    assert (
        classify(
            revision_recorded=revision_recorded,
            module_authority_present=module_authority_present,
            projection_fence_present=projection_fence_present,
        )
        is expected
    )


def test_the_probe_reads_the_module_owned_surface_and_not_the_retired_path() -> None:
    """The probe must exercise the current owner, with the retirement proved.

    The positive half comes first: the query really does name the module-owned
    authority relation and the activation's own projection fence. Only then is
    the absence of the retired assembly bootstrap path evidence of anything --
    an assertion of absence over a source this test could not read would pass
    for the wrong reason.
    """
    source = Path(inspect.getfile(probe)).read_text(encoding="utf-8")

    assert "mod_people.employment_types" in source
    assert "hr.enforce_employment_type_projection()" in source
    assert "module_authority_present" in probe._STATE_QUERY.text
    assert "projection_fence_present" in probe._STATE_QUERY.text

    assert "employment_type_bootstrap" not in source
    assert "bootstrap" not in source.casefold()
