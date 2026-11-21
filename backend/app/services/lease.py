from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.services.ready import (
    free_standbys,
    main_slot_blocker,
    type_adjudication,
)

# ============================================================================
# 并发与类型门禁口径（全局唯一）：**以类型裁决回执为准，写锁内一次裁落**
# ----------------------------------------------------------------------------
# 本模块所有写操作都必须由调用方包在 transaction() 里（BEGIN IMMEDIATE）。
# open_lease 在同一把写锁、同一连接、同一事务内按序完成：
#   读批窗 → 类型门禁裁决（ready.type_adjudication，与预检 API 同口径）→
#   槽位检查/候补扫描 → 唯一一次 open 落库。
#
# 以类型裁决回执为准：
#   · 拒绝（类型硬拒 / 无同类型可落执行器 / 槽位冲突无候补）→ 只落冲突回执
#     （conflicts 行 + conflict_count 水位），不得留下 open 租约、执行器/槽位
#     占用、open_leases 水位加、批窗 live 态；事务内根本不做占用写入，
#     插入期异常则由 transaction() 整体回滚，已泄漏必回滚。
#   · 成功 → 租约 executor_code 必落在裁决能力集合内；走类型链替身时，
#     落点写进租约（executor_code=落点、designated_executor_code=指定、
#     exec_substituted=1），租约页/接口可查。
# 检查与插入同锁；锁外不先读后写。后到者在自己的 BEGIN IMMEDIATE 拿到锁时，
# 必然看到先到者已提交的 open，于是裁决/候补扫描会把它排除，绝不会
# 「并发双跑一拒一漏」：要么整单成功，要么整单拒绝，不存在半截双态。
# ============================================================================


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bump(conn: sqlite3.Connection, key: str, delta: int) -> int:
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (key,)).fetchone()
    cur = int(row["value"]) if row else 0
    nxt = cur + delta
    conn.execute(
        "INSERT INTO watermarks(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, nxt, _now()),
    )
    return nxt


def _next_lease_no(conn: sqlite3.Connection, day: str) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM leases").fetchone()["c"] + 1
    return f"L-{day.replace('-', '')}-{n:04d}"


def _record_conflict(
    conn: sqlite3.Connection,
    *,
    kind: str,
    session: sqlite3.Row,
    resource_code: str,
    reason: str,
) -> None:
    conn.execute(
        "INSERT INTO conflicts(kind, window_code, resource_code, day, start_min, end_min, reason, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            kind,
            session["code"],
            resource_code,
            session["day"],
            session["start_min"],
            session["end_min"],
            reason,
            _now(),
        ),
    )
    _bump(conn, "conflict_count", 1)


def _insert_open_lease(
    conn: sqlite3.Connection,
    *,
    session: sqlite3.Row,
    slot_code: str,
    degraded: bool,
    adjudication: dict,
) -> dict:
    """唯一占用写点：一次开窗至多调用一次。

    executor_code 一律取裁决落点（必在能力集合内）；替身时落点与指定都落库可查。
    """
    landing = adjudication["landing_executor"]
    lease_no = _next_lease_no(conn, session["day"])
    try:
        conn.execute(
            "INSERT INTO leases(lease_no, window_code, slot_code, main_slot_code, "
            "executor_code, designated_executor_code, exec_substituted, "
            "day, start_min, end_min, degraded, status, opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open', ?)",
            (
                lease_no,
                session["code"],
                slot_code,
                session["slot_code"],
                landing,
                adjudication["designated_executor"],
                1 if adjudication["substituted"] else 0,
                session["day"],
                session["start_min"],
                session["end_min"],
                1 if degraded else 0,
                _now(),
            ),
        )
    except sqlite3.IntegrityError as e:
        # 触发器/唯一索引兜底：写锁内理论上到不了这里，到了说明口径被破坏。
        # 异常向上抛 → transaction() 整体回滚，不留任何半截占用。
        msg = str(e)
        if "slot_overlap" in msg:
            raise ValueError("候选算力槽刚被占用，开窗失败") from e
        if "executor_overlap" in msg:
            raise ValueError("执行器刚被占用，开窗失败") from e
        if "ux_leases_one_open_per_window" in msg or "UNIQUE" in msg:
            raise ValueError("批窗已在占用中，勿重复开窗") from e
        raise

    conn.execute("UPDATE windows SET status='live' WHERE code=?", (session["code"],))
    open_n = _bump(conn, "open_leases", 1)
    return {
        "ok": True,
        "lease_no": lease_no,
        "window_code": session["code"],
        "slot_code": slot_code,
        "main_slot_code": session["slot_code"],
        "degraded": degraded,
        "executor_code": landing,
        "designated_executor_code": adjudication["designated_executor"],
        "exec_substituted": bool(adjudication["substituted"]),
        "status": "open",
        "open_leases": open_n,
    }


def open_lease(conn: sqlite3.Connection, window_code: str) -> dict:
    """在写锁内（调用方持有 BEGIN IMMEDIATE）完成一次开窗。

    返回值约定：
      ok=True  已开；含 degraded / slot_code / executor_code（裁决落点）。
      ok=False 拒绝（已落冲突回执行，由调用方提交；库内无任何占用写入）。
    """
    session = conn.execute(
        "SELECT * FROM windows WHERE code=?", (window_code,)
    ).fetchone()
    if session is None:
        raise ValueError(f"批窗不存在：{window_code}")
    if session["status"] == "live":
        raise ValueError("批窗已在占用中，勿重复开窗")
    if session["status"] == "done":
        raise ValueError("批窗已结束")

    # ---- 1. 类型门禁裁决（写锁内，与类型预检 API 同一口径）----
    # 拒绝则只落冲突回执，不做任何占用写入：回执为准，库内不得留成功腿。
    adj = type_adjudication(conn, session)
    if adj["verdict"] == "reject_type":
        _record_conflict(
            conn,
            kind="type",
            session=session,
            resource_code=session["executor_code"],
            reason=adj["reject_reason"],
        )
        return {"ok": False, "error": adj["reject_reason"], "type_gate": adj}
    if adj["verdict"] == "no_capable_free":
        reason = (
            f"执行器 {adj['designated_executor']} 被占，"
            "能力集合内无同类型空闲替身"
        )
        _record_conflict(
            conn,
            kind="teacher",
            session=session,
            resource_code=session["executor_code"],
            reason=reason,
        )
        return {"ok": False, "error": reason, "type_gate": adj}

    # ---- 2. 主槽可用：直接落 open（非降级；执行器为裁决落点） ----
    slot_block = main_slot_blocker(conn, session)
    if slot_block is None:
        return _insert_open_lease(
            conn, session=session, slot_code=session["slot_code"],
            degraded=False, adjudication=adj,
        )

    # ---- 3. 主槽冲突：按优先级在写锁内扫候补并直接落 open ----
    # 允许降级（本题默认允许候补降级占用）。扫描与插入同锁，后到者看不到已占候补。
    free = free_standbys(conn, session)
    if free:
        return _insert_open_lease(
            conn, session=session, slot_code=free[0]["code"],
            degraded=True, adjudication=adj,
        )

    # ---- 4. 无空闲候补：落主槽冲突回执，拒绝（不留占用） ----
    _record_conflict(
        conn,
        kind="room",
        session=session,
        resource_code=session["slot_code"],
        reason=slot_block,
    )
    return {
        "ok": False,
        "error": slot_block + "；且无空闲候补",
        "type_gate": adj,
    }


def close_lease(conn: sqlite3.Connection, lease_no: str) -> dict:
    lease = conn.execute(
        "SELECT * FROM leases WHERE lease_no=?", (lease_no,)
    ).fetchone()
    if lease is None:
        raise ValueError(f"租约不存在：{lease_no}")
    if lease["status"] != "open":
        raise ValueError("租约不在占用态")
    conn.execute(
        "UPDATE leases SET status='closed', closed_at=? WHERE lease_no=?",
        (_now(), lease_no),
    )
    conn.execute(
        "UPDATE windows SET status='done' WHERE code=?",
        (lease["window_code"],),
    )
    open_n = _bump(conn, "open_leases", -1)
    return {
        "lease_no": lease_no,
        "status": "closed",
        "open_leases": open_n,
    }
