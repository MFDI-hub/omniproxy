#!/usr/bin/env python3
"""
context_dump.py

Watch source files/directories and continuously dump them to one text file.

Default: read paths from scripts/omniproxy_files.txt and write
outputs/builds/omniproxy_dump.txt.

Line ranges (1-based, inclusive) are supported on file entries:
  path/to/file.py:10-50
  path/to/file.py:10:50
  path/to/file.py:10        # from line 10 through EOF

Usage:
  uv run python scripts/context_dump.py --once
  uv run python scripts/context_dump.py
  uv run python scripts/context_dump.py --file-list scripts/omniproxy_files.txt --once
  uv run python scripts/context_dump.py --file-list scripts/pool-flows/03-acquire.txt --once
  uv run python scripts/context_dump.py omniproxy/pool.py:552-671 --once
  uv run python scripts/context_dump.py omniproxy --ext py,md --once

inside virtual environment:
  context-dump --once
  context-dump --file-list scripts/pool-flows/03-acquire.txt --once
  context-dump --all-flows --once
  context-dump omniproxy/pool.py:157-221 --once

"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `uv run python scripts/context_dump.py` without PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts._lib.bootstrap import load_env, resolve_path
from scripts._lib.chosen_file_sync import SyncConfig, resolve_source_specs, run_watch

DEFAULT_FILE_LIST = "scripts/omniproxy_files.txt"
DEFAULT_CODE_OUT = "outputs/builds/omniproxy_dump.txt"
DEFAULT_POOL_FLOW_LISTS = "scripts/pool-flows"
DEFAULT_POOL_FLOW_OUT_DIR = "outputs/builds/pool-flows"
DEFAULT_EXT = "py,md"


def _pool_flow_list_paths(lists_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in lists_dir.glob("*.txt")
        if path.is_file() and path.name != "all.txt"
    )


def _sync_all_pool_flows(
    *,
    lists_dir: Path,
    out_dir: Path,
    mode: str,
    code_globs: tuple[str, ...],
    dedupe: bool,
    sort_longest: bool,
    include_header: bool,
) -> int:
    from scripts._lib.chosen_file_sync import SyncConfig, SyncState, sync_once

    list_paths = _pool_flow_list_paths(lists_dir)
    if not list_paths:
        raise SystemExit(f"No pool-flow lists found in {lists_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    total_lines = 0
    for list_path in list_paths:
        source_specs = resolve_source_specs([], file_list=list_path)
        out_path = out_dir / f"{list_path.stem}.txt"
        config = SyncConfig(
            source_specs=source_specs,
            out_path=out_path,
            mode=mode,
            code_globs=code_globs,
            dedupe=dedupe,
            sort_longest=sort_longest,
            include_header=include_header,
        )
        state = SyncState()
        sync_once(config, state, force=True)
        total_lines += state.last_line_count
        print(
            f"wrote {state.last_line_count} lines from {len(state.resolved_specs)} slice(s) "
            f"-> {out_path}"
        )

    print(f"done: {len(list_paths)} flow dump(s), {total_lines} total lines -> {out_dir}")
    return 0


def main() -> int:
    load_env()

    ap = argparse.ArgumentParser(
        description=(
            "Continuously sync source code (or other files) into one text dump. "
            "File entries may include line ranges: path:start-end."
        )
    )
    ap.add_argument(
        "paths",
        nargs="*",
        default=None,
        help="Files or directories to watch (path or path:start-end; default: use --file-list)",
    )
    ap.add_argument(
        "--dir",
        action="append",
        default=[],
        metavar="PATH",
        help="Directory to watch recursively (repeatable; no line ranges)",
    )
    ap.add_argument(
        "--file-list",
        default=None,
        help=f"Text file with one path[/range] per line (default: {DEFAULT_FILE_LIST})",
    )
    ap.add_argument(
        "--out",
        default=DEFAULT_CODE_OUT,
        help=f"Output text file (default: {DEFAULT_CODE_OUT})",
    )
    ap.add_argument(
        "--all-flows",
        action="store_true",
        help=(
            "Dump each scripts/pool-flows/*.txt list to its own file under "
            f"{DEFAULT_POOL_FLOW_OUT_DIR}/ (implies --once)"
        ),
    )
    ap.add_argument(
        "--flows-dir",
        default=DEFAULT_POOL_FLOW_LISTS,
        help=f"Directory of per-flow list files (default: {DEFAULT_POOL_FLOW_LISTS})",
    )
    ap.add_argument(
        "--out-dir",
        default=DEFAULT_POOL_FLOW_OUT_DIR,
        help=f"Output directory for --all-flows (default: {DEFAULT_POOL_FLOW_OUT_DIR})",
    )
    ap.add_argument(
        "--mode",
        choices=("code", "concat", "lines", "phrases", "tsv-phrases", "tsv-approved"),
        default="code",
        help="code=full source dump (default), concat=raw bodies, lines/tsv*=data modes",
    )
    ap.add_argument(
        "--ext",
        default=DEFAULT_EXT,
        help=f"Comma-separated extensions for directory scan (default: {DEFAULT_EXT})",
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Poll interval in seconds (default: 2)",
    )
    ap.add_argument(
        "--no-dedupe",
        action="store_true",
        help="Keep duplicate lines (data modes only)",
    )
    ap.add_argument(
        "--sort-longest",
        action="store_true",
        help="Sort output longest-first (data modes only)",
    )
    ap.add_argument(
        "--no-header",
        action="store_true",
        help="Omit header block from output",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="Write one dump and exit (default: watch and re-sync)",
    )
    args = ap.parse_args()

    code_globs = tuple(f"*.{ext.strip().lstrip('.')}" for ext in args.ext.split(",") if ext.strip())

    if args.all_flows:
        return _sync_all_pool_flows(
            lists_dir=resolve_path(args.flows_dir),
            out_dir=resolve_path(args.out_dir),
            mode=args.mode,
            code_globs=code_globs or ("*.py", "*.md"),
            dedupe=not args.no_dedupe,
            sort_longest=args.sort_longest,
            include_header=not args.no_header,
        )

    file_list = args.file_list
    if args.paths:
        watch_paths = args.paths
    elif file_list:
        watch_paths = []
    else:
        # Default: chosen file list only.
        file_list = DEFAULT_FILE_LIST
        watch_paths = []

    source_specs = resolve_source_specs(watch_paths, dirs=args.dir, file_list=file_list)

    missing = [spec.path for spec in source_specs if not spec.path.exists()]
    for path in missing:
        print(f"warning: not found yet (will watch for it): {path}")

    config = SyncConfig(
        source_specs=source_specs,
        out_path=resolve_path(args.out),
        interval=args.interval,
        mode=args.mode,
        code_globs=code_globs or ("*.py", "*.md"),
        dedupe=not args.no_dedupe,
        sort_longest=args.sort_longest,
        include_header=not args.no_header,
    )
    if args.once:
        from scripts._lib.chosen_file_sync import SyncState, sync_once

        state = SyncState()
        sync_once(config, state, force=True)
        print(
            f"wrote {state.last_line_count} lines from {len(state.resolved_specs)} slice(s) "
            f"-> {config.out_path}"
        )
        return 0
    return run_watch(config)


if __name__ == "__main__":
    raise SystemExit(main())
