from __future__ import annotations
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import PROJECT_NAME
from app.routers import audit, bags, chutes, collisions, dashboard, diverts, lines, routes, scans, settings, shifts
from app.seed import seed_if_empty
from app.services.collision import CollisionReject

seed_if_empty()

app = FastAPI(title=PROJECT_NAME)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
for mod in (dashboard, lines, chutes, routes, scans, diverts, bags, shifts, settings, audit, collisions):
    app.include_router(mod.router, prefix="/api")


@app.exception_handler(CollisionReject)
async def collision_reject_handler(request: Request, exc: CollisionReject):
    # detail 保持为中文字符串（兼容既有断言），extra 提供结构化裁决回执/同刷视图
    body = {"detail": exc.detail}
    if exc.extra:
        body.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers or None)


@app.get("/api/健康")
def health():
    return {"产品": PROJECT_NAME, "状态": "正常"}
