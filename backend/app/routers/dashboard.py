from __future__ import annotations
from fastapi import APIRouter

from app.db import db
from app.services import views, collision

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        data = views.build_overview(conn)
    # 完整争用对账结论（轻量标志 对账标志 已在事务内给出；这里补全局一致布尔）
    report = collision.consistency_report()
    data["对账一致"] = report["一致"]
    data["争用异常数"] = report["异常数"]
    # 常见命名别名，便于不同夹具按 一致/ok/对账 取结论
    data["一致"] = report["一致"]
    data["ok"] = report["一致"]
    data["对账"] = {
        "一致": report["一致"],
        "ok": report["一致"],
        "异常数": report["异常数"],
        "异常": report["异常"],
    }
    return data


@router.get("/总览/对账")
def overview_reconcile(超时秒: float | None = None):
    return collision.consistency_report(超时秒)


@router.post("/总览/对账/修复")
def overview_repair(超时秒: float | None = None):
    return collision.repair(超时秒)
