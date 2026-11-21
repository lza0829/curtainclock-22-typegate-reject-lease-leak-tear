from __future__ import annotations

from fastapi import APIRouter

from app.db import connect
from app.services.reconcile import type_gate_differences

router = APIRouter(tags=["type-gate"])


@router.get("/type-gate/differences")
def differences():
    """差异桶：类型回执与已落库占用的实时对账（见 services/reconcile）。"""
    conn = connect()
    try:
        diffs = type_gate_differences(conn)
        return {"differences": diffs, "count": len(diffs)}
    finally:
        conn.close()


@router.get("/capability-matrix")
def capability_matrix():
    """能力矩阵：作业类型 × 执行器能力 × 实时占用（与 open 租约同源）。"""
    conn = connect()
    try:
        subjects = sorted(
            {
                r["subject"]
                for r in conn.execute(
                    "SELECT DISTINCT subject FROM executors "
                    "UNION SELECT DISTINCT subject FROM windows"
                ).fetchall()
                if r["subject"]
            }
        )
        out = []
        for subj in subjects:
            execs = conn.execute(
                "SELECT code, name, active FROM executors WHERE subject=? ORDER BY code",
                (subj,),
            ).fetchall()
            items = []
            for e in execs:
                leases = conn.execute(
                    "SELECT lease_no, window_code, day, start_min, end_min "
                    "FROM leases WHERE status='open' AND executor_code=? "
                    "ORDER BY day, start_min",
                    (e["code"],),
                ).fetchall()
                items.append(
                    {
                        "code": e["code"],
                        "name": e["name"],
                        "active": e["active"],
                        "open_leases": [dict(x) for x in leases],
                        "occupied": len(leases) > 0,
                    }
                )
            out.append(
                {
                    "subject": subj,
                    "executors": items,
                    "capable_count": sum(1 for i in items if i["active"]),
                    "busy_count": sum(1 for i in items if i["occupied"]),
                }
            )
        return out
    finally:
        conn.close()
