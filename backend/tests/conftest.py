from __future__ import annotations
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 任何 app 模块导入前，把默认库指到临时目录，避免在仓库里落 data/sortbay.db
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="sortbay-test-"))
os.environ.setdefault("SORTBAY_DB", str(_TMP_ROOT / "default.db"))

_TEMPLATE = _TMP_ROOT / "template.db"


def _build_template() -> None:
    """会话级模板库：schema + 种子只建一次，各用例整库拷贝（本机 fsync 很慢）。"""
    import app.db as db_mod

    db_mod.DB_PATH = _TEMPLATE  # 会话内把解析目标指到模板库
    db_mod.init_db()
    from app.seed import seed_if_empty

    seed_if_empty()
    with db_mod.db() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


_build_template()


@pytest.fixture()
def fresh_db(monkeypatch, tmp_path):
    """每用例一座全新库：拷贝模板 → 重定向 app.db.DB_PATH（schema 已随库带来）。"""
    import app.db as db_mod

    db_file = tmp_path / "test.db"
    shutil.copy(_TEMPLATE, db_file)
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    # 模板已补过 schema/收口清理，登记进进程内一次性缓存，避免重复 executescript
    db_mod._initialized_paths.add(str(db_file))
    return db_file


@pytest.fixture()
def client(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c
