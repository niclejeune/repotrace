"""Generate small, agent-efficient context bundles."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import queries


def _is_test_file(path: str) -> bool:
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or Path(path).name.startswith("test_")
    )


def build_context(repo_root: Path, query: str, max_files: int = 6) -> str:
    """Return a markdown context bundle for `query`."""
    matches = queries.find(repo_root, query, limit=12)

    # group by file, keep best symbol per file
    by_file: dict[str, list[dict]] = {}
    for m in matches:
        by_file.setdefault(m["file"], []).append(m)

    # pick top files in match order
    top_files = list(by_file.keys())[:max_files]

    lines: list[str] = []
    lines.append("# Code Context Bundle")
    lines.append("")
    lines.append(f"- Query: `{query}`")
    lines.append(f"- Repo: `{repo_root}`")
    lines.append(f"- Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")

    if not matches:
        lines.append("## No matching symbols found")
        lines.append("")
        lines.append(
            "Try a shorter or more general query, or run `repotrace overview` "
            "to see what's indexed."
        )
        return "\n".join(lines) + "\n"

    production_matches = [m for m in matches if not _is_test_file(m["file"])]
    test_matches = [m for m in matches if _is_test_file(m["file"])]

    if production_matches:
        lines.append("## Relevant production symbols")
        lines.append("")
        for m in production_matches:
            lines.append(
                f"- `{m['qualified_name'] or m['name']}` "
                f"({m['kind']}) — `{m['file']}:{m['lines']}`"
            )
        lines.append("")

    if test_matches:
        lines.append("## Relevant tests")
        lines.append("")
        for m in test_matches[:8]:
            lines.append(
                f"- `{m['qualified_name'] or m['name']}` "
                f"({m['kind']}) — `{m['file']}:{m['lines']}`"
            )
        lines.append("")

    prod_files = [fp for fp in top_files if not _is_test_file(fp)]
    test_files = [fp for fp in top_files if _is_test_file(fp)]

    lines.append("## Files to read first")
    lines.append("")
    for i, fp in enumerate(prod_files[:max_files], start=1):
        first_sym = by_file[fp][0]
        lines.append(f"{i}. `{fp}` — start at line {first_sym['lines'].split('-')[0]}")
    if test_files:
        lines.append("")
        lines.append("Tests to read after production flow is clear:")
        for fp in test_files[:3]:
            first_sym = by_file[fp][0]
            lines.append(f"- `{fp}` — start at line {first_sym['lines'].split('-')[0]}")
    lines.append("")

    # Likely callers for the top symbol
    top = matches[0]
    cs = queries.callers(repo_root, top["name"], limit=10)
    if cs:
        prod_callers = [c for c in cs if not _is_test_file(c["path"])]
        test_callers = [c for c in cs if _is_test_file(c["path"])]
        lines.append(f"## Likely production callers of `{top['name']}`")
        lines.append("")
        if prod_callers:
            for c in prod_callers:
                caller = c.get("caller_qname") or c.get("caller_name") or "<module>"
                lines.append(f"- `{caller}` — `{c['path']}:{c['line']}`")
        else:
            lines.append("- No production callers found in the static index.")
        lines.append("")
        if test_callers:
            lines.append(f"## Test callers of `{top['name']}`")
            lines.append("")
            for c in test_callers[:8]:
                caller = c.get("caller_qname") or c.get("caller_name") or "<module>"
                lines.append(f"- `{caller}` — `{c['path']}:{c['line']}`")
            lines.append("")

    # Recent changes intersecting these files
    try:
        ch = queries.changed(repo_root, since="HEAD~10")
        recent = [
            c for c in ch.get("changed_files", []) if c["file"] in by_file
        ]
        if recent:
            lines.append("## Recently changed (last 10 commits)")
            lines.append("")
            for r in recent:
                lines.append(f"- `{r['file']}`")
            lines.append("")
    except Exception:
        pass

    lines.append("## Suggested next reads")
    lines.append("")
    lines.append(
        "Read the listed files in order. Do not bulk-read other files until you have "
        "a concrete reason. Use `repotrace callers <symbol>` and "
        "`repotrace impact <file-or-symbol>` to widen the bundle if needed."
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_bundle(repo_root: Path, query: str, out_path: Path | None = None) -> Path:
    """Write a context bundle to `out_path` (or default under .repotrace/context/)."""
    body = build_context(repo_root, query)
    if out_path is None:
        slug = "".join(c if c.isalnum() else "-" for c in query.lower()).strip("-") or "context"
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        out_path = repo_root / ".repotrace" / "context" / f"{ts}-{slug}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    return out_path
