from __future__ import annotations

import sqlite3

from app.services.ready import capability_set

# ============================================================================
# 差异桶（对账）：类型回执 vs 已落库占用，实时互证。
# 全局口径 = 以类型裁决回执为准，因此以下任一并存都算差异：
#   · 类型拒绝回执与 open 占用并存（假拒 / 泄漏成功腿）；
#   · open 租约落点执行器越出批窗能力集合（成功腿越权）；
#   · 批窗态与租约态撕裂（live 无租约 / 租约 open 而批窗非 live）；
#   · 水位与实槽不一致。
# 差异未清前，总览类型门禁徽章不得显示「通过」（见 routers/dashboard）。
# 正常开窗路径在写锁内裁落一致，差异桶应为空；它存在是为了让任何撕裂
# （历史遗留、口径退化）立刻可见，而不是被徽章掩盖。
# ============================================================================


def _wm(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row else 0


def type_gate_differences(conn: sqlite3.Connection) -> list[dict]:
    diffs: list[dict] = []

    opens = conn.execute("SELECT * FROM leases WHERE status='open'").fetchall()
    open_by_window = {l["window_code"]: l for l in opens}

    # 1. open 租约：落点执行器必须在批窗能力集合内；批窗态必须是 live。
    for lease in opens:
        win = conn.execute(
            "SELECT * FROM windows WHERE code=?", (lease["window_code"],)
        ).fetchone()
        if win is None:
            diffs.append({
                "kind": "lease_without_window",
                "window_code": lease["window_code"],
                "detail": f"租约 {lease['lease_no']} 占用中，但批窗记录不存在",
            })
            continue
        exe = conn.execute(
            "SELECT * FROM executors WHERE code=?", (lease["executor_code"],)
        ).fetchone()
        cap = [e["code"] for e in capability_set(conn, win["subject"])]
        if exe is None or exe["active"] != 1 or exe["subject"] != win["subject"]:
            diffs.append({
                "kind": "executor_out_of_capability",
                "window_code": lease["window_code"],
                "detail": (
                    f"租约 {lease['lease_no']} 落点执行器 {lease['executor_code']} "
                    f"越出能力集合（批窗类型 {win['subject']}，集合 {','.join(cap) or '空'}）"
                ),
            })
        if win["status"] != "live":
            diffs.append({
                "kind": "window_state_torn",
                "window_code": lease["window_code"],
                "detail": (
                    f"租约 {lease['lease_no']} 占用中，但批窗状态为 {win['status']}"
                ),
            })

    # 2. live 批窗必须有 open 租约。
    for win in conn.execute("SELECT * FROM windows WHERE status='live'").fetchall():
        if win["code"] not in open_by_window:
            diffs.append({
                "kind": "live_without_lease",
                "window_code": win["code"],
                "detail": "批窗为占用态，但无 open 租约",
            })

    # 3. 类型拒绝回执与占用并存（假拒 / 泄漏）。
    type_rejects = conn.execute(
        "SELECT * FROM conflicts WHERE kind='type'"
    ).fetchall()
    for c in type_rejects:
        lease = open_by_window.get(c["window_code"])
        if lease is not None:
            diffs.append({
                "kind": "reject_receipt_with_open_lease",
                "window_code": c["window_code"],
                "detail": (
                    f"存在类型拒绝回执（{c['reason']}），但租约 {lease['lease_no']} "
                    "仍占用中——假拒与真泄漏并存"
                ),
            })

    # 4. 水位与实槽。
    open_wm = _wm(conn, "open_leases")
    open_actual = len(opens)
    live_n = conn.execute(
        "SELECT COUNT(*) c FROM windows WHERE status='live'"
    ).fetchone()["c"]
    if not (open_wm == open_actual == live_n):
        diffs.append({
            "kind": "watermark_diverged",
            "window_code": "-",
            "detail": (
                f"open_leases 水位 {open_wm} / open 租约实数 {open_actual} "
                f"/ 占用中批窗 {live_n} 不一致"
            ),
        })
    conflict_wm = _wm(conn, "conflict_count")
    conflict_actual = conn.execute("SELECT COUNT(*) c FROM conflicts").fetchone()["c"]
    if conflict_wm != conflict_actual:
        diffs.append({
            "kind": "watermark_diverged",
            "window_code": "-",
            "detail": (
                f"conflict_count 水位 {conflict_wm} / 冲突实数 {conflict_actual} 不一致"
            ),
        })

    return diffs
