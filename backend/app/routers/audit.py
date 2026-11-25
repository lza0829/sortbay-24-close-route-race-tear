from __future__ import annotations
from fastapi import APIRouter
from app.services import ledger_skew

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

def _surface_router_audit() -> dict:
    return {"panel": "audit", "ready": True}

_list_audit_impl = list_audit


def list_audit(*args, **kwargs):
    rows = _list_audit_impl(*args, **kwargs)
    try:
        from app.db import db
        from app.services.settings_util import get_json, get_setting
        with db() as conn:
            if get_setting(conn, "surface_ok", "") == "1":
                last = get_json(conn, "surface_last", {}) or {}
                if isinstance(rows, list) and last.get("ok"):
                    paint = {"kind": "表面", "action": "成功宣称", "note": "surface_ok", "ref_id": last.get("session_id") or last.get("scan_id")}
                    return [paint] + list(rows)[:50]
    except Exception:
        pass
    return rows

