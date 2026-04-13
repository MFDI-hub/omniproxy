# Global configuration: `settings`

Runtime defaults live on a **thread-safe singleton** exposed as **`settings`** (instance of **`OmniproxyConfig`** in **`omniproxy.config`**).

---

## Core networking defaults

| Attribute | Role |
|-----------|------|
| **`default_backend`** | Short name passed to **`get_backend`** when a check does not override **`backend=`**. |
| **`default_timeout`** | Per-request socket timeout for checks, fetches, and other HTTP operations that defer to settings. |
| **`default_connect_timeout`** | Optional separate connect-phase timeout where the backend supports it. |

Mutating these affects **all subsequent calls** in the process unless a call passes explicit overrides.

---

## Check URL lists

### `default_check_urls`

- Type: **validated non-empty `list[str]`**.
- Used by default check functions when no explicit **`url=`** is provided.
- Each invocation typically picks **one** URL using **`random.choice`**.
- When multiple URLs exist, **retry** paths prefer a **different** URL than the last attempt.

### `default_check_info_url_templates`

- Type: **validated non-empty `list[str]`** when used for **`with_info=True`** checks.
- **Every template must include the substring `{fields}`** so the library can splice the ip-api-style field mask into the request.

### `health_check_urls`

- Optional **override list** for **`run_health_check`** / **`arun_health_check`** when **`HealthCheckConfig.url`** is unset.
- May be assigned an **empty list** to mean “fall back to **`default_check_urls`**” in the `health_check_urls or default_check_urls` resolution.
- When assigned a **non-empty** list, **each entry** must be a non-empty string (validated on assignment).

---

## `PoolConfig` and `HealthCheckConfig`

Advanced pool behavior is configured through **`PoolConfig`** and nested **`HealthCheckConfig`**, both defined in **`omniproxy.config`** and documented in the [API Reference](../api.md).

Even when **`config.__all__`** emphasizes the singleton API, **importing `PoolConfig` from `omniproxy.config` or `omniproxy.pool`** is the supported pattern for applications.

---

## Thread safety

**`OmniproxyConfig`** is designed for concurrent reads and controlled updates from multiple threads. Still treat **`settings`** like any shared global: prefer setting defaults **once** at startup, or pass explicit parameters per call when values differ per task.

---

## Related pages

- [Checking](checking.md) — how URL lists interact with retries and health checks.
- [Pools](pools.md) — **`PoolConfig`** fields at a narrative level.
