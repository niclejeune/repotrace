"""Import graph, cycle detection, and architectural checks."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable

from . import db


def _is_python(path: str) -> bool:
    return path.endswith(".py") or path.endswith(".pyi")


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    return path.startswith("tests/") or "/tests/" in path or name.startswith("test_")


def _module_for_path(path: str) -> str | None:
    if not _is_python(path):
        return None
    stem = path.rsplit(".", 1)[0].replace("/", ".")
    if stem.endswith(".__init__"):
        return stem[: -len(".__init__")]
    return stem


def _package_for_path(path: str) -> str:
    module = _module_for_path(path) or ""
    if path.endswith("/__init__.py"):
        return module
    return module.rsplit(".", 1)[0] if "." in module else ""


def _resolve_relative(source_path: str, module: str, imported_name: str | None, level: int) -> list[str]:
    package = _package_for_path(source_path)
    parts = package.split(".") if package else []
    # Python level=1 means current package, level=2 parent, etc.
    keep = max(0, len(parts) - max(0, level - 1))
    prefix = ".".join(parts[:keep])
    base = ".".join(p for p in [prefix, module] if p)
    candidates = [base] if base else []
    if imported_name:
        candidates.append(".".join(p for p in [base, imported_name] if p))
    return [c for c in candidates if c]


def _resolve_absolute(module: str, imported_name: str | None) -> list[str]:
    candidates = [module] if module else []
    if module and imported_name:
        candidates.append(f"{module}.{imported_name}")
    elif imported_name:
        candidates.append(imported_name)
    return candidates


def _best_module_match(candidates: Iterable[str], module_to_path: dict[str, str]) -> str | None:
    matches: list[str] = []
    for candidate in candidates:
        parts = candidate.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in module_to_path:
                matches.append(prefix)
    if not matches:
        return None
    return max(matches, key=lambda m: (m.count("."), len(m)))


def import_graph(repo_root: Path) -> dict:
    """Build a Python import graph from indexed files/imports."""
    conn = db.connect(repo_root)
    file_rows = conn.execute(
        "SELECT id, path, line_count FROM files WHERE language = 'python' ORDER BY path"
    ).fetchall()
    files_by_id = {r["id"]: dict(r) for r in file_rows}
    module_to_path: dict[str, str] = {}
    path_to_module: dict[str, str] = {}
    for row in file_rows:
        module = _module_for_path(row["path"])
        if module:
            module_to_path[module] = row["path"]
            path_to_module[row["path"]] = module

    import_rows = conn.execute(
        "SELECT i.file_id, i.module, i.imported_name, i.alias, i.level, i.line, f.path AS source "
        "FROM imports i JOIN files f ON i.file_id = f.id "
        "WHERE f.language = 'python' ORDER BY f.path, i.line"
    ).fetchall()
    conn.close()

    edges: list[dict] = []
    unresolved: list[dict] = []
    adjacency: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)

    for imp in import_rows:
        source = imp["source"]
        source_module = path_to_module.get(source)
        if not source_module:
            continue
        level = imp["level"] or 0
        candidates = (
            _resolve_relative(source, imp["module"] or "", imp["imported_name"], level)
            if level
            else _resolve_absolute(imp["module"] or "", imp["imported_name"])
        )
        target_module = _best_module_match(candidates, module_to_path)
        if not target_module:
            unresolved.append(
                {
                    "source": source,
                    "line": imp["line"],
                    "module": imp["module"],
                    "imported_name": imp["imported_name"],
                    "level": level,
                }
            )
            continue
        target = module_to_path[target_module]
        if target == source:
            continue
        edge = {
            "source": source,
            "source_module": source_module,
            "target": target,
            "target_module": target_module,
            "line": imp["line"],
            "module": imp["module"],
            "imported_name": imp["imported_name"],
            "level": level,
            "source_is_test": _is_test_file(source),
            "target_is_test": _is_test_file(target),
        }
        edges.append(edge)
        adjacency[source].add(target)
        reverse[target].add(source)

    nodes = [
        {
            "path": r["path"],
            "module": path_to_module.get(r["path"]),
            "line_count": r["line_count"],
            "is_test": _is_test_file(r["path"]),
            "imports": len(adjacency.get(r["path"], set())),
            "imported_by": len(reverse.get(r["path"], set())),
        }
        for r in file_rows
    ]

    return {
        "repo_root": str(repo_root),
        "nodes": nodes,
        "edges": edges,
        "unresolved": unresolved,
        "summary": {
            "python_files": len(nodes),
            "resolved_edges": len(edges),
            "unresolved_imports": len(unresolved),
        },
    }


def cycles(repo_root: Path, limit: int = 50) -> dict:
    graph = import_graph(repo_root)
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph["edges"]:
        adjacency[edge["source"]].append(edge["target"])

    found: list[list[str]] = []
    seen_keys: set[tuple[str, ...]] = set()
    visiting: set[str] = set()
    stack: list[str] = []

    def canonical(cycle: list[str]) -> tuple[str, ...]:
        body = cycle[:-1]
        rotations = [tuple(body[i:] + body[:i]) for i in range(len(body))]
        rev = list(reversed(body))
        rotations.extend(tuple(rev[i:] + rev[:i]) for i in range(len(rev)))
        return min(rotations)

    def dfs(node: str) -> None:
        if len(found) >= limit:
            return
        visiting.add(node)
        stack.append(node)
        for nxt in adjacency.get(node, []):
            if nxt in visiting:
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                key = canonical(cycle)
                if key not in seen_keys:
                    seen_keys.add(key)
                    found.append(cycle)
            elif nxt not in stack:
                dfs(nxt)
        stack.pop()
        visiting.remove(node)

    for node in sorted(adjacency):
        if len(found) >= limit:
            break
        dfs(node)

    return {
        "repo_root": str(repo_root),
        "cycle_count": len(found),
        "cycles": found,
        "truncated": len(found) >= limit,
    }


def check(repo_root: Path, *, max_lines: int = 1000, cycle_limit: int = 50) -> dict:
    graph = import_graph(repo_root)
    cyc = cycles(repo_root, limit=cycle_limit)
    issues: list[dict] = []

    for cycle in cyc["cycles"]:
        issues.append(
            {
                "severity": "error",
                "code": "import-cycle",
                "message": "Python import cycle detected",
                "files": cycle,
            }
        )

    for edge in graph["edges"]:
        if not edge["source_is_test"] and edge["target_is_test"]:
            issues.append(
                {
                    "severity": "error",
                    "code": "production-imports-test",
                    "message": "Production code imports test code",
                    "source": edge["source"],
                    "target": edge["target"],
                    "line": edge["line"],
                }
            )

    for node in graph["nodes"]:
        if not node["is_test"] and (node["line_count"] or 0) > max_lines:
            issues.append(
                {
                    "severity": "warning",
                    "code": "large-file",
                    "message": f"Production file exceeds {max_lines} lines",
                    "file": node["path"],
                    "line_count": node["line_count"],
                }
            )

    for node in graph["nodes"]:
        if not node["is_test"] and node["imported_by"] >= 20:
            issues.append(
                {
                    "severity": "warning",
                    "code": "high-fan-in",
                    "message": "High fan-in module; edits need extra care",
                    "file": node["path"],
                    "imported_by": node["imported_by"],
                }
            )

    error_count = sum(1 for i in issues if i["severity"] == "error")
    warning_count = sum(1 for i in issues if i["severity"] == "warning")
    return {
        "repo_root": str(repo_root),
        "status": "fail" if error_count else "pass",
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
        "summary": graph["summary"],
    }
