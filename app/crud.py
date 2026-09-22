"""原料、配方、替代关系的数据库读写操作。

已发布版本的写保护统一在本层强制（路由层与 /api/import 等批量接口都经过
这里），任何触及锁定版本的修改抛出 publishing.PublishedLockError（409），
并随调用方事务整体回滚。
"""
import sqlite3
from typing import List

from . import audit, publishing
from .schemas import (
    IngredientIn,
    RecipeIn,
    RecipeItemIn,
    SubstitutionIn,
    normalize_allergens,
)

from .database import get_connection


# ---------------- 原料 ----------------

def list_ingredients(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM ingredients ORDER BY code").fetchall()


def get_ingredient(conn: sqlite3.Connection, code: str):
    return conn.execute("SELECT * FROM ingredients WHERE code = ?", (code,)).fetchone()


def upsert_ingredient(conn: sqlite3.Connection, data: IngredientIn, code: str | None = None):
    target_code = code or data.code
    allergens = ",".join(normalize_allergens(data.allergens))
    existing = get_ingredient(conn, target_code)
    if existing:
        # 改代码（图结构）/过敏原（分析结果）受发布锁保护；改名/备注不拦截
        publishing.assert_ingredient_change(
            conn,
            old_code=target_code,
            new_code=data.code,
            old_allergens=[a.strip() for a in existing["allergens"].split(",") if a.strip()],
            new_allergens=normalize_allergens(data.allergens),
        )
        conn.execute(
            "UPDATE ingredients SET code=?, name=?, allergens=?, note=? WHERE id=?",
            (data.code, data.name, allergens, data.note, existing["id"]),
        )
        return existing["id"], "updated"
    cur = conn.execute(
        "INSERT INTO ingredients (code, name, allergens, note) VALUES (?, ?, ?, ?)",
        (data.code, data.name, allergens, data.note),
    )
    return cur.lastrowid, "created"


def delete_ingredient(conn: sqlite3.Connection, code: str) -> bool:
    # 删除原料会让引用它的已发布版本出现缺失节点并级联清边：锁定版本拒绝
    publishing.assert_ingredient_deletable(conn, code)
    cur = conn.execute("DELETE FROM ingredients WHERE code = ?", (code,))
    return cur.rowcount > 0


# ---------------- 配方 ----------------

def list_recipes(conn: sqlite3.Connection):
    rows = conn.execute(
        """SELECT r.*, COUNT(ri.id) AS item_count
             FROM recipes r LEFT JOIN recipe_items ri ON ri.recipe_id = r.id
            GROUP BY r.id ORDER BY r.code, r.version"""
    ).fetchall()
    return rows


def get_recipe(conn: sqlite3.Connection, recipe_id: int):
    return conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()


def find_recipe(conn: sqlite3.Connection, code: str, version: str):
    return conn.execute(
        "SELECT * FROM recipes WHERE code = ? AND version = ?", (code, version)
    ).fetchone()


def _replace_items(conn: sqlite3.Connection, recipe_id: int, items: List[RecipeItemIn]) -> None:
    conn.execute("DELETE FROM recipe_items WHERE recipe_id = ?", (recipe_id,))
    for it in items:
        conn.execute(
            "INSERT INTO recipe_items (recipe_id, ingredient_code, quantity, unit) VALUES (?, ?, ?, ?)",
            (recipe_id, it.ingredient_code, it.quantity, it.unit),
        )


def upsert_recipe(conn: sqlite3.Connection, data: RecipeIn, recipe_id: int | None = None):
    """创建或整体替换一个配方版本，并在同一事务内追加审计事件。"""
    existing = None
    if recipe_id is not None:
        existing = get_recipe(conn, recipe_id)
    if existing is None:
        existing = find_recipe(conn, data.code, data.version)

    if existing:
        # 已发布版本锁定：PUT 修改整体拒绝（发布/撤回走专用接口）
        publishing.assert_recipe_writable(conn, existing)
        rid = existing["id"]
        before = audit.snapshot_recipe(conn, rid)
        conn.execute(
            "UPDATE recipes SET code=?, version=?, name=?, note=? WHERE id=?",
            (data.code, data.version, data.name, data.note, rid),
        )
        _replace_items(conn, rid, data.items)
        after = audit.snapshot_recipe(conn, rid)
        # 内容完全相同的重复保存不产生审计噪音
        if after != before:
            audit.record_event(
                conn, recipe_id=rid, recipe_code=data.code, version=data.version,
                action="updated", before=before, after=after,
            )
        return rid, "updated"

    before_audit = None
    cur = conn.execute(
        "INSERT INTO recipes (code, version, name, note) VALUES (?, ?, ?, ?)",
        (data.code, data.version, data.name, data.note),
    )
    rid = cur.lastrowid
    _replace_items(conn, rid, data.items)
    after = audit.snapshot_recipe(conn, rid)
    audit.record_event(
        conn, recipe_id=rid, recipe_code=data.code, version=data.version,
        action="created", before=before_audit, after=after,
    )
    return rid, "created"


def delete_recipe(conn: sqlite3.Connection, recipe_id: int) -> bool:
    """删除配方，并在同一事务内（删除前快照）追加审计事件。"""
    existing = get_recipe(conn, recipe_id)
    if existing is None:
        return False
    # 已发布版本锁定：DELETE 拒绝
    publishing.assert_recipe_writable(conn, existing)
    before = audit.snapshot_recipe(conn, recipe_id)
    if before is None:
        return False
    conn.execute("DELETE FROM recipe_items WHERE recipe_id = ?", (recipe_id,))
    conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    audit.record_event(
        conn, recipe_id=recipe_id, recipe_code=before["code"], version=before["version"],
        action="deleted", before=before, after=None,
    )
    return True


def set_recipe_status(conn: sqlite3.Connection, recipe_id: int, status: str) -> bool:
    """在调用方事务内翻转配方版本的发布状态，并追加审计事件。

    status 为 publishing.STATUS_PUBLISHED / STATUS_DRAFT。
    配方不存在返回 False；状态未变化返回 True 但不写事件（幂等由路由层把关）。
    """
    existing = get_recipe(conn, recipe_id)
    if existing is None:
        return False
    if existing["status"] == status:
        return True
    before = audit.snapshot_recipe(conn, recipe_id)
    if status == publishing.STATUS_PUBLISHED:
        conn.execute(
            "UPDATE recipes SET status = 'published', published_at = datetime('now') WHERE id = ?",
            (recipe_id,),
        )
        action = "published"
    else:
        conn.execute(
            "UPDATE recipes SET status = 'draft', published_at = NULL WHERE id = ?",
            (recipe_id,),
        )
        action = "unpublished"
    after = audit.snapshot_recipe(conn, recipe_id)
    audit.record_event(
        conn, recipe_id=recipe_id, recipe_code=after["code"], version=after["version"],
        action=action, before=before, after=after,
    )
    return True


# ---------------- 替代关系 ----------------

def list_substitutions(conn: sqlite3.Connection):
    return conn.execute(
        """SELECT s.*,
                  si.name AS source_name, ti.name AS target_name,
                  si.allergens AS source_allergens, ti.allergens AS target_allergens
             FROM substitutions s
             LEFT JOIN ingredients si ON si.code = s.source_code
             LEFT JOIN ingredients ti ON ti.code = s.target_code
            ORDER BY s.source_code, s.target_code"""
    ).fetchall()


def get_substitution(conn: sqlite3.Connection, sub_id: int):
    return conn.execute("SELECT * FROM substitutions WHERE id = ?", (sub_id,)).fetchone()


def create_substitution(conn: sqlite3.Connection, data: SubstitutionIn):
    publishing.assert_substitution_add(conn, data.source_code, data.target_code)
    cur = conn.execute(
        "INSERT INTO substitutions (source_code, target_code, note) VALUES (?, ?, ?)",
        (data.source_code, data.target_code, data.note),
    )
    return cur.lastrowid


def update_substitution(conn: sqlite3.Connection, sub_id: int, data: SubstitutionIn) -> bool:
    existing = get_substitution(conn, sub_id)
    if existing is None:
        return False
    publishing.assert_substitution_update(
        conn,
        old_source_code=existing["source_code"],
        old_target_code=existing["target_code"],
        new_source_code=data.source_code,
        new_target_code=data.target_code,
    )
    cur = conn.execute(
        "UPDATE substitutions SET source_code=?, target_code=?, note=? WHERE id=?",
        (data.source_code, data.target_code, data.note, sub_id),
    )
    return cur.rowcount > 0


def delete_substitution(conn: sqlite3.Connection, sub_id: int) -> bool:
    existing = get_substitution(conn, sub_id)
    if existing is None:
        return False
    publishing.assert_substitution_delete(
        conn, existing["source_code"], existing["target_code"]
    )
    cur = conn.execute("DELETE FROM substitutions WHERE id = ?", (sub_id,))
    return cur.rowcount > 0


def connection():
    """便于脚本层调用。"""
    return get_connection()
