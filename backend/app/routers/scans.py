from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db, run_write
from app.services.common import audit, current_shift, now_str, require_unsealed
from app.services import collision, views

router = APIRouter(tags=["过机扫描"])


class ScanIn(BaseModel):
    条码: str = Field(min_length=1)
    目的地码: str = ""
    分拣线编号: int | None = None
    备注: str = ""


class AdjudicateIn(BaseModel):
    扫描编号: int | None = None
    scan_id: int | None = None
    id: int | None = None
    条码: str | None = None
    barcode: str | None = None


@router.get("/扫描")
def list_scans(状态: str | None = None, status: str | None = None):
    state = 状态 or status
    with db() as conn:
        if state:
            rows = conn.execute(
                "SELECT * FROM scans WHERE status=? ORDER BY id DESC LIMIT 200", (state,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 200").fetchall()
        return [views.scan_view(conn, r) for r in rows]


@router.get("/扫描册")
def scan_book():
    with db() as conn:
        return views.scan_book(conn, 200)


@router.get("/扫描/{scan_id}")
def get_scan(scan_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not row:
            raise HTTPException(404, "扫描不存在")
        d = views.scan_view(conn, row)
        d["联动刷新"] = views.build_linkage(conn, d.get("chute_id"), scan_id)
        return d


@router.post("/扫描")
def create_scan(body: ScanIn):
    def work(conn):
        require_unsealed(conn)
        shift = current_shift(conn)
        if body.分拣线编号:
            line = conn.execute("SELECT * FROM lines WHERE id=?", (body.分拣线编号,)).fetchone()
        else:
            line = conn.execute("SELECT * FROM lines WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        if not line:
            raise HTTPException(400, "无可用分拣线")
        if not line["active"]:
            raise HTTPException(400, "分拣线已停用")
        dup = conn.execute(
            "SELECT id FROM scans WHERE barcode=? AND shift_id=?",
            (body.条码, shift["id"]),
        ).fetchone()
        if dup:
            raise HTTPException(400, "本班次条码已过机")
        conn.execute(
            """
            INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)
            VALUES (?, ?, ?, ?, '待路由', ?, ?)
            """,
            (body.条码, body.目的地码, shift["id"], line["id"], now_str(), body.备注),
        )
        row = conn.execute(
            "SELECT * FROM scans WHERE barcode=? AND shift_id=?",
            (body.条码, shift["id"]),
        ).fetchone()
        audit(conn, "扫描", row["id"], "过机装载", body.条码)
        return dict(row)

    return run_write(work)


@router.post("/扫描/{scan_id}/路由")
def route_scan(scan_id: int):
    # 唯一裁决落锤入口（闭口优先仲裁）；回执即同刷视图
    return collision.adjudicate(scan_id)


@router.post("/扫描/{scan_id}/裁决")
def adjudicate_scan_alias(scan_id: int):
    return collision.adjudicate(scan_id)


@router.post("/裁决/{scan_id}")
def adjudicate_short_alias(scan_id: int):
    return collision.adjudicate(scan_id)


@router.post("/裁决")
def adjudicate_body(body: AdjudicateIn):
    has_any = any(v is not None for v in (
        body.扫描编号, body.scan_id, body.id, body.条码, body.barcode))
    with db() as conn:
        scan_id = collision.resolve_scan_id(
            conn,
            扫描编号=body.扫描编号, scan_id=body.scan_id, id=body.id,
            条码=body.条码, barcode=body.barcode,
        )
    if scan_id is None:
        if has_any:
            raise HTTPException(404, "扫描不存在")
        raise HTTPException(400, "缺少扫描编号")
    return collision.adjudicate(scan_id)

