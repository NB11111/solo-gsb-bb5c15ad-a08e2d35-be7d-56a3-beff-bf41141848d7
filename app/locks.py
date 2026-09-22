"""发布锁：已发布版本对配方 / 原料 / 替代关系写接口的锁定校验。

已发布版本代表经过替代图分析（无循环、无缺失节点）确认的基线。凡是会改变其
分析结果的写操作都必须拒绝，包括：

- 修改 / 删除该版本配方本身（代码、版本号、名称备注之外的配料表……整体拒绝）；
- 修改 / 删除该版本依赖范围内的原料（配方配料及其沿替代边可达的全部节点）；
- 新增 / 修改 / 删除源节点位于该依赖范围内的替代边（会改变可达子图）。

依赖范围 = 从配方配料出发，沿 source -> target 替代边可达的全部节点闭包
（含配料自身）。缺边、重边与成环都不会影响闭包计算的终止性（以已访问集合去重）。
"""
import sqlite3
from typing import Iterable, List, Optional


class PublishedLockError(Exception):
    """写操作触及已发布版本的锁定范围；由 FastAPI 异常处理器映射为 409。"""

    def __init__(self, message: str, locked_recipe: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.locked_recipe = locked_recipe


def _describe(recipe: sqlite3.Row) -> str:
    return f"配方 {recipe['code']} v{recipe['version']}（id={recipe['id']}，名称：{recipe['name']}）"


def list_published_recipes(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT id, code, version, name FROM recipes WHERE status = 'published' ORDER BY id"
    ).fetchall()


def _published_scopes(conn: sqlite3.Connection):
    """返回 [(recipe_row, reachable_code_set), ...]，每个已发布版本一条。"""
    adjacency: dict = {}
    for row in conn.execute("SELECT source_code, target_code FROM substitutions"):
        adjacency.setdefault(row["source_code"], []).append(row["target_code"])

    scopes = []
    for recipe in list_published_recipes(conn):
        roots = [
            r["ingredient_code"]
            for r in conn.execute(
                "SELECT ingredient_code FROM recipe_items WHERE recipe_id = ? ORDER BY id",
                (recipe["id"],),
            ).fetchall()
        ]
        reachable: set = set()
        stack = list(roots)
        while stack:
            code = stack.pop()
            if code in reachable:
                continue
            reachable.add(code)
            stack.extend(adjacency.get(code, []))
        scopes.append((recipe, reachable))
    return scopes


def find_locked_recipes(
    conn: sqlite3.Connection,
    *,
    ingredient_codes: Iterable[str] = (),
    edge_source_codes: Iterable[str] = (),
) -> List[sqlite3.Row]:
    """返回会被指定原料 / 替代边源节点影响到的已发布版本。

    - ingredient_codes：拟修改或删除的原料代码（配料自身或闭包内任一节点都会受影响）；
    - edge_source_codes：拟新增 / 修改 / 删除的替代边的源节点代码（源的可达子图变化）。
    """
    targets = set(ingredient_codes) | set(edge_source_codes)
    if not targets:
        return []
    locked = []
    for recipe, scope in _published_scopes(conn):
        if targets & scope:
            locked.append(recipe)
    return locked


def assert_ingredient_unlocked(
    conn: sqlite3.Connection,
    codes: Iterable[str],
    *,
    action: str,
) -> None:
    """原料写操作（改代码/过敏原/名称/删除）的发布锁校验。"""
    codes = [c for c in codes if c]
    if not codes:
        return
    locked = find_locked_recipes(conn, ingredient_codes=codes)
    if locked:
        code_text = "、".join(sorted(set(codes)))
        raise PublishedLockError(
            f"原料 {code_text} 被 {len(locked)} 个已发布版本依赖，禁止{action}；"
            f"锁定版本：" + "；".join(_describe(r) for r in locked)
            + "。请先调用对应版本的 /unpublish 撤回发布后再修改。",
            locked_recipe=dict(locked[0]),
        )


def assert_substitution_unlocked(
    conn: sqlite3.Connection,
    source_codes: Iterable[str],
    *,
    action: str,
) -> None:
    """替代关系写操作的发布锁校验：以边的源节点判定可达子图是否会变化。"""
    source_codes = [c for c in source_codes if c]
    if not source_codes:
        return
    locked = find_locked_recipes(conn, edge_source_codes=source_codes)
    if locked:
        src_text = "、".join(sorted(set(source_codes)))
        raise PublishedLockError(
            f"源原料 {src_text} 的替代边会影响 {len(locked)} 个已发布版本的替代图，"
            f"禁止{action}；锁定版本：" + "；".join(_describe(r) for r in locked)
            + "。请先调用对应版本的 /unpublish 撤回发布后再修改。",
            locked_recipe=dict(locked[0]),
        )


def assert_recipe_unlocked(conn: sqlite3.Connection, recipe_id: int, action: str = "修改") -> None:
    """配方版本写操作（编辑 / 删除）的发布锁校验。"""
    row = conn.execute(
        "SELECT id, code, version, name, status FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    if row is not None and row["status"] == "published":
        raise PublishedLockError(
            f"{_describe(row)} 已发布并锁定，禁止{action}；"
            f"请先调用 POST /api/recipes/{recipe_id}/unpublish 撤回发布。",
            locked_recipe=dict(row),
        )
