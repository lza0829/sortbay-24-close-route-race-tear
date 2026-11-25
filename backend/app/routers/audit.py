from __future__ import annotations
from fastapi import APIRouter

from app.db import db, rows_to_list

router = APIRouter(tags=["审计"])


@router.get("/审计")
def list_audit(kind: str | None = None, 动作: str | None = None,
               action: str | None = None, limit: int = 200):
    q = "SELECT * FROM audit_events"
    cond, args = [], []
    if kind:
        cond.append("kind=?"); args.append(kind)
    act = 动作 or action
    if act:
        cond.append("action=?"); args.append(act)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(min(max(int(limit), 1), 1000))
    with db() as conn:
        return rows_to_list(conn.execute(q, tuple(args)).fetchall())
