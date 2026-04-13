# Core model: `Proxy`

The end-user type is the extended **`Proxy`** class in `omniproxy.extended_proxy` (re-exported from the **`omniproxy`** package root). It subclasses Python’s built-in **`str`** so instances behave like canonical proxy strings in logs, CSVs, and templates, while exposing structured fields and metadata.

---

## Type and storage

- **`Proxy` is a subclass of `str`**, with **fixed structural fields** stored in `__slots__`, plus **metadata** that can change over the object’s lifetime (for example after a successful check).
- **Structural fields are read-only** after construction in the sense intended by the design: you do not reassign `ip` / `port` / etc.; metadata is updated via helpers such as `apply_check_result_metadata` or internal check paths.

---

## Construction

Signature (conceptually):

```text
Proxy(raw, /, protocol=None)
```

- **`raw`:** Any supported proxy string, or an existing **`Proxy`** instance.
- **`protocol`:** Optional override: **`http`**, **`https`**, **`socks4`**, or **`socks5`**.
- **Identity fast path:** Constructing from an existing **`Proxy`** with the **same** `protocol` (including both `None`) returns the **same instance**.
- **Protocol change:** If you request a different protocol, the implementation dumps the parsed struct via **`OmniproxyParser`**, reapplies the new protocol, and returns a newly canonicalized proxy string/object.

---

## Parsing pipeline

Parsing is implemented by **`OmniproxyParser`** together with **`constants.PROXY_FORMATS_REGEXP`** (see [Parser](parser.md)). Supported shapes include:

- URL forms: `protocol://…`
- `user:pass@host:port`
- `host:port:user:pass`
- `host:port|user:pass`
- Various user/pass/host/port permutations accepted by the regex set
- Optional **`rotation_url`** in a trailing **`[url]`** bracket (mobile rotation APIs)
- Bracketed **IPv6** addresses
- Bare **`host:port`** with optional scheme inference

---

## Structural fields

| Field | Role |
|--------|------|
| `ip` | Host or address portion used for checks |
| `port` | TCP port |
| `username` | Auth user (may be empty) |
| `password` | Auth password (may be empty) |
| `protocol` | `http`, `https`, `socks4`, `socks5` |
| `rotation_url` | Optional rotation API URL (not sent through the proxy) |

---

## Metadata fields

| Field | Role |
|--------|------|
| `latency` | Last observed latency where applicable |
| `anonymity` | `transparent`, `anonymous`, or `elite` when detected |
| `last_checked` | Epoch timestamp of last check |
| `last_status` | Boolean success after a check, when set |
| `country`, `city`, `asn`, `org` | Optional enrichment strings; included in **`to_dict()`** / pickle when not `None` |

Metadata may be set by **`apply_check_result_metadata`** or your own pipeline.

---

## Derived and convenience attributes

- **`url`** — Canonical string form used for equality and hashing.
- **`safe_url`** — Same idea with password masked for logging.
- **`host`** — `ip:port` style host descriptor.
- **`address`** — Alias of **`ip`**.
- **`login`** — Alias of **`username`**.
- **`server`** — `protocol://ip:port` with **`http`** fallback when appropriate.
- **`has_auth`** — Whether credentials are present.
- **`is_working`** — Uses **`last_status`** when set; otherwise may use a latency heuristic.

---

## Interoperability helpers

- **`as_requests_proxies()`** — Mapping suitable for `requests` sessions.
- **`playwright`** — **`PlaywrightProxySettings`** for browser automation.
- **`to_dict()`** / **`to_json_string()`** — Serialization including metadata when set.

---

## IP version

**`version`** returns **`4`**, **`6`**, or **`None`** (for hostnames that are not resolved at parse time).

---

## Rotation (`rotation_url`)

- **`rotate()`** / **`arotate()`** issue a **direct** HTTP request to **`rotation_url`** (the request does **not** go through the proxy).
- Method and backend follow the implementation; success is modeled as **HTTP 200**.

---

## Formatting and patterns

- **`ProxyPattern`** defines tokens: `protocol`, `username`, `password`, `ip`, `port`, `rotation_url`.
- **`get_formatted_proxy_string`** in **`omniproxy.utils`** renders a **`Proxy`** or **`OmniproxyParser`** with a pattern or template (token replacement and optional-field collapse).
- **`Proxy.set_default_pattern(...)`** sets a default pattern for formatting operations that rely on it.

---

## Validation and persistence

- **`Proxy.validate(v)`** wraps construction and raises **`ValueError`** on invalid input.
- **Pickle:** `__reduce__` / `__setstate__` include metadata in the persisted state so round-trips preserve check results when applicable.

---

## Equality and hashing

Proxies compare equal and hash consistently based on the **canonical `url`**.

---

## Extended surface (methods on the extended class)

Beyond string behavior, the extended **`Proxy`** provides:

| Method / accessor | Role |
|-------------------|------|
| `.check()` / `.acheck()` | Convenience wrappers around **`check_proxy`** / **`acheck_proxy`** |
| `.get_info()` / `.aget_info()` | Same stack with **`with_info=True`** (richer payload path) |
| `.get_client()` | Sync **`httpx.Client`** with proxy-mounted transport |
| `.get_async_client()` | **`httpx.AsyncClient`** with proxy-mounted transport |

For bulk checking, URL lists, retries, and anonymity, see [Checking](checking.md).
