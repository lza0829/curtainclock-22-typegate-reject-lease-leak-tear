# -*- coding: utf-8 -*-
"""类型门禁四夹具自检（口径：以类型裁决回执为准，写锁内一次裁落）。

夹具① 409 仍有 open：类型硬拒后，库内不得留 open/占用/水位加/批窗 live。
夹具② 成功执行器越权：替身成功时租约落点必须在能力集合内且可查；
       能力集合全忙时必须 409，不得越权落到异类型执行器。
夹具③ 交错半截：降级成功腿与回执必须同态（ok=租约开=批窗 live=水位+1），
       不得再落冲突行；拒绝腿同样不得留半截。
夹具④ 并发漏写：并发类型检查双跑必须收敛——成功者各落集合内不同执行器，
       类型不符者 409 且零占用，无双重占，水位一致，差异桶为空。

只依赖标准库，对运行中的 API 发请求：
    python _type_gate_check.py [http://127.0.0.1:11100/api]
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11100/api"
RUN = uuid.uuid4().hex[:6].upper()


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"detail": raw}
        return e.code, payload


def create_window(code, subject, slot, executor, day, s, e):
    st, out = call("POST", "/windows", {
        "code": code, "title": code, "subject": subject,
        "slot_code": slot, "executor_code": executor,
        "day": day, "start_min": s, "end_min": e,
    })
    assert st == 200, (code, st, out)
    return out


def open_leases():
    st, rows = call("GET", "/leases")
    assert st == 200
    return [r for r in rows if r["status"] == "open"]


def open_lease_of(window_code):
    hits = [r for r in open_leases() if r["window_code"] == window_code]
    assert len(hits) <= 1, f"同窗双开: {window_code}"
    return hits[0] if hits else None


def window_row(code):
    st, rows = call("GET", "/windows")
    assert st == 200
    for r in rows:
        if r["code"] == code:
            return r
    raise AssertionError(f"批窗不存在: {code}")


def dashboard():
    st, d = call("GET", "/dashboard")
    assert st == 200
    return d


def differences():
    st, d = call("GET", "/type-gate/differences")
    assert st == 200
    return d["differences"]


def capability_set_of(subject):
    st, matrix = call("GET", "/capability-matrix")
    assert st == 200
    for row in matrix:
        if row["subject"] == subject:
            return [e["code"] for e in row["executors"] if e["active"]]
    raise AssertionError(f"能力矩阵缺类型: {subject}")


def assert_clean(label):
    """全局互证：水位一致、差异桶为空、类型门禁徽章通过。"""
    d = dashboard()
    assert d["watermarks_consistent"] is True, (label, "水位撕裂", d)
    assert d["open_leases"] == len(open_leases()) == d["live_sessions"], (label, d)
    diffs = differences()
    assert diffs == [], (label, "差异桶非空", diffs)
    assert d["type_gate"]["status"] == "passed", (label, d["type_gate"])
    return d


def close_all():
    for r in open_leases():
        call("POST", "/leases/close", {"lease_no": r["lease_no"]})


def race(codes):
    results, lock = [], threading.Lock()
    barrier = threading.Barrier(len(codes))

    def worker(code):
        barrier.wait()
        st, out = call("POST", "/leases/open", {"window_code": code})
        with lock:
            results.append((code, st, out))

    ts = [threading.Thread(target=worker, args=(c,)) for c in codes]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results


def fixture_1_reject_leaves_no_occupancy():
    """夹具①：类型硬拒（409）后库内不得留任何成功腿。"""
    day = "2026-12-01"
    code = f"TG1-MIS-{RUN}"
    create_window(code, "ETL", "S-ETL-01", "E-BI-01", day, 9 * 60, 10 * 60)

    # 类型预检（只读）必须先说清楚：拒绝，且不得因此落任何回执写。
    st, chk = call("GET", f"/windows/{code}/type-check")
    assert st == 200 and chk["verdict"] == "reject_type", chk
    assert "E-BI-01" not in chk["capability_set"], chk

    before = dashboard()
    st, out = call("POST", "/leases/open", {"window_code": code})
    assert st == 409, (st, out)
    assert "类型不匹配" in str(out.get("detail", "")), out

    # 回执为准：拒绝 ⇒ 无 open 租约、批窗仍 draft、open 水位不动。
    assert open_lease_of(code) is None, "夹具①泄漏：409 后库内仍有 open"
    assert window_row(code)["status"] == "draft", "夹具①泄漏：批窗态被改写"
    after = dashboard()
    assert after["open_leases"] == before["open_leases"], (before, after)
    assert after["conflict_count"] == before["conflict_count"] + 1, (before, after)

    # 拒绝回执可查（冲突桶 kind=type），且与占用不并存 ⇒ 差异桶必须为空。
    st, rows = call("GET", "/conflicts")
    assert st == 200
    mine = [r for r in rows if r["window_code"] == code and r["kind"] == "type"]
    assert len(mine) == 1, ("类型拒绝回执未落冲突桶", rows)
    assert_clean("夹具①")
    print("夹具① PASS: 409 类型硬拒 → 无 open/无占用/水位不动，回执落冲突桶可查")


def fixture_2_substitute_lands_in_capability_set():
    """夹具②：替身成功落点必须在能力集合内；集合全忙必须 409 不得越权。"""
    day = "2026-12-02"
    S, E = 9 * 60, 10 * 60
    cap_etl = capability_set_of("ETL")
    assert len(cap_etl) >= 4, cap_etl

    main = f"TG2-MAIN-{RUN}"
    create_window(main, "ETL", "S-ETL-01", "E-PIPE-01", day, S, E)
    st, r1 = call("POST", "/leases/open", {"window_code": main})
    assert st == 200 and r1["executor_code"] == "E-PIPE-01", r1

    # 指定执行器被占 → 类型链替身，落点必须是集合内另一台。
    sub = f"TG2-SUB-{RUN}"
    create_window(sub, "ETL", "S-ETL-02", "E-PIPE-01", day, S, E)
    st, chk = call("GET", f"/windows/{sub}/type-check")
    assert st == 200 and chk["verdict"] == "ok" and chk["substituted"], chk
    predicted = chk["landing_executor"]
    assert predicted in cap_etl and predicted != "E-PIPE-01", chk
    st, r2 = call("POST", "/leases/open", {"window_code": sub})
    assert st == 200 and r2["ok"], r2
    assert r2["exec_substituted"] is True, r2
    assert r2["designated_executor_code"] == "E-PIPE-01", r2
    assert r2["executor_code"] in cap_etl and r2["executor_code"] != "E-PIPE-01", r2
    assert r2["executor_code"] == predicted, ("预检与开窗事务口径不一", chk, r2)

    # 替身落点写进租约可查（租约页执行器列同刷）。
    lease2 = open_lease_of(sub)
    assert lease2 and lease2["executor_code"] == r2["executor_code"], lease2
    assert lease2["exec_substituted"] == 1, lease2
    assert lease2["designated_executor_code"] == "E-PIPE-01", lease2

    # 占满 ETL 能力集合剩余两台（走候补槽，互不抢执行器）。
    x3 = f"TG2-X3-{RUN}"
    create_window(x3, "ETL", "S-ETL-01", "E-PIPE-03", day, S, E)
    st, r3 = call("POST", "/leases/open", {"window_code": x3})
    assert st == 200 and r3["executor_code"] == "E-PIPE-03", r3
    x4 = f"TG2-X4-{RUN}"
    create_window(x4, "ETL", "S-ETL-02", "E-PIPE-04", day, S, E)
    st, r4 = call("POST", "/leases/open", {"window_code": x4})
    assert st == 200 and r4["executor_code"] == "E-PIPE-04", r4

    # 能力集合全忙：必须 409，且不得越权落到空闲的 BI 执行器。
    over = f"TG2-OVER-{RUN}"
    create_window(over, "ETL", "S-ETL-01", "E-PIPE-01", day, S, E)
    st, chk = call("GET", f"/windows/{over}/type-check")
    assert st == 200 and chk["verdict"] == "no_capable_free", chk
    st, out = call("POST", "/leases/open", {"window_code": over})
    assert st == 409, (st, out)
    assert open_lease_of(over) is None, "夹具②越权：集合全忙仍落 open"
    st, execs = call("GET", "/executors")
    bi = {e["code"]: e for e in execs if e["code"].startswith("E-BI")}
    assert all(not e["occupied"] for e in bi.values()), ("越权占用 BI 执行器", bi)

    assert_clean("夹具②")
    print("夹具② PASS: 替身落点在能力集合内且租约可查；集合全忙 409 不越权")


def fixture_3_no_half_state():
    """夹具③：降级成功腿与回执同态，不得半截双态。"""
    day = "2026-12-03"
    S, E = 13 * 60, 14 * 60
    h1 = f"TG3-H1-{RUN}"
    create_window(h1, "ETL", "S-ETL-01", "E-PIPE-01", day, S, E)
    st, r1 = call("POST", "/leases/open", {"window_code": h1})
    assert st == 200 and r1["ok"], r1

    before = dashboard()
    h2 = f"TG3-H2-{RUN}"
    create_window(h2, "ETL", "S-ETL-01", "E-PIPE-02", day, S, E)
    st, r2 = call("POST", "/leases/open", {"window_code": h2})
    # 成功腿：回执 ok 与库内态必须同真。
    assert st == 200 and r2["ok"] is True, ("半截：库内成功回执失败", st, r2)
    assert r2["degraded"] is True and r2["slot_code"].startswith("S-ETL-SB"), r2
    lease2 = open_lease_of(h2)
    assert lease2 is not None, "半截：回执成功但无租约"
    assert window_row(h2)["status"] == "live", "半截：租约开但批窗非 live"
    after = dashboard()
    assert after["open_leases"] == before["open_leases"] + 1, (before, after)
    # 成功腿不得再落冲突行（旧 bug：落 open 后又记冲突）。
    assert after["conflict_count"] == before["conflict_count"], (before, after)
    st, rows = call("GET", "/conflicts")
    assert not any(r["window_code"] == h2 for r in rows), "成功腿留下冲突回执"

    assert_clean("夹具③")
    print("夹具③ PASS: 降级成功回执=租约开=批窗 live=水位+1，无冲突行、无半截")


def fixture_4_concurrent_converges():
    """夹具④：并发双跑必须收敛——一拒一漏不得出现。"""
    day = "2026-12-04"
    S, E = 9 * 60, 10 * 60
    cap_etl = capability_set_of("ETL")
    n_cap = len(cap_etl)

    etl_slots = ["S-ETL-01", "S-ETL-02", "S-ETL-SB1", "S-ETL-SB2",
                 "S-ETL-01", "S-ETL-02"][: n_cap + 2]
    racers = []
    for i, slot in enumerate(etl_slots):
        code = f"TG4-R{i}-{RUN}"
        create_window(code, "ETL", slot, "E-PIPE-01", day, S, E)
        racers.append(code)
    mismatched = []
    for j, exe in enumerate(["E-BI-01", "E-BI-02"]):
        code = f"TG4-M{j}-{RUN}"
        create_window(code, "ETL", f"S-BI-0{j + 1}", exe, day, S, E)
        mismatched.append(code)
        racers.append(code)

    results = race(racers)
    oks = [(c, p) for c, st, p in results if st == 200]
    fails = [(c, p) for c, st, p in results if st != 200]

    # 成功数 == 能力集合大小；落点互不相同且都在集合内。
    assert len(oks) == n_cap, ("成功数应等于能力集合大小", results)
    landed = sorted(p["executor_code"] for _, p in oks)
    assert landed == sorted(cap_etl), ("落点应恰好用尽能力集合", landed, cap_etl)
    # 类型不符的两路必须 409 且零占用。
    for code in mismatched:
        hit = [p for c, p in fails if c == code]
        assert hit and "类型不匹配" in str(hit[0].get("detail", "")), (code, results)
    # 每一路失败都不得留下占用（一拒一漏收敛）。
    for code, _ in fails:
        assert open_lease_of(code) is None, f"并发漏写：{code} 被拒却留 open"
    # 无双重占：同窗/同槽/同执行器都不重叠。
    rows = open_leases()
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if a["day"] == b["day"] and a["start_min"] < b["end_min"] and b["start_min"] < a["end_min"]:
                assert a["slot_code"] != b["slot_code"], ("双占同槽", a, b)
                assert a["executor_code"] != b["executor_code"], ("双占同执行器", a, b)
            assert a["window_code"] != b["window_code"], ("同窗双开", a, b)

    assert_clean("夹具④")
    print(f"夹具④ PASS: {len(oks)} 路成功各落集合内不同执行器，"
          f"{len(fails)} 路 409 零占用，无双重占，差异桶为空")


def main():
    close_all()
    assert_clean("前置")
    fixture_1_reject_leaves_no_occupancy()
    fixture_2_substitute_lands_in_capability_set()
    fixture_3_no_half_state()
    fixture_4_concurrent_converges()
    close_all()
    assert_clean("终态")
    print("ALL TYPE-GATE FIXTURES PASSED")


if __name__ == "__main__":
    main()
