"""SQLite 数据库连接与表结构初始化。

启动时自动建表/升级：
- 新版本为 recipes 增加 ``status``（draft/published）与 ``published_at`` 列；
- 旧库的审计表会在同一启动过程中重建（扩展 action 的 CHECK 范围以容纳
  published/unpublished 事件），历史事件原样保留；
- 已发布版本的锁定除应用层强制外，还通过触发器在数据库层做最后一道防线
  （trg_recipes_published_* / trg_items_published_*）。
"""
import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))

# 表定义（全新库直接使用；旧库已存在的表不受 IF NOT EXISTS 影响，列由迁移补齐）
DDL = """
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
                    CHECK (status IN ('draft', 'published')),
    published_at    TEXT,
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
    action          TEXT NOT NULL
                    CHECK (action IN ('created', 'updated', 'deleted',
                                      'published', 'unpublished')),
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now')),
    before_snapshot TEXT,                      -- JSON：操作前完整配方+配料快照
    after_snapshot  TEXT                       -- JSON：操作后完整配方+配料快照
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_items_recipe ON recipe_items(recipe_id);
CREATE INDEX IF NOT EXISTS idx_sub_source ON substitutions(source_code);
CREATE INDEX IF NOT EXISTS idx_sub_target ON substitutions(target_code);
CREATE INDEX IF NOT EXISTS idx_audit_recipe ON recipe_audit_events(recipe_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_code ON recipe_audit_events(recipe_code, occurred_at);
"""

TRIGGERS = """
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

-- 已发布版本锁定：除 status / published_at 外的任何字段修改都拒绝
-- （发布与撤回本身只改这两列，可正常通过；应用层在发布前另做图校验）
CREATE TRIGGER IF NOT EXISTS trg_recipes_published_no_update
BEFORE UPDATE ON recipes
WHEN OLD.status = 'published'
     AND (NEW.code IS NOT OLD.code
          OR NEW.version IS NOT OLD.version
          OR NEW.name IS NOT OLD.name
          OR NEW.note IS NOT OLD.note)
BEGIN
    SELECT RAISE(ABORT, '该配方版本已发布锁定：请先撤回发布（unpublish）再修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_recipes_published_no_delete
BEFORE DELETE ON recipes
WHEN OLD.status = 'published'
BEGIN
    SELECT RAISE(ABORT, '该配方版本已发布锁定：请先撤回发布（unpublish）再删除');
END;

CREATE TRIGGER IF NOT EXISTS trg_items_published_no_insert
BEFORE INSERT ON recipe_items
WHEN EXISTS (SELECT 1 FROM recipes r WHERE r.id = NEW.recipe_id AND r.status = 'published')
BEGIN
    SELECT RAISE(ABORT, '该配方版本已发布锁定，不能增删配料：请先撤回发布（unpublish）');
END;

CREATE TRIGGER IF NOT EXISTS trg_items_published_no_update
BEFORE UPDATE ON recipe_items
WHEN EXISTS (SELECT 1 FROM recipes r
              WHERE r.id IN (NEW.recipe_id, OLD.recipe_id) AND r.status = 'published')
BEGIN
    SELECT RAISE(ABORT, '该配方版本已发布锁定，不能修改配料：请先撤回发布（unpublish）');
END;

CREATE TRIGGER IF NOT EXISTS trg_items_published_no_delete
BEFORE DELETE ON recipe_items
WHEN EXISTS (SELECT 1 FROM recipes r WHERE r.id = OLD.recipe_id AND r.status = 'published')
BEGIN
    SELECT RAISE(ABORT, '该配方版本已发布锁定，不能删除配料：请先撤回发布（unpublish）');
END;
"""

# 旧库升级：审计表的 action CHECK 需要从 ('created','updated','deleted')
# 扩展到含 published/unpublished。SQLite 无法直接改 CHECK，按官方推荐流程
# 重建表并原样拷回历史事件（仅在检测到 recipes 表缺少 status 列时执行一次）。
AUDIT_REBUILD = """
DROP TRIGGER IF EXISTS trg_recipe_audit_no_update;
DROP TRIGGER IF EXISTS trg_recipe_audit_no_delete;
DROP INDEX IF EXISTS idx_audit_recipe;
DROP INDEX IF EXISTS idx_audit_code;
ALTER TABLE recipe_audit_events RENAME TO recipe_audit_events_old;
CREATE TABLE recipe_audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       INTEGER NOT NULL,
    recipe_code     TEXT NOT NULL,
    version         TEXT NOT NULL DEFAULT '',
    action          TEXT NOT NULL
                    CHECK (action IN ('created', 'updated', 'deleted',
                                      'published', 'unpublished')),
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now')),
    before_snapshot TEXT,
    after_snapshot  TEXT
);
INSERT INTO recipe_audit_events
    (id, recipe_id, recipe_code, version, action, occurred_at, before_snapshot, after_snapshot)
SELECT id, recipe_id, recipe_code, version, action, occurred_at, before_snapshot, after_snapshot
  FROM recipe_audit_events_old;
DROP TABLE recipe_audit_events_old;
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


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """把旧版本数据库平滑升级到含发布锁的结构；全新库无操作。"""
    recipe_cols = _table_columns(conn, "recipes")
    if not recipe_cols or "status" in recipe_cols:
        return  # 全新库（DDL 已建好）或已升级过
    conn.execute(
        "ALTER TABLE recipes ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'"
    )
    conn.execute("ALTER TABLE recipes ADD COLUMN published_at TEXT")
    # 旧库里审计表必然存在（与 recipes 同一版本引入），重建以放宽 action CHECK
    conn.executescript(AUDIT_REBUILD)


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(DDL)
        _migrate(conn)
        conn.executescript(INDEXES)
        conn.executescript(TRIGGERS)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
