"""SQLite schema and connection helpers for repotrace."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    language TEXT,
    line_count INTEGER,
    size_bytes INTEGER,
    mtime REAL,
    git_last_commit TEXT,
    git_last_modified TEXT,
    indexed_at REAL
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    qualified_name TEXT,
    kind TEXT NOT NULL,
    file_id INTEGER NOT NULL,
    parent_id INTEGER,
    start_line INTEGER,
    end_line INTEGER,
    docstring TEXT,
    decorators TEXT,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES symbols(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    module TEXT NOT NULL,
    imported_name TEXT,
    alias TEXT,
    level INTEGER NOT NULL DEFAULT 0,
    line INTEGER,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    caller_symbol_id INTEGER,
    callee_name TEXT NOT NULL,
    callee_base TEXT NOT NULL DEFAULT '',
    callee_qualifier TEXT,
    line INTEGER,
    file_id INTEGER NOT NULL,
    FOREIGN KEY (caller_symbol_id) REFERENCES symbols(id) ON DELETE CASCADE,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    framework TEXT,
    method TEXT,
    path TEXT,
    handler_symbol_id INTEGER,
    line INTEGER,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY (handler_symbol_id) REFERENCES symbols(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name);
CREATE INDEX IF NOT EXISTS idx_calls_base ON calls(callee_base);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_symbol_id);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS idx_routes_file ON routes(file_id);
CREATE INDEX IF NOT EXISTS idx_files_language ON files(language);
"""


def repo_index_path(repo_root: Path) -> Path:
    """Return the SQLite path for a repo, creating the .repotrace dir if needed."""
    d = repo_root / ".repotrace"
    d.mkdir(parents=True, exist_ok=True)
    return d / "index.sqlite"


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply lightweight schema migrations for existing local indexes."""
    call_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(calls)").fetchall()
    }
    if "callee_base" not in call_columns:
        conn.execute("ALTER TABLE calls ADD COLUMN callee_base TEXT NOT NULL DEFAULT ''")
    if "callee_qualifier" not in call_columns:
        conn.execute("ALTER TABLE calls ADD COLUMN callee_qualifier TEXT")

    import_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(imports)").fetchall()
    }
    if "level" not in import_columns:
        conn.execute("ALTER TABLE imports ADD COLUMN level INTEGER NOT NULL DEFAULT 0")


def connect(repo_root: Path) -> sqlite3.Connection:
    """Open (and initialize if needed) the repo's SQLite index."""
    path = repo_index_path(repo_root)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def reset(repo_root: Path) -> None:
    """Drop and recreate the schema (used by `repotrace index --reset`)."""
    path = repo_index_path(repo_root)
    if path.exists():
        path.unlink()
    # connect() will recreate
    conn = connect(repo_root)
    conn.close()
