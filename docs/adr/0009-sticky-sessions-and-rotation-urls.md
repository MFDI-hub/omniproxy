# ADR-0009: Sticky sessions and rotation URLs for residential/mobile

- **Status:** Accepted
- **Date:** 2026-07-16
- **Tags:** sessions, residential, mobile

## Context

Some targets require the same egress IP across a logical session (login flows, carts, anti-bot continuity). Separately, residential/mobile providers expose a **rotation URL** that forces IP change without changing the proxy endpoint string.

These concerns interact with cooldown: if a sticky proxy cools down mid-session, the pool must choose a policy (block, rebind, or raise) rather than silently breaking affinity.

## Decision

1. Support **sticky sessions** via `session_key` on acquire, backed by an in-memory session registry with TTL and rebind hooks.
2. Define **session cooldown policies**: `BLOCK`, `REBIND`, `RAISE`.
3. Parse optional trailing `[rotation_url]` into `Proxy.rotation_url` and expose `rotate` / `arotate`.
4. Let pool config control when rotation fires (`use_rotation_urls`, `rotate_on_acquire`, `rotate_on_failure`), with a dedicated `rotating_residential_preset`.
5. Keep the public rename path `session_id` → `session_key` for compatibility during the 4.x transition.

## Consequences

- **Positive:** First-class support for session affinity and provider rotation without app-specific glue.
- **Negative:** Sticky maps are process-local; multi-process affinity needs an external store.
- **Neutral:** Rotation is best-effort against provider APIs; failures surface through normal mark_failed / hooks.

## Alternatives considered

| Alternative | Why not |
|-------------|---------|
| App-managed sticky maps only | Reimplements the same registry in every project |
| Always rotate on every acquire | Breaks session affinity and burns provider quotas |
| Built-in distributed session store | Out of library scope (see ADR-0001) |

## Evidence

- `omniproxy/session.py`, `omniproxy/proxy.py` (`rotation_url`, `rotate` / `arotate`)
- `omniproxy/config.py` (`SessionConfig`, `rotating_residential_preset`)
- `omniproxy/pool.py` (session resolve / rebind paths)
