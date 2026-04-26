"""Query functions used by CLI commands."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from . import db, git_utils


def overview(repo_root: Path) -> dict:
    conn = db.connect(repo_root)
    rows = conn.execute(
        "SELECT language, COUNT(*) AS files, SUM(line_count) AS lines "
        "FROM files GROUP BY language ORDER BY files DESC"
    ).fetchall()
    by_lang = [
        {"language": r["language"] or "unknown", "files": r["files"], "lines": r["lines"] or 0}
        for r in rows
    ]
    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    total_imports = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    total_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    total_routes = conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]

    biggest = conn.execute(
        "SELECT path, line_count FROM files ORDER BY line_count DESC LIMIT 10"
    ).fetchall()

    most_called = conn.execute(
        "SELECT s.qualified_name AS name, s.kind, f.path, COUNT(c.id) AS callers "
        "FROM symbols s "
        "JOIN files f ON s.file_id = f.id "
        "LEFT JOIN calls c ON ("
        "  c.callee_name = s.qualified_name "
        "  OR (c.callee_qualifier IS NULL AND c.callee_base = s.name)"
        ") "
        "  AND c.file_id IN ("
        "    SELECT id FROM files "
        "    WHERE path NOT LIKE 'tests/%' "
        "      AND path NOT LIKE '%/tests/%' "
        "      AND path NOT LIKE 'test_%' "
        "      AND path NOT LIKE '%/test_%'"
        "  ) "
        "WHERE s.kind IN ('function','async_function','method','async_method') "
        "  AND f.path NOT LIKE 'tests/%' "
        "  AND f.path NOT LIKE '%/tests/%' "
        "  AND f.path NOT LIKE 'test_%' "
        "  AND f.path NOT LIKE '%/test_%' "
        "GROUP BY s.id ORDER BY callers DESC LIMIT 10"
    ).fetchall()

    indexed_at = conn.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
    conn.close()
    return {
        "repo_root": str(repo_root),
        "indexed_at": indexed_at["value"] if indexed_at else None,
        "totals": {
            "files": total_files,
            "symbols": total_symbols,
            "imports": total_imports,
            "calls": total_calls,
            "routes": total_routes,
        },
        "by_language": by_lang,
        "biggest_files": [{"path": r["path"], "lines": r["line_count"]} for r in biggest],
        "most_called": [
            {
                "name": r["name"],
                "kind": r["kind"],
                "file": r["path"],
                "callers": r["callers"],
            }
            for r in most_called
        ],
    }


def find(repo_root: Path, query: str, limit: int = 20) -> list[dict]:
    conn = db.connect(repo_root)
    q = f"%{query}%"
    rows = conn.execute(
        "SELECT s.name, s.qualified_name, s.kind, s.start_line, s.end_line, f.path "
        "FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.name LIKE ? OR s.qualified_name LIKE ? "
        "ORDER BY "
        "  CASE WHEN f.path LIKE 'tests/%' OR f.path LIKE '%/tests/%' "
        "         OR f.path LIKE 'test_%' OR f.path LIKE '%/test_%' "
        "       THEN 1 ELSE 0 END, "
        "  (s.name = ?) DESC, length(s.name), s.name LIMIT ?",
        (q, q, query, limit),
    ).fetchall()
    conn.close()
    return [
        {
            "name": r["name"],
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "file": r["path"],
            "lines": f"{r['start_line']}-{r['end_line']}",
        }
        for r in rows
    ]


def symbol(repo_root: Path, name: str) -> list[dict]:
    conn = db.connect(repo_root)
    rows = conn.execute(
        "SELECT s.id, s.name, s.qualified_name, s.kind, s.start_line, s.end_line, "
        "s.docstring, s.decorators, f.path "
        "FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.name = ? OR s.qualified_name = ? OR s.qualified_name LIKE ? "
        "ORDER BY (s.name = ?) DESC, s.qualified_name LIMIT 20",
        (name, name, f"%.{name}", name),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def callers(repo_root: Path, name: str, limit: int = 50, broad: bool = False) -> list[dict]:
    """Find call sites referencing `name` using normalized callee data.

    Default mode is precise: direct calls (`foo()`) or exact qualified calls.
    If precise mode finds nothing, fall back to a broad base-name query so
    module-qualified calls (`module.foo()`) are still discoverable.
    """
    conn = db.connect(repo_root)

    def run(where: str, params: tuple) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT c.line, c.callee_name, c.callee_base, f.path, "
            "       s.name AS caller_name, s.qualified_name AS caller_qname, s.kind AS caller_kind "
            "FROM calls c "
            "JOIN files f ON c.file_id = f.id "
            "LEFT JOIN symbols s ON c.caller_symbol_id = s.id "
            f"WHERE {where} "
            "ORDER BY "
            "  CASE WHEN f.path LIKE 'tests/%' OR f.path LIKE '%/tests/%' "
            "         OR f.path LIKE 'test_%' OR f.path LIKE '%/test_%' "
            "       THEN 1 ELSE 0 END, "
            "  f.path, c.line LIMIT ?",
            (*params, limit),
        ).fetchall()

    if "." in name:
        rows = run("c.callee_name = ? OR c.callee_name LIKE ?", (name, f"%.{name}"))
    elif broad:
        rows = run("c.callee_base = ?", (name,))
    else:
        rows = run("c.callee_base = ? AND c.callee_qualifier IS NULL", (name,))
        if not rows:
            rows = run("c.callee_base = ?", (name,))

    conn.close()
    return [dict(r) for r in rows]


def callees(repo_root: Path, name: str, limit: int = 50) -> list[dict]:
    """List calls made from inside the symbol named `name`."""
    conn = db.connect(repo_root)
    rows = conn.execute(
        "SELECT c.callee_name, c.line, f.path, s.qualified_name AS caller_qname "
        "FROM calls c "
        "JOIN symbols s ON c.caller_symbol_id = s.id "
        "JOIN files f ON c.file_id = f.id "
        "WHERE s.name = ? OR s.qualified_name = ? OR s.qualified_name LIKE ? "
        "ORDER BY c.line LIMIT ?",
        (name, name, f"%.{name}", limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def file_outline(repo_root: Path, rel_path: str) -> dict:
    conn = db.connect(repo_root)
    f = conn.execute(
        "SELECT * FROM files WHERE path = ?",
        (rel_path,),
    ).fetchone()
    if not f:
        conn.close()
        return {}
    syms = conn.execute(
        "SELECT name, qualified_name, kind, start_line, end_line, parent_id "
        "FROM symbols WHERE file_id = ? ORDER BY start_line",
        (f["id"],),
    ).fetchall()
    imps = conn.execute(
        "SELECT module, imported_name, alias, level, line FROM imports "
        "WHERE file_id = ? ORDER BY line",
        (f["id"],),
    ).fetchall()
    conn.close()
    return {
        "file": dict(f),
        "symbols": [dict(s) for s in syms],
        "imports": [dict(i) for i in imps],
    }


def routes(repo_root: Path) -> list[dict]:
    conn = db.connect(repo_root)
    rows = conn.execute(
        "SELECT r.method, r.path, r.framework, r.line, f.path AS file, "
        "       s.qualified_name AS handler "
        "FROM routes r "
        "JOIN files f ON r.file_id = f.id "
        "LEFT JOIN symbols s ON r.handler_symbol_id = s.id "
        "ORDER BY r.path, r.method"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def changed(repo_root: Path, since: str = "main") -> dict:
    """Map files changed vs `since` ref to their symbols."""
    files = git_utils.changed_files_since(repo_root, since)
    conn = db.connect(repo_root)
    out: list[dict] = []
    for rel in files:
        f = conn.execute("SELECT id, path FROM files WHERE path = ?", (rel,)).fetchone()
        if not f:
            out.append({"file": rel, "indexed": False, "symbols": []})
            continue
        syms = conn.execute(
            "SELECT name, qualified_name, kind, start_line, end_line "
            "FROM symbols WHERE file_id = ? ORDER BY start_line",
            (f["id"],),
        ).fetchall()
        out.append(
            {
                "file": rel,
                "indexed": True,
                "symbols": [dict(s) for s in syms],
            }
        )
    conn.close()
    return {"since": since, "changed_files": out}


def impact(repo_root: Path, target: str, depth: int = 2) -> dict:
    """Blast radius for a symbol or file path.

    For a symbol: callers (transitively up to `depth`).
    For a file: importers + callers of all symbols in the file.
    """
    conn = db.connect(repo_root)

    is_file = bool(conn.execute("SELECT 1 FROM files WHERE path = ?", (target,)).fetchone())

    visited_symbols: set[int] = set()
    affected_files: dict[str, dict] = {}

    def _expand_symbol(sym_id: int, current_depth: int) -> None:
        if sym_id in visited_symbols or current_depth > depth:
            return
        visited_symbols.add(sym_id)
        sym = conn.execute(
            "SELECT s.name, s.qualified_name, s.kind, f.path "
            "FROM symbols s JOIN files f ON s.file_id = f.id WHERE s.id = ?",
            (sym_id,),
        ).fetchone()
        if not sym:
            return
        # callers of this symbol
        crows = conn.execute(
            "SELECT c.line, f.path AS file, s.id AS caller_id, s.qualified_name AS caller_qname "
            "FROM calls c "
            "JOIN files f ON c.file_id = f.id "
            "LEFT JOIN symbols s ON c.caller_symbol_id = s.id "
            "WHERE c.callee_base = ? OR c.callee_name = ?",
            (sym["name"], sym["qualified_name"]),
        ).fetchall()
        for cr in crows:
            entry = affected_files.setdefault(
                cr["file"], {"file": cr["file"], "callers": [], "imports": []}
            )
            entry["callers"].append(
                {
                    "line": cr["line"],
                    "caller": cr["caller_qname"],
                    "calling": sym["qualified_name"],
                }
            )
            if cr["caller_id"]:
                _expand_symbol(cr["caller_id"], current_depth + 1)

    if is_file:
        # find importers
        # crude: any import whose module ends with the file's stem or matches dotted form
        file_row = conn.execute(
            "SELECT id, path FROM files WHERE path = ?", (target,)
        ).fetchone()
        stem = Path(target).stem
        dotted = (
            target.replace("/", ".").rsplit(".", 1)[0]
            if target.endswith(".py")
            else target.replace("/", ".")
        )
        importers = conn.execute(
            "SELECT DISTINCT f.path "
            "FROM imports i JOIN files f ON i.file_id = f.id "
            "WHERE i.module = ? OR i.module LIKE ? OR i.imported_name = ?",
            (dotted, f"%.{stem}", stem),
        ).fetchall()
        for imp in importers:
            entry = affected_files.setdefault(
                imp["path"], {"file": imp["path"], "callers": [], "imports": []}
            )
            entry["imports"].append(stem)

        # expand each symbol in the file
        sym_rows = conn.execute(
            "SELECT id FROM symbols WHERE file_id = ?", (file_row["id"],)
        ).fetchall()
        for s in sym_rows:
            _expand_symbol(s["id"], 1)
        kind = "file"
    else:
        # treat as symbol
        sym_rows = conn.execute(
            "SELECT id FROM symbols WHERE name = ? OR qualified_name = ? OR qualified_name LIKE ?",
            (target, target, f"%.{target}"),
        ).fetchall()
        for s in sym_rows:
            _expand_symbol(s["id"], 1)
        kind = "symbol"

    conn.close()
    return {
        "target": target,
        "target_kind": kind,
        "depth": depth,
        "affected_files": list(affected_files.values()),
        "affected_file_count": len(affected_files),
    }
