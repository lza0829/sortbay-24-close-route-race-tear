from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import db
from app.services import collision, views

router = APIRouter(tags=["对撞重放"])


class ReplayIn(BaseModel):
    run_id: int | None = None
    id: int | None = None
    对撞编号: int | None = None
    对撞号: str | None = None
    code: str | None = None


@router.get("/对撞")
def list_collisions(limit: int = 100):
    return collision.list_runs(limit)


@router.get("/对撞/{run_id}")
def get_collision(run_id: int):
    run = collision.get_run(run_id=run_id)
    if not run:
        raise HTTPException(404, "对撞记录不存在")
    return run


@router.post("/对撞/{run_id}/重放")
def replay_collision(run_id: int):
    return collision.replay_run(run_id)


@router.post("/对撞/{run_id}/退回快照")
def restore_collision(run_id: int):
    return collision.restore_snapshot(run_id)


@router.get("/对撞/按号/{code}/重放")
def replay_by_code(code: str):
    run = collision.get_run(code=code)
    if not run:
        raise HTTPException(404, "对撞记录不存在")
    return collision.replay_run(run["id"])


@router.post("/对撞/按号/{code}/重放")
def replay_by_code_post(code: str):
    return replay_by_code(code)


@router.post("/对撞/重放")
def replay_generic(body: ReplayIn):
    run_id = body.run_id if body.run_id is not None else body.id
    run_id = run_id if run_id is not None else body.对撞编号
    if run_id is not None:
        return collision.replay_run(int(run_id))
    code = body.对撞号 or body.code
    if code:
        run = collision.get_run(code=str(code))
        if not run:
            raise HTTPException(404, "对撞记录不存在")
        return collision.replay_run(run["id"])
    raise HTTPException(400, "缺少对撞编号或对撞号")


@router.get("/联动刷新")
def linkage(chute_id: int | None = None, scan_id: int | None = None):
    with db() as conn:
        return views.build_linkage(conn, chute_id, scan_id)
