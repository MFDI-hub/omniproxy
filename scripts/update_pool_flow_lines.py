"""Refresh pool-flow line ranges from source AST and doc citations."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "pool-flows"
LISTS = ROOT / "scripts" / "pool-flows"

INIT_SLICE = "omniproxy/pool.py:157-222"  # updated after scan
CONFIG_ENTRY = "omniproxy/config.py"

# Manual flow definitions keyed by stem; values are list of (file, start_name, end_name|None)
# or (file, start, end) int tuples for fixed slices / non-function spans.
FLOW_SPECS: dict[str, list[str | tuple[str, int, int]]] = {}


def parse_file(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def function_ranges(path: Path) -> dict[str, tuple[int, int]]:
    tree = parse_file(path)
    ranges: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    end = getattr(item, "end_lineno", item.lineno)
                    qualified = f"{node.name}.{item.name}"
                    ranges[qualified] = (item.lineno, end)
                    if item.name not in ranges:
                        ranges[item.name] = (item.lineno, end)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            ranges[node.name] = (node.lineno, end)
    return ranges


def rng(path: str, fn: str, *, end_fn: str | None = None) -> str:
    p = ROOT / path
    fr = function_ranges(p)
    if fn not in fr:
        raise KeyError(f"{fn} not found in {path}")
    start, end = fr[fn]
    if end_fn:
        if end_fn not in fr:
            raise KeyError(f"{end_fn} not found in {path}")
        _, end = fr[end_fn]
    return f"{path}:{start}-{end}"


def class_range(path: Path, name: str) -> tuple[int, int]:
    tree = parse_file(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node.lineno, getattr(node, "end_lineno", node.lineno)
    raise KeyError(f"class {name} not found in {path}")


def slice_path(path: str, start: int, end: int) -> str:
    return f"{path}:{start}-{end}"


def build_flow_entries() -> dict[str, list[str]]:
    pool = ROOT / "omniproxy" / "pool.py"
    pfr = function_ranges(pool)
    init_start, init_end = pfr["AsyncProxyPool.__init__"]

    entries: dict[str, list[str]] = {
        "01-async-pool-start": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "AsyncProxyPool.__aenter__"),
            rng("omniproxy/pool.py", "AsyncProxyPool._start"),
            rng("omniproxy/pool.py", "AsyncProxyPool._build_strategy"),
        ],
        "02-sync-pool-start": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "SyncProxyPool.__init__"),
            rng("omniproxy/pool.py", "_daemon_loop_runner"),
            rng("omniproxy/pool.py", "SyncProxyPool._run_on_loop"),
        ],
        "03-acquire": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "AcquireOptions.from_kwargs"),
            rng("omniproxy/pool.py", "AsyncProxyPool.acquire"),
            rng("omniproxy/pool.py", "AsyncProxyPool._select"),
            slice_path("omniproxy/pool.py", pfr["AsyncProxyPool._get_eligible"][0], pfr["AsyncProxyPool._mark_acquired"][1]),
            rng("omniproxy/pool.py", "AsyncProxyPool._check_availability"),
            rng("omniproxy/hooks.py", "run_deferred"),
        ],
        "04-release": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "AsyncProxyPool.release"),
            rng("omniproxy/pool.py", "AsyncProxyPool._return_lease"),
        ],
        "05-mark-success": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "AsyncProxyPool.mark_success"),
            rng("omniproxy/scoring.py", "update_ema"),
            rng("omniproxy/circuit_breaker.py", "record_success"),
        ],
        "06-mark-failed": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "AsyncProxyPool.mark_failed"),
            rng("omniproxy/pool.py", "AsyncProxyPool._apply_cooldown"),
            rng("omniproxy/cooldown.py", "coerce_exception_type"),
            rng("omniproxy/cooldown.py", "compute_cooldown"),
            rng("omniproxy/circuit_breaker.py", "record_failure"),
        ],
        "07-close-drain": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "AsyncProxyPool.__aexit__"),
            rng("omniproxy/pool.py", "AsyncProxyPool._close"),
            rng("omniproxy/pool.py", "SyncProxyPool.close"),
        ],
        "08-refresh-merge": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "AsyncProxyPool._attempt_on_demand_refresh"),
            slice_path(
                "omniproxy/pool.py",
                pfr["AsyncProxyPool._fetch_new_proxies"][0],
                pfr["AsyncProxyPool._merge_new_proxies"][1],
            ),
            rng("omniproxy/pool.py", "AsyncProxyPool._refresh_loop"),
            slice_path(
                "omniproxy/refresh.py",
                function_ranges(ROOT / "omniproxy/refresh.py")["fetch_from_refresh_config"][0],
                function_ranges(ROOT / "omniproxy/refresh.py")["fetch_from_fetchers"][1],
            ),
        ],
        "09-sticky-sessions": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            slice_path("omniproxy/pool.py", pfr["from_kwargs"][0], pfr["from_kwargs"][0] + 2),
            rng("omniproxy/session.py", "resolve_session"),
            slice_path(
                "omniproxy/pool.py",
                pfr["AsyncProxyPool._get_eligible"][0],
                pfr["AsyncProxyPool._mark_acquired"][1],
            ),
        ],
        "10-circuit-breaker": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            slice_path("omniproxy/pool.py", 193, 195),  # breaker construction in __init__
            rng("omniproxy/pool.py", "AsyncProxyPool._check_availability"),
            slice_path(
                "omniproxy/circuit_breaker.py",
                function_ranges(ROOT / "omniproxy/circuit_breaker.py")["record_failure"][0],
                function_ranges(ROOT / "omniproxy/circuit_breaker.py")["allow_request"][1],
            ),
            rng("omniproxy/pool.py", "AsyncProxyPool._apply_check_result"),
        ],
        "11-health-check-loop": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "AsyncProxyPool._health_check_loop"),
            rng("omniproxy/pool.py", "AsyncProxyPool._apply_check_result"),
            rng("omniproxy/pool.py", "_record_health_check_result"),
            rng("omniproxy/extended_proxy.py", "arun_health_check"),
        ],
        "12-warmup": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            # full _start (warmup hooks + RAISE + except → _close)
            rng("omniproxy/pool.py", "AsyncProxyPool._start"),
            rng("omniproxy/warmup.py", "_proxy_counts_as_ready"),
            rng("omniproxy/warmup.py", "run_warmup"),
            rng("omniproxy/pool.py", "AsyncProxyPool._unchecked_proxies"),
            rng("omniproxy/pool.py", "AsyncProxyPool._record_health_check_result"),
        ],
        "13-config-presets": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
        ],
        "14-statistics-metrics": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            slice_path("omniproxy/pool.py", *class_range(pool, "PoolStatistics")),
            rng("omniproxy/pool.py", "AsyncProxyPool.statistics"),
            # counter update sites
            rng("omniproxy/pool.py", "AsyncProxyPool._mark_acquired"),
            rng("omniproxy/pool.py", "AsyncProxyPool.mark_failed"),
            rng("omniproxy/pool.py", "AsyncProxyPool._return_lease"),
            rng("omniproxy/pool.py", "AsyncProxyPool.acquire"),  # exhausted_count
            rng("omniproxy/pool.py", "AsyncProxyPool._apply_check_result"),  # health → failed
            # metrics worker lifecycle + enqueue (incl. QueueFull)
            rng("omniproxy/pool.py", "AsyncProxyPool._spawn_background_tasks"),
            rng("omniproxy/pool.py", "AsyncProxyPool._stop_background_tasks"),
            slice_path(
                "omniproxy/pool.py",
                pfr["AsyncProxyPool._metrics_worker"][0],
                pfr["AsyncProxyPool._enqueue_metric"][1],
            ),
            rng("omniproxy/hooks.py", "run_deferred"),
        ],
        "15-dead-letter": [
            slice_path("omniproxy/pool.py", init_start, init_end),
            CONFIG_ENTRY,
            rng("omniproxy/pool.py", "AsyncProxyPool._spawn_background_tasks"),
            rng("omniproxy/pool.py", "AsyncProxyPool._evict_proxy"),
            rng("omniproxy/pool.py", "AsyncProxyPool._dead_letter_retrier"),
            "omniproxy/dead_letter.py",
        ],
    }

    # Fix flow 09 - from_kwargs session_id lines are inside from_kwargs, use explicit search
    src_lines = pool.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(src_lines, 1):
        if 'session_key" not in filters and "session_id" in filters' in line:
            entries["09-sticky-sessions"][2] = slice_path("omniproxy/pool.py", i, i + 2)
            break

    # Fix flow 10 circuit breaker construction lines
    for i, line in enumerate(src_lines, 1):
        if "self._circuit_breaker = (" in line:
            entries["10-circuit-breaker"][2] = slice_path("omniproxy/pool.py", i, i + 2)
            break

    return entries


CITATION_RE = re.compile(r"^```(\d+):(\d+):([^\s]+)$")


def code_slice_entries(new_entries: list[str], init_entry: str) -> list[str]:
    """Slices referenced by ## Code fences (skip __init__ + full config file)."""
    skipped_init = False
    out: list[str] = []
    for entry in new_entries:
        if entry == CONFIG_ENTRY:
            continue
        if not skipped_init and entry == init_entry:
            skipped_init = True
            continue
        if ":" in entry:
            out.append(entry)
    return out


def sync_doc_code_citations(md_path: Path, new_entries: list[str], init_entry: str) -> None:
    """Update ```start:end:path fences in the Code section from slice entries."""
    from collections import defaultdict

    slices = code_slice_entries(new_entries, init_entry)
    by_path: dict[str, list[str]] = defaultdict(list)
    for entry in slices:
        path = entry.split(":", 1)[0]
        by_path[path].append(entry)

    counters: dict[str, int] = defaultdict(int)
    lines = md_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_code_section = False
    for line in lines:
        if line.startswith("## Code"):
            in_code_section = True
        elif line.startswith("## ") and not line.startswith("## Code"):
            in_code_section = False
        m = CITATION_RE.match(line)
        if in_code_section and m:
            path = m.group(3)
            idx = counters[path]
            if idx < len(by_path[path]):
                entry = by_path[path][idx]
                start, end = entry.split(":", 1)[1].split("-", 1)
                out.append(f"```{start}:{end}:{path}")
                counters[path] += 1
                continue
        out.append(line)
    md_path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")

def main() -> None:
    entries = build_flow_entries()

    # Build citation mapping from old txt files to new entries (by position per flow)
    for stem, new_entries in entries.items():
        list_path = LISTS / f"{stem}.txt"
        md_path = DOCS / f"{stem}.md"

        header = [
            f"# Context dump slices for pool flow {stem}",
            f"# context-dump --file-list scripts/pool-flows/{stem}.txt --once",
        ]
        list_path.write_text(
            "\n".join([*header, *new_entries]) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print("list", stem, "->", len(new_entries), "entries")

        if md_path.is_file():
            init_entry = new_entries[0]
            sync_doc_code_citations(md_path, new_entries, init_entry)
            # refresh Context dump section
            text = md_path.read_text(encoding="utf-8")
            block = "\n".join(new_entries)
            section = (
                "## Context dump\n\n"
                f"Slices for `context-dump` (also in "
                f"[`scripts/pool-flows/{stem}.txt`](../../scripts/pool-flows/{stem}.txt)):\n\n"
                f"```text\n{block}\n```\n\n"
                f"```bash\n"
                f"context-dump --file-list scripts/pool-flows/{stem}.txt --once\n"
                f"```\n"
            )
            start = text.find("## Context dump")
            if start != -1:
                end = None
                for marker in ("## Related ADR", "## Notes"):
                    idx = text.find(f"\n{marker}", start)
                    if idx != -1:
                        end = idx + 1
                        break
                if end is not None:
                    text = text[:start] + section + "\n" + text[end:]
                    md_path.write_text(text, encoding="utf-8", newline="\n")
            print("doc", stem)

    # rebuild all.txt
    all_lines = [
        "# Combined context dump slices for all pool flows",
        "# context-dump --file-list scripts/pool-flows/all.txt --once",
        "",
    ]
    for stem in sorted(entries):
        all_lines.append(f"# --- {stem} ---")
        all_lines.extend(entries[stem])
        all_lines.append("")
    (LISTS / "all.txt").write_text("\n".join(all_lines).rstrip() + "\n", encoding="utf-8", newline="\n")
    print("rebuilt all.txt")


if __name__ == "__main__":
    main()
