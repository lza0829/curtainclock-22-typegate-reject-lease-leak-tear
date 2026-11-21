from __future__ import annotations

from fastapi import APIRouter

from app.db import connect
from app.services.reconcile import type_gate_differences

router = APIRouter(tags=["dashboard"])


def _wm(conn, key: str) -> int:
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row else 0


@router.get("/dashboard")
def dashboard():
    conn = connect()
    try:
        day_row = conn.execute(
            "SELECT value FROM settings WHERE key='fixture_day'"
        ).fetchone()
        day = day_row["value"] if day_row else None

        # 水位数字 vs 实槽：同源互证。open_leases 水位应恒等于 open 租约实数，
        # live_sessions（windows 表）也应与之一致——三者任一不等即暴露撕裂。
        open_wm = _wm(conn, "open_leases")
        conflict_wm = _wm(conn, "conflict_count")
        open_actual = conn.execute(
            "SELECT COUNT(*) c FROM leases WHERE status='open'"
        ).fetchone()["c"]
        live_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='live'"
        ).fetchone()["c"]
        draft_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='draft'"
        ).fetchone()["c"]
        degraded_n = conn.execute(
            "SELECT COUNT(*) c FROM leases WHERE status='open' AND degraded=1"
        ).fetchone()["c"]
        conflict_actual = conn.execute(
            "SELECT COUNT(*) c FROM conflicts"
        ).fetchone()["c"]
        type_rejections = conn.execute(
            "SELECT COUNT(*) c FROM conflicts WHERE kind='type'"
        ).fetchone()["c"]
        # 类型门禁徽章：以差异桶为准。差异未清前不得显示「通过」——
        # 徽章、租约落点、水位三者同刷可核对，禁止各说各话。
        diffs = type_gate_differences(conn)
        return {
            "fixture_day": day,
            "open_leases": open_wm,
            "conflict_count": conflict_wm,
            "live_sessions": live_n,
            "draft_sessions": draft_n,
            "degraded_leases": degraded_n,
            "open_leases_actual": open_actual,
            "conflict_count_actual": conflict_actual,
            "watermarks_consistent": (
                open_wm == open_actual == live_n
                and conflict_wm == conflict_actual
            ),
            "type_gate": {
                "status": "passed" if not diffs else "diverged",
                "differences": len(diffs),
                "type_rejections": type_rejections,
            },
        }
    finally:
        conn.close()
