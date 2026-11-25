from __future__ import annotations
import json
import os
import threading
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.db import db, run_write
from app.services.common import audit, now_str, require_unsealed
from app.services import views

# 全局唯一架构口径：闭口优先，拒在途路由。任何入口都不得混用另一套。
ARCHITECTURE = "闭口优先"
DEFAULT_INFLIGHT_TIMEOUT = float(os.environ.get("SORTBAY_INFLIGHT_TIMEOUT", "120"))

# 进程内争用锁：同一进程（TestClient / 单 uvicorn worker）内把闭口与裁决的
# 落锤段完全串行；跨进程由 BEGIN IMMEDIATE + busy_timeout 兜底。
_CONTENTION_LOCK = threading.RLock()

# 结局常量（同时以中文短语落库，夹具可按短语核对）
CLOSE_FIRST_REJECT = "闭口先落锤_在途裁决整笔失败"
ROUTE_FIRST_LAND = "裁决先落锤_裁决落入"
CLOSE_CARRY = "闭口收口_带上在口件"
HALF_ROLLBACK = "半截回滚_退回对撞前快照"

_CANONICAL_CLOSE_MSG = "口门已闭，禁止落入"

# 异常类名同时保留任务书原句，方便按中文短语核对
ANOM_ORPHAN_EVENT = "裁决成功却无落入（裁决成功却无处落入）"
ANOM_DANGLING_RECEIPT = "裁决回执成功却无落入（回执成功却无落入）"
ANOM_INFLIGHT = "在途无着落（在途路由无着落）"
ANOM_CLOSED_LANDED = "口已闭仍增在口（口已闭仍落入/增在口）"


class CollisionReject(HTTPException):
    """闭口优先拒绝：detail 仍是中文字符串，extra 附带裁决回执与同刷视图。"""

    def __init__(self, detail: str, *, code: str | None = None, extra: dict | None = None):
        super().__init__(
            status_code=400,
            detail=detail,
            headers={"X-Collision-Code": code} if code else None,
        )
        self.extra = extra or {}


def seam(event: str, **kwargs):
    """夹具拦截点。默认无动作；测试可 monkeypatch 成本函数制造确定性交集/半截。"""
    return None


def critical(work):
    """争用面唯一落锤段：进程锁 + BEGIN IMMEDIATE 同一事务。"""
    with _CONTENTION_LOCK:
        return run_write(work)


# ---------------------------------------------------------------- 基础读取

def _load_route_context(conn, scan_id: int):
    require_unsealed(conn)
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not scan:
        raise HTTPException(404, "扫描不存在")
    if scan["status"] not in ("待路由", "错分回流"):
        raise HTTPException(400, f"当前状态不可路由: {scan['status']}")
    if not scan["dest_code"]:
        raise HTTPException(400, "缺少目的地码")
    rule = conn.execute(
        "SELECT * FROM route_rules WHERE dest_code=?", (scan["dest_code"],)
    ).fetchone()
    if not rule:
        raise HTTPException(400, "无匹配路由")
    chute = conn.execute("SELECT * FROM chutes WHERE id=?", (rule["chute_id"],)).fetchone()
    if not chute:
        raise HTTPException(400, "口门不存在")
    return scan, rule, chute


def make_snapshot(conn, chute_id: int | None, scan_id: int | None = None) -> dict:
    snap = {"at": now_str()}
    if chute_id is not None:
        row = conn.execute("SELECT * FROM chutes WHERE id=?", (chute_id,)).fetchone()
        snap["chute"] = views.chute_detail(conn, row)
        snap["landed"] = views.landed_items(conn, chute_id)
        snap["inflight"] = views.inflight_items(conn, chute_id)
    if scan_id is not None:
        s = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        snap["scan"] = views.scan_view(conn, s) if s else None
    return snap


def _new_code(chute_id, scan_id, prefix="DZ"):
    return f"{prefix}-C{chute_id or 0}-S{scan_id or 0}-{uuid.uuid4().hex[:10]}"


def _insert_run(conn, *, outcome, chute_id, scan_id, before, after,
                code=None, close_receipt=None, route_receipt=None, note="",
                kind="闭口-裁决对撞"):
    code = code or _new_code(chute_id, scan_id)
    conn.execute(
        """
        INSERT INTO collision_runs
          (code, kind, architecture, chute_id, scan_id, outcome,
           close_receipt, route_receipt, before_snapshot, after_snapshot,
           replay_count, created_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            code, kind, ARCHITECTURE, chute_id, scan_id, outcome,
            json.dumps(close_receipt or {}, ensure_ascii=False),
            json.dumps(route_receipt or {}, ensure_ascii=False),
            json.dumps(before or {}, ensure_ascii=False),
            json.dumps(after or {}, ensure_ascii=False),
            now_str(), note,
        ),
    )
    return code


# ---------------------------------------------------------------- 在途登记

def register_inflight(scan_id: int, chute_id: int | None, dest_code: str) -> str:
    token = uuid.uuid4().hex

    def work(conn):
        conn.execute(
            """
            INSERT INTO route_inflight
              (token, scan_id, chute_id, dest_code, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, '在途', ?, ?)
            """,
            (token, scan_id, chute_id, dest_code, now_str(), now_str()),
        )

    run_write(work)
    return token


def finish_inflight(token: str, status: str, note: str = "") -> None:
    try:
        def work(conn):
            conn.execute(
                "UPDATE route_inflight SET status=?, updated_at=?, note=? WHERE token=?",
                (status, now_str(), note[:300], token),
            )
        run_write(work)
    except Exception:
        # 登记收尾失败不能掩盖主异常；超时对账会回收无着落在途。
        pass


def _best_effort_half_run(scan_id, chute_id, token, err) -> None:
    try:
        def work(conn):
            before = make_snapshot(conn, chute_id, scan_id)
            code = _insert_run(
                conn, outcome=HALF_ROLLBACK, chute_id=chute_id, scan_id=scan_id,
                before=before, after=before,
                route_receipt={"token": token, "异常": repr(err)[:300]},
                note="裁决半截，写事务整体回滚，退回对撞前快照",
            )
            audit(conn, "裁决", scan_id, "半截回滚", f"{code} {repr(err)[:120]}")
        run_write(work)
    except Exception:
        pass


def _best_effort_half_gate_run(chute_id, target, err) -> None:
    """写闸（闭口/开口）半截：落锤事务已整笔回滚，补记对撞账本与审计钉死这次半截。

    与裁决半截同口径：before==after==回滚后现态，重放据此核对「徽标不得绿而
    半空仍落入」——回滚后口门态必须退回对撞前快照。
    """
    try:
        def work(conn):
            snap = make_snapshot(conn, chute_id)
            code = _insert_run(
                conn, outcome=HALF_ROLLBACK, chute_id=chute_id, scan_id=None,
                before=snap, after=snap,
                close_receipt={"目标状态": target, "异常": repr(err)[:300]},
                note="写闸半截，写事务整体回滚，退回对撞前快照",
            )
            audit(conn, "口门", chute_id, "半截回滚", f"{code} {repr(err)[:120]}")
        run_write(work)
    except Exception:
        pass


# ---------------------------------------------------------------- 裁决入口

def adjudicate(scan_id: int) -> dict:
    # 阶段 0：无锁快速校验/解析目标口门（落锤段内会重新校验）
    with db() as conn:
        scan, _rule, chute = _load_route_context(conn, scan_id)
        chute_id = chute["id"]
        dest_code = scan["dest_code"]

    # 臂内在途登记（独立已提交可见，闭口令可据此列在途，但闭口优先不等待）
    token = register_inflight(scan_id, chute_id, dest_code)
    seam("inflight_registered", scan_id=scan_id, chute_id=chute_id, token=token)

    try:
        outcome = critical(lambda conn: _route_decisive(conn, scan_id, token))
    except HTTPException as exc:
        finish_inflight(token, "已拒绝", str(exc.detail))
        raise
    except Exception as exc:  # 半截：run_write 已回滚整笔
        finish_inflight(token, "半截回滚", repr(exc))
        _best_effort_half_run(scan_id, chute_id, token, exc)
        raise

    if outcome.get("rejected"):
        # 拒绝结局已随落锤事务提交（对撞账本+审计+在途终态）
        raise CollisionReject(
            outcome["message"], code=outcome.get("code"), extra=outcome.get("extra")
        )
    return outcome["payload"]


def _route_decisive(conn, scan_id: int, token: str) -> dict:
    # 持锁 + BEGIN IMMEDIATE 后重新装载，保证看到闭口已提交的最新口门态
    scan, _rule, chute = _load_route_context(conn, scan_id)
    chute_id = chute["id"]
    before = make_snapshot(conn, chute_id, scan_id)
    seam("route_before_guard", conn=conn, scan_id=scan_id, chute_id=chute_id, token=token)

    if chute["status"] != "口门开":
        # detail 保持与既有接口逐字一致，扩展说明另放 说明/详细信息
        message = _CANONICAL_CLOSE_MSG
        full_message = (
            f"{message}；{ARCHITECTURE}："
            "闭口已提交，在途裁决整笔失败，不改扫描口门列、不增在口件数"
        )
        conn.execute(
            "UPDATE route_inflight SET status='已拒绝', updated_at=?, note=? WHERE token=?",
            (now_str(), CLOSE_FIRST_REJECT, token),
        )
        audit(conn, "裁决", scan_id, "裁决被拒", f"口门{chute['code']}已闭·闭口优先整笔失败")
        after = make_snapshot(conn, chute_id, scan_id)
        code = _insert_run(
            conn, outcome=CLOSE_FIRST_REJECT, chute_id=chute_id, scan_id=scan_id,
            before=before, after=after,
            route_receipt={"token": token, "成功": False, "信息": full_message},
        )
        reject_receipt = {
            "回执类型": "裁决回执",
            "ok": False,
            "裁决结果": "整笔拒绝",
            "裁决结局": CLOSE_FIRST_REJECT,
            "对撞号": code,
            "collision_code": code,
            "口门编号": chute_id,
            "口门状态": "口门闭",
            "扫描编号": scan_id,
            "架构口径": ARCHITECTURE,
            "说明": full_message,
        }
        extra = {
            "ok": False,
            "message": message,
            "说明": full_message,
            "详细信息": full_message,
            "裁决回执": reject_receipt,
            **reject_receipt,
            "联动刷新": views.build_linkage(conn, chute_id, scan_id),
        }
        return {"rejected": True, "message": message, "code": code, "extra": extra}

    # 口门开：裁决落入的全部写操作在同一事务内
    conn.execute(
        "UPDATE scans SET status='已路由', chute_id=? WHERE id=?",
        (chute_id, scan_id),
    )
    conn.execute(
        "INSERT INTO route_events(scan_id, chute_id, action, created_at, note) "
        "VALUES (?, ?, '路由落入', ?, ?)",
        (scan_id, chute_id, now_str(), chute["code"]),
    )
    # 半截夹具拦截点：此时已写 scans/route_events 但尚未提交，抛出即整笔回滚
    seam("route_after_landing_writes", conn=conn, scan_id=scan_id,
         chute_id=chute_id, token=token)
    audit(conn, "扫描", scan_id, "路由裁决", chute["code"])  # 与既有审计口径一致
    conn.execute(
        "UPDATE route_inflight SET status='已落入', updated_at=?, note=? WHERE token=?",
        (now_str(), ROUTE_FIRST_LAND, token),
    )
    after = make_snapshot(conn, chute_id, scan_id)
    code = _insert_run(
        conn, outcome=ROUTE_FIRST_LAND, chute_id=chute_id, scan_id=scan_id,
        before=before, after=after,
        route_receipt={"token": token, "成功": True, "落入": chute["code"]},
    )
    row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    payload = views.scan_view(conn, row)
    detail = views.chute_detail(
        conn, conn.execute("SELECT * FROM chutes WHERE id=?", (chute_id,)).fetchone()
    )
    payload.update({
        "回执类型": "裁决回执",
        "ok": True,
        "裁决结果": "路由落入",
        "裁决结局": ROUTE_FIRST_LAND,
        "结局": ROUTE_FIRST_LAND,
        "对撞号": code,
        "code": code,
        "collision_code": code,
        "口门编号": chute_id,
        "在口件数": detail["在口件数"],
        "架构口径": ARCHITECTURE,
        "联动刷新": views.build_linkage(conn, chute_id, scan_id),
    })
    return {"rejected": False, "payload": payload, "code": code}


# ---------------------------------------------------------------- 闭口入口

_GATE_STATE_ALIASES = {
    "口门开": "口门开", "开": "口门开", "开启": "口门开", "open": "口门开",
    "opened": "口门开", "0": "口门开", "false": "口门开",
    "口门闭": "口门闭", "闭": "口门闭", "闭口": "口门闭", "关闭": "口门闭",
    "close": "口门闭", "closed": "口门闭", "1": "口门闭", "true": "口门闭",
}


def normalize_gate_state(value) -> str:
    if isinstance(value, bool):
        return "口门闭" if value else "口门开"
    if isinstance(value, int):
        return "口门闭" if value else "口门开"
    key = str(value).strip()
    if key in _GATE_STATE_ALIASES:
        return _GATE_STATE_ALIASES[key]
    low = key.lower()
    if low in _GATE_STATE_ALIASES:
        return _GATE_STATE_ALIASES[low]
    raise HTTPException(400, "状态仅允许口门开或口门闭")


def set_gate(chute_id: int, target: str) -> dict:
    target = normalize_gate_state(target)
    try:
        return critical(lambda conn: _gate_decisive(conn, chute_id, target))
    except HTTPException:
        # 校验类失败（口门不存在等）未产生任何半截写入，直接透传
        raise
    except Exception as exc:
        # 写闸半截：run_write 已整笔回滚；补记对撞账本+审计，钉死这次半截
        _best_effort_half_gate_run(chute_id, target, exc)
        raise


def resolve_chute_id(conn, *, 口门编号=None, chute_id=None, id=None,
                    口门码=None, code=None) -> int | None:
    cid = 口门编号 if 口门编号 is not None else chute_id
    cid = cid if cid is not None else id
    if cid is not None:
        return int(cid)
    code_val = 口门码 or code
    if code_val:
        row = conn.execute("SELECT id FROM chutes WHERE code=?", (str(code_val),)).fetchone()
        if row:
            return row["id"]
    return None


def resolve_scan_id(conn, *, 扫描编号=None, scan_id=None, id=None,
                    条码=None, barcode=None) -> int | None:
    sid = 扫描编号 if 扫描编号 is not None else scan_id
    sid = sid if sid is not None else id
    if sid is not None:
        return int(sid)
    code_val = 条码 or barcode
    if code_val:
        row = conn.execute(
            "SELECT id FROM scans WHERE barcode=? ORDER BY id DESC LIMIT 1",
            (str(code_val),),
        ).fetchone()
        if row:
            return row["id"]
    return None


def _gate_decisive(conn, chute_id: int, target: str) -> dict:
    row = conn.execute("SELECT * FROM chutes WHERE id=?", (chute_id,)).fetchone()
    if not row:
        raise HTTPException(404, "口门不存在")
    before = make_snapshot(conn, chute_id)
    already = row["status"] == target

    conn.execute("UPDATE chutes SET status=? WHERE id=?", (target, chute_id))
    # 与既有审计完全一致：kind=口门 action=开闭 note=目标状态
    audit(conn, "口门", chute_id, "开闭", target)
    seam("gate_after_status_write", conn=conn, chute_id=chute_id, target=target)

    updated = conn.execute("SELECT * FROM chutes WHERE id=?", (chute_id,)).fetchone()
    landed = views.landed_items(conn, chute_id)
    inflight = views.inflight_items(conn, chute_id)

    if target == "口门闭" and inflight:
        # 写闸拒证：闭口落锤同事务把该口在途裁决登记收口为「已拒绝」。
        # 与裁决落锤段共用同一把进程锁 + 同一 BEGIN IMMEDIATE 写事务，
        # 不存在半截；在途裁决自身的落锤段随后仍会补写自己的拒收回执与
        # 对撞账本（同命运终态，不是另一张影子账）。
        conn.execute(
            "UPDATE route_inflight SET status='已拒绝', updated_at=?, note=? "
            "WHERE chute_id=? AND status='在途'",
            (now_str(), f"{CLOSE_FIRST_REJECT}·闭口令同闸收口", chute_id),
        )
        audit(conn, "口门", chute_id, "闭口拒在途",
              f"在途{len(inflight)}笔同闸整拒: 扫描{[x['scan_id'] for x in inflight]}")

    detail = views.chute_detail(conn, updated)

    if target == "口门闭":
        outcome = CLOSE_CARRY
        code = _insert_run(
            conn, outcome=outcome, chute_id=chute_id, scan_id=None,
            before=before, after=make_snapshot(conn, chute_id),
            close_receipt={
                "在口件数": detail["在口件数"],
                "在途裁决数": len(inflight),
                "带上件编号": [x["id"] for x in landed],
                "在途已拒编号": [x["scan_id"] for x in inflight],
            },
            note=("重复闭口" if already else "闭口") +
                 (f"；在口{len(landed)}件已带上；在途{len(inflight)}笔同闸整拒" if inflight
                  else f"；在口{len(landed)}件已带上"),
        )
    else:
        outcome = "口门开启"
        code = None

    payload = dict(updated)
    payload.update(detail)
    payload.update({
        "回执类型": "闭口回执" if target == "口门闭" else "开口回执",
        "ok": True,
        "口门状态": target,
        "重复收口": already,
        "架构口径": ARCHITECTURE,
        "联动刷新": views.build_linkage(conn, chute_id, None),
    })
    if target == "口门闭":
        receipt = {
            "code": code,
            "口门编号": chute_id,
            "口门码": updated["code"],
            "口门状态": target,
            "在口件数": detail["在口件数"],
            "在口件": landed,
            "在途裁决数": len(inflight),
            "在途裁决": inflight,
            "在途将拒": [x["scan_id"] for x in inflight],
            "在途已拒": [x["scan_id"] for x in inflight],
            "对撞号": code,
            "collision_code": code,
            "结局": outcome,
            "架构口径": ARCHITECTURE,
        }
        payload.update({
            "code": code,
            "闭口回执": receipt,
            "对撞号": code,
            "collision_code": code,
            "结局": outcome,
            # 顶层别名，方便不同夹具按名取件
            "在口件": landed,
            "在口清单": landed,
            "带上该件": landed,
            "带上件编号": [x["id"] for x in landed],
            "在途裁决": inflight,
            "在途裁决数": len(inflight),
            "在途路由数": len(inflight),
            "在途将拒": [x["scan_id"] for x in inflight],
            "在途将拒编号": [x["scan_id"] for x in inflight],
            "在途已拒": [x["scan_id"] for x in inflight],
            "在途已拒编号": [x["scan_id"] for x in inflight],
        })
    else:
        payload.update({"开口回执": {"口门编号": chute_id, "口门状态": target}})
    return payload


# ---------------------------------------------------------------- 对撞账本/重放

def list_runs(limit: int = 100) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM collision_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_run_to_dict(r) for r in rows]


def _run_to_dict(row) -> dict:
    d = dict(row)
    d["close_receipt"] = views.loads(d.get("close_receipt"), {})
    d["route_receipt"] = views.loads(d.get("route_receipt"), {})
    d["before_snapshot"] = views.loads(d.get("before_snapshot"), {})
    d["after_snapshot"] = views.loads(d.get("after_snapshot"), {})
    return d


def get_run(run_id: int | None = None, code: str | None = None) -> dict | None:
    with db() as conn:
        if run_id is not None:
            row = conn.execute("SELECT * FROM collision_runs WHERE id=?", (run_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM collision_runs WHERE code=?", (code,)).fetchone()
        return _run_to_dict(row) if row else None


def replay_run(run_id: int) -> dict:
    """按账本重放/核对同一争用结局。

    历史对撞之后允许发生合法演进（后来闭口、再开口、再路由、入袋封袋），
    因此重放核对的是：账本快照与结局自洽 + 该结局的持久落账后果成立 +
    全局对账无异常，而不是要求世界永远停在 after_snapshot。
    """
    with db() as conn:
        row = conn.execute("SELECT * FROM collision_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "对撞记录不存在")
        run = _run_to_dict(row)
        checks = _self_consistency_checks(run)
        checks += _durable_checks(conn, run)
        hard_ok = all(c["级别"] != "硬" or c["一致"] for c in checks)
        conn.execute(
            "UPDATE collision_runs SET replay_count=replay_count+1, last_replay_at=? WHERE id=?",
            (now_str(), run_id),
        )
        audit(conn, "对撞", run_id, "重放", f"{run['code']} 硬一致={hard_ok}")
        replay_no = (run["replay_count"] or 0) + 1
        code = run["code"]
        outcome = run["outcome"]
        after = run["after_snapshot"]
    # 事务已提交，再开连接做全局对账，避免同进程写事务自阻塞
    report = consistency_report()
    consistent = hard_ok and report["一致"]
    return {
        "对撞号": code,
        "run_id": run_id,
        "原结局": outcome,
        "重放一致": consistent,
        "账本自洽": hard_ok,
        "全局对账一致": report["一致"],
        "检查": checks,
        "全局异常": report["异常"],
        "期望快照": after,
        "重放次数": replay_no,
    }


def _chk(name, expected, actual, *, hard=True):
    return {"项": name, "期望": expected, "实际": actual,
            "一致": expected == actual, "级别": "硬" if hard else "软"}


def _self_consistency_checks(run: dict) -> list[dict]:
    """纯函数式重放：账本记录的 before/outcome 必须能推出 after 的关键事实。"""
    before, after = run["before_snapshot"], run["after_snapshot"]
    out = []
    if run["outcome"] == CLOSE_FIRST_REJECT:
        out.append(_chk("结局自洽:闭口后快照为闭", "口门闭",
                        (after.get("chute") or {}).get("status")))
        out.append(_chk("结局自洽:被拒件仍待路由", "待路由",
                        (after.get("scan") or {}).get("status")))
        out.append(_chk("结局自洽:被拒件不写扫描口门列", None,
                        (after.get("scan") or {}).get("chute_id")))
        out.append(_chk("结局自洽:在口件数不增",
                        (before.get("chute") or {}).get("在口件数"),
                        (after.get("chute") or {}).get("在口件数")))
    elif run["outcome"] == HALF_ROLLBACK:
        if "scan" in before or "scan" in after:
            out.append(_chk("结局自洽:半截后扫描仍待路由", "待路由",
                            (after.get("scan") or {}).get("status")))
            out.append(_chk("结局自洽:半截后扫描口门列为空", None,
                            (after.get("scan") or {}).get("chute_id")))
        if "chute" in before and "chute" in after:
            out.append(_chk("结局自洽:半截后口门态退回对撞前",
                            (before.get("chute") or {}).get("status"),
                            (after.get("chute") or {}).get("status")))
    elif run["outcome"] == ROUTE_FIRST_LAND:
        out.append(_chk("结局自洽:裁决后已路由", "已路由",
                        (after.get("scan") or {}).get("status")))
        expected_cid = run["chute_id"]
        out.append(_chk("结局自洽:裁决后写入口门列", expected_cid,
                        (after.get("scan") or {}).get("chute_id")))
        b = (before.get("chute") or {}).get("在口件数")
        a = (after.get("chute") or {}).get("在口件数")
        if b is not None and a is not None:
            out.append(_chk("结局自洽:在口件数 +1", b + 1, a))
    elif run["outcome"] == CLOSE_CARRY:
        out.append(_chk("结局自洽:收口后口门闭", "口门闭",
                        (after.get("chute") or {}).get("status")))
        b = (before.get("chute") or {}).get("在口件数")
        a = (after.get("chute") or {}).get("在口件数")
        out.append(_chk("结局自洽:闭口不改变在口件数(带上)", b, a))
        carried = {x["id"] for x in run.get("close_receipt", {}).get("在口件", [])} \
            if isinstance(run.get("close_receipt"), dict) else set()
        landed = {x["id"] for x in after.get("landed", [])}
        if carried:
            out.append(_chk("结局自洽:回执带上件=快照在口件", landed, carried))
    return out


def _later_success_run(conn, scan_id: int, after_run_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM collision_runs WHERE scan_id=? AND id>? AND outcome=? LIMIT 1",
        (scan_id, after_run_id, ROUTE_FIRST_LAND),
    ).fetchone()
    return row is not None


def _durable_checks(conn, run: dict) -> list[dict]:
    out = []
    sid, cid, rid = run["scan_id"], run["chute_id"], run["id"]
    if not sid:
        return out
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (sid,)).fetchone()
    if not scan:
        return [_chk("落账:扫描仍存在", True, False)]
    outcome = run["outcome"]
    if outcome == ROUTE_FIRST_LAND:
        ev = conn.execute(
            "SELECT 1 FROM route_events WHERE scan_id=? AND chute_id=? AND action='路由落入' LIMIT 1",
            (sid, cid),
        ).fetchone()
        out.append(_chk("落账:路由落入事件存在", True, bool(ev)))
        reversed_ev = conn.execute(
            "SELECT 1 FROM route_events WHERE scan_id=? AND action='路由落入冲正' LIMIT 1",
            (sid,),
        ).fetchone()
        reversed_au = conn.execute(
            "SELECT 1 FROM audit_events WHERE kind='扫描' AND ref_id=? "
            "AND action='路由裁决冲正' LIMIT 1",
            (sid,),
        ).fetchone()
        if reversed_ev or reversed_au:
            # 合法后续：错账已通过冲正 + 退回快照核销，重放应认得这段演进
            out.append(_chk("落账:已冲正退回对撞前快照(合法后续)", True, True, hard=False))
            out.append(_chk(
                "落账:冲正后扫描退回待路由/空口门", True,
                scan["status"] == "待路由" and scan["chute_id"] is None, hard=False,
            ))
            return out
        out.append(_chk("落账:扫描口门列与裁决口门一致", cid, scan["chute_id"]))
        out.append(_chk("落账:不再是待路由", True, scan["status"] != "待路由"))
    elif outcome in (CLOSE_FIRST_REJECT, HALF_ROLLBACK):
        held = scan["status"] == "待路由" and scan["chute_id"] is None
        evolved = (not held) and _later_success_run(conn, sid, rid)
        if held:
            out.append(_chk("落账:被拒/回滚件仍待路由且不占口门", True, True))
        elif evolved:
            out.append(_chk("落账:被拒后经后续对撞再路由(合法演进)", True, True, hard=False))
        else:
            out.append(_chk("落账:被拒/回滚件无正当后续却已落入", "待路由/空口门",
                            f"{scan['status']}/C{scan['chute_id']}"))
    return out


def restore_snapshot(run_id: int) -> dict:
    """半截/错账兜底：把该对撞涉及的 chutes/scans 主状态退回 before_snapshot。

    审计与事件只追加不删除：对原“路由落入/路由裁决”补一条冲正，
    使回执-落账可以经审计重放达成一致，而不是抹掉历史。
    """
    with db() as conn:
        row = conn.execute("SELECT * FROM collision_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "对撞记录不存在")
        run = _run_to_dict(row)
        before = run["before_snapshot"]
        restored = {"chute": None, "scan": None}
        if before.get("chute") and run["chute_id"]:
            c = before["chute"]
            conn.execute("UPDATE chutes SET status=? WHERE id=?",
                         (c.get("status", "口门开"), run["chute_id"]))
            restored["chute"] = run["chute_id"]
        if before.get("scan") and run["scan_id"]:
            s = before["scan"]
            conn.execute(
                "UPDATE scans SET status=?, chute_id=?, bag_id=?, divert_batch_id=? WHERE id=?",
                (s.get("status", "待路由"), s.get("chute_id"), s.get("bag_id"),
                 s.get("divert_batch_id"), run["scan_id"]),
            )
            restored["scan"] = run["scan_id"]
            # 冲正原落入事件/裁决回执（若有），对账据此核销
            if run["chute_id"]:
                conn.execute(
                    "INSERT INTO route_events(scan_id, chute_id, action, created_at, note) "
                    "VALUES (?, ?, '路由落入冲正', ?, ?)",
                    (run["scan_id"], run["chute_id"], now_str(), run["code"]),
                )
            audit(conn, "扫描", run["scan_id"], "路由裁决冲正",
                  f"{run['code']} 退回对撞前快照")
        conn.execute("UPDATE collision_runs SET restored_at=? WHERE id=?", (now_str(), run_id))
        audit(conn, "对撞", run_id, "退回快照", run["code"])
        return {"ok": True, "对撞号": run["code"], "已退回": restored,
                "当前快照": make_snapshot(conn, run["chute_id"], run["scan_id"])}


# ---------------------------------------------------------------- 对账

def _parse_ts(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def consistency_report(stale_seconds: float | None = None) -> dict:
    stale_seconds = DEFAULT_INFLIGHT_TIMEOUT if stale_seconds is None else stale_seconds
    anomalies: list[dict] = []
    with db() as conn:
        # 1) route_events 有“路由落入”，但 scans 未落账 —— 回执/落账不一致
        #    （已有后续“路由落入冲正”核销的不算异常）
        for r in conn.execute(
            """
            SELECT e.id eid, e.scan_id, e.chute_id echute, e.created_at et,
                   s.status sstatus, s.chute_id schute
            FROM route_events e LEFT JOIN scans s ON s.id=e.scan_id
            WHERE e.action='路由落入'
              AND NOT EXISTS (
                SELECT 1 FROM route_events x
                WHERE x.scan_id=e.scan_id AND x.action='路由落入冲正' AND x.id>e.id
              )
            """
        ).fetchall():
            d = dict(r)
            if d["sstatus"] is None:
                anomalies.append({"类": "落入事件无扫描", **d})
            elif d["sstatus"] == "待路由" or d["schute"] is None or d["schute"] != d["echute"]:
                anomalies.append({"类": ANOM_ORPHAN_EVENT, **d})

        # 2) 审计里有 canonical 路由裁决，但扫描不在已落入族
        #    （已有后续“路由裁决冲正”核销的不算异常）
        for a in conn.execute(
            """
            SELECT a.id, a.ref_id, a.created_at FROM audit_events a
            WHERE a.action='路由裁决' AND a.kind='扫描'
              AND NOT EXISTS (
                SELECT 1 FROM audit_events x
                WHERE x.kind='扫描' AND x.ref_id=a.ref_id
                  AND x.action='路由裁决冲正' AND x.id>a.id
              )
            """
        ).fetchall():
            s = conn.execute("SELECT status, chute_id FROM scans WHERE id=?", (a["ref_id"],)).fetchone()
            if not s or s["status"] == "待路由" or s["chute_id"] is None:
                anomalies.append({
                    "类": ANOM_DANGLING_RECEIPT,
                    "audit_id": a["id"], "scan_id": a["ref_id"],
                    "status": s["status"] if s else None,
                    "chute_id": s["chute_id"] if s else None,
                })

        # 3) 在途无着落（超时仍为 在途）
        cutoff = datetime.now() - timedelta(seconds=stale_seconds)
        for r in conn.execute("SELECT * FROM route_inflight WHERE status='在途'").fetchall():
            ts = _parse_ts(r["created_at"])
            if ts is None or ts <= cutoff:
                anomalies.append({"类": ANOM_INFLIGHT, "inflight_id": r["id"],
                                  "scan_id": r["scan_id"], "token": r["token"]})

        # 4) 对撞账本判定：闭口优先拒绝/半截回滚的件，除非后续另有成功落入对撞，
        #    否则不得处于已落入/占口门状态
        for r in conn.execute(
            "SELECT id, code, scan_id, outcome FROM collision_runs "
            "WHERE outcome IN (?, ?) AND scan_id IS NOT NULL ORDER BY id",
            (CLOSE_FIRST_REJECT, HALF_ROLLBACK),
        ).fetchall():
            s = conn.execute("SELECT status, chute_id FROM scans WHERE id=?", (r["scan_id"],)).fetchone()
            later_ok = conn.execute(
                "SELECT 1 FROM collision_runs WHERE scan_id=? AND id>? AND outcome=? LIMIT 1",
                (r["scan_id"], r["id"], ROUTE_FIRST_LAND),
            ).fetchone()
            if s and not later_ok and (s["status"] != "待路由" or s["chute_id"] is not None):
                anomalies.append({
                    "类": ANOM_CLOSED_LANDED,
                    "run": r["code"], "run_id": r["id"], "scan_id": r["scan_id"],
                    "status": s["status"], "chute_id": s["chute_id"],
                })

        overview = views.build_overview(conn)
        consistent = not anomalies
        return {
            "一致": consistent,
            "ok": consistent,
            "consistent": consistent,
            "异常数": len(anomalies),
            "anomaly_count": len(anomalies),
            "异常": anomalies,
            "anomalies": anomalies,
            "总览": overview,
            "架构口径": ARCHITECTURE,
            "architecture": ARCHITECTURE,
            "生成时间": now_str(),
        }


def repair(stale_seconds: float | None = None) -> dict:
    """保守修复：回收超时在途；错账优先按对撞前快照退回，无快照则追加冲正核销。"""
    report = consistency_report(stale_seconds)
    reaped, restored, reversed_codes = [], [], []
    with db() as conn:
        cutoff = datetime.now() - timedelta(
            seconds=DEFAULT_INFLIGHT_TIMEOUT if stale_seconds is None else stale_seconds
        )
        for r in conn.execute("SELECT * FROM route_inflight WHERE status='在途'").fetchall():
            ts = _parse_ts(r["created_at"])
            if ts is None or ts <= cutoff:
                conn.execute(
                    "UPDATE route_inflight SET status='超时回收', updated_at=?, note='对账回收' WHERE id=?",
                    (now_str(), r["id"]),
                )
                reaped.append(r["id"])
                audit(conn, "裁决", r["scan_id"], "在途超时回收", r["token"])
        conn.commit()

    def latest_land_run_id(scan_id):
        with db() as conn:
            row = conn.execute(
                "SELECT id FROM collision_runs WHERE scan_id=? AND outcome=? "
                "ORDER BY id DESC LIMIT 1",
                (scan_id, ROUTE_FIRST_LAND),
            ).fetchone()
            return row["id"] if row else None

    # 1) 能对上对撞账本的错账：退回该对撞前快照（内部追加冲正，不删历史）
    handled_runs: set[int] = set()
    for a in report["异常"]:
        run_id = None
        if a.get("类") == ANOM_CLOSED_LANDED and a.get("run"):
            run = get_run(code=a["run"])
            run_id = run["id"] if run else None
        elif a.get("类") in (ANOM_ORPHAN_EVENT, ANOM_DANGLING_RECEIPT) and a.get("scan_id"):
            run_id = latest_land_run_id(a["scan_id"])
        if run_id is not None and run_id not in handled_runs:
            handled_runs.add(run_id)
            r = restore_snapshot(run_id)
            restored.append(r["对撞号"])

    # 2) 无账本可退的悬空成功回执/落入事件：追加冲正行使对账闭合
    mid = consistency_report(stale_seconds)
    with db() as conn:
        for a in mid["异常"]:
            kind = a.get("类")
            sid = a.get("scan_id")
            if kind == ANOM_ORPHAN_EVENT and a.get("echute") is not None:
                exists = conn.execute(
                    "SELECT 1 FROM route_events WHERE scan_id=? AND action='路由落入冲正' LIMIT 1",
                    (sid,),
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO route_events(scan_id, chute_id, action, created_at, note) "
                        "VALUES (?, ?, '路由落入冲正', ?, '对账修复核销悬空落入')",
                        (sid, a["echute"], now_str()),
                    )
                    reversed_codes.append(f"event:S{sid}")
            elif kind == ANOM_DANGLING_RECEIPT and sid is not None:
                exists = conn.execute(
                    "SELECT 1 FROM audit_events WHERE kind='扫描' AND ref_id=? "
                    "AND action='路由裁决冲正' LIMIT 1",
                    (sid,),
                ).fetchone()
                if not exists:
                    audit(conn, "扫描", sid, "路由裁决冲正", "对账修复核销悬空成功回执")
                    reversed_codes.append(f"audit:S{sid}")

    after = consistency_report(stale_seconds)
    return {
        "回收在途": reaped,
        "退回快照对撞": restored,
        "冲正核销": reversed_codes,
        "修复后": after,
        "一致": after["一致"],
    }

