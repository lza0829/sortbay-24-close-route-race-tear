"""事故 24「关口成功灯与半空裁决证据撕裂」回归夹具。

全局唯一口径：闭口优先（ARCHITECTURE）。四组事故夹具 + 同刷面 + 重放对账：
  1. 徽标已关仍裁决成功 —— 闭口必须真落库，关后裁决整笔拒绝；
  2. 裁决绿口已关悬空 —— 裁决先落入的件，闭口回执必须显式带上；
  3. 半截关态占用仍开 —— 写闸/裁决半截整笔回滚，退回对撞前快照并钉账；
  4. 双回执已关又落入 —— 并发/连点不得留下「已关」与「已落入」两张影子账。
"""
from __future__ import annotations
import threading

import pytest

from app.db import db
from app.services import collision
from app.services.collision import CollisionReject
from app.routers import audit as audit_router
from app.routers import chutes as chutes_router
from app.routers import dashboard as dashboard_router
from app.routers import scans as scans_router

CANONICAL_CLOSE_MSG = "口门已闭，禁止落入"


# ---------------------------------------------------------------- 工具

def _chute_id(code="A01"):
    with db() as conn:
        return conn.execute("SELECT id FROM chutes WHERE code=?", (code,)).fetchone()["id"]


def _make_scan(client, barcode, dest="110"):
    r = client.post("/api/扫描", json={"条码": barcode, "目的地码": dest})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _scan(sid):
    with db() as conn:
        return dict(conn.execute("SELECT * FROM scans WHERE id=?", (sid,)).fetchone())


def _chute(cid):
    with db() as conn:
        return dict(conn.execute("SELECT * FROM chutes WHERE id=?", (cid,)).fetchone())


def _inflight_rows(sid):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM route_inflight WHERE scan_id=? ORDER BY id", (sid,)
        ).fetchall()
        return [dict(r) for r in rows]


def _runs(**where):
    sql = "SELECT * FROM collision_runs"
    args = []
    if where:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        args = list(where.values())
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _settings_keys():
    with db() as conn:
        return [r["key"] for r in conn.execute("SELECT key FROM settings").fetchall()]


def _assert_no_shadow_keys():
    """不得留下 surface_*/close_surface:*/close_route:* 影子账键。"""
    bad = [k for k in _settings_keys()
           if k.startswith("surface") or k.startswith("close_")]
    assert not bad, f"发现影子账键: {bad}"


def _assert_consistent(client):
    rep = client.get("/api/总览/对账").json()
    assert rep["一致"], rep["异常"]
    assert rep["异常数"] == 0


def _replay_all(client):
    runs = client.get("/api/对撞").json()
    assert runs, "对撞账本不应为空"
    for run in runs:
        rr = client.post(f"/api/对撞/{run['id']}/重放").json()
        assert rr["重放一致"], rr
        assert rr["账本自洽"], rr


# ---------------------------------------------------------------- 夹具 1：徽标已关仍裁决成功

def test_close_persists_and_adjudication_rejected(client):
    cid = _chute_id("A01")
    sid = _make_scan(client, "F1-001")

    # 直接函数调用（旧涂写层劫持过的入口）必须真闭口，不得被旁路重新打开
    out = chutes_router.set_chute(cid, 状态="口门闭")
    assert out["口门状态"] == "口门闭"
    assert out["ok"] is True
    assert _chute(cid)["status"] == "口门闭", "闭口后库内口门态被旁路改回开"

    # 关后裁决：整笔失败，不改扫描口门列、不增在口件数
    r = client.post(f"/api/扫描/{sid}/路由")
    assert r.status_code == 400
    body = r.json()
    assert body["detail"] == CANONICAL_CLOSE_MSG
    assert body["ok"] is False
    assert body["裁决结局"] == collision.CLOSE_FIRST_REJECT
    # 拒绝回执自带同刷视图：口门页已闭、扫描册仍待路由
    linkage = body["联动刷新"]
    assert linkage["口门详情"]["status"] == "口门闭"
    assert linkage["扫描"]["status"] == "待路由"
    assert linkage["扫描"]["chute_id"] is None

    s = _scan(sid)
    assert s["status"] == "待路由" and s["chute_id"] is None
    rows = _inflight_rows(sid)
    assert rows and all(x["status"] == "已拒绝" for x in rows)

    # 模块级直调入口（旧涂写层）现在都必须是真实现
    ov = dashboard_router.overview()
    assert "surface_ok" not in ov and "surface_probe" not in ov
    ch = {c["id"]: c for c in ov["口门"]}[cid]
    assert ch["status"] == "口门闭" and ch["在口件数"] == 0
    rows = {r["id"]: r for r in scans_router.list_scans()}
    assert rows[sid]["status"] == "待路由" and rows[sid]["chute_id"] is None
    audits = audit_router.list_audit()
    assert not [a for a in audits if a["kind"] == "表面"], "审计不得出现涂写层成功宣称"
    assert any(a["kind"] == "裁决" and a["action"] == "裁决被拒" and a["ref_id"] == sid
               for a in audits)

    _assert_no_shadow_keys()
    _assert_consistent(client)
    _replay_all(client)


def test_close_via_body_alias_persists(client):
    cid = _chute_id("A01")
    sid = _make_scan(client, "F1-002")
    r = client.post("/api/口门/闭口", json={"口门编号": cid})
    assert r.status_code == 200 and r.json()["口门状态"] == "口门闭"
    assert _chute(cid)["status"] == "口门闭"
    r = client.post("/api/裁决", json={"扫描编号": sid})
    assert r.status_code == 400 and r.json()["detail"] == CANONICAL_CLOSE_MSG
    assert _scan(sid)["chute_id"] is None
    _assert_no_shadow_keys()
    _assert_consistent(client)


# ---------------------------------------------------------------- 夹具 2：裁决绿口已关悬空

def test_land_then_close_carries_item(client):
    cid = _chute_id("A01")
    sid = _make_scan(client, "F2-001")

    r = client.post(f"/api/扫描/{sid}/路由")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["裁决结局"] == collision.ROUTE_FIRST_LAND
    assert body["在口件数"] == 1

    r = client.post(f"/api/口门/{cid}/开闭", params={"状态": "口门闭"})
    assert r.status_code == 200
    body = r.json()
    assert body["结局"] == collision.CLOSE_CARRY
    assert sid in body["带上件编号"], "裁决先落入的件必须由闭口回执显式带上"
    assert body["在口件数"] == 1

    # 落账不悬空：扫描已路由@该口，口门已闭，同刷面一致
    s = _scan(sid)
    assert s["status"] == "已路由" and s["chute_id"] == cid
    assert _chute(cid)["status"] == "口门闭"
    ov = client.get("/api/总览").json()
    ch = {c["id"]: c for c in ov["口门"]}[cid]
    assert ch["status"] == "口门闭" and ch["在口件数"] == 1
    assert ov["对账一致"] is True

    _assert_no_shadow_keys()
    _assert_consistent(client)
    _replay_all(client)


# ---------------------------------------------------------------- 夹具 3：半截关态占用仍开

def test_adjudicate_halfway_rolls_back(client, monkeypatch):
    cid = _chute_id("A01")
    sid = _make_scan(client, "F3-001")

    def boom(event, **kw):
        if event == "route_after_landing_writes":
            raise RuntimeError("夹具注入裁决半截")

    monkeypatch.setattr(collision, "seam", boom)
    with pytest.raises(RuntimeError):
        collision.adjudicate(sid)

    # 整笔回滚：扫描仍待路由、口门列为空、无落入事件、在口件数不增
    s = _scan(sid)
    assert s["status"] == "待路由" and s["chute_id"] is None
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM route_events WHERE scan_id=? AND action='路由落入'",
            (sid,),
        ).fetchone()["c"]
    assert n == 0
    assert _inflight_rows(sid)[-1]["status"] == "半截回滚"
    with db() as conn:
        detail = collision.views.chute_detail(
            conn, conn.execute("SELECT * FROM chutes WHERE id=?", (cid,)).fetchone()
        )
    assert detail["在口件数"] == 0

    # 半截必须钉账：半截回滚对撞账本 + 审计，可重放
    halves = _runs(outcome=collision.HALF_ROLLBACK, scan_id=sid)
    assert len(halves) == 1
    audits = audit_router.list_audit()
    assert any(a["action"] == "半截回滚" and a["ref_id"] == sid for a in audits)

    _assert_no_shadow_keys()
    _assert_consistent(client)
    _replay_all(client)


def test_gate_halfway_rolls_back(client, monkeypatch):
    cid = _chute_id("A01")
    sid = _make_scan(client, "F3-002")

    def boom(event, **kw):
        if event == "gate_after_status_write":
            raise RuntimeError("夹具注入写闸半截")

    monkeypatch.setattr(collision, "seam", boom)
    with pytest.raises(RuntimeError):
        collision.set_gate(cid, "口门闭")
    monkeypatch.setattr(collision, "seam", lambda *a, **k: None)

    # 写闸半截整笔回滚：口门仍开，无闭口账本，占用探针（在口件数）不脏
    assert _chute(cid)["status"] == "口门开"
    assert _runs(outcome=collision.CLOSE_CARRY, chute_id=cid) == []
    halves = _runs(outcome=collision.HALF_ROLLBACK, chute_id=cid)
    assert len(halves) == 1 and halves[0]["scan_id"] is None
    audits = audit_router.list_audit()
    assert any(a["kind"] == "口门" and a["action"] == "半截回滚" and a["ref_id"] == cid
               for a in audits)

    # 回滚后不得「徽标绿而半空仍落入」的反向：口门真开，裁决可落入
    out = collision.adjudicate(sid)
    assert out["ok"] and _scan(sid)["chute_id"] == cid
    # 再闭口成功，带上该件
    closed = collision.set_gate(cid, "口门闭")
    assert sid in closed["带上件编号"]

    _assert_no_shadow_keys()
    _assert_consistent(client)
    _replay_all(client)


# ---------------------------------------------------------------- 夹具 4：双回执已关又落入

def test_close_while_adjudication_inflight(client, monkeypatch):
    cid = _chute_id("A01")
    sid = _make_scan(client, "F4-001")
    registered = threading.Event()
    release = threading.Event()

    def hold_seam(event, **kw):
        if event == "inflight_registered":
            registered.set()
            assert release.wait(5), "夹具放行超时"

    monkeypatch.setattr(collision, "seam", hold_seam)
    result = {}

    def do_adj():
        try:
            result["out"] = collision.adjudicate(sid)
        except CollisionReject as exc:
            result["rej"] = exc

    t = threading.Thread(target=do_adj)
    t.start()
    assert registered.wait(5), "在途登记未发生"

    close_out = collision.set_gate(cid, "口门闭")
    assert sid in close_out["在途将拒"]
    assert sid in close_out["在途已拒"]
    release.set()
    t.join(5)

    # 闭口先落锤：在途裁决整笔失败，拒绝回执与审计同刷
    assert "rej" in result and "out" not in result
    rej = result["rej"]
    assert rej.detail == CANONICAL_CLOSE_MSG
    assert rej.extra["裁决回执"]["ok"] is False
    s = _scan(sid)
    assert s["status"] == "待路由" and s["chute_id"] is None
    assert _inflight_rows(sid)[-1]["status"] == "已拒绝"
    assert _chute(cid)["status"] == "口门闭"

    _assert_no_shadow_keys()
    _assert_consistent(client)
    _replay_all(client)


def test_adjudicate_lands_before_close_is_carried(client, monkeypatch):
    cid = _chute_id("A01")
    sid = _make_scan(client, "F4-002")
    landing = threading.Event()

    def signal_seam(event, **kw):
        if event == "route_after_landing_writes":
            landing.set()

    monkeypatch.setattr(collision, "seam", signal_seam)
    result = {}

    def do_adj():
        result["out"] = collision.adjudicate(sid)

    t = threading.Thread(target=do_adj)
    t.start()
    assert landing.wait(5), "裁决落入写入未发生"
    # 裁决落锤段持锁，闭口排队；裁决先提交，闭口必须带上该件
    close_out = collision.set_gate(cid, "口门闭")
    t.join(5)

    assert result["out"]["ok"] is True
    assert sid in close_out["带上件编号"]
    s = _scan(sid)
    assert s["status"] == "已路由" and s["chute_id"] == cid
    assert _chute(cid)["status"] == "口门闭"

    _assert_no_shadow_keys()
    _assert_consistent(client)
    _replay_all(client)


def test_double_adjudicate_single_landing(client):
    sid = _make_scan(client, "F4-003")
    barrier = threading.Barrier(2)
    results = []

    def hit():
        barrier.wait(5)
        try:
            results.append(("ok", collision.adjudicate(sid)))
        except Exception as exc:  # 双击败者：状态不可路由，干净拒绝
            results.append(("err", exc))

    ts = [threading.Thread(target=hit) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)

    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 1, f"双键连点只能落入一次: {results}"
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM route_events WHERE scan_id=? AND action='路由落入'",
            (sid,),
        ).fetchone()["c"]
    assert n == 1, "不得留下两张落入影子账"
    assert _scan(sid)["status"] == "已路由"
    _assert_no_shadow_keys()
    _assert_consistent(client)


def test_double_close_idempotent(client):
    cid = _chute_id("A01")
    r1 = client.post(f"/api/口门/{cid}/闭口")
    r2 = client.post(f"/api/口门/{cid}/闭口")
    assert r1.status_code == r2.status_code == 200
    assert r2.json()["重复收口"] is True
    assert _chute(cid)["status"] == "口门闭"
    # 连点之后仍不得落入
    sid = _make_scan(client, "F4-004")
    r = client.post(f"/api/扫描/{sid}/路由")
    assert r.status_code == 400
    _assert_no_shadow_keys()
    _assert_consistent(client)


def test_http_race_close_vs_adjudicate(client):
    """并发压测：闭口与多笔在途裁决对撞，结局只能二选一，不得双回执。"""
    cid = _chute_id("A01")
    sids = [_make_scan(client, f"F4-RACE-{i}") for i in range(6)]
    barrier = threading.Barrier(len(sids) + 1)
    results = {}

    def adj(sid):
        barrier.wait(5)
        results[sid] = client.post(f"/api/扫描/{sid}/路由")

    threads = [threading.Thread(target=adj, args=(s,)) for s in sids]
    for t in threads:
        t.start()
    barrier.wait(5)
    close_r = client.post(f"/api/口门/{cid}/闭口")
    for t in threads:
        t.join(15)

    assert close_r.status_code == 200
    carried = set(close_r.json()["带上件编号"])
    rejected = set(close_r.json()["在途将拒"])
    for sid, r in results.items():
        if r.status_code == 200:
            assert sid in carried, "落入件必须被闭口回执带上，不得悬空"
        else:
            assert r.status_code == 400
            assert r.json()["detail"] == CANONICAL_CLOSE_MSG
    for sid in sids:
        s = _scan(sid)
        if sid in carried:
            assert s["status"] == "已路由" and s["chute_id"] == cid
        else:
            assert s["status"] == "待路由" and s["chute_id"] is None
    assert not (carried & rejected), "同一件不得既被带上又被拒（双回执）"

    _assert_no_shadow_keys()
    _assert_consistent(client)
    _replay_all(client)


# ---------------------------------------------------------------- 同刷面

def test_surfaces_refresh_together(client):
    cid = _chute_id("A01")
    s1 = _make_scan(client, "F5-001")
    s2 = _make_scan(client, "F5-002")
    client.post(f"/api/扫描/{s1}/路由")          # s1 落入
    client.post(f"/api/口门/{cid}/闭口")          # 闭口带上 s1
    r = client.post(f"/api/扫描/{s2}/路由")       # s2 关后被拒
    assert r.status_code == 400

    # 总览/调度在途条
    ov = client.get("/api/总览").json()
    ch = {c["id"]: c for c in ov["口门"]}[cid]
    assert ch["status"] == "口门闭" and ch["在口件数"] == 1 and ch["在途路由数"] == 0
    assert ov["对账一致"] is True and ov["争用异常数"] == 0
    # 口门页
    page = client.get(f"/api/口门/{cid}").json()
    assert page["status"] == "口门闭" and page["在口件数"] == 1
    assert [x["id"] for x in page["在口件"]] == [s1]
    # 扫描册
    book = {s["id"]: s for s in client.get("/api/扫描册").json()}
    assert book[s1]["status"] == "已路由" and book[s1]["chute_id"] == cid
    assert book[s2]["status"] == "待路由" and book[s2]["chute_id"] is None
    assert book[s2]["回流入口可见"] is False
    # 回流入口：被拒件不得露出
    entry = client.get("/api/回流入口").json()
    assert s1 in entry["可回流扫描编号"] and s2 not in entry["可回流扫描编号"]
    r = client.post("/api/回流批次", json={"扫描编号": s2})
    assert r.status_code == 400
    # 审计流水与回执同刷
    audits = client.get("/api/审计").json()
    acts = {(a["kind"], a["action"], a["ref_id"]) for a in audits}
    assert ("口门", "开闭", cid) in acts
    assert ("扫描", "路由裁决", s1) in acts
    assert ("裁决", "裁决被拒", s2) in acts
    assert not [a for a in audits if a["kind"] == "表面"]
    # 关口流水（对撞账本）三种结局齐备且可重放
    runs = client.get("/api/对撞").json()
    outcomes = {r["outcome"] for r in runs}
    assert {collision.ROUTE_FIRST_LAND, collision.CLOSE_CARRY,
            collision.CLOSE_FIRST_REJECT} <= outcomes
    # 班次页对账
    shift_rec = client.get("/api/班次/对账").json()
    assert shift_rec["争用一致"] is True
    # 设置里不得有影子键
    keys = [s["键"] for s in client.get("/api/设置").json()]
    assert not [k for k in keys if k.startswith("surface") or k.startswith("close_")]

    _replay_all(client)


def test_bag_flow_after_landing(client):
    """封袋集包链路不受收口修复影响：落入→开袋→入袋→封口。"""
    cid = _chute_id("A01")
    sid = _make_scan(client, "F5-BAG")
    client.post(f"/api/扫描/{sid}/路由")
    bag = client.post("/api/封袋/开袋", json={"口门编号": cid}).json()
    r = client.post(f"/api/封袋/{bag['id']}/入袋", json={"扫描编号": sid})
    assert r.status_code == 200
    r = client.post(f"/api/封袋/{bag['id']}/封口")
    assert r.status_code == 200
    assert _scan(sid)["status"] == "已封袋"
    _assert_consistent(client)


# ---------------------------------------------------------------- 涂写层拆除与遗留收口

def test_bypass_layer_removed():
    import importlib.util

    for name in ("app.services.close_surface", "app.services.ledger_skew",
                 "app.services.surface_probe"):
        assert importlib.util.find_spec(name) is None, f"涂写层模块仍存在: {name}"
    for mod in (audit_router, chutes_router, dashboard_router, scans_router):
        assert not any(k.startswith("_surface_router") for k in vars(mod))
    assert not hasattr(chutes_router, "_set_chute_impl")
    assert not hasattr(scans_router, "_list_scans_impl")
    assert not hasattr(dashboard_router, "_overview_impl")
    assert not hasattr(audit_router, "_list_audit_impl")
    assert not hasattr(collision, "_adjudicate_impl")


def test_legacy_surface_keys_purged(fresh_db):
    from app.db import init_db

    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES "
            "('surface_ok','1'),('surface_last','{}'),"
            "('close_surface:1','{}'),('close_route:2','{}')"
        )
    init_db()
    keys = _settings_keys()
    assert not [k for k in keys if k.startswith("surface") or k.startswith("close_")]
