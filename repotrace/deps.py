"""Language-agnostic import graph, cycle detection, and architectural checks."""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath, Path
from typing import Iterable

from . import db

CODE_LANGUAGES = {
    "python",
    "javascript",
    "typescript",
    "go",
    "rust",
    "java",
    "kotlin",
    "swift",
    "c",
    "cpp",
    "csharp",
    "ruby",
    "php",
}

JS_TS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
RUST_EXTS = (".rs",)
RUBY_EXTS = (".rb",)
PHP_EXTS = (".php",)
C_EXTS = (".h", ".hpp", ".hh", ".hxx", ".c", ".cc", ".cpp", ".cxx")


def _is_python(path: str) -> bool:
    return path.endswith(".py") or path.endswith(".pyi")


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith(".test.js")
        or name.endswith(".test.jsx")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.js")
        or name.endswith(".spec.jsx")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.tsx")
        or "/src/test/" in path
        or "/__tests__/" in path
    )


def _strip_suffix(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in Path(path).name else path


def _without_common_source_root(stem: str) -> str:
    prefixes = (
        "src/main/java/",
        "src/test/java/",
        "src/main/kotlin/",
        "src/test/kotlin/",
        "app/src/main/java/",
        "app/src/test/java/",
        "app/src/main/kotlin/",
        "app/src/test/kotlin/",
        "Sources/",
        "Tests/",
        "src/",
    )
    for prefix in prefixes:
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    return stem


def _module_for_path(path: str, language: str | None = None) -> str | None:
    if language == "python" or (language is None and _is_python(path)):
        stem = path.rsplit(".", 1)[0].replace("/", ".")
        if stem.endswith(".__init__"):
            return stem[: -len(".__init__")]
        return stem

    stem = _strip_suffix(path)
    if language in {"javascript", "typescript"}:
        if stem.endswith("/index"):
            stem = stem[: -len("/index")]
        return stem.replace("/", ".")
    if language == "go":
        parent = str(PurePosixPath(path).parent)
        return "" if parent == "." else parent
    if language == "rust":
        if path in {"src/lib.rs", "src/main.rs"}:
            return "crate"
        if stem.endswith("/mod"):
            stem = stem[: -len("/mod")]
        if stem.startswith("src/"):
            stem = stem[len("src/") :]
        return stem.replace("/", "::")
    if language in {"java", "kotlin", "swift", "csharp", "php"}:
        return _without_common_source_root(stem).replace("/", ".").replace("\\", ".")
    if language in {"c", "cpp", "ruby"}:
        return stem.replace("/", ".")
    return None


def _package_for_path(path: str) -> str:
    module = _module_for_path(path, "python") or ""
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


def _best_module_match(candidates: Iterable[str], module_to_paths: dict[str, list[str]]) -> str | None:
    matches: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip().strip(";").replace("::", ".").replace("/", ".")
        if cleaned.endswith(".*"):
            cleaned = cleaned[: -len(".*")]
        parts = cleaned.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in module_to_paths:
                matches.append(prefix)
    if not matches:
        return None
    return max(matches, key=lambda m: (m.count("."), len(m)))


def _normalize_posix(path: PurePosixPath | str) -> str:
    return str(PurePosixPath(str(path)))


def _first_existing(candidates: Iterable[str], files_by_path: dict[str, dict]) -> str | None:
    for candidate in candidates:
        normalized = _normalize_posix(candidate)
        if normalized in files_by_path:
            return normalized
    return None


def _resolve_path_like(
    source_path: str,
    module: str,
    files_by_path: dict[str, dict],
    extensions: Iterable[str],
    *,
    relative_to_source: bool = True,
) -> str | None:
    spec = module.strip().strip("'").strip('"')
    if not spec:
        return None
    bases: list[PurePosixPath] = []
    spec_path = PurePosixPath(spec)
    if relative_to_source and spec.startswith("."):
        bases.append(PurePosixPath(source_path).parent / spec_path)
    bases.append(spec_path)

    candidates: list[str] = []
    for base in bases:
        candidates.append(str(base))
        for ext in extensions:
            candidates.append(str(base) + ext)
        for ext in extensions:
            candidates.append(str(base / ("index" + ext)))
            candidates.append(str(base / ("mod" + ext)))
    return _first_existing(candidates, files_by_path)


def _representative_file_in_dir(
    directory: str,
    files_by_path: dict[str, dict],
    language: str,
) -> str | None:
    prefix = "" if directory in {"", "."} else directory.rstrip("/") + "/"
    matches = [
        path
        for path, row in files_by_path.items()
        if row["language"] == language
        and str(PurePosixPath(path).parent) == (directory or ".")
    ]
    if not matches and prefix:
        matches = [
            path
            for path, row in files_by_path.items()
            if row["language"] == language and path.startswith(prefix)
        ]
    if not matches:
        return None
    return sorted(matches, key=lambda p: (_is_test_file(p), len(p), p))[0]


def _resolve_go_import(module: str, files_by_path: dict[str, dict]) -> str | None:
    spec = module.strip().strip('"')
    if not spec:
        return None
    dirs = sorted(
        {
            str(PurePosixPath(path).parent)
            for path, row in files_by_path.items()
            if row["language"] == "go"
        },
        key=lambda d: (-len(d), d),
    )
    for directory in dirs:
        if directory == ".":
            continue
        if spec == directory or spec.endswith("/" + directory) or directory.endswith("/" + spec):
            target = _representative_file_in_dir(directory, files_by_path, "go")
            if target:
                return target
    return None


def _resolve_rust_import(source_path: str, module: str, files_by_path: dict[str, dict]) -> str | None:
    cleaned = module.strip().strip(";")
    cleaned = cleaned.split(" as ", 1)[0].strip()
    cleaned = cleaned.replace("{", "::").replace("}", "")
    cleaned = cleaned.replace("self::", "")
    parts = [p for p in cleaned.split("::") if p and p != "*"]
    if not parts:
        return None

    source = PurePosixPath(source_path)
    source_dir = source.parent
    if source.name in {"lib.rs", "main.rs", "mod.rs"}:
        module_dir = source_dir
    else:
        module_dir = source_dir

    if parts[0] == "crate":
        base = PurePosixPath("src").joinpath(*parts[1:])
    elif parts[0] == "super":
        rest = parts[1:]
        base = module_dir.parent.joinpath(*rest)
    else:
        base = module_dir.joinpath(*parts)

    candidates: list[str] = []
    # use paths often include an item inside the module; try longest module prefix first.
    base_parts = list(base.parts)
    for i in range(len(base_parts), 0, -1):
        prefix = PurePosixPath(*base_parts[:i])
        candidates.append(str(prefix) + ".rs")
        candidates.append(str(prefix / "mod.rs"))
    return _first_existing(candidates, files_by_path)


def _resolve_c_include(source_path: str, module: str, files_by_path: dict[str, dict]) -> str | None:
    target = _resolve_path_like(source_path, module, files_by_path, C_EXTS, relative_to_source=True)
    if target:
        return target
    basename = PurePosixPath(module).name
    matches = [
        path
        for path in files_by_path
        if PurePosixPath(path).name == basename
    ]
    return sorted(matches, key=lambda p: (len(p), p))[0] if matches else None


def _resolve_import(imp: dict, files_by_path: dict[str, dict], module_to_paths: dict[str, list[str]]) -> str | None:
    source = imp["source"]
    language = imp["language"]
    module = imp["module"] or ""
    imported_name = imp.get("imported_name")
    level = imp.get("level") or 0

    if language == "python":
        candidates = (
            _resolve_relative(source, module, imported_name, level)
            if level
            else _resolve_absolute(module, imported_name)
        )
        target_module = _best_module_match(candidates, module_to_paths)
        return module_to_paths[target_module][0] if target_module else None

    if language in {"javascript", "typescript"}:
        if module.startswith(".") or module.startswith("/"):
            return _resolve_path_like(source, module, files_by_path, JS_TS_EXTS, relative_to_source=True)
        # Simple support for path aliases/monorepo imports that mirror repo paths.
        return _resolve_path_like(source, module, files_by_path, JS_TS_EXTS, relative_to_source=False)

    if language == "go":
        return _resolve_go_import(module, files_by_path)

    if language == "rust":
        return _resolve_rust_import(source, module, files_by_path)

    if language in {"java", "kotlin", "csharp", "php"}:
        candidates = [module]
        if imported_name:
            candidates.append(f"{module}.{imported_name}")
        target_module = _best_module_match(candidates, module_to_paths)
        return module_to_paths[target_module][0] if target_module else None

    if language == "swift":
        target_module = _best_module_match([module], module_to_paths)
        return module_to_paths[target_module][0] if target_module else None

    if language in {"c", "cpp"}:
        return _resolve_c_include(source, module, files_by_path)

    if language == "ruby":
        return _resolve_path_like(source, module, files_by_path, RUBY_EXTS, relative_to_source=True)

    return None


def import_graph(repo_root: Path) -> dict:
    """Build a language-agnostic import/dependency graph from indexed files/imports."""
    conn = db.connect(repo_root)
    file_rows = conn.execute(
        "SELECT id, path, language, line_count FROM files "
        "WHERE language IN ({}) ORDER BY path".format(
            ",".join("?" for _ in CODE_LANGUAGES)
        ),
        tuple(sorted(CODE_LANGUAGES)),
    ).fetchall()
    files_by_id = {r["id"]: dict(r) for r in file_rows}
    files_by_path = {r["path"]: dict(r) for r in file_rows}

    module_to_paths: dict[str, list[str]] = defaultdict(list)
    path_to_module: dict[str, str] = {}
    for row in file_rows:
        module = _module_for_path(row["path"], row["language"])
        if module is not None:
            module_to_paths[module].append(row["path"])
            path_to_module[row["path"]] = module

    import_rows = conn.execute(
        "SELECT i.file_id, i.module, i.imported_name, i.alias, i.level, i.line, "
        "       f.path AS source, f.language AS language "
        "FROM imports i JOIN files f ON i.file_id = f.id "
        "ORDER BY f.path, i.line"
    ).fetchall()
    conn.close()

    edges: list[dict] = []
    unresolved: list[dict] = []
    adjacency: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)

    for row in import_rows:
        imp = dict(row)
        source = imp["source"]
        if source not in files_by_path:
            continue
        target = _resolve_import(imp, files_by_path, module_to_paths)
        if not target:
            unresolved.append(
                {
                    "source": source,
                    "language": imp["language"],
                    "line": imp["line"],
                    "module": imp["module"],
                    "imported_name": imp["imported_name"],
                    "level": imp["level"] or 0,
                }
            )
            continue
        if target == source:
            continue
        target_row = files_by_path[target]
        edge = {
            "source": source,
            "source_module": path_to_module.get(source),
            "source_language": imp["language"],
            "target": target,
            "target_module": path_to_module.get(target),
            "target_language": target_row["language"],
            "line": imp["line"],
            "module": imp["module"],
            "imported_name": imp["imported_name"],
            "level": imp["level"] or 0,
            "source_is_test": _is_test_file(source),
            "target_is_test": _is_test_file(target),
        }
        edges.append(edge)
        adjacency[source].add(target)
        reverse[target].add(source)

    nodes = [
        {
            "path": r["path"],
            "language": r["language"],
            "module": path_to_module.get(r["path"]),
            "line_count": r["line_count"],
            "is_test": _is_test_file(r["path"]),
            "imports": len(adjacency.get(r["path"], set())),
            "imported_by": len(reverse.get(r["path"], set())),
        }
        for r in file_rows
    ]
    languages: dict[str, int] = defaultdict(int)
    for node in nodes:
        languages[node["language"] or "unknown"] += 1

    return {
        "repo_root": str(repo_root),
        "nodes": nodes,
        "edges": edges,
        "unresolved": unresolved,
        "summary": {
            "files": len(nodes),
            "languages": dict(sorted(languages.items())),
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
                "message": "Import cycle detected",
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
