# repotrace

Local-first code intelligence for coding agents and CLI workflows.

`repotrace` builds a lightweight SQLite index of a repository so agents can find the right files, understand Python symbols/imports/calls/routes, detect architecture risks, and generate compact context bundles without sending code to an external service.

It works with any coding agent that can run shell commands. The original workflow uses it with [pi](https://github.com/mariozechner/pi-coding-agent) agents, but there is no pi-specific runtime dependency.

## Why repotrace?

Coding agents — including pi agents, Claude Code, Codex-style CLIs, Cursor agents, and custom scripts — are much better when they start with a small, relevant map instead of bulk-reading a repository. `repotrace` provides that map locally:

- no telemetry
- no server
- no graph database
- no editor auto-install
- inspectable SQLite index under `.repotrace/`
- JSON output for agents and automation

## Install

### With uv

```bash
uv tool install git+https://github.com/niclejeune/repotrace.git
```

### From a local checkout

```bash
git clone https://github.com/niclejeune/repotrace.git
cd repotrace
uv tool install --editable .
```

After install, `repotrace` should be available on your `PATH`:

```bash
repotrace --help
```

## Quick start

```bash
cd /path/to/your/repo
repotrace index .
repotrace overview
repotrace context "candidate scoring bug"
```

Common commands:

```bash
repotrace find score_candidate
repotrace symbol score_candidate
repotrace callers score_candidate         # strict by default
repotrace callers score_candidate --broad # include method/base-name matches
repotrace callees score_candidate
repotrace file services/scoring.py
repotrace impact services/scoring.py
repotrace routes
repotrace deps
repotrace cycles
repotrace check
repotrace changed --since main
```

Use global `--json` before the subcommand for machine-readable output:

```bash
repotrace --json check --no-fail
repotrace --json context "rerank flow" --print
```

## Generated files

`repotrace` stores local artifacts in the repository being indexed:

```text
.repotrace/
  index.sqlite           # local structural index
  context/               # generated markdown context bundles
```

Add `.repotrace/` to `.gitignore` in projects that use it.

## Context bundles

```bash
repotrace context "rerank flow"
```

A context bundle is a short markdown file that separates:

- relevant production symbols
- relevant tests
- files to read first
- likely production callers
- test callers
- suggested next commands

This is designed for coding agents. In a pi-agent workflow, the agent should run `repotrace context "task"`, read the bundle first, then read only the listed files unless there is a concrete reason to widen scope.

## Python support

Current Python indexing extracts:

- files and line counts
- classes, functions, methods, async functions, nested functions
- imports, including relative import levels
- normalized call targets
- FastAPI/Flask-style route decorators
- git last-commit metadata when available

TypeScript/JavaScript support is planned, but not part of the current parser.

## Architecture checks

`repotrace check` currently flags:

- Python import cycles (`error`)
- production files importing test files (`error`)
- large production files over `--max-lines` (`warning`, default `1000`)
- high fan-in production modules (`warning`, imported by `>=20` files)

It exits nonzero when errors are present. Use `--no-fail` for report-only runs:

```bash
repotrace check --no-fail
repotrace --json check --no-fail
```

## Dependency graph commands

```bash
repotrace deps
repotrace cycles
```

`deps` summarizes the Python import graph and most-imported modules. `cycles` reports directed Python import cycles.

## Optional: pair with a second brain

`repotrace` is for local code structure, not long-term project memory. If you want an Obsidian/Markdown-style context vault for durable notes and agent handoffs, see:

- <https://github.com/niclejeune/second-brain-starter>

A typical workflow is:

1. Use `repotrace context "task"` to find the right code files.
2. Use your editor/agent to make the change.
3. Save durable project decisions or recurring lessons in a separate Markdown vault.
4. Do **not** commit `.repotrace/` artifacts unless you intentionally want to share them.

## Non-goals

- hosted service
- telemetry
- embeddings
- graph database
- MCP server
- watch daemon
- editor auto-install
- perfect multi-language call resolution

## License

MIT
