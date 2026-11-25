from __future__ import annotations
from typing import Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from app.db import db, rows_to_list, row_to_dict
from app.services.common import audit
from app.services import collision, views

router = APIRouter(tags=["口门"])


class ChuteIn(BaseModel):
    分拣线编号: int
    口门码: str = Field(min_length=1)
    标题: str = ""
    软容量: int = 120


class GateIdIn(BaseModel):
    口门编号: int | None = None
    chute_id: int | None = None
    id: int | None = None
    口门码: str | None = None
    code: str | None = None


class GateStateIn(GateIdIn):
    状态: Any = None
    status: Any = None


@router.get("/口门")
def list_chutes():
    with db() as conn:
        rows = conn.execute(
            "SELECT c.*, l.name AS 分拣线名 FROM chutes c "
            "JOIN lines l ON l.id=c.line_id ORDER BY c.code"
        ).fetchall()
        return [views.chute_detail(conn, r) for r in rows]


@router.get("/口门/{chute_id}")
def get_chute(chute_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT c.*, l.name AS 分拣线名 FROM chutes c "
            "JOIN lines l ON l.id=c.line_id WHERE c.id=?",
            (chute_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "口门不存在")
        d = views.chute_detail(conn, row)
        d["在口件"] = views.landed_items(conn, chute_id)
        d["在途裁决"] = views.inflight_items(conn, chute_id)
        return d


@router.post("/口门")
def create_chute(body: ChuteIn):
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO chutes(line_id, code, title, status, soft_cap) VALUES (?, ?, ?, '口门开', ?)",
                (body.分拣线编号, body.口门码, body.标题, body.软容量),
            )
        except Exception as e:
            raise HTTPException(400, f"创建失败: {e}")
        row = conn.execute(
            "SELECT * FROM chutes WHERE line_id=? AND code=?",
            (body.分拣线编号, body.口门码),
        ).fetchone()
        audit(conn, "口门", row["id"], "新建", body.口门码)
        return row_to_dict(row)


@router.post("/口门/{chute_id}/开闭")
def set_chute(chute_id: int, 状态: str = Query(...)):
    # 唯一闭口/开口落锤入口：闭口优先仲裁，回执与所有联动面同事务/同刷
    return collision.set_gate(chute_id, 状态)


@router.post("/口门/{chute_id}/闭口")
def close_chute_alias(chute_id: int):
    return collision.set_gate(chute_id, "口门闭")


@router.post("/口门/{chute_id}/开口")
def open_chute_alias(chute_id: int):
    return collision.set_gate(chute_id, "口门开")


def _resolve_body_gate(body: GateIdIn) -> int:
    with db() as conn:
        cid = collision.resolve_chute_id(
            conn,
            口门编号=body.口门编号, chute_id=body.chute_id, id=body.id,
            口门码=body.口门码, code=body.code,
        )
    if cid is None:
        if any(v is not None for v in (body.口门编号, body.chute_id, body.id, body.口门码, body.code)):
            raise HTTPException(404, "口门不存在")
        raise HTTPException(400, "缺少口门编号")
    return cid


@router.post("/口门/闭口")
def close_gate_body(body: GateIdIn):
    return collision.set_gate(_resolve_body_gate(body), "口门闭")


@router.post("/口门/开口")
def open_gate_body(body: GateIdIn):
    return collision.set_gate(_resolve_body_gate(body), "口门开")


@router.post("/口门/开闭")
def set_gate_body(body: GateStateIn):
    target = body.状态 if body.状态 is not None else body.status
    if target is None:
        raise HTTPException(400, "缺少状态")
    return collision.set_gate(_resolve_body_gate(body), target)

