"""File walking, multi-language parsing heuristics, route detection, and SQLite writes."""

from __future__ import annotations

import ast
import json
import os
import re
import time
from dataclasses import dataclass, field
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
    "target",
    ".gradle",
    ".build",
    ".swiftpm",
    "vendor",
}

LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sql": "sql",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
}

CODE_LANGUAGES = {
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

CALLABLE_KINDS = {
    "function",
    "async_function",
    "method",
    "async_method",
    "nested_function",
    "nested_async_function",
    "constructor",
}

CONTAINER_KINDS = {
    "class",
    "interface",
    "enum",
    "struct",
    "trait",
    "protocol",
    "extension",
    "impl",
    "namespace",
}

# Route patterns for FastAPI / Flask / aiohttp / Sanic via regex on decorators.
# This is a heuristic; the AST decorator pass below is the authoritative one for Python.
ROUTE_DECORATOR_RE = re.compile(
    r"@(?P<obj>[\w\.]+)\.(?P<method>get|post|put|delete|patch|options|head|route)\s*\(",
    re.IGNORECASE,
)

CALL_RE = re.compile(
    r"(?<![\w$])([A-Za-z_$][\w$]*(?:(?:\.|::|->)[A-Za-z_$][\w$]*)*)"
    r"\s*(?:<[^;\n{}()]*>)?\s*\("
)

CALL_SKIP_NAMES = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "with",
    "return",
    "throw",
    "throws",
    "sizeof",
    "typeof",
    "delete",
    "await",
    "yield",
    "new",
    "else",
    "do",
    "import",
    "require",
    "super",
    "this",
    "self",
    "Self",
    "match",
    "guard",
    "defer",
    "using",
    "namespace",
    "class",
    "struct",
    "enum",
    "interface",
    "trait",
    "impl",
    "func",
    "function",
    "fn",
}

CONTROL_START_RE = re.compile(
    r"^\s*(if|for|while|switch|catch|else|do|try|finally|return|throw|throws|guard|defer|using)\b"
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


@dataclass
class SymbolCandidate:
    name: str
    kind: str
    start_line: int
    end_line: int
    qualified_name: Optional[str] = None
    decorators: tuple[str, ...] = field(default_factory=tuple)


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


def _read_source(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _insert_import(
    conn,
    file_id: int,
    module: str,
    imported_name: str | None = None,
    alias: str | None = None,
    level: int = 0,
    line: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO imports(file_id, module, imported_name, alias, level, line) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, module, imported_name, alias, level, line),
    )


def _insert_symbol(
    conn,
    file_id: int,
    candidate: SymbolCandidate,
    parent_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO symbols(name, qualified_name, kind, file_id, parent_id, "
        "start_line, end_line, docstring, decorators) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            candidate.name,
            candidate.qualified_name or candidate.name,
            candidate.kind,
            file_id,
            parent_id,
            candidate.start_line,
            candidate.end_line,
            "",
            json.dumps(list(candidate.decorators)),
        ),
    )
    return int(cur.lastrowid)


def _insert_call(conn, file_id: int, caller_symbol_id: int | None, call_name: CallName, line: int) -> None:
    conn.execute(
        "INSERT INTO calls(caller_symbol_id, callee_name, callee_base, "
        "callee_qualifier, line, file_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            caller_symbol_id,
            call_name.callee_name,
            call_name.callee_base,
            call_name.callee_qualifier,
            line,
            file_id,
        ),
    )


def _insert_route(
    conn,
    file_id: int,
    framework: str,
    method: str,
    path: str,
    handler_symbol_id: int | None,
    line: int,
) -> None:
    conn.execute(
        "INSERT INTO routes(file_id, framework, method, path, "
        "handler_symbol_id, line) VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, framework, method.upper(), path, handler_symbol_id, line),
    )


def _decorators_to_strs(decorators: list[ast.expr]) -> list[str]:
    out: list[str] = []
    for d in decorators:
        try:
            out.append(ast.unparse(d))
        except Exception:
            out.append("<decorator>")
    return out



# Next.js App Router detection
NEXT_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
NEXT_ROUTE_FILE_RE = re.compile(r"^route\.(ts|tsx|js|jsx|mjs|cjs)$")


def _next_app_route_path(path: Path) -> Optional[str]:
    """Return derived URL path for a Next.js App Router handler, or None.

    Triggers when the file is named ``route.{ts,tsx,js,jsx,mjs,cjs}`` and lives
    under an ``app/`` directory. The URL path is the segment list between
    ``app/`` and the file's parent, with Next.js conventions applied:
    ``[id]`` -> ``:id``, ``[...slug]`` and ``[[...slug]]`` -> ``*slug``,
    route groups ``(name)`` and parallel routes ``@name`` are stripped.
    """
    if not NEXT_ROUTE_FILE_RE.match(path.name):
        return None
    parts = path.parts
    app_idx = None
    for i, p_ in enumerate(parts):
        if p_ == "app":
            app_idx = i
    if app_idx is None:
        return None
    segments = parts[app_idx + 1 : -1]
    out: list[str] = []
    for seg in segments:
        if seg.startswith("(") and seg.endswith(")"):
            continue
        if seg.startswith("@"):
            continue
        if seg.startswith("[[...") and seg.endswith("]]"):
            out.append("*" + seg[5:-2])
        elif seg.startswith("[...") and seg.endswith("]"):
            out.append("*" + seg[4:-1])
        elif seg.startswith("[") and seg.endswith("]"):
            out.append(":" + seg[1:-1])
        else:
            out.append(seg)
    return "/" + "/".join(out) if out else "/"


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


def _index_python(conn, file_id: int, path: Path) -> None:
    source = _read_source(path)
    if source is None:
        return
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return

    # Imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _insert_import(conn, file_id, alias.name, None, alias.asname, 0, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                _insert_import(conn, file_id, module, alias.name, alias.asname, node.level, node.lineno)

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
                        _insert_route(conn, file_id, framework, method, path_val, sym_id, node.lineno)

                # Calls inside this symbol body, excluding nested defs/classes so
                # local helpers do not get attributed to the outer function too.
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for call_node, call_name in _calls_in_body(node.body):
                        _insert_call(conn, file_id, sym_id, call_name, call_node.lineno)

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
    return _call_name_from_parts(parts)


def _call_name_from_parts(parts: list[str]) -> Optional[CallName]:
    if not parts:
        return None
    callee_name = ".".join(parts)
    callee_base = parts[-1].removesuffix("()")
    if not callee_base or callee_base in CALL_SKIP_NAMES:
        return None
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


def _strip_line_comment(line: str, language: str) -> str:
    stripped = line.lstrip()
    if language in {"python", "ruby"}:
        return line.split("#", 1)[0]
    if language == "sql":
        return line.split("--", 1)[0]
    if language == "php" and stripped.startswith("#"):
        return ""
    return line.split("//", 1)[0]


def _find_block_end(lines: list[str], start_line: int, language: str) -> int:
    """Best-effort brace block end for C-like languages."""
    depth = 0
    saw_open = False
    for idx in range(start_line - 1, len(lines)):
        text = _strip_line_comment(lines[idx], language)
        for ch in text:
            if ch == "{":
                depth += 1
                saw_open = True
            elif ch == "}":
                depth -= 1
                if saw_open and depth <= 0:
                    return idx + 1
        if saw_open and depth <= 0:
            return idx + 1
        if not saw_open and idx + 1 > start_line + 3:
            break
    return start_line


def _decorators_before(lines: list[str], line_no: int) -> tuple[str, ...]:
    decorators: list[str] = []
    idx = line_no - 2
    while idx >= 0:
        stripped = lines[idx].strip()
        if not stripped:
            break
        if stripped.startswith("@"):  # Java/Kotlin/Spring, C# attributes are handled separately.
            decorators.append(stripped)
            idx -= 1
            continue
        break
    decorators.reverse()
    return tuple(decorators)


def _route_from_text(text: str, language: str) -> tuple[str, str, str] | None:
    stripped = text.strip()

    # Express/Fastify/Koa-like APIs: app.get('/path', handler)
    m = re.search(
        r"\b(?:app|router|server|route)\.(get|post|put|delete|patch|options|head|all|use)"
        r"\s*\(\s*['\"]([^'\"]+)",
        stripped,
        re.IGNORECASE,
    )
    if m:
        method = m.group(1).upper()
        if method in {"ALL", "USE"}:
            method = "*"
        return ("javascript-router", method, m.group(2))

    # Go stdlib/net-http and common routers.
    m = re.search(r"\bHandleFunc\s*\(\s*['\"]([^'\"]+)", stripped)
    if m:
        return ("go-net-http", "*", m.group(1))

    # Java/Kotlin/Spring annotations.
    annotation_methods = {
        "GetMapping": "GET",
        "PostMapping": "POST",
        "PutMapping": "PUT",
        "DeleteMapping": "DELETE",
        "PatchMapping": "PATCH",
    }
    m = re.search(r"@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)\s*\((.*)\)", stripped)
    if m:
        path = _first_string_literal(m.group(2))
        if path:
            return ("spring", annotation_methods[m.group(1)], path)
    m = re.search(r"@RequestMapping\s*\((.*)\)", stripped)
    if m:
        args = m.group(1)
        path = _first_string_literal(args)
        method = "*"
        method_match = re.search(r"RequestMethod\.([A-Z]+)", args)
        if method_match:
            method = method_match.group(1)
        if path:
            return ("spring", method, path)

    # C# ASP.NET attributes.
    m = re.search(r"\[(HttpGet|HttpPost|HttpPut|HttpDelete|HttpPatch)(?:\((.*)\))?\]", stripped)
    if m:
        method = m.group(1).removeprefix("Http").upper()
        path = _first_string_literal(m.group(2) or "") or ""
        return ("aspnet", method, path)

    return None


def _first_string_literal(text: str) -> str | None:
    m = re.search(r"['\"]([^'\"]+)['\"]", text)
    return m.group(1) if m else None


def _generic_imports(language: str, lines: list[str]) -> list[tuple[str, str | None, str | None, int, int]]:
    imports: list[tuple[str, str | None, str | None, int, int]] = []
    in_go_block = False

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue

        if language in {"javascript", "typescript"}:
            m = re.match(r"import\s+(?:type\s+)?(?:.+?\s+from\s+)?['\"]([^'\"]+)['\"]", stripped)
            if not m:
                m = re.match(r"export\s+.+?\s+from\s+['\"]([^'\"]+)['\"]", stripped)
            if m:
                module = m.group(1)
                named = re.search(r"\{([^}]+)\}", stripped)
                if named:
                    for part in named.group(1).split(","):
                        name = part.strip().split(" as ", 1)[0].strip()
                        if name:
                            imports.append((module, name, None, 0, line_no))
                else:
                    imports.append((module, None, None, 0, line_no))
            for req in re.finditer(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", stripped):
                imports.append((req.group(1), None, None, 0, line_no))

        elif language == "go":
            if in_go_block:
                if stripped.startswith(")"):
                    in_go_block = False
                    continue
                m = re.search(r"(?:[\w\.]+\s+)?['\"]([^'\"]+)['\"]", stripped)
                if m:
                    imports.append((m.group(1), None, None, 0, line_no))
                continue
            if re.match(r"import\s*\(", stripped):
                in_go_block = True
                continue
            m = re.match(r"import\s+(?:[\w\.]+\s+)?['\"]([^'\"]+)['\"]", stripped)
            if m:
                imports.append((m.group(1), None, None, 0, line_no))

        elif language == "rust":
            m = re.match(r"(?:pub\s+)?use\s+(.+?);", stripped)
            if m:
                imports.append((m.group(1).strip(), None, None, 0, line_no))
            m = re.match(r"mod\s+([A-Za-z_][\w]*);", stripped)
            if m:
                imports.append((m.group(1), None, None, 0, line_no))

        elif language in {"java", "kotlin"}:
            m = re.match(r"import\s+(?:static\s+)?([\w.*]+);?", stripped)
            if m:
                imports.append((m.group(1), None, None, 0, line_no))

        elif language == "swift":
            m = re.match(r"import\s+([A-Za-z_][\w.]*)", stripped)
            if m:
                imports.append((m.group(1), None, None, 0, line_no))

        elif language in {"c", "cpp", "csharp"}:
            m = re.match(r"#include\s+[<\"]([^>\"]+)[>\"]", stripped)
            if m:
                imports.append((m.group(1), None, None, 0, line_no))
            m = re.match(r"using\s+([\w.]+);", stripped)
            if m:
                imports.append((m.group(1), None, None, 0, line_no))

        elif language == "ruby":
            m = re.match(r"require(?:_relative)?\s+['\"]([^'\"]+)['\"]", stripped)
            if m:
                imports.append((m.group(1), None, None, 0, line_no))

        elif language == "php":
            m = re.match(r"use\s+([^;]+);", stripped)
            if m:
                imports.append((m.group(1).replace("\\", "."), None, None, 0, line_no))
            m = re.match(r"(?:require|include)(?:_once)?\s*[\( ]\s*['\"]([^'\"]+)['\"]", stripped)
            if m:
                imports.append((m.group(1), None, None, 0, line_no))

    return imports


def _generic_symbols(language: str, lines: list[str]) -> list[SymbolCandidate]:
    symbols: list[SymbolCandidate] = []

    for line_no, line in enumerate(lines, start=1):
        stripped = _strip_line_comment(line, language).strip()
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue

        decorators = _decorators_before(lines, line_no)

        if language in {"javascript", "typescript"}:
            _add_js_ts_symbol(symbols, lines, language, stripped, line_no, decorators)
        elif language == "go":
            _add_go_symbol(symbols, lines, language, stripped, line_no)
        elif language == "rust":
            _add_rust_symbol(symbols, lines, language, stripped, line_no)
        elif language == "java":
            _add_java_symbol(symbols, lines, language, stripped, line_no, decorators)
        elif language == "kotlin":
            _add_kotlin_symbol(symbols, lines, language, stripped, line_no, decorators)
        elif language == "swift":
            _add_swift_symbol(symbols, lines, language, stripped, line_no)
        elif language in {"c", "cpp", "csharp"}:
            _add_c_like_symbol(symbols, lines, language, stripped, line_no)
        elif language == "ruby":
            _add_ruby_symbol(symbols, lines, language, stripped, line_no)
        elif language == "php":
            _add_php_symbol(symbols, lines, language, stripped, line_no)

    return _qualify_nested_symbols(symbols)


def _add_candidate(
    symbols: list[SymbolCandidate],
    lines: list[str],
    language: str,
    name: str,
    kind: str,
    line_no: int,
    decorators: tuple[str, ...] = (),
    qualified_name: str | None = None,
) -> None:
    symbols.append(
        SymbolCandidate(
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            start_line=line_no,
            end_line=_find_block_end(lines, line_no, language),
            decorators=decorators,
        )
    )


def _add_js_ts_symbol(
    symbols: list[SymbolCandidate],
    lines: list[str],
    language: str,
    stripped: str,
    line_no: int,
    decorators: tuple[str, ...],
) -> None:
    m = re.match(r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1), "class", line_no, decorators)
        return
    m = re.match(r"(?:export\s+)?(?:interface|type)\s+([A-Za-z_$][\w$]*)", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1), "interface" if "interface" in stripped else "type", line_no)
        return
    m = re.match(r"(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1), "enum", line_no)
        return
    m = re.match(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", stripped)
    if m:
        kind = "async_function" if "async" in stripped.split("function", 1)[0].split() else "function"
        _add_candidate(symbols, lines, language, m.group(1), kind, line_no)
        return
    m = re.match(
        r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
        stripped,
    )
    if m:
        kind = "async_function" if "async" in stripped else "function"
        _add_candidate(symbols, lines, language, m.group(1), kind, line_no)
        return
    if not CONTROL_START_RE.match(stripped):
        m = re.match(r"(?:public\s+|private\s+|protected\s+|static\s+|async\s+|override\s+|readonly\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*[:\w<>\[\], ?|&]*\s*\{", stripped)
        if m and m.group(1) not in CALL_SKIP_NAMES:
            kind = "async_method" if "async" in stripped.split(m.group(1), 1)[0].split() else "method"
            _add_candidate(symbols, lines, language, m.group(1), kind, line_no, decorators)


def _add_go_symbol(symbols: list[SymbolCandidate], lines: list[str], language: str, stripped: str, line_no: int) -> None:
    m = re.match(r"func\s*\(([^)]+)\)\s*([A-Za-z_][\w]*)\s*\(", stripped)
    if m:
        receiver = m.group(1).strip().split()[-1].lstrip("*")
        name = m.group(2)
        _add_candidate(symbols, lines, language, name, "method", line_no, qualified_name=f"{receiver}.{name}")
        return
    m = re.match(r"func\s+([A-Za-z_][\w]*)\s*\(", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1), "function", line_no)
        return
    m = re.match(r"type\s+([A-Za-z_][\w]*)\s+(struct|interface)\b", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1), m.group(2), line_no)


def _add_rust_symbol(symbols: list[SymbolCandidate], lines: list[str], language: str, stripped: str, line_no: int) -> None:
    m = re.match(r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)\s*(?:<[^>]+>)?\s*\(", stripped)
    if m:
        kind = "async_function" if "async" in stripped.split("fn", 1)[0].split() else "function"
        _add_candidate(symbols, lines, language, m.group(1), kind, line_no)
        return
    m = re.match(r"(?:pub\s+)?(struct|enum|trait)\s+([A-Za-z_][\w]*)", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(2), m.group(1), line_no)
        return
    m = re.match(r"impl(?:\s*<[^>]+>)?\s+([A-Za-z_][\w]*(?:::[A-Za-z_][\w]*)*)", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1).split("::")[-1], "impl", line_no, qualified_name=m.group(1).replace("::", "."))


def _add_java_symbol(
    symbols: list[SymbolCandidate],
    lines: list[str],
    language: str,
    stripped: str,
    line_no: int,
    decorators: tuple[str, ...],
) -> None:
    m = re.match(r"(?:public\s+|private\s+|protected\s+|abstract\s+|final\s+|static\s+)*(class|interface|enum|record)\s+([A-Za-z_][\w]*)", stripped)
    if m:
        kind = "class" if m.group(1) == "record" else m.group(1)
        _add_candidate(symbols, lines, language, m.group(2), kind, line_no, decorators)
        return
    if CONTROL_START_RE.match(stripped) or stripped.startswith("@"):
        return
    m = re.match(
        r"(?:public\s+|private\s+|protected\s+|static\s+|final\s+|synchronized\s+|abstract\s+|native\s+)*"
        r"(?:<[^>]+>\s*)?(?:[\w.$<>\[\], ?]+\s+)?([A-Za-z_][\w]*)\s*\([^;{}]*\)\s*(?:throws\s+[\w., ]+\s*)?\{?",
        stripped,
    )
    if m and m.group(1) not in CALL_SKIP_NAMES:
        _add_candidate(symbols, lines, language, m.group(1), "method", line_no, decorators)


def _add_kotlin_symbol(
    symbols: list[SymbolCandidate],
    lines: list[str],
    language: str,
    stripped: str,
    line_no: int,
    decorators: tuple[str, ...],
) -> None:
    m = re.match(r"(?:public\s+|private\s+|internal\s+|open\s+|data\s+|sealed\s+|abstract\s+)*(class|interface|object|enum\s+class)\s+([A-Za-z_][\w]*)", stripped)
    if m:
        kind = "enum" if m.group(1).startswith("enum") else ("class" if m.group(1) == "object" else m.group(1))
        _add_candidate(symbols, lines, language, m.group(2), kind, line_no, decorators)
        return
    m = re.match(r"(?:public\s+|private\s+|internal\s+|suspend\s+|inline\s+|operator\s+|override\s+)*fun\s+(?:[A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)\s*\(", stripped)
    if m:
        kind = "async_function" if "suspend" in stripped.split("fun", 1)[0].split() else "function"
        _add_candidate(symbols, lines, language, m.group(1), kind, line_no, decorators)


def _add_swift_symbol(symbols: list[SymbolCandidate], lines: list[str], language: str, stripped: str, line_no: int) -> None:
    m = re.match(r"(?:public\s+|private\s+|internal\s+|open\s+|final\s+)*(class|struct|enum|protocol|extension)\s+([A-Za-z_][\w]*)", stripped)
    if m:
        kind = "class" if m.group(1) == "extension" else m.group(1)
        _add_candidate(symbols, lines, language, m.group(2), kind, line_no)
        return
    m = re.match(r"(?:public\s+|private\s+|internal\s+|static\s+|class\s+|mutating\s+)*func\s+([A-Za-z_][\w]*)\s*\(", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1), "function", line_no)


def _add_c_like_symbol(symbols: list[SymbolCandidate], lines: list[str], language: str, stripped: str, line_no: int) -> None:
    m = re.match(r"(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|abstract\s+|sealed\s+|partial\s+)*(class|struct|interface|enum|namespace)\s+([A-Za-z_][\w]*)", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(2), m.group(1), line_no)
        return
    if CONTROL_START_RE.match(stripped) or stripped.startswith("#"):
        return
    m = re.match(
        r"(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|virtual\s+|override\s+|async\s+|inline\s+|constexpr\s+|extern\s+)*"
        r"[A-Za-z_][\w:<>,~*&\s\[\]]+\s+([A-Za-z_~][\w~]*)\s*\([^;{}]*\)\s*(?:const\s*)?\{?",
        stripped,
    )
    if m and m.group(1) not in CALL_SKIP_NAMES:
        kind = "async_function" if "async" in stripped.split(m.group(1), 1)[0].split() else "function"
        _add_candidate(symbols, lines, language, m.group(1), kind, line_no)


def _add_ruby_symbol(symbols: list[SymbolCandidate], lines: list[str], language: str, stripped: str, line_no: int) -> None:
    m = re.match(r"class\s+([A-Z][\w:]*)", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1).split("::")[-1], "class", line_no, qualified_name=m.group(1).replace("::", "."))
        return
    m = re.match(r"def\s+(?:self\.)?([A-Za-z_]\w*[!?=]?)", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1), "function", line_no)


def _add_php_symbol(symbols: list[SymbolCandidate], lines: list[str], language: str, stripped: str, line_no: int) -> None:
    m = re.match(r"(?:abstract\s+|final\s+)?(class|interface|trait|enum)\s+([A-Za-z_][\w]*)", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(2), m.group(1), line_no)
        return
    m = re.match(r"(?:public\s+|private\s+|protected\s+|static\s+|final\s+|abstract\s+)*function\s+([A-Za-z_][\w]*)\s*\(", stripped)
    if m:
        _add_candidate(symbols, lines, language, m.group(1), "function", line_no)


def _qualify_nested_symbols(symbols: list[SymbolCandidate]) -> list[SymbolCandidate]:
    ordered = sorted(symbols, key=lambda s: (s.start_line, -(s.end_line or s.start_line)))
    for idx, sym in enumerate(ordered):
        if sym.qualified_name and sym.qualified_name != sym.name:
            continue
        parents = [
            p
            for p in ordered[:idx]
            if p.kind in CONTAINER_KINDS
            and p.start_line < sym.start_line
            and (p.end_line or p.start_line) >= (sym.end_line or sym.start_line)
        ]
        if parents and sym.kind in CALLABLE_KINDS:
            parent = max(parents, key=lambda p: p.start_line)
            sym.qualified_name = f"{parent.qualified_name or parent.name}.{sym.name}"
    return ordered


def _generic_calls_in_range(lines: list[str], language: str, start_line: int, end_line: int, current_name: str) -> list[tuple[int, CallName]]:
    calls: list[tuple[int, CallName]] = []
    for line_no in range(start_line, min(end_line, len(lines)) + 1):
        line = _strip_line_comment(lines[line_no - 1], language)
        if not line.strip() or line.strip().startswith(("import ", "package ", "use ", "#include")):
            continue
        for match in CALL_RE.finditer(line):
            raw = match.group(1)
            parts = [p for p in re.split(r"\.|::|->", raw) if p]
            if not parts:
                continue
            base = parts[-1]
            prefix = line[: match.start()].rstrip()
            if base in CALL_SKIP_NAMES or (line_no == start_line and base == current_name):
                continue
            if prefix.endswith(("function", "func", "fn", "def")):
                continue
            call_name = _call_name_from_parts(parts)
            if call_name:
                calls.append((line_no, call_name))
    return calls


def _index_generic(conn, file_id: int, path: Path, language: str) -> None:
    source = _read_source(path)
    if source is None:
        return
    lines = source.splitlines()

    for module, imported_name, alias, level, line in _generic_imports(language, lines):
        _insert_import(conn, file_id, module, imported_name, alias, level, line)

    symbols = _generic_symbols(language, lines)
    symbol_ids: dict[int, int] = {}
    inserted: list[tuple[SymbolCandidate, int]] = []

    for idx, candidate in enumerate(symbols):
        parent_id = None
        parents = [
            (parent, sym_id)
            for parent, sym_id in inserted
            if parent.start_line < candidate.start_line
            and (parent.end_line or parent.start_line) >= (candidate.end_line or candidate.start_line)
        ]
        if parents:
            parent_id = max(parents, key=lambda item: item[0].start_line)[1]
        sym_id = _insert_symbol(conn, file_id, candidate, parent_id)
        symbol_ids[idx] = sym_id
        inserted.append((candidate, sym_id))

        for dec in candidate.decorators:
            route = _route_from_text(dec, language)
            if route:
                framework, method, route_path = route
                _insert_route(conn, file_id, framework, method, route_path, sym_id, candidate.start_line)

        if candidate.kind in CALLABLE_KINDS:
            for line_no, call_name in _generic_calls_in_range(
                lines,
                language,
                candidate.start_line,
                candidate.end_line,
                candidate.name,
            ):
                _insert_call(conn, file_id, sym_id, call_name, line_no)

    # Next.js App Router: file path + HTTP-method exports define the route.
    if language in {"javascript", "typescript"}:
        next_path = _next_app_route_path(path)
        if next_path is not None:
            for candidate, sym_id in inserted:
                if (
                    candidate.kind in CALLABLE_KINDS
                    and candidate.name in NEXT_HTTP_METHODS
                ):
                    _insert_route(
                        conn,
                        file_id,
                        "next-app",
                        candidate.name,
                        next_path,
                        sym_id,
                        candidate.start_line,
                    )

    for line_no, line in enumerate(lines, start=1):
        route = _route_from_text(line, language)
        if route:
            framework, method, route_path = route
            # Decorator-backed routes were already inserted with a handler id.
            if line.strip().startswith("@"):
                continue
            _insert_route(conn, file_id, framework, method, route_path, None, line_no)


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

        before_syms = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        before_imps = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
        before_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        before_routes = conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]

        if lang == "python":
            _index_python(conn, file_id, path)
        elif lang in CODE_LANGUAGES:
            _index_generic(conn, file_id, path, lang)

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
