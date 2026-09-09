"""
Tests for AnalysisCubeService.
"""

from datetime import datetime, timezone

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.finance.rpt.analysis_cube import AnalysisCubeService


def _cube(
    *,
    source_view: str = "rpt.sales_analysis_mv",
):
    return SimpleNamespace(
        code="sales",
        source_view=source_view,
        dimensions=[
            {"field": "period_label", "label": "Period"},
            {"field": "customer_name", "label": "Customer"},
        ],
        measures=[
            {"field": "amount_total", "label": "Total", "agg": "sum"},
            {"field": "record_count", "label": "Records", "agg": "sum"},
        ],
    )


def test_query_cube_success():
    db = MagicMock()
    db.execute.return_value.mappings.return_value.all.return_value = [
        {"period_label": "Feb 2026", "amount_total": 123.45}
    ]
    service = AnalysisCubeService(db)
    organization_id = uuid4()

    with patch.object(service, "_get_cube", return_value=_cube()):
        result = service.query_cube(
            organization_id,
            "sales",
            row_dimensions=["period_label"],
            measures=["amount_total"],
            filters=[{"field": "customer_name", "value": "Acme"}],
            limit=500,
        )

    assert result.cube_code == "sales"
    assert result.columns == ["period_label", "amount_total"]
    assert result.rows == [{"period_label": "Feb 2026", "amount_total": 123.45}]
    _, params = db.execute.call_args[0]
    assert params["org_id"] == str(organization_id)
    assert params["f_0"] == "Acme"
    assert params["limit"] == 500


def test_query_cube_validates_dimension():
    service = AnalysisCubeService(MagicMock())
    with patch.object(service, "_get_cube", return_value=_cube()):
        with pytest.raises(ValueError, match="Unknown dimension"):
            service.query_cube(
                uuid4(),
                "sales",
                row_dimensions=["bad_dimension"],
                measures=["amount_total"],
            )


def test_query_cube_validates_measure():
    service = AnalysisCubeService(MagicMock())
    with patch.object(service, "_get_cube", return_value=_cube()):
        with pytest.raises(ValueError, match="Unknown measure"):
            service.query_cube(
                uuid4(),
                "sales",
                row_dimensions=["period_label"],
                measures=["bad_measure"],
            )


def test_query_cube_validates_filter_field():
    service = AnalysisCubeService(MagicMock())
    with patch.object(service, "_get_cube", return_value=_cube()):
        with pytest.raises(ValueError, match="Unknown filter field"):
            service.query_cube(
                uuid4(),
                "sales",
                row_dimensions=["period_label"],
                measures=["amount_total"],
                filters=[{"field": "bad_filter", "value": "x"}],
            )


def test_query_cube_rejects_invalid_source_view():
    service = AnalysisCubeService(MagicMock())
    with patch.object(
        service, "_get_cube", return_value=_cube(source_view="rpt.sales;drop table")
    ):
        with pytest.raises(ValueError, match="Invalid source view"):
            service.query_cube(
                uuid4(),
                "sales",
                row_dimensions=["period_label"],
                measures=["amount_total"],
            )


def test_query_cube_validates_limit_bounds():
    service = AnalysisCubeService(MagicMock())
    with patch.object(service, "_get_cube", return_value=_cube()):
        with pytest.raises(ValueError, match="between 1 and 5000"):
            service.query_cube(
                uuid4(),
                "sales",
                row_dimensions=["period_label"],
                measures=["amount_total"],
                limit=0,
            )


def test_refresh_due_cubes_refreshes_only_due():
    now = datetime(2026, 2, 25, 12, 0, tzinfo=UTC)
    due_cube = _cube()
    due_cube.is_active = True
    due_cube.last_refreshed_at = None
    due_cube.refresh_interval_minutes = 60

    fresh_cube = _cube()
    fresh_cube.code = "fresh"
    fresh_cube.is_active = True
    fresh_cube.last_refreshed_at = now
    fresh_cube.refresh_interval_minutes = 60

    db = MagicMock()
    db.scalars.return_value.all.return_value = [due_cube, fresh_cube]
    service = AnalysisCubeService(db)

    with patch.object(service, "refresh_cube") as refresh_cube:
        result = service.refresh_due_cubes(now=now)

    assert result == {"checked": 2, "refreshed": 1, "errors": 0}
    refresh_cube.assert_called_once_with(due_cube, now=now)


def test_refresh_cube_uses_concurrently_when_matview_is_populated():
    cube = _cube()
    cube.last_refreshed_at = None
    db = MagicMock()
    # First execute: pg_matviews.ispopulated lookup. Second: the refresh.
    populated_result = MagicMock()
    populated_result.scalar.return_value = True
    db.execute.side_effect = [populated_result, MagicMock()]
    service = AnalysisCubeService(db)
    now = datetime(2026, 2, 25, 12, 0, tzinfo=UTC)

    service.refresh_cube(cube, now=now)

    assert db.execute.call_count == 2
    refresh_sql = str(db.execute.call_args_list[1].args[0])
    assert "SELECT rpt.refresh_sales_analysis_mv" in refresh_sql
    assert db.execute.call_args_list[1].args[1] == {"use_concurrently": True}
    assert cube.last_refreshed_at == now


def test_refresh_cube_skips_concurrently_when_matview_is_not_populated():
    """First-time refresh: pg rejects CONCURRENTLY against an empty matview
    and aborts the txn, so we have to detect this up front and use the
    plain variant. Otherwise the retry path lands in InFailedSqlTransaction
    and any sibling cubes' last_refreshed_at updates would roll back.
    """
    cube = _cube()
    cube.last_refreshed_at = None
    db = MagicMock()
    unpopulated_result = MagicMock()
    unpopulated_result.scalar.return_value = False
    db.execute.side_effect = [unpopulated_result, MagicMock()]
    service = AnalysisCubeService(db)
    now = datetime(2026, 2, 25, 12, 0, tzinfo=UTC)

    service.refresh_cube(cube, now=now)

    assert db.execute.call_count == 2
    refresh_sql = str(db.execute.call_args_list[1].args[0])
    assert "SELECT rpt.refresh_sales_analysis_mv" in refresh_sql
    assert db.execute.call_args_list[1].args[1] == {"use_concurrently": False}
    assert cube.last_refreshed_at == now


def test_refresh_cube_raises_when_matview_does_not_exist():
    cube = _cube()
    db = MagicMock()
    missing_result = MagicMock()
    missing_result.scalar.return_value = None
    db.execute.return_value = missing_result
    service = AnalysisCubeService(db)

    with pytest.raises(RuntimeError, match="does not exist"):
        service.refresh_cube(cube)


def test_refresh_cube_rejects_a_view_without_an_owner_bridge():
    service = AnalysisCubeService(MagicMock())

    with pytest.raises(RuntimeError, match="no approved refresh function"):
        service.refresh_cube(_cube(source_view="rpt.unapproved_mv"))
