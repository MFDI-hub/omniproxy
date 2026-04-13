# HTTP backends

All reachability checks and many helper requests go through a small **backend abstraction** so the same check logic can run on **httpx**, **aiohttp**, **requests**, **curl_cffi**, or **tls_client**, depending on which extras you install.

---

## Registry

| Function | Purpose |
|----------|---------|
| **`get_backend(name)`** | Return a backend implementation by short name (`"httpx"`, `"aiohttp"`, …). |
| **`supported_backends()`** | Return the names that are **currently importable** in the environment (reflects installed extras). |

The active default for checks is also influenced by **`settings.default_backend`** (see [Configuration](configuration.md)).

---

## Implementations

Each backend implements a common surface used by checks and related code:

- **Proxied HTTP:** **`get`** / **`aget`** — perform a request **through** the configured proxy (used for reachability and anonymity probes).
- **Direct HTTP:** **`request_direct`** / **`arequest_direct`** — perform a request **without** the proxy (used for **`rotation_url`** calls, which must not loop through the proxy itself).

Concrete names shipped in-tree include:

- **`httpx`**
- **`aiohttp`**
- **`requests`**
- **`curl_cffi`**
- **`tls_client`**

Install the matching **optional dependency** (see the root package **extras** in `pyproject.toml`) before calling **`get_backend("…")`** for that name.

---

## Response shape

Backends normalize responses into **`BackendResponse`**:

- **`status_code`**
- **`headers`**
- **`json_data`** (decoded JSON when applicable)
- **`text`**

Check logic, anonymity classification, and info-url templates all consume this normalized shape so they do not depend on a specific third-party response object.

---

## Relationship to checks

- **`check_proxy`**, **`acheck_proxy`**, **`check_proxies`**, and **`acheck_proxies`** select a backend by name or fall back to **`settings.default_backend`**.
- Per-call **`backend=`** overrides the default for that invocation only.

See [Checking](checking.md) for retries, URLs, and anonymity.
