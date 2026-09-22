"""SQLite 数据库连接与表结构初始化。"""
import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    allergens   TEXT NOT NULL DEFAULT '',   -- 逗号分隔的过敏原
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS recipes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL,
    version         TEXT NOT NULL,
    name            TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'published')),   -- 草稿 / 已发布（发布锁）
    published_at    TEXT,                                       -- 最近一次发布时间（撤回后清空）
    UNIQUE(code, version)
);

CREATE TABLE IF NOT EXISTS recipe_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    ingredient_code TEXT NOT NULL,
    quantity        REAL,
    unit            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS substitutions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code TEXT NOT NULL,
    target_code TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    UNIQUE(source_code, target_code)
);

CREATE TABLE IF NOT EXISTS recipe_audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       INTEGER NOT NULL,          -- 配方删除后仍保留事件，故不设外键级联
    recipe_code     TEXT NOT NULL,
    version         TEXT NOT NULL DEFAULT '',
    action          TEXT NOT NULL CHECK (action IN ('created', 'updated', 'deleted')),
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now')),
    before_snapshot TEXT,                      -- JSON：操作前完整配方+配料快照
    after_snapshot  TEXT                       -- JSON：操作后完整配方+配料快照
);

CREATE INDEX IF NOT EXISTS idx_items_recipe ON recipe_items(recipe_id);
CREATE INDEX IF NOT EXISTS idx_sub_source ON substitutions(source_code);
CREATE INDEX IF NOT EXISTS idx_sub_target ON substitutions(target_code);
CREATE INDEX IF NOT EXISTS idx_audit_recipe ON recipe_audit_events(recipe_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_code ON recipe_audit_events(recipe_code, occurred_at);

-- 审计事件只允许追加：触发器禁止 UPDATE / DELETE，保证审计链不可变
CREATE TRIGGER IF NOT EXISTS trg_recipe_audit_no_update
BEFORE UPDATE ON recipe_audit_events
BEGIN
    SELECT RAISE(ABORT, 'recipe_audit_events 为不可变审计表，禁止 UPDATE');
END;

CREATE TRIGGER IF NOT EXISTS trg_recipe_audit_no_delete
BEFORE DELETE ON recipe_audit_events
BEGIN
    SELECT RAISE(ABORT, 'recipe_audit_events 为不可变审计表，禁止 DELETE');
END;
"""


def get_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：FastAPI 的同步生成器依赖（创建连接 / 端点使用 /
    # 关闭连接）会被分别调度到线程池的不同工作线程，而 sqlite3 默认禁止
    # 跨线程使用同一连接。每个请求都在 conn_dep 中新建独立连接，不存在共享。
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        # 平滑升级旧库：为已有的 recipes 表补充发布锁列（新建库已由建表语句包含）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)").fetchall()}
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE recipes ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'"
            )
        if "published_at" not in cols:
            conn.execute("ALTER TABLE recipes ADD COLUMN published_at TEXT")
