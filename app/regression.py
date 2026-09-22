"""过敏原回归矩阵：按版本号 + 创建时间稳定排序，逐版复用替代图分析。

对同一配方代码的全部版本：
- 每个版本调用 graph._analyze_recipe_graph（同一份原料/替代边图，仅构建一次）；
- 以稳定顺序相邻比较，输出版本相对上一版的新增 / 消失：
  直接过敏原、替代链过敏原（含替代链的全部过敏原）、受影响配料；
- 记录每种过敏原首次出现的版本（直接 / 替代链分开统计）；
- 每版保留循环与缺失节点标记（含相对上一版新出现 / 已解除），
  并为受影响配料保留可展开的相关来源路径（新增过敏原经由哪些替代链引入）。
"""
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from . import graph

_VERSION_PART = re.compile(r"\d+|[^\d.]+")


def version_sort_key(version: str):
    """把版本字符串拆成 (数字段, 非数字段) 序列：数字按数值比，其余按字符串比。

    如 "1.0" -> (1, 0)；"1.0.1" -> (1, 0, 1)；"2.0-beta" -> (2, 0, "-beta")。
    保证 1.0 < 1.1 < 1.10 < 2.0 < 10.0 这类直觉顺序。
    """
    parts = _VERSION_PART.findall(version.strip())
    key: List[Tuple[int, Any]] = []
    for p in parts:
        if p.isdigit():
            key.append((0, int(p)))
        else:
            key.append((1, p.lower()))
    return key


def list_recipe_versions(conn: sqlite3.Connection, code: str) -> List[sqlite3.Row]:
    """同一配方代码的全部版本：版本号 -> 创建时间 -> id 三级稳定排序。"""
    rows = conn.execute(
        "SELECT id, code, version, name, note, created_at FROM recipes WHERE code = ?",
        (code,),
    ).fetchall()
    return sorted(
        rows,
        key=lambda r: (version_sort_key(r["version"]), r["created_at"] or "", r["id"]),
    )


def _setdiff(cur: List[str], prev: List[str]) -> List[str]:
    """保持分析结果原有顺序的集合差。"""
    prev_set = set(prev)
    return [a for a in cur if a not in prev_set]


def _index_roots(analysis: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {r["ingredient_code"]: r for r in analysis["roots"]}


def _cycle_signatures(analysis: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """以 (起点, 成环节点, 节点序列) 作为一条循环的稳定指纹。"""
    sigs: Dict[str, Dict[str, Any]] = {}
    for c in analysis["cycles"]:
        sig = f"{c['root']}|{c['cycle_node']}|{'>'.join(c['nodes'])}"
        sigs[sig] = {
            "root": c["root"],
            "cycle_node": c["cycle_node"],
            "nodes": list(c["nodes"]),
            "edges": c["edges"],
            "path_id": c["path_id"],
        }
    return sigs


def _relevant_paths(root: Dict[str, Any], direct_added: List[str], chain_added: List[str]):
    """挑出需要随受影响配料保留的来源路径：

    - 根路径本身（配料直接过敏原，长度 0）；
    - 携带任一新增替代链过敏原的路径（展示该过敏原经由哪条替代链引入）；
    - 循环路径 / 缺失节点路径 / 超深截断路径（保留标记，供展开核查）。
    """
    wanted_chain = set(chain_added)
    picked: List[Dict[str, Any]] = []
    seen_ids = set()
    for p in root["paths"]:
        is_root_path = len(p["nodes"]) == 1
        carries_chain = bool(wanted_chain & set(p["allergens"]))
        flagged = p["cyclic"] or p["missing"] or p["truncated"]
        if not (is_root_path or carries_chain or flagged):
            continue
        if p["id"] in seen_ids:
            continue
        seen_ids.add(p["id"])
        introduced = sorted(a for a in p["allergens"] if a in wanted_chain)
        picked.append(
            {
                "id": p["id"],
                "nodes": p["nodes"],
                "edges": p["edges"],
                "cyclic": p["cyclic"],
                "cycle_node": p["cycle_node"],
                "missing": p["missing"],
                "truncated": p["truncated"],
                "terminal": p["terminal"],
                "allergens": p["allergens"],
                "new_allergens": p["new_allergens"],
                "regression_allergens": introduced,
            }
        )
    return picked


def _affected_ingredients(
    cur: Dict[str, Any],
    prev: Optional[Dict[str, Any]],
    item_first_seen: Dict[str, Dict[str, Any]],
    cur_ref: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """计算一版相对上一版的受影响配料：新增/消失的配料，或直接/替代链过敏原集合变化的配料。"""
    cur_roots, prev_roots = _index_roots(cur), _index_roots(prev) if prev else {}
    codes = sorted(set(cur_roots) | set(prev_roots))
    result: List[Dict[str, Any]] = []

    for code in codes:
        rcur, rprev = cur_roots.get(code), prev_roots.get(code)
        cur_direct = rcur["direct_allergens"] if rcur else []
        prev_direct = rprev["direct_allergens"] if rprev else []
        cur_chain = rcur["total_allergens"] if rcur else []
        prev_chain = rprev["total_allergens"] if rprev else []
        direct_added = _setdiff(cur_direct, prev_direct)
        direct_removed = _setdiff(prev_direct, cur_direct)
        chain_added = _setdiff(cur_chain, prev_chain)
        chain_removed = _setdiff(prev_chain, cur_chain)

        if rcur and rprev:
            if not (direct_added or direct_removed or chain_added or chain_removed):
                continue
            status = "changed"
        elif rcur:
            status = "added"
        else:
            status = "removed"

        ref_root = rcur or rprev
        first = item_first_seen.get(code, {})
        result.append(
            {
                "ingredient_code": code,
                "ingredient_name": ref_root["ingredient_name"],
                "status": status,
                "quantity": rcur["quantity"] if rcur else None,
                "unit": rcur["unit"] if rcur else None,
                "from_quantity": rprev["quantity"] if rprev else None,
                "from_unit": rprev["unit"] if rprev else None,
                "exists": bool(ref_root["exists"]),
                "direct_allergens": cur_direct,
                "prev_direct_allergens": prev_direct,
                "chain_allergens": cur_chain,
                "prev_chain_allergens": prev_chain,
                "direct_added": direct_added,
                "direct_removed": direct_removed,
                "chain_added": chain_added,
                "chain_removed": chain_removed,
                "first_seen_version": first.get("version"),
                "first_seen_index": first.get("index"),
                "first_seen_kind": first.get("kind"),
                "paths": _relevant_paths(rcur, direct_added, chain_added) if rcur else [],
            }
        )
    return result


def _version_summary(row: sqlite3.Row, analysis: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        "recipe_id": row["id"],
        "index": index,
        "code": row["code"],
        "version": row["version"],
        "name": row["name"],
        "note": row["note"],
        "created_at": row["created_at"],
        "direct_allergens": analysis["direct_allergens"],
        "chain_allergens": analysis["total_allergens"],
        "introduced_allergens": analysis["introduced_allergens"],
        "item_codes": [r["ingredient_code"] for r in analysis["roots"]],
        "cycle_count": len(analysis["cycles"]),
        "missing_nodes": analysis["missing_nodes"],
        "has_cycles": analysis["has_cycles"],
    }


def build_regression_matrix(conn: sqlite3.Connection, code: str) -> Optional[Dict[str, Any]]:
    """构建某配方代码的过敏原回归矩阵；配方代码不存在时返回 None。"""
    rows = list_recipe_versions(conn, code)
    if not rows:
        return None

    # 图（原料库 + 替代边）只构建一次，逐版复用 DFS 分析
    nodes, adjacency = graph._build_graph(conn)
    analyses: List[Dict[str, Any]] = [
        graph._analyze_recipe_graph(conn, row, nodes, adjacency) for row in rows
    ]

    # 过敏原首次出现版本（直接过敏原 / 替代链过敏原分别记录，取最早版本）
    first_direct: Dict[str, Dict[str, Any]] = {}
    first_chain: Dict[str, Dict[str, Any]] = {}
    item_first_seen: Dict[str, Dict[str, Any]] = {}
    versions: List[Dict[str, Any]] = []
    for idx, (row, an) in enumerate(zip(rows, analyses)):
        ref = {"version": row["version"], "index": idx}
        versions.append(_version_summary(row, an, idx))
        for a in an["direct_allergens"]:
            if a not in first_direct:
                first_direct[a] = ref
        for a in an["total_allergens"]:
            if a not in first_chain:
                first_chain[a] = ref
        for r in an["roots"]:
            if r["ingredient_code"] not in item_first_seen:
                item_first_seen[r["ingredient_code"]] = {
                    **ref,
                    "kind": "present",
                }

    matrix: List[Dict[str, Any]] = []
    prev_sigs: Dict[str, Dict[str, Any]] = {}
    prev_missing: List[str] = []
    for idx, (row, an) in enumerate(zip(rows, analyses)):
        prev = analyses[idx - 1] if idx > 0 else None
        is_baseline = idx == 0
        cur_ref = {"version": row["version"], "index": idx}

        direct_added = an["direct_allergens"] if is_baseline else _setdiff(an["direct_allergens"], prev["direct_allergens"])
        direct_removed = [] if is_baseline else _setdiff(prev["direct_allergens"], an["direct_allergens"])
        chain_added = an["total_allergens"] if is_baseline else _setdiff(an["total_allergens"], prev["total_allergens"])
        chain_removed = [] if is_baseline else _setdiff(prev["total_allergens"], an["total_allergens"])

        def _added_items(added: List[str], first_map: Dict[str, Dict[str, Any]], kind: str):
            return [
                {
                    "allergen": a,
                    "first_seen_version": first_map.get(a, cur_ref)["version"],
                    "first_seen_index": first_map.get(a, cur_ref)["index"],
                    "is_returning": (not is_baseline) and first_map.get(a, cur_ref)["index"] < idx,
                    "kind": kind,
                }
                for a in added
            ]

        cur_sigs = _cycle_signatures(an)
        new_cycle_sigs = sorted(set(cur_sigs) - set(prev_sigs)) if not is_baseline else sorted(cur_sigs)
        resolved_sigs = sorted(set(prev_sigs) - set(cur_sigs)) if not is_baseline else []
        new_missing = sorted(set(an["missing_nodes"]) - set(prev_missing)) if not is_baseline else list(an["missing_nodes"])
        resolved_missing = sorted(set(prev_missing) - set(an["missing_nodes"])) if not is_baseline else []

        matrix.append(
            {
                "index": idx,
                "recipe_id": row["id"],
                "version": row["version"],
                "created_at": row["created_at"],
                "name": row["name"],
                "note": row["note"],
                "is_baseline": is_baseline,
                "prev_version": None if is_baseline else rows[idx - 1]["version"],
                "prev_recipe_id": None if is_baseline else rows[idx - 1]["id"],
                "direct_added": _added_items(direct_added, first_direct, "direct"),
                "direct_removed": direct_removed,
                "chain_added": _added_items(chain_added, first_chain, "chain"),
                "chain_removed": chain_removed,
                "direct_allergens": an["direct_allergens"],
                "chain_allergens": an["total_allergens"],
                "affected_ingredients": _affected_ingredients(an, prev, item_first_seen, cur_ref),
                "cycles": list(cur_sigs.values()),
                "cycles_new": [cur_sigs[s] for s in new_cycle_sigs],
                "cycles_resolved": [prev_sigs[s] for s in resolved_sigs],
                "missing_nodes": an["missing_nodes"],
                "missing_new": new_missing,
                "missing_resolved": resolved_missing,
            }
        )
        prev_sigs, prev_missing = cur_sigs, an["missing_nodes"]

    return {
        "code": code,
        "version_count": len(rows),
        "sufficient_versions": len(rows) >= 2,
        "message": (
            None
            if len(rows) >= 2
            else f"配方 {code} 目前仅有 {len(rows)} 个版本，回归矩阵至少需要 2 个版本；已展示该版本的基线情况，新增更多版本后即可逐版对比。"
        ),
        "sort_rule": "按版本号（数字段按数值、其余按字符）升序，版本号相同再按创建时间、id 排序",
        "first_seen": {
            "direct": [
                {"allergen": a, "version": ref["version"], "index": ref["index"]}
                for a, ref in sorted(first_direct.items(), key=lambda kv: (kv[1]["index"], kv[0]))
            ],
            "chain": [
                {"allergen": a, "version": ref["version"], "index": ref["index"]}
                for a, ref in sorted(first_chain.items(), key=lambda kv: (kv[1]["index"], kv[0]))
            ],
        },
        "versions": versions,
        "matrix": matrix,
    }
