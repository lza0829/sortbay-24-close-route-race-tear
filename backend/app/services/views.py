from __future__ import annotations
import json

from app.services.common import now_str

# 在口件数口径与 /总览 既有口径保持一致：已路由、已入袋仍在口门；
# 已封袋随袋离场，错分回流转回流批次。
AT_CHUTE_STATUSES = ("已路由", "已入袋")
LANDED_FAMILY = ("已路由", "已入袋", "已封袋")
DIVERTIBLE_STATUSES = ("已路由", "已入袋")


def loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def pressure_of(count: int, soft_cap) -> dict:
    cap = int(soft_cap or 0)
    ratio = round(count / cap, 3) if cap > 0 else 0.0
    if cap <= 0:
        level = "未设容量"
    elif count >= cap:
        level = "超限"
    elif count >= cap * 0.8:
        level = "偏高"
    else:
        level = "正常"
    return {
        "在口件数": count,
        "软容量": cap,
        "压力值": ratio,
        "压力": level,
        "压力等级": level,
        "建议封袋": level in ("偏高", "超限"),
    }


def chute_counts(conn, chute_id: int) -> dict:
    count = conn.execute(
        "SELECT COUNT(*) c FROM scans WHERE chute_id=? AND status IN ('已路由','已入袋')",
        (chute_id,),
    ).fetchone()["c"]
    inflight = conn.execute(
        "SELECT COUNT(*) c FROM route_inflight WHERE chute_id=? AND status='在途'",
        (chute_id,),
    ).fetchone()["c"]
    return {"在口件数": count, "在途路由数": inflight}


def chute_detail(conn, chute_or_row) -> dict | None:
    if chute_or_row is None:
        return None
    d = dict(chute_or_row)
    cid = d["id"]
    counts = chute_counts(conn, cid)
    d.update(counts)
    d["在途裁决数"] = counts["在途路由数"]
    d.update(pressure_of(counts["在口件数"], d.get("soft_cap")))
    return d


def landed_items(conn, chute_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, barcode, dest_code, status, chute_id, bag_id, scanned_at
        FROM scans
        WHERE chute_id=? AND status IN ('已路由','已入袋')
        ORDER BY id
        """,
        (chute_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def inflight_items(conn, chute_id: int | None = None) -> list[dict]:
    if chute_id is None:
        rows = conn.execute(
            "SELECT * FROM route_inflight WHERE status='在途' ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM route_inflight WHERE chute_id=? AND status='在途' ORDER BY id",
            (chute_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def scan_view(conn, row) -> dict:
    d = dict(row)
    d["可回流"] = d.get("status") in DIVERTIBLE_STATUSES
    # 回流入口可见性：只有已经落入（已路由/已入袋）的件才允许从回流入口选中；
    # 待路由（含闭口优先被拒、半截回滚）不得露出可建回流的入口。
    d["回流入口可见"] = d["可回流"]
    d["可入袋"] = d.get("status") == "已路由"
    # “扫描口门列”即 scans.chute_id 这一列的同刷别名（未裁决/被拒时为 None）
    d["扫描口门列"] = d.get("chute_id")
    if d.get("chute_id") and "口门码" not in d:
        c = conn.execute("SELECT code, status FROM chutes WHERE id=?", (d["chute_id"],)).fetchone()
        if c:
            d["口门码"] = c["code"]
            d["口门状态"] = c["status"]
    return d


def scan_book(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.*, c.code AS 口门码, c.status AS 口门状态
        FROM scans s LEFT JOIN chutes c ON c.id=s.chute_id
        ORDER BY s.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [scan_view(conn, r) for r in rows]


def invariant_flags(conn) -> dict:
    """轻量、可在争用事务内读取的对账标志。"""
    closed_open_landed = conn.execute(
        """
        SELECT COUNT(*) c FROM scans s JOIN chutes c ON c.id=s.chute_id
        WHERE c.status='口门闭' AND s.status IN ('已路由','已入袋')
        """
    ).fetchone()["c"]
    orphan_receipt = conn.execute(
        """
        SELECT COUNT(*) c FROM audit_events a
        LEFT JOIN scans s ON s.id=a.ref_id AND a.kind='扫描'
        WHERE a.action='路由裁决'
          AND (s.id IS NULL OR s.status='待路由' OR s.chute_id IS NULL)
        """
    ).fetchone()["c"]
    stale_inflight = conn.execute(
        "SELECT COUNT(*) c FROM route_inflight WHERE status='在途'"
    ).fetchone()["c"]
    return {
        # 注意：口闭但存在「闭口前已落入并被带上」的件是合法态，
        # 因此这里只给计数，是否为异常要结合对撞账本判定。
        "口闭在口件数": closed_open_landed,
        "裁决无落入数": orphan_receipt,
        "在途路由数": stale_inflight,
    }


def build_overview(conn) -> dict:
    shift = conn.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 1").fetchone()
    counts = {
        "待路由": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='待路由'").fetchone()["c"],
        "已路由": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='已路由'").fetchone()["c"],
        "错分回流": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='错分回流'").fetchone()["c"],
        "已入袋": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='已入袋'").fetchone()["c"],
        "已封袋": conn.execute("SELECT COUNT(*) c FROM scans WHERE status='已封袋'").fetchone()["c"],
    }
    chutes = [
        chute_detail(conn, r)
        for r in conn.execute("SELECT * FROM chutes ORDER BY code").fetchall()
    ]
    inflight = inflight_items(conn)
    flags = invariant_flags(conn)
    return {
        "班次": dict(shift) if shift else None,
        "分布": counts,
        "口门": chutes,
        "在途路由数": len(inflight),
        "在途路由": inflight,
        "对账标志": flags,
        "架构口径": "闭口优先",
    }


def divert_entry(conn) -> dict:
    eligible = scan_book(conn, 200)
    visible = [s for s in eligible if s["可回流"]]
    return {
        "回流入口": "错分回流",
        "可见": True,
        "可回流扫描": visible,
        "可回流扫描编号": [s["id"] for s in visible],
        "说明": "仅已路由/已入袋（确已落入）的件露出回流入口；待路由与闭口被拒件不露出。",
        "生成时间": now_str(),
    }


def bag_pressure(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM chutes ORDER BY code").fetchall()
    out = []
    for r in rows:
        d = chute_detail(conn, r)
        open_bags = conn.execute(
            "SELECT COUNT(*) c FROM bags WHERE chute_id=? AND status='集包中'", (r["id"],)
        ).fetchone()["c"]
        d["集包中袋数"] = open_bags
        out.append(d)
    return out


def recent_audit(conn, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()]


def build_linkage(conn, chute_id: int | None = None, scan_id: int | None = None) -> dict:
    """闭口回执/裁决回执共享的「同刷」切片：一次返回所有联动面。"""
    linkage = {
        "总览": build_overview(conn),
        "扫描册": scan_book(conn, 100),
        "回流入口": divert_entry(conn),
        "封袋压力": bag_pressure(conn),
        "审计": recent_audit(conn, 20),
        "刷新时间": now_str(),
    }
    if chute_id is not None:
        row = conn.execute("SELECT * FROM chutes WHERE id=?", (chute_id,)).fetchone()
        linkage["口门详情"] = chute_detail(conn, row)
        linkage["在口件"] = landed_items(conn, chute_id)
    if scan_id is not None:
        s = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        linkage["扫描"] = scan_view(conn, s) if s else None
    return linkage
