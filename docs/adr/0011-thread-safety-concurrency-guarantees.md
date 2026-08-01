    # ADR-0011: Thread‑safety and concurrency guarantees

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** concurrency, pool, api-contract

## Context

`AsyncProxyPool` manages mutable state (leases, scoring, cooldowns, session registry) and runs background coroutines. `SyncProxyPool` wraps this async engine inside a daemon thread holding a private event loop. Users inevitably share pool instances across threads, `concurrent.futures` executors, or async tasks. Without an explicit contract they will make unsafe assumptions (fork‑safety, mixing sync and async calls on the same pool, calling `acquire` from two threads simultaneously).

## Decision

1. **`AsyncProxyPool`** is safe for concurrent use from **multiple asyncio tasks executing in the same event loop**. Internal mutations are protected by `asyncio.Lock` where needed. It is **not** designed for direct use from multiple event loops or threads without an external synchronisation wrapper.

2. **`SyncProxyPool`** provides a **thread‑safe** façade: every public method dispatches to the private loop through a `threading.Lock`. This means multiple threads may call `acquire` / `release` / `mark_success` / `mark_failed` concurrently.

3. **Not fork‑safe**: neither pool survives `os.fork()`. The daemon thread and event loop are not recreated after fork; users must instantiate fresh pools in child processes.

4. **Not re‑entrant into async code**: do not call `SyncProxyPool` methods from within a coroutine running in the same event loop as the private one (it would deadlock). Users mixing styles should keep the async pool as the single source and use `asyncio.to_thread` if needed.

5. User‑supplied hooks (e.g. `on_acquire`, `on_release`, fetcher callbacks) execute inside the pool’s event loop. Callers must ensure they are safe in that context.

## Consequences

- **Positive:** Clear safety boundaries; thread‑safe sync API lets scripts and threading‑based apps share a pool; async users get efficient native orchestration.
- **Negative:** Sync overhead (lock + cross‑thread call) under heavy contention; fork‑safety limitation must be prominently documented.
- **Neutral:** The internal lock and loop ownership may be revisited if a future version adds a `ProcessPool` variant.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| No thread‑safety guarantees (caller must lock) | Forces every user to reinvent synchronisation; pool state would be corrupted easily. |
| Multiprocessing‑safe pool | Out of library scope; adds heavy IPC dependencies. |
| Allow `SyncProxyPool` inside async code | Creates deadlock hazards and violates the single‑loop ownership model. |

## Evidence

- `omniproxy/pool.py` (`SyncProxyPool` lock implementation, daemon thread lifecycle)
- `omniproxy/pool.py` (`AsyncProxyPool` internal `asyncio.Lock` for scoring/leasing)
- `docs/for_mine_and_only_my.md` (user guide mentions threading constraints)