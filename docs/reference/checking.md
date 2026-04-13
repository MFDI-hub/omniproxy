# Checking and metadata

This page describes how proxies are checked for reachability, how **anonymity** and **geo-style metadata** are attached, and how **health checks** integrate with pools.

---

## Entry points

### Public check APIs

| Function | Sync / async |
|----------|----------------|
| **`check_proxy`** | Sync |
| **`acheck_proxy`** | Async |
| **`check_proxies`** | Sync facade (may run async or threads internally) |
| **`acheck_proxies`** | Async bulk |

### On `Proxy` instances

- **`await proxy.acheck(...)`** / **`proxy.check(...)`** (extended class)
- **`await proxy.aget_info(...)`** / **`proxy.get_info(...)`** — wrappers that enable **`with_info=True`** on the underlying check path.

### Health-check drivers (pools and advanced use)

Defined in **`omniproxy.extended_proxy`** (import explicitly; **not** re-exported from **`import omniproxy`**):

| Function | Role |
|----------|------|
| **`run_health_check`** | Sync driver given a **`HealthCheckConfig`** |
| **`arun_health_check`** | Async variant |

These power **`HealthMonitor`** inside pools and can be called directly in your own supervision loops.

---

## Default URLs and rotation

### Reachability (`default_check_urls`)

- **`settings.default_check_urls`** is a **non-empty `list[str]`** of URLs used for basic “is this proxy up?” checks.
- Each check picks **one** URL via **`random.choice`**.
- When **more than one** URL is configured, **retry** logic prefers a **different** URL on subsequent attempts so a single bad endpoint does not dominate.

### Info / geo templates (`default_check_info_url_templates`)

- When **`with_info=True`**, templates come from **`settings.default_check_info_url_templates`**.
- That list is also validated as **non-empty** when used.
- **Each template must contain a `{fields}` placeholder** for the field mask (ip-api-style composition).

### Per-call override

- Passing an explicit **`url=`** to a check **bypasses** the rotating default list for that call.

### Health-check URL resolution

For **`run_health_check`** / **`arun_health_check`**:

- If **`HealthCheckConfig.url`** is **unset**, the URL is **`random.choice(settings.health_check_urls or settings.default_check_urls)`**.
- This lets monitoring use **`settings.health_check_urls`** as a **dedicated** list while normal **`check_proxy`** traffic keeps using **`default_check_urls`**.
- **`health_check_urls`** may be an **empty list** to mean “fall back to **`default_check_urls`**” for that resolution expression.

---

## Check options (conceptual)

Typical knobs exposed across the check stack include:

| Option | Role |
|--------|------|
| **`url`** | Fixed check URL (skips default list rotation for that call). |
| **`backend`** | Which HTTP backend implementation to use. |
| **`timeout`** | Per-request timeout. |
| **`detect_anonymity`** | Extra GET to a headers echo endpoint; classifies **`transparent` \| `anonymous` \| `elite`**. |
| **`raise_on_error`** | Whether transport/protocol errors propagate vs return structured failure. |
| **`with_info`** | Return richer **`dict`** payload vs a simple boolean outcome. |
| **`max_retries`** | Retry count for flaky endpoints. |
| **`retry_backoff`** | Delay growth between retries. |
| **`retry_on_status`** | Which HTTP status codes trigger retry (defaults commonly include **502 / 503 / 504**). |

Exact signatures appear in the [API Reference](../api.md).

---

## Bulk execution modes

- **`check_proxies(..., use_async=True)`** (default in many call sites) ultimately drives **`asyncio.run(acheck_proxies(...))`** when no loop is running.
- **`check_proxies(..., use_async=False)`** uses a **`ThreadPoolExecutor`** with **`max_workers`** (default follows a capped heuristic).

Choose **async** for large batches on a single machine when your environment allows it; choose **threaded** when you must avoid running an event loop in the host process.

---

## Anonymity

When **`detect_anonymity`** is enabled:

- The library performs an **additional GET** to a **headers echo** endpoint.
- It inspects **forwarded** / **via** / **proxy** style headers.
- It assigns **`transparent`**, **`anonymous`**, or **`elite`** based on what leaks.

This is **slower** than a simple reachability probe alone.

---

## Applying metadata after external pipelines

**`apply_check_result_metadata(proxy, latency=, anonymity=, status=, country=, city=, asn=, org=)`** sets:

- **`latency`**, **`last_checked`**, **`last_status`**
- Optional **`anonymity`**
- Optional geo/ASN string fields when you supply them

Use this when your own service classifies proxies but you still want **`Proxy`** instances to carry uniform metadata for pools and serialization.

---

## `CheckResult`

**`.check()`** / **`.acheck()`** on the extended **`Proxy`** return a **`CheckResult`** dataclass, including fields such as:

- **`success`**
- **`latency`**
- **`exc_type`**
- **`status_code`**

Use it for structured logging without catching exceptions around every call.

---

## Related reading

- [Backends](backends.md) — normalized **`BackendResponse`**.
- [Configuration](configuration.md) — **`settings`**, **`HealthCheckConfig`**.
- [Pools](pools.md) — **`HealthMonitor`**, **`on_check_complete`**.
