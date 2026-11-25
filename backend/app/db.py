from __future__ import annotations
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app import config
from app.config import DB_PATH

# import 时的配置值，用来识别夹具是否改写了 app.db.DB_PATH / app.config.DB_PATH
_IMPORTED_CONFIG_DB_PATH = DB_PATH


def resolve_db_path() -> Path:
    """测试夹具重定向数据库时的兼容解析。

    优先级：显式 monkeypatch app.db.DB_PATH > 改写 app.config.DB_PATH >
    连接时的 SORTBAY_DB 环境变量 > 导入期默认值。
    """
    if str(DB_PATH) != str(_IMPORTED_CONFIG_DB_PATH):
        return Path(DB_PATH)
    if str(config.DB_PATH) != str(_IMPORTED_CONFIG_DB_PATH):
        return Path(config.DB_PATH)
    env_path = os.environ.get("SORTBAY_DB")
    if env_path:
        return Path(env_path)
    return Path(DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL,
  name TEXT NOT NULL,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  UNIQUE(business_day, name)
);
CREATE TABLE IF NOT EXISTS chutes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  line_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '口门开',
  soft_cap INTEGER NOT NULL DEFAULT 200,
  UNIQUE(line_id, code),
  FOREIGN KEY(line_id) REFERENCES lines(id)
);
CREATE TABLE IF NOT EXISTS route_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dest_code TEXT NOT NULL UNIQUE,
  chute_id INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  barcode TEXT NOT NULL,
  dest_code TEXT NOT NULL DEFAULT '',
  shift_id INTEGER NOT NULL,
  line_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '待路由',
  chute_id INTEGER,
  bag_id INTEGER,
  divert_batch_id INTEGER,
  scanned_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(line_id) REFERENCES lines(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS route_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(scan_id) REFERENCES scans(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS divert_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '回流中',
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id)
);
CREATE TABLE IF NOT EXISTS bags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '集包中',
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref_id INTEGER,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- 臂内在途裁决登记（闭口/裁决争用面的一部分；状态名实现自洽）
CREATE TABLE IF NOT EXISTS route_inflight (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT NOT NULL UNIQUE,
  scan_id INTEGER NOT NULL,
  chute_id INTEGER,
  dest_code TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '在途',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_inflight_chute ON route_inflight(chute_id, status);
-- 对撞收口账本：同一争用结局的前后快照、两类回执与重放次数
CREATE TABLE IF NOT EXISTS collision_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT '闭口-裁决对撞',
  architecture TEXT NOT NULL DEFAULT '闭口优先',
  chute_id INTEGER,
  scan_id INTEGER,
  outcome TEXT NOT NULL,
  close_receipt TEXT NOT NULL DEFAULT '{}',
  route_receipt TEXT NOT NULL DEFAULT '{}',
  before_snapshot TEXT NOT NULL DEFAULT '{}',
  after_snapshot TEXT NOT NULL DEFAULT '{}',
  replay_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_replay_at TEXT,
  restored_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_collision_chute_scan ON collision_runs(chute_id, scan_id);
"""


def connect() -> sqlite3.Connection:
    path = resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=8.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 争用下让锁等待而不是立刻 database is locked
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


# 测试/夹具可能在 import 后把 db.DB_PATH 指到全新临时文件；
# 已补过 schema 的路径在本进程内只补一次。夹具若删库重建，通常会自行再调
# init_db()（该函数始终强制执行 SCHEMA，不经此缓存）。
_schema_lock = threading.RLock()
_initialized_paths: set[str] = set()
_schema_state = threading.local()


def ensure_schema() -> None:
    if getattr(_schema_state, "in_init_db", False):
        return
    key = str(resolve_db_path())
    if key in _initialized_paths:
        return
    with _schema_lock:
        if key in _initialized_paths:
            return
        init_db()
        _initialized_paths.add(key)


@contextmanager
def db():
    ensure_schema()
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_write(work, *, retries: int = 80, base_delay: float = 0.008):
    """在单个 BEGIN IMMEDIATE 写事务里执行 work(conn) 并提交。

    锁冲突（SQLITE_BUSY/LOCKED）时整笔重试，因此 work 必须可重入；
    其它异常原样抛出并回滚——半截写入随事务整体退回。
    """
    ensure_schema()
    last: Exception | None = None
    for attempt in range(retries):
        conn = connect()
        conn.isolation_level = None  # 显式事务模式，杜绝隐式 BEGIN 交错
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = work(conn)
            conn.commit()
            return result
        except sqlite3.OperationalError as exc:
            last = exc
            msg = str(exc).lower()
            code = getattr(exc, "sqlite_errorcode", None)
            try:
                conn.rollback()
            except Exception:
                pass
            if code in (5, 6) or "locked" in msg or "busy" in msg:
                time.sleep(min(base_delay * (attempt + 1), 0.2))
                continue
            raise
        except BaseException:
            # 任何半截（含夹具注入异常、HTTP 4xx）都整笔回滚，退回对撞前快照
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
    assert last is not None
    raise last


def init_db() -> None:
    _schema_state.in_init_db = True
    try:
        with db() as conn:
            # WAL 让对撞期间总览/扫描册读取不被写事务挡住；不支持时静默退回回滚日志
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            conn.executescript(SCHEMA)
    finally:
        _schema_state.in_init_db = False


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]
