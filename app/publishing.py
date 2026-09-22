"""配方版本发布锁：发布校验（循环/缺失节点）与已发布版本的写保护。

- 版本初始为 ``draft``（草稿），POST ``/api/recipes/{id}/publish`` 时在调用方
  的同一事务内重新执行替代图分析，发现循环路径或缺失节点即拒绝发布并返回
  对应路径证据；通过后置为 ``published``。
- ``unpublish`` 撤回到草稿。
- 已发布版本对三类写接口施加保护，任何**会影响其替代图分析结果**的修改都拒绝：
  配方自身（PUT/DELETE 配方版本）、原料（改代码/过敏原、删除）、替代关系
  （增删改替代边）。原料改名、改备注等不影响过敏原分析的操作仍允许。
- 拒绝响应（HTTP 409）统一抛出 :class:`PublishedLockError`，detail 内含锁定
  版本清单与「该被修改对象是经由哪条路径进入该版本」的证据。
"""
import collections
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

from . import graph


STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"


class PublishedLockError(Exception):
    """写操作触及已发布版本的锁定范围。"""

    def __init__(self, message: str, detail: Dict[str, Any]):
        super().__init__(message)
        self.message = message
        self.detail = detail


# ---------------- 已发布版本与其可达闭包 ----------------

def published_recipes(conn) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM recipes WHERE status = 'published' ORDER BY id"
    ).fetchall()


def _roots_for(conn, recipe_id: int) -> List[str]:
    return [
        r["ingredient_code"]
        for r in conn.execute(
            "SELECT ingredient_code FROM recipe_items WHERE recipe_id = ? ORDER BY id",
            (recipe_id,),
        ).fetchall()
    ]


def _undirected_closure(
    roots: List[str],
    nodes: Dict[str, Dict[str, Any]],
    adjacency: Dict[str, List[Dict[str, str]]],
) -> Set[str]:
    """从配方配料出发，在「忽略方向」的替代图上做 BFS。

    判定一个改动是否会影响某已发布版本时取闭包：只要原料代码/过敏原出现在
    该版本任一同向路径可达的位置上即可，反向遍历覆盖所有可能的同向可达点
    （无向可达是同向可达的超集，宁可多拒绝并给出提示，也不漏掉真实影响）。
    """
    undirected: Dict[str, Set[str]] = collections.defaultdict(set)
    for source, edges in adjacency.items():
        for e in edges:
            undirected[source].add(e["target_code"])
            undirected[e["target_code"]].add(source)
    seen: Set[str] = set()
    queue = collections.deque(r for r in roots)
    while queue:
        code = queue.popleft()
        if code in seen:
            continue
        seen.add(code)
        for nxt in undirected.get(code, ()):  # 替代边可指向已缺失的节点
            if nxt not in seen:
                queue.append(nxt)
    # 配方配料本身（即使原料缺失）也属于受影响范围
    seen.update(roots)
    return seen


def _shortest_directed_path(
    roots: List[str],
    target: str,
    adjacency: Dict[str, List[Dict[str, str]]],
) -> Tuple[Optional[str], Optional[List[str]]]:
    """返回 target 进入某已发布版本的一条最短**同向**路径（含根与目标）。

    用于在拒绝证据中指出「该原料/替代边是经由哪条替代链影响锁定版本的」。
    target 为配料自身时路径长度 0。
    """
    roots_set = set(roots)
    if target in roots_set:
        first = next(r for r in roots if r == target)
        return first, [first]
    prev: Dict[str, Optional[str]] = {r: None for r in roots}
    queue = collections.deque(roots)
    while queue:
        code = queue.popleft()
        for e in adjacency.get(code, ()):
            nxt = e["target_code"]
            if nxt in prev:
                continue
            prev[nxt] = code
            if nxt == target:
                path = [nxt]
                cur: Optional[str] = code
                while cur is not None:
                    path.append(cur)
                    cur = prev[cur]
                path.reverse()
                return path[0], path
            queue.append(nxt)
    return None, None


def _published_closures(
    conn,
    nodes: Dict[str, Dict[str, Any]],
    adjacency: Dict[str, List[Dict[str, str]]],
) -> List[Dict[str, Any]]:
    result = []
    for row in published_recipes(conn):
        roots = _roots_for(conn, row["id"])
        result.append(
            {
                "recipe": row,
                "roots": roots,
                "closure": _undirected_closure(roots, nodes, adjacency),
            }
        )
    return result


def _recipe_brief(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "version": row["version"],
        "name": row["name"],
        "status": row["status"],
        "published_at": row["published_at"],
    }


def _lock_entries(
    closures: List[Dict[str, Any]],
    adjacency: Dict[str, List[Dict[str, str]]],
    hit,
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for c in closures:
        detail = hit(c)
        if detail is None:
            continue
        root, path = _shortest_directed_path(c["roots"], detail["code"], adjacency)
        entries.append(
            {
                **_recipe_brief(c["recipe"]),
                "via_code": detail["code"],
                "via_direct": detail["code"] in set(c["roots"]),
                "via_path": path,
                "via_root": root,
                "reason": detail["reason"],
            }
        )
    # 去重（同版本同代码只保留一条），按 id 稳定排序
    dedup = {f"{e['id']}|{e['via_code']}": e for e in entries}
    return [dedup[k] for k in sorted(dedup)]


def _message(action_text: str, entries: List[Dict[str, Any]]) -> str:
    locked = "、".join(f"{e['code']} v{e['version']}（id={e['id']}）" for e in entries)
    return f"{action_text}会影响已发布锁定的配方版本：{locked}。请先对相关版本调用撤回发布（POST /api/recipes/{{id}}/unpublish）后再修改。"


# ---------------- 各写接口的锁校验 ----------------

def assert_recipe_writable(conn, recipe_row) -> None:
    """PUT / DELETE 配方版本：已发布版本直接锁定。"""
    if recipe_row is not None and recipe_row["status"] == STATUS_PUBLISHED:
        entry = {**_recipe_brief(recipe_row), "via_code": None, "via_path": None,
                 "via_direct": None, "via_root": None, "reason": "recipe_locked"}
        detail = {
            "message": _message("该修改", [entry]),
            "locked_versions": [entry],
        }
        raise PublishedLockError(detail["message"], detail)


def assert_ingredient_change(
    conn,
    *,
    old_code: str,
    new_code: str,
    old_allergens: List[str],
    new_allergens: List[str],
    nodes: Optional[Dict[str, Dict[str, Any]]] = None,
    adjacency: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> None:
    """PUT 原料：仅改代码（图结构）或过敏原集合（分析结果）时受锁保护。

    改名、改备注不影响替代图分析，始终允许。
    """
    code_changed = old_code != new_code
    allergens_changed = set(old_allergens) != set(new_allergens)
    if not code_changed and not allergens_changed:
        return
    nodes, adjacency = _ensure_graph(conn, nodes, adjacency)
    closures = _published_closures(conn, nodes, adjacency)
    if not closures:
        return

    if code_changed:
        # 代码是节点身份：原代码与新代码只要进入任一已发布版本闭包即拒绝
        hit_old = lambda c: (  # noqa: E731
            {"code": old_code, "reason": "ingredient_code"}
            if old_code in c["closure"] else None
        )
        entries = _lock_entries(closures, adjacency, hit_old)
        if not entries:
            # 改名后还可能把节点并入已被版本引用的另一代码，按新代码再查一次
            hit_new = lambda c: (  # noqa: E731
                {"code": new_code, "reason": "ingredient_code"}
                if new_code in c["closure"] else None
            )
            entries = _lock_entries(closures, adjacency, hit_new)
        if entries:
            raise PublishedLockError(
                _message(f"将原料 {old_code} 的代码改为 {new_code} ", entries),
                {"message": _message("该修改", entries), "locked_versions": entries},
            )

    if allergens_changed:
        hit = lambda c: (  # noqa: E731
            {"code": old_code, "reason": "ingredient_allergens"}
            if old_code in c["closure"] else None
        )
        entries = _lock_entries(closures, adjacency, hit)
        if entries:
            raise PublishedLockError(
                _message(f"修改原料 {old_code} 的过敏原 ", entries),
                {"message": _message("该修改", entries), "locked_versions": entries},
            )


def assert_ingredient_deletable(
    conn,
    code: str,
    nodes: Optional[Dict[str, Dict[str, Any]]] = None,
    adjacency: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> None:
    """DELETE 原料：该原料进入任一已发布版本闭包即拒绝（删除同时会级联清边）。"""
    nodes, adjacency = _ensure_graph(conn, nodes, adjacency)
    closures = _published_closures(conn, nodes, adjacency)
    if not closures:
        return
    hit = lambda c: (  # noqa: E731
        {"code": code, "reason": "ingredient_delete"} if code in c["closure"] else None
    )
    entries = _lock_entries(closures, adjacency, hit)
    if entries:
        raise PublishedLockError(
            _message(f"删除原料 {code} ", entries),
            {"message": _message("该修改", entries), "locked_versions": entries},
        )


def _guard_codes(
    conn,
    action_text: str,
    codes: Dict[str, str],
    nodes: Optional[Dict[str, Dict[str, Any]]],
    adjacency: Optional[Dict[str, List[Dict[str, str]]]],
) -> None:
    """codes: 会被本次改动影响的原料代码 -> 锁定原因。任一进入已发布版本闭包即拒绝。"""
    nodes, adjacency = _ensure_graph(conn, nodes, adjacency)
    closures = _published_closures(conn, nodes, adjacency)
    if not closures:
        return

    def hit(c):
        for code, reason in codes.items():
            if code in c["closure"]:
                return {"code": code, "reason": reason}
        return None

    entries = _lock_entries(closures, adjacency, hit)
    if entries:
        raise PublishedLockError(
            _message(action_text, entries),
            {"message": _message("该修改", entries), "locked_versions": entries},
        )


def assert_substitution_add(
    conn,
    source_code: str,
    target_code: str,
    nodes: Optional[Dict[str, Dict[str, Any]]] = None,
    adjacency: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> None:
    """新增边：source 已进入某已发布版本闭包即拒绝（新边会改变其替代路径，可能成环）。"""
    _guard_codes(
        conn,
        f"新增替代边 {source_code} -> {target_code} ",
        {source_code: "substitution_add"},
        nodes,
        adjacency,
    )


def assert_substitution_delete(
    conn,
    source_code: str,
    target_code: str,
    nodes: Optional[Dict[str, Dict[str, Any]]] = None,
    adjacency: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> None:
    """删除边：任一端点进入某已发布版本闭包即拒绝。"""
    _guard_codes(
        conn,
        f"删除替代边 {source_code} -> {target_code} ",
        {source_code: "substitution_delete", target_code: "substitution_delete"},
        nodes,
        adjacency,
    )


def assert_substitution_update(
    conn,
    *,
    old_source_code: str,
    old_target_code: str,
    new_source_code: str,
    new_target_code: str,
    nodes: Optional[Dict[str, Dict[str, Any]]] = None,
    adjacency: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> None:
    """修改边：旧边任一端点或新边 source 进入闭包即拒绝。"""
    _guard_codes(
        conn,
        f"修改替代边 {old_source_code} -> {old_target_code} 为 {new_source_code} -> {new_target_code} ",
        {
            old_source_code: "substitution_update",
            old_target_code: "substitution_update",
            new_source_code: "substitution_update",
        },
        nodes,
        adjacency,
    )


def _ensure_graph(conn, nodes, adjacency):
    if nodes is None or adjacency is None:
        nodes, adjacency = graph._build_graph(conn)
    return nodes, adjacency


# ---------------- 发布校验 ----------------

def build_publish_evidence(analysis: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从一次替代图分析结果中提取阻止发布的证据（循环 / 缺失节点）。

    返回 None 表示分析通过、可以发布；否则返回结构化证据：
    cycles  —— 每条循环的根配料、成环节点、完整节点序列与路径编号；
    missing —— 每个缺失节点及其（一条）出现路径。
    """
    cycles = [
        {
            "path_id": c["path_id"],
            "root": c["root"],
            "cycle_node": c["cycle_node"],
            "nodes": list(c["nodes"]),
        }
        for c in analysis["cycles"]
    ]
    missing_evidence: List[Dict[str, Any]] = []
    seen_missing: Set[str] = set()
    for root in analysis["roots"]:
        for p in root["paths"]:
            if not p["missing"]:
                continue
            # 取该路径上第一个缺失节点作为证据节点
            missing_node = next((n["code"] for n in p["nodes"] if not n["exists"]), None)
            if missing_node is None or missing_node in seen_missing:
                continue
            seen_missing.add(missing_node)
            missing_evidence.append(
                {
                    "code": missing_node,
                    "path_id": p["id"],
                    "root": root["ingredient_code"],
                    "nodes": [n["code"] for n in p["nodes"]],
                    "root_exists": root["exists"],
                }
            )

    if not cycles and not missing_evidence:
        return None
    problems = []
    if cycles:
        problems.append(f"{len(cycles)} 条循环替代路径")
    if missing_evidence:
        problems.append(f"{len(missing_evidence)} 个缺失原料节点")
    return {
        "message": "替代图分析未通过（" + "、".join(problems) + "），发布已拒绝；请先解除循环或补录缺失原料",
        "cycles": cycles,
        "missing": missing_evidence,
    }
