"""repotrace CLI — argparse dispatch."""

from __future__ import annotations

import argparse
import json as _json
import sys
from pathlib import Path
from typing import Any

from . import context as ctx_mod
from . import deps as deps_mod
from . import indexer, queries
from .git_utils import repo_root as git_root


def _resolve_root(path: str | None) -> Path:
    base = Path(path).resolve() if path else Path.cwd()
    # prefer git root if available
    return git_root(base)


def _emit(data: Any, *, as_json: bool, formatter=None) -> None:
    if as_json:
        print(_json.dumps(data, indent=2, default=str))
        return
    if formatter is not None:
        formatter(data)
        return
    print(_json.dumps(data, indent=2, default=str))


def _fmt_overview(d: dict) -> None:
    print(f"Repo:         {d['repo_root']}")
    print(f"Indexed at:   {d['indexed_at']}")
    t = d["totals"]
    print(
        f"Totals:       files={t['files']}  symbols={t['symbols']}  "
        f"imports={t['imports']}  calls={t['calls']}  routes={t['routes']}"
    )
    print()
    print("By language:")
    for r in d["by_language"]:
        print(f"  {r['language']:<12} files={r['files']:>5}  lines={r['lines']:>8}")
    print()
    print("Largest files:")
    for r in d["biggest_files"]:
        print(f"  {r['lines']:>6}  {r['path']}")
    print()
    print("Most called symbols:")
    for r in d["most_called"]:
        if r["callers"] == 0:
            continue
        print(f"  {r['callers']:>4}  {r['name']:<40}  {r['file']}")


def _fmt_find(rows: list[dict]) -> None:
    if not rows:
        print("(no matches)")
        return
    for r in rows:
        print(
            f"{r['kind']:<14} {r['qualified_name'] or r['name']:<50} "
            f"{r['file']}:{r['lines']}"
        )


def _fmt_symbol(rows: list[dict]) -> None:
    if not rows:
        print("(symbol not found)")
        return
    for r in rows:
        print(f"== {r['qualified_name'] or r['name']}  ({r['kind']})")
        print(f"   {r['path']}:{r['start_line']}-{r['end_line']}")
        if r.get("decorators"):
            decs = r["decorators"]
            try:
                decs = ", ".join(_json.loads(decs))
            except Exception:
                pass
            if decs and decs != "[]":
                print(f"   decorators: {decs}")
        if r.get("docstring"):
            doc = (r["docstring"] or "").strip().splitlines()
            if doc:
                print(f"   doc: {doc[0]}")
        print()


def _fmt_callers(rows: list[dict]) -> None:
    if not rows:
        print("(no callers found)")
        return
    for r in rows:
        caller = r.get("caller_qname") or r.get("caller_name") or "<module>"
        print(f"{caller:<50} {r['path']}:{r['line']}")


def _fmt_callees(rows: list[dict]) -> None:
    if not rows:
        print("(no callees found)")
        return
    for r in rows:
        print(f"{r['callee_name']:<50} {r['path']}:{r['line']}")


def _fmt_routes(rows: list[dict]) -> None:
    if not rows:
        print("(no routes detected)")
        return
    for r in rows:
        handler = r.get("handler") or "(?)"
        print(
            f"{r['method']:<6} {r['path']:<40} -> {handler:<40} "
            f"({r['file']}:{r['line']})"
        )


def _fmt_changed(d: dict) -> None:
    files = d.get("changed_files", [])
    if not files:
        print(f"(no files changed since {d['since']})")
        return
    print(f"Changed since {d['since']}:")
    for f in files:
        marker = "" if f["indexed"] else " (not indexed)"
        print(f"  {f['file']}{marker}")
        for s in f["symbols"]:
            print(f"      {s['kind']:<12} {s['qualified_name'] or s['name']}  "
                  f"L{s['start_line']}-{s['end_line']}")


def _fmt_file(d: dict) -> None:
    if not d:
        print("(file not indexed)")
        return
    f = d["file"]
    print(f"{f['path']}  ({f['language']}, {f['line_count']} lines)")
    if f.get("git_last_commit"):
        print(f"  last commit: {f['git_last_commit']}  {f['git_last_modified']}")
    print()
    if d["imports"]:
        print("Imports:")
        for i in d["imports"]:
            tail = f".{i['imported_name']}" if i["imported_name"] else ""
            alias = f" as {i['alias']}" if i["alias"] else ""
            print(f"  L{i['line']:<4} {i['module']}{tail}{alias}")
        print()
    if d["symbols"]:
        print("Symbols:")
        for s in d["symbols"]:
            indent = "    " if s["parent_id"] else "  "
            print(
                f"{indent}{s['kind']:<14} {s['qualified_name'] or s['name']}  "
                f"L{s['start_line']}-{s['end_line']}"
            )


def _fmt_impact(d: dict) -> None:
    print(
        f"Impact for {d['target_kind']} '{d['target']}' (depth={d['depth']}): "
        f"{d['affected_file_count']} affected file(s)"
    )
    for entry in d["affected_files"]:
        print(f"  {entry['file']}")
        for c in entry.get("callers", []):
            caller = c.get("caller") or "<module>"
            print(f"      L{c['line']:<5} {caller}  -> {c['calling']}")
        for imp in entry.get("imports", []):
            print(f"      imports {imp}")


def _fmt_deps(d: dict) -> None:
    s = d["summary"]
    print(
        f"Python files={s['python_files']}  resolved_edges={s['resolved_edges']}  "
        f"unresolved_imports={s['unresolved_imports']}"
    )
    print()
    print("Most imported modules:")
    for node in sorted(d["nodes"], key=lambda n: n["imported_by"], reverse=True)[:15]:
        if node["imported_by"]:
            marker = " [test]" if node["is_test"] else ""
            print(f"  {node['imported_by']:>4}  {node['path']}{marker}")


def _fmt_cycles(d: dict) -> None:
    if not d["cycles"]:
        print("No Python import cycles detected.")
        return
    print(f"Detected {d['cycle_count']} Python import cycle(s):")
    for i, cycle in enumerate(d["cycles"], start=1):
        print(f"\nCycle {i}:")
        for path in cycle:
            print(f"  -> {path}")
    if d.get("truncated"):
        print("\n(truncated; increase --limit for more)")


def _fmt_check(d: dict) -> None:
    print(
        f"repotrace check: {d['status'].upper()} "
        f"({d['error_count']} error(s), {d['warning_count']} warning(s))"
    )
    for issue in d["issues"]:
        sev = issue["severity"].upper()
        code = issue["code"]
        print(f"\n[{sev}] {code}: {issue['message']}")
        if "files" in issue:
            for path in issue["files"]:
                print(f"  -> {path}")
        elif "source" in issue:
            print(f"  {issue['source']}:{issue.get('line')} -> {issue['target']}")
        elif "file" in issue:
            extra = ""
            if "line_count" in issue:
                extra = f" ({issue['line_count']} lines)"
            if "imported_by" in issue:
                extra = f" (imported_by={issue['imported_by']})"
            print(f"  {issue['file']}{extra}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="repotrace",
        description="Local-first code intelligence (Python AST + SQLite).",
    )
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p.add_argument(
        "--root",
        default=None,
        help="repo root (default: cwd; auto-detected to git root)",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("index", help="index a repo")
    sp.add_argument("path", nargs="?", default=None)
    sp.add_argument("--reset", action="store_true", help="drop and recreate the index")
    sp.add_argument("--verbose", "-v", action="store_true")

    sub.add_parser("overview", help="repo summary")

    sp = sub.add_parser("find", help="search symbols by name (substring)")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("symbol", help="symbol detail")
    sp.add_argument("name")

    sp = sub.add_parser("callers", help="who calls this symbol")
    sp.add_argument("name")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--broad", action="store_true", help="include method calls with the same base name")

    sp = sub.add_parser("callees", help="what does this symbol call")
    sp.add_argument("name")
    sp.add_argument("--limit", type=int, default=50)

    sp = sub.add_parser("file", help="file outline (symbols + imports)")
    sp.add_argument("path")

    sub.add_parser("routes", help="HTTP routes (Python decorators)")

    sp = sub.add_parser("changed", help="symbols in files changed since a git ref")
    sp.add_argument("--since", default="main")

    sp = sub.add_parser("impact", help="blast radius for a symbol or file")
    sp.add_argument("target")
    sp.add_argument("--depth", type=int, default=2)

    sub.add_parser("deps", help="Python import graph summary")

    sp = sub.add_parser("cycles", help="detect Python import cycles")
    sp.add_argument("--limit", type=int, default=50)

    sp = sub.add_parser("check", help="architecture checks; exits nonzero on errors")
    sp.add_argument("--max-lines", type=int, default=1000)
    sp.add_argument("--cycle-limit", type=int, default=50)
    sp.add_argument("--no-fail", action="store_true", help="always exit 0")

    sp = sub.add_parser("context", help="generate an agent context bundle")
    sp.add_argument("query")
    sp.add_argument("--out", default=None, help="output markdown path")
    sp.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print to stdout instead of writing a file",
    )

    args = p.parse_args(argv)
    root = _resolve_root(args.root if args.cmd != "index" else (args.path or args.root))

    if args.cmd == "index":
        stats = indexer.index_repo(root, reset=args.reset, verbose=args.verbose)
        _emit(stats, as_json=args.json, formatter=lambda d: print(
            f"Indexed {d['files_indexed']} files in {d['elapsed_seconds']}s "
            f"(symbols={d['symbols']}, imports={d['imports']}, calls={d['calls']}, "
            f"routes={d['routes']})  root={d['repo_root']}"
        ))
        return 0

    if args.cmd == "overview":
        _emit(queries.overview(root), as_json=args.json, formatter=_fmt_overview)
        return 0

    if args.cmd == "find":
        _emit(queries.find(root, args.query, args.limit), as_json=args.json, formatter=_fmt_find)
        return 0

    if args.cmd == "symbol":
        _emit(queries.symbol(root, args.name), as_json=args.json, formatter=_fmt_symbol)
        return 0

    if args.cmd == "callers":
        _emit(
            queries.callers(root, args.name, args.limit, broad=args.broad),
            as_json=args.json,
            formatter=_fmt_callers,
        )
        return 0

    if args.cmd == "callees":
        _emit(queries.callees(root, args.name, args.limit), as_json=args.json, formatter=_fmt_callees)
        return 0

    if args.cmd == "file":
        _emit(queries.file_outline(root, args.path), as_json=args.json, formatter=_fmt_file)
        return 0

    if args.cmd == "routes":
        _emit(queries.routes(root), as_json=args.json, formatter=_fmt_routes)
        return 0

    if args.cmd == "changed":
        _emit(queries.changed(root, args.since), as_json=args.json, formatter=_fmt_changed)
        return 0

    if args.cmd == "impact":
        _emit(queries.impact(root, args.target, args.depth), as_json=args.json, formatter=_fmt_impact)
        return 0

    if args.cmd == "deps":
        _emit(deps_mod.import_graph(root), as_json=args.json, formatter=_fmt_deps)
        return 0

    if args.cmd == "cycles":
        _emit(deps_mod.cycles(root, limit=args.limit), as_json=args.json, formatter=_fmt_cycles)
        return 0

    if args.cmd == "check":
        result = deps_mod.check(root, max_lines=args.max_lines, cycle_limit=args.cycle_limit)
        _emit(result, as_json=args.json, formatter=_fmt_check)
        return 0 if args.no_fail or result["status"] == "pass" else 1

    if args.cmd == "context":
        if args.print_only:
            print(ctx_mod.build_context(root, args.query))
            return 0
        out_path = Path(args.out).resolve() if args.out else None
        path = ctx_mod.write_bundle(root, args.query, out_path)
        if args.json:
            print(_json.dumps({"bundle": str(path)}, indent=2))
        else:
            print(f"Wrote context bundle: {path}")
        return 0

    p.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
