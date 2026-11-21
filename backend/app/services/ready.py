from __future__ import annotations

import sqlite3

# 就绪、类型裁决与候补扫描的唯一实现。所有“某槽是否空闲/某执行器能否用/
# 某候补能否用”的判断都走这里，开窗写锁内与只读页面共用同一口径，避免两套判断撕裂。
#
# ============================================================================
# 类型门禁口径（全局唯一，与 app.services.lease 同窗同事务贯彻）：
#   能力集合 = 与批窗同 subject 且启用中的执行器，按 code 升序（确定性落点）。
#   · 指定执行器不在能力集合（类型不匹配/未启用/不存在）→ 硬拒 reject_type，
#     不得落任何 open/占用/水位加。
#   · 指定执行器在集合内但时段被占 → 类型链替身：取集合内第一个空闲者落点；
#     落点必须写进租约（executor_code=落点、designated_executor_code=指定）。
#   · 集合内全部繁忙 → 拒绝 no_capable_free，同样不得落占用。
#   本模块只做纯读裁决；落库一律在 app.services.lease 的写锁事务内完成。
# ============================================================================


def room_active(conn: sqlite3.Connection, slot_code: str) -> bool:
    row = conn.execute(
        "SELECT active FROM slots WHERE code=?", (slot_code,)
    ).fetchone()
    return bool(row and row["active"] == 1)


def teacher_active(conn: sqlite3.Connection, executor_code: str) -> bool:
    row = conn.execute(
        "SELECT active FROM executors WHERE code=?", (executor_code,)
    ).fetchone()
    return bool(row and row["active"] == 1)


def overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def resource_busy(
    conn: sqlite3.Connection,
    *,
    kind: str,
    resource_code: str,
    day: str,
    start_min: int,
    end_min: int,
    exclude_session: str | None = None,
) -> sqlite3.Row | None:
    col = "slot_code" if kind == "room" else "executor_code"
    rows = conn.execute(
        f"SELECT * FROM leases WHERE {col}=? AND day=? AND status='open'",
        (resource_code, day),
    ).fetchall()
    for r in rows:
        if exclude_session and r["window_code"] == exclude_session:
            continue
        if overlap(start_min, end_min, r["start_min"], r["end_min"]):
            return r
    return None


def standby_candidates(
    conn: sqlite3.Connection, session: sqlite3.Row
) -> list[sqlite3.Row]:
    """按优先级返回同主题的候补槽（含是否空闲的判定在调用方按租约再算）。

    排序：standby_priority 升序（数字小=优先）→ capacity 降序 → code 升序。
    必须是启用态候补槽，且排除批窗自己的主槽。
    """
    rows = conn.execute(
        "SELECT * FROM slots "
        "WHERE active=1 AND is_standby=1 AND subject=? AND code<>? "
        "ORDER BY standby_priority ASC, capacity DESC, code ASC",
        (session["subject"], session["slot_code"]),
    ).fetchall()
    return list(rows)


def free_standbys(
    conn: sqlite3.Connection, session: sqlite3.Row
) -> list[sqlite3.Row]:
    """候补槽里时段真正空闲的那些，保持优先级顺序。"""
    out: list[sqlite3.Row] = []
    for slot in standby_candidates(conn, session):
        busy = resource_busy(
            conn,
            kind="room",
            resource_code=slot["code"],
            day=session["day"],
            start_min=session["start_min"],
            end_min=session["end_min"],
            exclude_session=session["code"],
        )
        if busy is None:
            out.append(slot)
    return out


def main_slot_blocker(conn: sqlite3.Connection, session: sqlite3.Row) -> str | None:
    """主槽不可用的原因（None 表示主槽可用）。只看主槽，不含执行器。"""
    if not room_active(conn, session["slot_code"]):
        return f"算力槽 {session['slot_code']} 未启用"
    busy = resource_busy(
        conn,
        kind="room",
        resource_code=session["slot_code"],
        day=session["day"],
        start_min=session["start_min"],
        end_min=session["end_min"],
        exclude_session=session["code"],
    )
    if busy is not None:
        return f"算力槽已被租约 {busy['lease_no']} 占用"
    return None


def executor_blocker(conn: sqlite3.Connection, session: sqlite3.Row) -> str | None:
    if not teacher_active(conn, session["executor_code"]):
        return f"执行器 {session['executor_code']} 未启用"
    busy = resource_busy(
        conn,
        kind="teacher",
        resource_code=session["executor_code"],
        day=session["day"],
        start_min=session["start_min"],
        end_min=session["end_min"],
        exclude_session=session["code"],
    )
    if busy is not None:
        return f"执行器已被租约 {busy['lease_no']} 占用"
    return None


# ---------------------------------------------------------------------------
# 类型门禁：能力集合与裁决
# ---------------------------------------------------------------------------


def capability_set(conn: sqlite3.Connection, subject: str) -> list[sqlite3.Row]:
    """某作业类型的能力集合：同 subject 且启用中的执行器，code 升序（确定性）。"""
    rows = conn.execute(
        "SELECT * FROM executors WHERE active=1 AND subject=? ORDER BY code ASC",
        (subject,),
    ).fetchall()
    return list(rows)


def _executor_free(
    conn: sqlite3.Connection,
    executor_code: str,
    *,
    day: str,
    start_min: int,
    end_min: int,
    exclude_session: str | None = None,
) -> bool:
    return (
        resource_busy(
            conn,
            kind="teacher",
            resource_code=executor_code,
            day=day,
            start_min=start_min,
            end_min=end_min,
            exclude_session=exclude_session,
        )
        is None
    )


def type_adjudication(conn: sqlite3.Connection, session: sqlite3.Row) -> dict:
    """类型门禁裁决（纯读，不落库）。开窗写锁事务与类型预检 API 共用这一份口径。

    verdict:
      reject_type      指定执行器不在能力集合（类型不匹配/未启用/不存在）→ 硬拒
      no_capable_free  指定执行器被占，且能力集合内无空闲替身 → 拒
      ok               可落点：landing_executor 为指定执行器或集合内替身
    """
    designated = session["executor_code"]
    cap = capability_set(conn, session["subject"])
    cap_codes = [e["code"] for e in cap]
    desig = conn.execute(
        "SELECT * FROM executors WHERE code=?", (designated,)
    ).fetchone()

    reject_reason: str | None = None
    if desig is None:
        reject_reason = f"类型不匹配：指定执行器 {designated} 不存在"
    elif desig["subject"] != session["subject"]:
        reject_reason = (
            f"类型不匹配：批窗作业类型 {session['subject']}，"
            f"指定执行器 {designated} 能力为 {desig['subject']}"
        )
    elif desig["active"] != 1:
        reject_reason = f"类型不匹配：指定执行器 {designated} 未启用，不在能力集合内"

    designated_free = False
    free_substitutes: list[str] = []
    if reject_reason is None:
        designated_free = _executor_free(
            conn,
            designated,
            day=session["day"],
            start_min=session["start_min"],
            end_min=session["end_min"],
            exclude_session=session["code"],
        )
        for e in cap:
            if e["code"] == designated:
                continue
            if _executor_free(
                conn,
                e["code"],
                day=session["day"],
                start_min=session["start_min"],
                end_min=session["end_min"],
                exclude_session=session["code"],
            ):
                free_substitutes.append(e["code"])

    if reject_reason is not None:
        verdict = "reject_type"
        landing: str | None = None
    elif designated_free:
        verdict = "ok"
        landing = designated
    elif free_substitutes:
        verdict = "ok"
        landing = free_substitutes[0]
    else:
        verdict = "no_capable_free"
        landing = None

    return {
        "window_code": session["code"],
        "subject": session["subject"],
        "designated_executor": designated,
        "capability_set": cap_codes,
        "designated_capable": reject_reason is None,
        "reject_reason": reject_reason,
        "designated_free": designated_free,
        "free_substitutes": free_substitutes,
        "landing_executor": landing,
        "substituted": bool(landing and landing != designated),
        "verdict": verdict,
    }


def readiness(conn: sqlite3.Connection, session: sqlite3.Row) -> dict:
    """只读态就绪判断（含类型门禁裁决，与开窗事务同口径）。

    ready：类型通过 + 主槽可用 + 指定执行器直落（不降级不替身）。
    degradable：主槽不可用、但执行器可落（指定或替身）且存在空闲候补。
    reasons：硬阻塞（类型拒绝 / 无同类型可落执行器 / 槽位冲突且无候补）。
    """
    adj = type_adjudication(conn, session)
    slot_block = main_slot_blocker(conn, session)
    free = free_standbys(conn, session)

    reasons: list[str] = []
    if adj["verdict"] == "reject_type":
        reasons.append(adj["reject_reason"])
    elif adj["verdict"] == "no_capable_free":
        reasons.append(
            f"执行器 {adj['designated_executor']} 被占，能力集合内无同类型空闲替身"
        )
    if slot_block and not free:
        reasons.append(slot_block + "；且无空闲候补")

    ready = (
        slot_block is None
        and adj["verdict"] == "ok"
        and not adj["substituted"]
    )
    degradable = (
        slot_block is not None
        and adj["landing_executor"] is not None
        and bool(free)
    )

    return {
        "ready": ready,
        "degradable": degradable,
        "reasons": reasons,
        "standby_free": [s["code"] for s in free],
        "type_gate": adj,
    }
