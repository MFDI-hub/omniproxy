# omniproxy — feature review

**Scope:** Python package for parsing proxy strings, checking them through pluggable HTTP clients, optional anonymity and optional geo/ASN metadata, list-based default check URLs with retry rotation, file/URL ingestion, and **sync or asyncio proxy pools** with cooldowns, filtering, optional per-proxy RPS limiting, optional refresh, optional lifecycle hooks, and optional background health monitoring.

**Doc shape:** Overview → core types → parser struct → execution surface (backends, checks, I/O) → orchestration → CLI → configuration → public exports. Bullets are the contract a reviewer should trace against the code.

---

## 1. Core model (`Proxy`)

- **Type:** Subclass of `str` with fixed structural fields in `__slots__`, plus metadata. Structural fields are read-only after construction; metadata is updated via internal helpers (e.g. after checks).
- **Construction:** `Proxy(raw, /, protocol=None)` — parse a string or reuse a `Proxy` instance; optional `protocol` override (`http` | `https` | `socks4` | `socks5`). Existing `Proxy` + same protocol returns the same instance (identity fast path); protocol change rebuilds via `OmniproxyParser(**dumped_data)` then canonical formatting.
- **Parsing:** Implemented by `OmniproxyParser` + `constants.PROXY_FORMATS_REGEXP`: URL forms (`protocol://…`), `user:pass@host:port`, `host:port:user:pass`, `host:port|user:pass`, user/pass/host/port permutations, optional `rotation_url` in trailing `[url]`, bracketed IPv6, optional scheme on bare `host:port`.
- **Structural:** `ip`, `port`, `username`, `password`, `protocol`, `rotation_url`.
- **Metadata:** `latency`, `anonymity`, `last_checked` (epoch), `last_status` (`bool` after check), plus optional enrichment fields `country`, `city`, `asn`, `org` (set via `apply_check_result_metadata` or your own pipeline; included in `to_dict()` / pickle when not `None`).
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

- **Entry points:** `check_proxy`, `acheck_proxy`, `check_proxies`, `acheck_proxies`; **`run_health_check`** / **`arun_health_check`** (`extended_proxy`) drive checks from a `HealthCheckConfig` (used by the pool health loop and callable directly).
- **URLs (defaults):** `settings.default_check_urls` — non-empty list of reachability URLs; each default check picks one via `random.choice`, and **retries** prefer a **different** URL when the list has more than one entry. With `with_info=True`, templates come from `settings.default_check_info_url_templates` (each must support `{fields}` for the ip-api-style field mask). A custom per-call `url=` bypasses list rotation for that invocation.
- **Health-check URL resolution:** For `run_health_check` / `arun_health_check`, if `HealthCheckConfig.url` is unset, the URL is `random.choice(settings.health_check_urls or settings.default_check_urls)` so monitoring can use a dedicated list without changing normal checks.
- **Options:** Custom `url`, `backend`, `timeout`, `detect_anonymity`, `raise_on_error`, `with_info` (return `dict` payload vs `bool`), `max_retries`, `retry_backoff`, `retry_on_status` (defaults include 502/503/504).
- **Bulk:** `check_proxies(..., use_async=True)` runs `asyncio.run(acheck_proxies(...))`; `use_async=False` uses `ThreadPoolExecutor` with `max_workers` (default capped heuristic).
- **Anonymity:** Extra GET to a headers echo endpoint; classify `transparent` | `anonymous` | `elite` from forwarded/proxy headers.
- **Metadata application:** `apply_check_result_metadata(proxy, latency=, anonymity=, status=, country=, city=, asn=, org=)` — sets latency, `last_checked`, `last_status`, optional anonymity and optional geo/ASN strings when provided.
- **`CheckResult`:** Dataclass returned from `.check()` / `.acheck()` on extended `Proxy` (`success`, `latency`, `exc_type`, `status_code`).

---

## 5. File & network I/O

- **`read_proxies(path, encoding=, on_invalid=raise|skip, errors_out=)`** — line-based `Proxy` construction; optional per-line error capture.
- **`iter_proxies_from_file(...)`** — same semantics, generator for large files.
- **`save_proxies(path, iterable, mode=, encoding=)`** — newline-separated export.
- **`fetch_proxies(url, timeout=, pattern=, unique=, user_agent=, headers=)`** — direct HTTPS fetch, regex extraction (default pattern prefers explicit schemes + conservative host:port lines), dedupe by URL when `unique=True`.

---

## 6. Pools (`SyncProxyPool`, `AsyncProxyPool`, `BaseProxyPool`, protocols, `HealthMonitor`, `PoolConfig`, errors)

- **Two concrete pools:** **`SyncProxyPool`** — threading `Lock` + `threading.Condition` for `get_next` / `with pool:` / `close()`. **`AsyncProxyPool`** — same `Lock` for shared state with lazily created **`asyncio.Lock`**, **`asyncio.Condition`**, and **`asyncio.Event`** (refresh serialization) bound to the consumer event loop; **`await aget_next()`** / **`async with pool:`** / **`await aclose()`**. Both subclass **`BaseProxyPool`** (shared `_PoolState`, prototypes, per-URL in-flight counts, `weakref.finalize` to clear connection counters if a pool is garbage-collected without `close` / `aclose`).
- **Protocols (exported from `omniproxy.pool`):** **`BasePoolProtocol`**, **`MonitorablePoolProtocol`** (`is_closed`, `cooling_proxies`), **`SyncPoolProtocol`**, **`AsyncPoolProtocol`** — structural typing for pool consumers.
- **Keys / membership:** Internal identity is canonical **`Proxy.url`** (`_PoolState._nolock_key`). **`__contains__`** coerces non-`Proxy` items through **`Proxy(...)`** (then matches active keys after cooldown purge).
- **Selection:** **`get_next(**filters)`** / **`aget_next(**filters)`** — `round_robin` or `random` (random forces **`list`** backing when config would use **`deque`**). Optional **attribute filters:** keyword args match `getattr(proxy, name) == value`; **`min_anonymity`** uses `ANONYMITY_RANKS` (`transparent` < `anonymous` < `elite`). Filter results are cached per filter key until state dirties (`mark_failed` / merge / reset).
- **Exceptions:** **`PoolClosedError`** (pool closed or operation after `close` / `aclose`), **`PoolExhausted`** (no active proxies, or refresh wait timed out), **`NoMatchingProxy`**, **`PoolSaturated`** (matching proxies blocked by **`max_connections_per_proxy`** and/or **`max_rps_per_proxy`**), **`MissingProxyMetadata`** (when `filter_missing_metadata="raise"`).
- **`PoolConfig` (`omniproxy.config`, re-exported in `omniproxy.pool.__all__`):** `strategy`, `structure` (`deque` | `list`), `cooldown`, `failure_threshold`, `failure_penalties` (`exc_type` → cooldown multiplier), `max_connections_per_proxy`, **`max_rps_per_proxy`** (lazy **`TokenBucket`** per URL on **successful selections**), **`acquire_timeout`** with **`wait_fallback_interval`** for bounded waits when saturated, `on_saturated`, `on_exhausted`, **`refresh_callback`** (sync) / **`arefresh_callback`** (async), **`refresh_timeout`** (sync: `threading.Event.wait`; async: `asyncio.wait_for` on the refresh event), `filter_missing_metadata` (`skip` | `raise` | `include`), context flags `auto_mark_failed_on_exception`, `auto_mark_success_on_exit`, `reraise`, `extra`, optional nested **`HealthCheckConfig`**.
- **Refresh on exhaustion:** If selection raises **`PoolExhausted`**, optional **`on_exhausted`** runs, then when **`refresh_callback`** / **`arefresh_callback`** is set the pool merges returned strings or **`Proxy`** instances via **`_merge_refreshed_proxies`** (dedupe by URL against prototypes; re-adds if not cooling) and retries selection once.
- **Lifecycle hooks (`PoolConfig`):** `on_proxy_acquired` / `on_proxy_released`, `on_proxy_failed` / `on_proxy_cooled_down`, `on_proxy_recovered` (after **`mark_success`** when failures were > 0, and when cooldown purge restores proxies), **`on_check_complete`** (per health-check result).
- **`HealthMonitor` (class, exported):** Holds a **`weakref.ref`** to the pool. **`run()`** loop: builds ordered active+cooling snapshot keyed by URL, **`asyncio.gather`** of **`arun_health_check`**, invokes **`on_check_complete`**, then **`mark_success`** / **`mark_failed`**. Stops on **`PoolClosedError`** or cancellation; logs if the pool was GC’d without close. **`AsyncProxyPool`:** **`start_monitoring()`** requires a **running asyncio loop** (idempotent on same loop; error if a task already runs on another loop). **`async with pool.monitoring():`** starts then **`stop_monitoring()`**. **`SyncProxyPool`:** **`start_monitoring_thread()`** — daemon thread, dedicated loop, internal ready **`threading.Event`** (waits up to 5s); **`RuntimeError`** if in-loop monitoring is already active on the current loop. **`stop_monitoring()`** cancels async task and/or stops the health thread and joins. **`close()`** (sync) sets closed, **`notify_all`** on the sync condition, then **`stop_monitoring()`** — does **not** call **`reset_pool`**. **`aclose()`** (async) documents that the final **`notify_all`** for blocked async waiters may run **after** the coroutine returns.
- **Accounting:** **`mark_failed` / `mark_success`** require an open pool; sync path notifies the threading condition on cooldown; async path calls **`_notify_async_condition`** (schedules wake on the bound consumer loop, with a small set of pending notify tasks cleared on **`aclose`**).
- **`reset()` / `reset_pool()`:** **`reset()`** aliases **`reset_pool()`**. **`SyncProxyPool.reset_pool`** resets state and **`notify_all`** sync waiters only (by design **does not** signal async condition). **`AsyncProxyPool.reset_pool`** resets state then **`_notify_async_condition(notify_all=True)`**.
- **Context managers:** Sync **`with pool:`** — thread-local held proxy + **`_release_active_slot`**. Async **`async with pool:`** — **`ContextVar`** for proxy and nested carry token for **`__aexit__`** cleanup. **`acquire()`** / **`aacquire()`** return **`self`** for nested context style.
- **Introspection:** **`proxies`** property returns a **new `list`** snapshot after purge; **`cooling_proxies`** lists prototypes whose URL is in **`_cooldown_until`**; **`len`**, **`__iter__`**, **`__repr__`** (active/cooling counts, strategy, structure, cooldown).
- **`ProxyPool`:** **Deprecated** subclass of **`SyncProxyPool`** — emits **`DeprecationWarning`**; documented that **`async with ProxyPool(...)`** will fail (use **`AsyncProxyPool`** for asyncio).
- **`omniproxy.pool` exports:** **`TokenBucket`**, **`BaseProxyPool`**, **`HealthMonitor`**, protocols, **`PoolConfig`**, **`SyncProxyPool`**, **`AsyncProxyPool`**, **`ProxyPool`** (see **`__all__`** in `pool.py`).

---

## 7. CLI (`omniproxy`)

- **`check <file>`:** `--backend`, `--timeout`, `--anonymity`, `-o` / `--output-good`; **async by default**, `--sync` for threaded sync path.
- **`scrape <url>`:** `-o` / `--output`, `--timeout`.

---

## 8. Global configuration (`settings`)

- Thread-safe singleton (`OmniproxyConfig` in `omniproxy.config`): `default_backend`, `default_timeout`, `default_connect_timeout` (optional).
- **Check URL lists:** `default_check_urls` — validated non-empty `list[str]` of default reachability URLs. `default_check_info_url_templates` — non-empty list of templates for `with_info=True` checks; each template must include a `{fields}` placeholder for the field mask. `health_check_urls` — optional override list for `run_health_check` / `arun_health_check` when `HealthCheckConfig.url` is unset; may be **empty** to mean “fall back to `default_check_urls`”. Assigning a non-empty list validates every entry as a non-empty string.

---

## 9. Package exports (reviewer checklist)

- **Root `omniproxy`:** `Proxy`, `ProxyPattern`, `PlaywrightProxySettings`, `ProxyPool` (deprecated alias of `SyncProxyPool`; prefer **`from omniproxy.pool import SyncProxyPool, AsyncProxyPool`** for new code), `CheckResult`, `check_*`, `acheck_*`, `apply_check_result_metadata`, `read_proxies`, `save_proxies`, `iter_proxies_from_file`, `fetch_proxies`, `settings`, `get_backend`, `supported_backends`, **`MissingProxyMetadata`**, **`NoMatchingProxy`**, **`PoolClosedError`**, **`PoolExhausted`**, **`PoolSaturated`**.
- **`omniproxy.pool`:** `SyncProxyPool`, `AsyncProxyPool`, `BaseProxyPool`, `ProxyPool` (deprecated), `HealthMonitor`, `PoolConfig`, `TokenBucket`, **`BasePoolProtocol`**, **`MonitorablePoolProtocol`**, **`SyncPoolProtocol`**, **`AsyncPoolProtocol`** (use `omniproxy.constants.ANONYMITY_RANKS` for anonymity ordering).
- **`omniproxy.config`:** `OmniproxyConfig`, `settings`; **`PoolConfig`**, **`HealthCheckConfig`**, strategy/structure literals live here — import even though `config.__all__` only documents the singleton API surface.
- **`omniproxy.utils`:** `OmniproxyParser`, `get_formatted_proxy_string`, `ALLOWED_PROTOCOLS` — use when parsing without building `Proxy`, or for benchmarks/tests.
- **Lazy / optional:** Top-level `Client` and `AsyncClient` (httpx wrappers) resolved on attribute access.
- **`omniproxy.extended_proxy` (import explicitly):** `run_health_check`, `arun_health_check` — not re-exported from the root `omniproxy` package; import from `omniproxy.extended_proxy` when needed.
- **Internal (typically omitted from user docs):** `constants` (compiled patterns), `adapter` (absent / commented), `cli` wiring.

---

*This file is a living map from user-facing behavior to modules (`proxy.py`, `utils.py`, `extended_proxy.py`, `backends/*`, `io.py`, `pool.py`, `config.py`, `errors.py`, `constants.py`, `cli.py`). Update when public API or defaults change.*


