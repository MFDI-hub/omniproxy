# ADR-0014: CLI design and integration principles

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** cli, architecture

## Context

The library ships a small CLI (`omniproxy check`, `omniproxy scrape`) described as “for ops”. As the feature set grows, there is pressure to add more commands (watch, scheduled refresh, metrics export). Without a design principle, business logic could leak into the CLI, or the CLI could introduce server‑like features that violate ADR‑0001.

## Decision

1. **The CLI is a thin wrapper**: it parses arguments, instantiates objects using the library’s public API, calls library functions, and formats output. No business logic lives inside `cli.py` or its supporting modules.

2. **Every CLI workflow is a library function first**: e.g. `check_proxies` and `fetch_proxies` are callable from Python; the CLI simply provides argument parsing and output formatting on top.

3. **Output formats**: the CLI supports plain text (default) and JSON (via a `--json` flag). Structured output uses `orjson` (already a hard dependency). No pretty‑printer framework is added; the formatting stays minimal.

4. **No `omniproxy serve`**: a long‑running process that accepts inbound traffic or acts as a daemon is explicitly out of scope, reinforcing ADR‑0001.

5. **CLI‑only dependencies** (e.g. `click`, `rich`) are optional extras that the main library does not import. The CLI may ship as a separate entry point that fails gracefully with an install hint if the extra is missing.

## Consequences

- **Positive:** Library stays free of CLI‑framework dependencies; all workflows are scriptable without the CLI; scope boundary is enforced.
- **Negative:** The CLI stays minimal; users who want rich TUI or daemon features must build their own.
- **Neutral:** CLI argument names mirror library parameter names closely, serving as informal documentation.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| Rich, multi‑command CLI with progress bars | Adds heavy dependencies and encourages business logic in the CLI. |
| No CLI at all | Loses quick operational checks and the scraping helper that many users rely on. |
| CLI that duplicates library logic | Maintenance fork; bugs fixed in one place but not the other. |

## Evidence

- `omniproxy/cli.py` (argparse‑based entry points)
- `omniproxy/io.py` (scrape helpers reused by CLI)
- `pyproject.toml` (`[project.scripts]` and optional‑dependency `cli`)