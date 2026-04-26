"""File walking, Python AST parsing, route detection, and SQLite writes."""

from __future__ import annotations

import ast
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import db, git_utils

# Default excludes — repo conventions + heavy dirs
EXCLUDE_DIRS = {
    ".git",
    ".repotrace",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    "dist",
    "build",
    ".next",
    ".turbo",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "coverage",
    "site-packages",
    ".tox",
}

LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".sql": "sql",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
}

# Route patterns for FastAPI / Flask / aiohttp / Sanic via regex on decorators.
# This is a heuristic; the AST decorator pass below is the authoritative one for Python.
ROUTE_DECORATOR_RE = re.compile(
    r"@(?P<obj>[\w\.]+)\.(?P<method>get|post|put|delete|patch|options|head|route)\s*\(",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CallName:
    """Normalized call target.

    `callee_name` is intentionally readable and argument-free. For example:
    `conn.execute("...").fetchone()` becomes `conn.execute().fetchone`.
    `callee_base` is the terminal name used for symbol matching.
    """

    callee_name: str
    callee_base: str
    callee_qualifier: Optional[str]


def iter_files(root: Path, use_git: bool) -> Iterable[Path]:
    """Yield candidate files under root, honoring git ls-files when available."""
    if use_git and git_utils.is_git_repo(root):
        for rel in git_utils.list_tracked_files(root):
            p = root / rel
            if p.is_file() and p.suffix in LANG_BY_EXT:
                yield p
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # prune excluded dirs in-place
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix in LANG_BY_EXT:
                yield p


def _line_count(path: Path) -> int:
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _decorators_to_strs(decorators: list[ast.expr]) -> list[str]:
    out: list[str] = []
    for d in decorators:
        try:
            out.append(ast.unparse(d))
        except Exception:
            out.append("<decorator>")
    return out


def _route_from_decorator(dec: ast.expr) -> Optional[tuple[str, str, str]]:
    """If decorator looks like a route, return (framework, method, path)."""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    method: Optional[str] = None
    obj: Optional[str] = None
    if isinstance(func, ast.Attribute):
        method = func.attr.lower()
        try:
            obj = ast.unparse(func.value)
        except Exception:
            obj = None
    if method not in {"get", "post", "put", "delete", "patch", "options", "head", "route"}:
        return None
    # path arg: first positional or kwarg `path`/`rule`
    path_val: Optional[str] = None
    if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
        path_val = dec.args[0].value
    else:
        for kw in dec.keywords:
            if kw.arg in {"path", "rule"} and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    path_val = kw.value.value
                    break
    if not path_val:
        return None
    framework = "flask" if method == "route" else "fastapi-or-flask"
    if obj and ("router" in obj or "fastapi" in obj.lower() or "app" in obj.lower()):
        framework = "fastapi-or-flask"
    return (framework, method.upper(), path_val)


def _index_python(
    conn,
    file_id: int,
    path: Path,
) -> None:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return

    # Imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                conn.execute(
                    "INSERT INTO imports(file_id, module, imported_name, alias, level, line) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (file_id, alias.name, None, alias.asname, 0, node.lineno),
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                conn.execute(
                    "INSERT INTO imports(file_id, module, imported_name, alias, level, line) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (file_id, module, alias.name, alias.asname, node.level, node.lineno),
                )

    # Symbols + nested + calls
    def walk_body(
        body: list[ast.stmt],
        parent_id: Optional[int],
        parent_kind: Optional[str],
        qual_prefix: str,
    ) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = (
                    "async_function"
                    if isinstance(node, ast.AsyncFunctionDef)
                    else "function"
                    if isinstance(node, ast.FunctionDef)
                    else "class"
                )
                if kind in {"function", "async_function"} and parent_kind == "class":
                    kind = "method" if kind == "function" else "async_method"
                elif kind in {"function", "async_function"} and parent_kind is not None:
                    kind = "nested_function" if kind == "function" else "nested_async_function"
                qname = f"{qual_prefix}.{node.name}" if qual_prefix else node.name
                docstring = ast.get_docstring(node) or ""
                decorators = _decorators_to_strs(getattr(node, "decorator_list", []))
                end_lineno = getattr(node, "end_lineno", None) or node.lineno
                cur = conn.execute(
                    "INSERT INTO symbols(name, qualified_name, kind, file_id, parent_id, "
                    "start_line, end_line, docstring, decorators) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        node.name,
                        qname,
                        kind,
                        file_id,
                        parent_id,
                        node.lineno,
                        end_lineno,
                        docstring[:2000],
                        json.dumps(decorators),
                    ),
                )
                sym_id = cur.lastrowid

                # Routes from decorators (Python only here)
                for dec in getattr(node, "decorator_list", []):
                    route = _route_from_decorator(dec)
                    if route:
                        framework, method, path_val = route
                        conn.execute(
                            "INSERT INTO routes(file_id, framework, method, path, "
                            "handler_symbol_id, line) VALUES (?, ?, ?, ?, ?, ?)",
                            (file_id, framework, method, path_val, sym_id, node.lineno),
                        )

                # Calls inside this symbol body, excluding nested defs/classes so
                # local helpers do not get attributed to the outer function too.
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for call_node, call_name in _calls_in_body(node.body):
                        conn.execute(
                            "INSERT INTO calls(caller_symbol_id, callee_name, callee_base, "
                            "callee_qualifier, line, file_id) VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                sym_id,
                                call_name.callee_name,
                                call_name.callee_base,
                                call_name.callee_qualifier,
                                call_node.lineno,
                                file_id,
                            ),
                        )

                # Recurse for nested defs / methods
                walk_body(node.body, sym_id, kind, qname)

    walk_body(tree.body, None, None, "")


class _CallCollector(ast.NodeVisitor):
    """Collect calls within one symbol without descending into nested symbols."""

    def __init__(self) -> None:
        self.calls: list[tuple[ast.Call, CallName]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        call_name = _call_name(node.func)
        if call_name:
            self.calls.append((node, call_name))
        self.generic_visit(node)


def _calls_in_body(body: list[ast.stmt]) -> list[tuple[ast.Call, CallName]]:
    collector = _CallCollector()
    for stmt in body:
        collector.visit(stmt)
    return collector.calls


def _call_name(func: ast.expr) -> Optional[CallName]:
    parts = _expr_name_parts(func)
    if not parts:
        return None
    callee_name = ".".join(parts)
    callee_base = parts[-1].removesuffix("()")
    qualifier = ".".join(parts[:-1]) or None
    return CallName(callee_name, callee_base, qualifier)


def _expr_name_parts(expr: ast.expr) -> Optional[list[str]]:
    """Return a readable, argument-free dotted name for a callable expression."""
    if isinstance(expr, ast.Name):
        return [expr.id]
    if isinstance(expr, ast.Attribute):
        base = _expr_name_parts(expr.value)
        if base:
            return [*base, expr.attr]
        return ["<expr>", expr.attr]
    if isinstance(expr, ast.Call):
        # Attribute access on call results: conn.execute("...").fetchone
        # should index as conn.execute().fetchone, not include SQL/string args.
        called = _expr_name_parts(expr.func)
        if called:
            return [*called[:-1], f"{called[-1]}()"]
        return None
    if isinstance(expr, ast.Subscript):
        return _expr_name_parts(expr.value)
    return None


def index_repo(repo_root: Path, *, reset: bool = False, verbose: bool = False) -> dict:
    """Index a repository. Returns a stats dict."""
    repo_root = repo_root.resolve()
    if reset:
        db.reset(repo_root)

    conn = db.connect(repo_root)
    use_git = git_utils.is_git_repo(repo_root)

    started = time.time()
    files_indexed = 0
    files_skipped = 0
    symbols_count = 0
    imports_count = 0
    calls_count = 0
    routes_count = 0

    # Wipe existing rows for a clean re-index without reset
    conn.executescript(
        "DELETE FROM calls; DELETE FROM routes; DELETE FROM imports; "
        "DELETE FROM symbols; DELETE FROM files;"
    )

    for path in iter_files(repo_root, use_git=use_git):
        rel = str(path.resolve().relative_to(repo_root))
        ext = path.suffix
        lang = LANG_BY_EXT.get(ext)
        try:
            stat = path.stat()
        except FileNotFoundError:
            files_skipped += 1
            continue

        sha, date = git_utils.last_commit_for_file(repo_root, rel) if use_git else (None, None)

        cur = conn.execute(
            "INSERT INTO files(path, language, line_count, size_bytes, mtime, "
            "git_last_commit, git_last_modified, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rel,
                lang,
                _line_count(path),
                stat.st_size,
                stat.st_mtime,
                sha,
                date,
                started,
            ),
        )
        file_id = cur.lastrowid
        files_indexed += 1

        if lang == "python":
            before_syms = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            before_imps = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
            before_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            before_routes = conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
            _index_python(conn, file_id, path)
            symbols_count += conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] - before_syms
            imports_count += conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] - before_imps
            calls_count += conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] - before_calls
            routes_count += conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0] - before_routes

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("indexed_at", str(started)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("repo_root", str(repo_root)),
    )
    conn.commit()
    conn.close()

    return {
        "repo_root": str(repo_root),
        "files_indexed": files_indexed,
        "files_skipped": files_skipped,
        "symbols": symbols_count,
        "imports": imports_count,
        "calls": calls_count,
        "routes": routes_count,
        "elapsed_seconds": round(time.time() - started, 3),
        "use_git": use_git,
    }
