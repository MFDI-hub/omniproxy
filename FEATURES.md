# omniproxy — feature review

**Scope:** Python package for parsing proxy strings, checking them through pluggable HTTP clients, optional anonymity/geo enrichment, file/URL ingestion, and pool-based selection with cooldowns, filtering, and optional refresh.

**Doc shape:** Overview → core types → parser struct → execution surface (backends, checks, I/O) → orchestration → CLI → configuration → public exports. Bullets are the contract a reviewer should trace against the code.

---

## 1. Core model (`Proxy`)

- **Type:** Subclass of `str` with fixed structural fields in `__slots__`, plus metadata. Structural fields are read-only after construction; metadata is updated via internal helpers (e.g. after checks).
- **Construction:** `Proxy(raw, /, protocol=None)` — parse a string or reuse a `Proxy` instance; optional `protocol` override (`http` | `https` | `socks4` | `socks5`). Existing `Proxy` + same protocol returns the same instance (identity fast path); protocol change rebuilds via `OmniproxyParser(**dumped_data)` then canonical formatting.
- **Parsing:** Implemented by `OmniproxyParser` + `constants.PROXY_FORMATS_REGEXP`: URL forms (`protocol://…`), `user:pass@host:port`, `host:port:user:pass`, `host:port|user:pass`, user/pass/host/port permutations, optional `rotation_url` in trailing `[url]`, bracketed IPv6, optional scheme on bare `host:port`.
- **Structural:** `ip`, `port`, `username`, `password`, `protocol`, `rotation_url`.
- **Metadata:** `latency`, `anonymity`, `last_checked` (epoch), `last_status` (`bool` after check).
- **Derived / convenience:** `url` (canonical string), `safe_url` (masked password), `host` (`ip:port`), `address` (alias of `ip`), `login` (alias of `username`), `server` (`protocol://ip:port` with `http` fallback), `has_auth`, `is_working` (`last_status` if set, else latency heuristic).
- **Interop:** `as_requests_proxies()`, `playwright` (`PlaywrightProxySettings`), `to_dict()`, `to_json_string()`.
- **IP:** `version` → `4` | `6` | `None` (hostname).
- **Rotation:** `rotate()` / `arotate()` — direct HTTP call to `rotation_url` (no proxy), GET/POST, backend + timeout; success = HTTP 200.
- **Formatting:** `ProxyPattern` tokens (`protocol`, `username`, `password`, `ip`, `port`, `rotation_url`); `get_formatted_proxy_string` in `utils`; `Proxy.set_default_pattern(...)`.
- **Validation / persistence:** `validate(v)` wraps `cls(v)` and raises `ValueError`; no Pydantic adapter in-tree (legacy `adapter` import is commented). `__reduce__` / `__setstate__` for pickle (metadata in state).
- **Equality / hashing:** By canonical `url`.
- **Extended surface (subclass in `extended_proxy`):** `.check()` / `.acheck()`, `.get_info()` / `.aget_info()` (wrappers around `check_proxy` / `acheck_proxy` with `with_info=True`), `.get_client()` → sync `httpx.Client`, `.get_async_client()` → `httpx.AsyncClient` (proxy-mounted transports).

---

## 2. Parser model (`OmniproxyParser`, `omniproxy.utils`)

- **Type:** `msgspec.Struct` mirroring structural fields before `Proxy` string canonicalization.
- **Regex dispatch:** Module helper `_proxy_format_groupdict(stripped)` returns the first `match.groupdict()` across `PROXY_FORMATS_REGEXP`, or `None`.
- **`from_string(proxy_string)`** — strip input, require a format match, then **`from_match(groupdict)`** (single construction path; protocol allow-list enforced there).
- **`from_match(groups)`** — build struct from a regex `groupdict` (`url` → `rotation_url`); unsupported protocol raises `ValueError`.
- **`batch_parse(lines)`** — each non-empty stripped line uses the same `_proxy_format_groupdict` + **`from_match`** path as `from_string` (no duplicate protocol/body logic).
- **`__post_init__`:** Port range; IP vs hostname (reject bogus dotted-numeric “IPs”); normalize bracketed IPv6; validate `rotation_url` if set (`scheme` + `netloc`).
- **`get_formatted_proxy_string(proxy, pattern)`** — render `Proxy` or `OmniproxyParser` with a `ProxyPattern` / template string (token replacement + optional-field collapse).

---

## 3. HTTP backends

- **Registry:** `get_backend(name)`, `supported_backends()`.
- **Implementations:** `httpx`, `aiohttp`, `requests`, `curl_cffi`, `tls_client` — unified `get` / `aget` for proxied checks; `request_direct` / `arequest_direct` for rotation URLs (no proxy).
- **Response shape:** Normalized `BackendResponse` (`status_code`, headers, `json_data`, `text`) — checks and anonymity probe consume this.

---

## 4. Checking & metadata

- **Entry points:** `check_proxy`, `acheck_proxy`, `check_proxies`, `acheck_proxies`.
- **URLs:** Default reachability URL from `settings.default_check_url`; with `with_info=True`, URL built from `settings.default_check_info_url_template` and `fields` (ip-api-style field mask, default in code).
- **Options:** Custom `url`, `backend`, `timeout`, `detect_anonymity`, `raise_on_error`, `with_info` (return `dict` payload vs `bool`), `max_retries`, `retry_backoff`, `retry_on_status` (defaults include 502/503/504).
- **Bulk:** `check_proxies(..., use_async=True)` runs `asyncio.run(acheck_proxies(...))`; `use_async=False` uses `ThreadPoolExecutor` with `max_workers` (default capped heuristic).
- **Anonymity:** Extra GET to a headers echo endpoint; classify `transparent` | `anonymous` | `elite` from forwarded/proxy headers.
- **Metadata application:** `apply_check_result_metadata(proxy, latency=, anonymity=, status=)` — sets latency, `last_checked`, `last_status`, optional anonymity.
- **`CheckResult`:** Dataclass returned from `.check()` / `.acheck()` on extended `Proxy` (`success`, `latency`, `exc_type`, `status_code`).

---

## 5. File & network I/O

- **`read_proxies(path, encoding=, on_invalid=raise|skip, errors_out=)`** — line-based `Proxy` construction; optional per-line error capture.
- **`iter_proxies_from_file(...)`** — same semantics, generator for large files.
- **`save_proxies(path, iterable, mode=, encoding=)`** — newline-separated export.
- **`fetch_proxies(url, timeout=, pattern=, unique=, user_agent=, headers=)`** — direct HTTPS fetch, regex extraction (default pattern prefers explicit schemes + conservative host:port lines), dedupe by URL when `unique=True`.

---

## 6. `ProxyPool`, `PoolConfig`, and pool errors

- **Selection:** `get_next(**filters)` / `aget_next(**filters)` — `round_robin` or `random` (random forces list-backed storage when config would use `deque`). Optional **attribute filters:** keyword args match `getattr(proxy, name) == value` for each filter (e.g. `protocol=`); **`min_anonymity`** compares against `ANONYMITY_RANKS` (`transparent` < `anonymous` < `elite`).
- **Exceptions:** `PoolExhausted` (no active proxies / refresh wait timeout), `NoMatchingProxy` (filters exclude everyone), `PoolSaturated` (all matching URLs at `max_connections_per_proxy`), `MissingProxyMetadata` (filter needs metadata and `filter_missing_metadata="raise"`).
- **`PoolConfig` (`omniproxy.config`, also listed in `omniproxy.pool.__all__`):** `strategy`, `structure` (`deque` | `list`), `cooldown`, `failure_threshold`, `failure_penalties` (`exc_type` → cooldown multiplier), `max_connections_per_proxy`, `on_saturated`, `on_exhausted`, `refresh_callback`, `arefresh_callback`, `refresh_timeout`, `filter_missing_metadata` (`skip` | `raise` | `include`), context flags `auto_mark_failed_on_exception`, `auto_mark_success_on_exit`, `reraise`, `extra`, optional nested **`HealthCheckConfig`** (`url`, `headers`, `expected_status`, `expected_fields`, `timeout`, `strategy`, `recovery_interval`, `ttl`).
- **Accounting:** `mark_failed(proxy, exc_type=None)` (threshold → cooldown remove from active; penalty from `failure_penalties`); `mark_success`; cooldown expiry restores prototypes into the active deque/list.
- **Context managers:** Sync `with pool:` uses thread-local slot + slot release; async `async with pool:` uses `ContextVar` per task — optional auto fail/success marks on exit.
- **Compatibility:** `acquire()` / `aacquire()` return self (nested context pattern). Constructor still accepts `strategy=` and `cooldown=` as shorthand over `config`.
- **Mutation / introspection:** `reset()`; `len`, iteration snapshot, `in` (by resolved proxy URL); `repr` shows active/cooling counts, strategy, structure, cooldown.

---

## 7. CLI (`omniproxy`)

- **`check <file>`:** `--backend`, `--timeout`, `--anonymity`, `-o` / `--output-good`; **async by default**, `--sync` for threaded sync path.
- **`scrape <url>`:** `-o` / `--output`, `--timeout`.

---

## 8. Global configuration (`settings`)

- Thread-safe singleton (`OmniproxyConfig` in `omniproxy.config`): `default_backend`, `default_timeout`, `default_connect_timeout` (optional), `default_check_url`, `default_check_info_url_template` (must include `{fields}` placeholder for info checks).

---

## 9. Package exports (reviewer checklist)

- **Root `omniproxy`:** `Proxy`, `ProxyPattern`, `PlaywrightProxySettings`, `ProxyPool`, `CheckResult`, `check_*`, `acheck_*`, `apply_check_result_metadata`, `read_proxies`, `save_proxies`, `iter_proxies_from_file`, `fetch_proxies`, `settings`, `get_backend`, `supported_backends`, **`MissingProxyMetadata`**, **`NoMatchingProxy`**, **`PoolExhausted`**, **`PoolSaturated`**.
- **`omniproxy.pool`:** `ProxyPool`, `PoolConfig`, `ANONYMITY_RANKS` (same ranks as `constants`).
- **`omniproxy.config`:** `OmniproxyConfig`, `settings`; **`PoolConfig`**, **`HealthCheckConfig`**, strategy/structure literals live here — import even though `config.__all__` only documents the singleton API surface.
- **`omniproxy.utils`:** `OmniproxyParser`, `get_formatted_proxy_string`, `ALLOWED_PROTOCOLS` — use when parsing without building `Proxy`, or for benchmarks/tests.
- **Lazy / optional:** Top-level `Client` and `AsyncClient` (httpx wrappers) resolved on attribute access.
- **Internal (typically omitted from user docs):** `constants` (compiled patterns), `adapter` (absent / commented), `cli` wiring.

---

*This file is a living map from user-facing behavior to modules (`proxy.py`, `utils.py`, `extended_proxy.py`, `backends/*`, `io.py`, `pool.py`, `config.py`, `errors.py`, `constants.py`, `cli.py`). Update when public API or defaults change.*
