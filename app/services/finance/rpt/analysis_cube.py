"""
Analysis cube querying service.
"""
# ruff: noqa: S608

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from uuid import UUID

from sqlalchemy import bindparam, column, func, select, table, text
from sqlalchemy.orm import Session

from app.models.finance.rpt.analysis_cube import AnalysisCube

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_VIEW_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
_AGG_RE = re.compile(r"^(sum|count|avg|min|max)$", re.IGNORECASE)
logger = logging.getLogger(__name__)

# Refresh authority stays with the migration owner. Every supported materialized
# view needs an explicit, fixed-purpose SECURITY DEFINER bridge installed by a
# migration; a database value may never be interpolated into owner-executed SQL.
_REFRESH_FUNCTIONS = {
    "rpt.sales_analysis_mv": "rpt.refresh_sales_analysis_mv",
}


@dataclass(frozen=True)
class CubeQueryResult:
    cube_code: str
    columns: list[str]
    rows: list[dict]


class AnalysisCubeService:
    """Validate and execute ad-hoc cube queries."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_cubes(self, organization_id: UUID) -> list[AnalysisCube]:
        from sqlalchemy import or_, select

        stmt = (
            select(AnalysisCube)
            .where(
                AnalysisCube.is_active.is_(True),
                or_(
                    AnalysisCube.organization_id.is_(None),
                    AnalysisCube.organization_id == organization_id,
                ),
            )
            .order_by(AnalysisCube.code.asc())
        )
        return list(self.db.scalars(stmt).all())

    def query_cube(
        self,
        organization_id: UUID,
        cube_code: str,
        *,
        row_dimensions: list[str],
        measures: list[str],
        filters: list[dict] | None = None,
        limit: int = 1000,
    ) -> CubeQueryResult:
        cube = self._get_cube(organization_id, cube_code)
        if not row_dimensions:
            raise ValueError("At least one row dimension is required.")
        if not measures:
            raise ValueError("At least one measure is required.")
        if limit < 1 or limit > 5000:
            raise ValueError("Limit must be between 1 and 5000.")

        dim_map = {d["field"]: d for d in (cube.dimensions or []) if d.get("field")}
        measure_map = {m["field"]: m for m in (cube.measures or []) if m.get("field")}

        group_parts: list[str] = []
        params: dict[str, object] = {"org_id": str(organization_id), "limit": limit}
        referenced_fields: set[str] = {"organization_id"}

        for dim in row_dimensions:
            if dim not in dim_map:
                raise ValueError(f"Unknown dimension: {dim}")
            field = self._safe_ident(str(dim_map[dim]["field"]))
            referenced_fields.add(field)
            group_parts.append(field)

        for measure in measures:
            if measure not in measure_map:
                raise ValueError(f"Unknown measure: {measure}")
            mdef = measure_map[measure]
            field = self._safe_ident(str(mdef["field"]))
            referenced_fields.add(field)
            agg = str(mdef.get("agg", "sum")).lower()
            if not _AGG_RE.match(agg):
                raise ValueError(f"Unsupported aggregation: {agg}")

        filter_items = filters or []
        for idx, filter_item in enumerate(filter_items):
            field_name = str(filter_item.get("field") or "")
            value = filter_item.get("value")
            if field_name not in dim_map and field_name not in measure_map:
                raise ValueError(f"Unknown filter field: {field_name}")
            field = self._safe_ident(field_name)
            referenced_fields.add(field)
            param_key = f"f_{idx}"
            params[param_key] = value

        view_name = self._safe_view(cube.source_view)
        schema_name, _, relation_name = view_name.partition(".")
        if not relation_name:
            relation_name = schema_name
            schema_name = ""

        source = table(
            relation_name,
            *(column(field) for field in sorted(referenced_fields)),
            schema=schema_name or None,
        )

        dimension_columns = [source.c[field].label(field) for field in group_parts]
        measure_columns = []
        for measure in measures:
            mdef = measure_map[measure]
            field = self._safe_ident(str(mdef["field"]))
            agg = str(mdef.get("agg", "sum")).lower()
            alias = self._safe_ident(measure)
            measure_columns.append(getattr(func, agg)(source.c[field]).label(alias))

        stmt = (
            select(*dimension_columns, *measure_columns)
            .select_from(source)
            .where(source.c.organization_id == bindparam("org_id"))
            .group_by(*(source.c[field] for field in group_parts))
            .order_by(*(source.c[field] for field in group_parts))
            .limit(bindparam("limit"))
        )

        for idx, filter_item in enumerate(filter_items):
            field_name = self._safe_ident(str(filter_item.get("field") or ""))
            stmt = stmt.where(source.c[field_name] == bindparam(f"f_{idx}"))

        rows = [dict(row) for row in self.db.execute(stmt, params).mappings().all()]
        columns = [*row_dimensions, *measures]
        return CubeQueryResult(cube_code=cube.code, columns=columns, rows=rows)

    def refresh_due_cubes(self, *, now: datetime | None = None) -> dict[str, int]:
        """Refresh due cube materialized views and update refresh timestamps."""
        from sqlalchemy import select

        current = now or datetime.now(UTC)
        cubes = list(
            self.db.scalars(
                select(AnalysisCube).where(AnalysisCube.is_active.is_(True))
            ).all()
        )
        results = {"checked": len(cubes), "refreshed": 0, "errors": 0}

        for cube in cubes:
            if not self._is_refresh_due(cube, current):
                continue
            try:
                self.refresh_cube(cube, now=current)
                results["refreshed"] += 1
            except Exception:
                logger.exception("Failed refreshing cube %s", cube.code)
                results["errors"] += 1

        return results

    def refresh_cube(self, cube: AnalysisCube, *, now: datetime | None = None) -> None:
        """Refresh one materialized view, using CONCURRENTLY when possible.

        Postgres only allows ``REFRESH MATERIALIZED VIEW CONCURRENTLY`` on
        matviews that have already been populated at least once — the
        first refresh after creation must be non-concurrent so the unique
        index has rows to diff against. Earlier code caught the resulting
        ``FeatureNotSupported`` and retried, but the failed CONCURRENTLY
        statement aborts the surrounding transaction, so the retry hit
        ``InFailedSqlTransaction`` and any sibling cubes' in-memory
        ``last_refreshed_at`` updates would be rolled back too. Picking
        the variant up front from ``pg_matviews.ispopulated`` keeps the
        whole loop in one healthy transaction.
        """
        view_name = self._safe_view(cube.source_view)
        refresh_function = _REFRESH_FUNCTIONS.get(view_name)
        if refresh_function is None:
            raise RuntimeError(
                f"Materialized view {view_name} has no approved refresh function"
            )
        schema_name, _, relation_name = view_name.partition(".")
        if not relation_name:
            relation_name = schema_name
            schema_name = ""
        target_schema = schema_name or "public"

        is_populated = self.db.execute(
            text(
                "SELECT ispopulated FROM pg_matviews "
                "WHERE schemaname = :schema AND matviewname = :name"
            ),
            {"schema": target_schema, "name": relation_name},
        ).scalar()
        if is_populated is None:
            raise RuntimeError(
                f"Materialized view {view_name} does not exist; "
                "did you run the migration that creates it?"
            )

        self.db.execute(
            text(f"SELECT {refresh_function}(:use_concurrently)"),
            {"use_concurrently": bool(is_populated)},
        )
        cube.last_refreshed_at = now or datetime.now(UTC)

    def _get_cube(self, organization_id: UUID, cube_code: str) -> AnalysisCube:
        from sqlalchemy import or_, select

        stmt = select(AnalysisCube).where(
            AnalysisCube.code == cube_code,
            AnalysisCube.is_active.is_(True),
            or_(
                AnalysisCube.organization_id.is_(None),
                AnalysisCube.organization_id == organization_id,
            ),
        )
        cube = self.db.scalar(stmt)
        if cube is None:
            raise ValueError(f"Analysis cube not found: {cube_code}")
        return cube

    @staticmethod
    def _is_refresh_due(cube: AnalysisCube, current: datetime) -> bool:
        interval = max(int(cube.refresh_interval_minutes or 60), 1)
        if cube.last_refreshed_at is None:
            return True
        return cube.last_refreshed_at <= current - timedelta(minutes=interval)

    @staticmethod
    def _safe_ident(value: str) -> str:
        if not _IDENT_RE.match(value):
            raise ValueError(f"Invalid identifier: {value}")
        return value

    @staticmethod
    def _safe_view(value: str) -> str:
        if not _VIEW_RE.match(value):
            raise ValueError(f"Invalid source view: {value}")
        return value
