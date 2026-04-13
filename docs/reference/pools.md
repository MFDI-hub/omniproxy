# Pools: `SyncProxyPool`, `AsyncProxyPool`, and monitoring

Pools manage **many proxies** with **rotation**, **cooldowns** after failures, optional **rate limits**, optional **refresh** when exhausted, **lifecycle hooks**, and optional **background health monitoring**.

---

## Types overview

| Class | Concurrency model | Primary API |
|--------|-------------------|-------------|
| **`SyncProxyPool`** | Threading **`Lock`** + **`Condition`** | **`get_next`**, **`with pool:`**, **`close()`** |
| **`AsyncProxyPool`** | Shared **`Lock`** + lazily created **`asyncio.Lock`**, **`Condition`**, **`Event`** | **`await aget_next()`**, **`async with pool:`**, **`await aclose()`** |
| **`BaseProxyPool`** | Shared implementation details | Subclass only if you extend the library |
| **`ProxyPool`** | Deprecated subclass of **`SyncProxyPool`** | Emits **`DeprecationWarning`**; do not use **`async with ProxyPool`** — use **`AsyncProxyPool`** instead |

Both concrete pools share:

- **`_PoolState`**, **prototype** proxies, **per-URL in-flight** connection accounting.
- **`weakref.finalize`** hooks to clear connection counters if a pool is garbage-collected without an explicit **`close()`** / **`aclose()`**.

---

## Protocols (`omniproxy.pool`)

Structural protocols for type checkers and generic utilities:

| Protocol | Guarantees (conceptually) |
|----------|---------------------------|
| **`BasePoolProtocol`** | Core config, **`mark_success`**, **`mark_failed`**, **`proxies`** |
| **`MonitorablePoolProtocol`** | Adds **`is_closed`**, **`cooling_proxies`** |
| **`SyncPoolProtocol`** | Sync acquisition: **`get_next`**, context manager, **`close`** |
| **`AsyncPoolProtocol`** | Async acquisition: **`aget_next`**, async context manager, **`aclose`** |

---

## Identity, keys, and membership

- Internal identity for accounting is the canonical **`Proxy.url`** (see **`_PoolState._nolock_key`** in the source).
- **`proxy in pool`** coerces non-**`Proxy`** values through **`Proxy(...)`**, then tests membership against **active** keys (after cooldown purge rules are applied).

---

## Selection: `get_next` / `aget_next`

- **Strategy:** **`round_robin`** or **`random`** (from **`PoolConfig`**).
- **Random note:** Random selection may force a **`list`** backing store even when the config would otherwise prefer a **`deque`**, so random picks stay uniform.
- **Filters:** Arbitrary **keyword filters** match **`getattr(proxy, name) == value`** for each supplied name.
- **`min_anonymity`:** Compares using **`ANONYMITY_RANKS`** from **`omniproxy.constants`** (`transparent` < `anonymous` < `elite`).
- **Filter caching:** Results for a given filter key are cached until state is marked dirty (**`mark_failed`**, merges, **`reset`**, etc.).

---

## Exceptions

| Exception | Typical cause |
|-----------|----------------|
| **`PoolClosedError`** | Pool is closed or used after **`close`** / **`aclose`**. |
| **`PoolExhausted`** | No active proxies, or **refresh wait** timed out. |
| **`NoMatchingProxy`** | Filters exclude every candidate. |
| **`PoolSaturated`** | Matching proxies exist but are blocked by **`max_connections_per_proxy`** and/or **`max_rps_per_proxy`**. |
| **`MissingProxyMetadata`** | **`filter_missing_metadata="raise"`** and a candidate lacks required metadata. |

---

## `PoolConfig` (defined in `omniproxy.config`, re-exported from `omniproxy.pool`)

High-signal fields (see source and [API Reference](../api.md) for the full dataclass):

| Area | Fields (names only) |
|------|---------------------|
| Selection | **`strategy`**, **`structure`** (`deque` or `list`) |
| Failure handling | **`cooldown`**, **`failure_threshold`**, **`failure_penalties`** (map **`exc_type`** → cooldown multiplier) |
| Connection / RPS | **`max_connections_per_proxy`**, **`max_rps_per_proxy`** (lazy **`TokenBucket`** per URL on **successful** selections) |
| Waiting | **`acquire_timeout`**, **`wait_fallback_interval`**, **`on_saturated`** |
| Exhaustion | **`on_exhausted`**, **`refresh_callback`**, **`arefresh_callback`**, **`refresh_timeout`** |
| Metadata | **`filter_missing_metadata`** (`skip`, `raise`, `include`) |
| Context manager behavior | **`auto_mark_failed_on_exception`**, **`auto_mark_success_on_exit`**, **`reraise`**, **`extra`** |
| Health | Nested optional **`HealthCheckConfig`** |
| Lifecycle hooks | **`on_proxy_acquired`**, **`on_proxy_released`**, **`on_proxy_failed`**, **`on_proxy_cooled_down`**, **`on_proxy_recovered`**, **`on_check_complete`** |

### Refresh on exhaustion

1. If **`get_next`** / **`aget_next`** raises **`PoolExhausted`**, an optional **`on_exhausted`** hook may run.
2. When **`refresh_callback`** / **`arefresh_callback`** is configured, the pool **merges** refreshed **`str`** or **`Proxy`** values via **`_merge_refreshed_proxies`** (dedupe by URL against prototypes; re-adds if not cooling).
3. Selection is **retried once** after a successful merge path.

### Rate limiting

**`max_rps_per_proxy`** installs a per-URL **`TokenBucket`** lazily. Tokens are consumed in the accounting path tied to **successful selections**, not merely enqueue attempts.

---

## `HealthMonitor`

- Holds a **`weakref.ref`** to its pool so the monitor does not keep the pool alive forever by itself.
- **`run()`** (async) loop:
  1. Snapshot **active + cooling** proxies in a stable order keyed by URL.
  2. **`asyncio.gather`** of **`arun_health_check`** for each entry.
  3. Invoke **`on_check_complete`** when configured.
  4. Call **`mark_success`** or **`mark_failed`** based on outcome.
- Stops on **`PoolClosedError`** or cancellation; logs if the pool disappeared due to GC without a clean **`close`**.

### Starting monitoring

**`AsyncProxyPool`:**

- **`start_monitoring()`** requires a **running asyncio event loop**.
- Idempotent on the **same** loop; errors if a monitoring task is already bound to a **different** loop.
- **`async with pool.monitoring():`** starts monitoring on enter and **`stop_monitoring()`** on exit.

**`SyncProxyPool`:**

- **`start_monitoring_thread()`** spins a **daemon thread** with a **dedicated** asyncio loop and an internal ready **`threading.Event`** (waits up to **5 seconds** for readiness).
- Raises **`RuntimeError`** if in-loop monitoring is already active on the **current** thread’s running loop.

**Stopping:**

- **`stop_monitoring()`** cancels async tasks and/or stops the health thread and **joins** it.
- **`close()`** (sync) marks the pool closed, **`notify_all`** on the sync condition, then **`stop_monitoring()`** — it does **not** call **`reset_pool`** automatically.
- **`aclose()`** (async) documents that the **final `notify_all`** for blocked async waiters may run **after** the coroutine returns (ordering quirk callers should be aware of).

---

## Accounting: `mark_failed` / `mark_success`

- Both require an **open** pool.
- **Sync** path: may **`notify_all`** on the threading **`Condition`** when cooldown state changes unblock waiters.
- **Async** path: uses **`_notify_async_condition`** to schedule wakes on the **consumer** event loop; pending notify tasks are cleared during **`aclose`**.

---

## `reset` / `reset_pool`

- **`reset()`** is an alias for **`reset_pool()`**.
- **`SyncProxyPool.reset_pool`**: resets internal state and wakes **sync** waiters only — **by design** it does **not** signal the async condition (async pool has a separate wake path).
- **`AsyncProxyPool.reset_pool`**: resets state then **`_notify_async_condition(notify_all=True)`**.

---

## Context managers

**Sync — `with pool:`**

- Uses **thread-local** storage for the currently held proxy.
- **`_release_active_slot`** runs on exit to decrement in-flight counters.

**Async — `async with pool:`**

- Uses a **`ContextVar`** for the held proxy plus a nested carry token so **`__aexit__`** can clean up correctly across tasks.

**`acquire()` / `aacquire()`**

- Return **`self`** to support nested **`with pool.acquire():`** style usage.

---

## Introspection

| Member | Behavior |
|--------|----------|
| **`proxies`** | Returns a **new `list`** snapshot after internal purge logic. |
| **`cooling_proxies`** | Lists prototypes whose URL maps to an entry in **`_cooldown_until`**. |
| **`len(pool)`** | Count semantics as implemented (active view). |
| **`iter(pool)`** | Iterate proxies according to pool rules. |
| **`repr(pool)`** | Includes active/cooling counts, strategy, structure, cooldown summary. |

---

## Deprecated `ProxyPool`

**`ProxyPool`** subclasses **`SyncProxyPool`** for backward compatibility and emits **`DeprecationWarning`**.

- **`async with ProxyPool(...)`** is **not supported** and will fail — use **`AsyncProxyPool`** for asyncio code.

---

## Imports (recommended)

```python
from omniproxy.pool import SyncProxyPool, AsyncProxyPool, PoolConfig, HealthMonitor
```

For anonymity ordering in filters, import **`ANONYMITY_RANKS`** from **`omniproxy.constants`**.
