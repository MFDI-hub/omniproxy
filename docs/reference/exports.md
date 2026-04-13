# Package layout and exports

This page lists **what to import from where** and calls out **lazy** or **explicit** modules so you do not rely on accidental re-exports.

---

## Root package: `import omniproxy`

Stable, documented exports include:

| Category | Names |
|----------|--------|
| Core types | **`Proxy`**, **`ProxyPattern`**, **`PlaywrightProxySettings`**, **`CheckResult`** |
| Checks | **`check_proxy`**, **`acheck_proxy`**, **`check_proxies`**, **`acheck_proxies`**, **`apply_check_result_metadata`** |
| I/O | **`read_proxies`**, **`save_proxies`**, **`iter_proxies_from_file`**, **`fetch_proxies`** |
| Configuration | **`settings`** |
| Backends | **`get_backend`**, **`supported_backends`** |
| Pool errors | **`MissingProxyMetadata`**, **`NoMatchingProxy`**, **`PoolClosedError`**, **`PoolExhausted`**, **`PoolSaturated`** |
| Deprecated pool alias | **`ProxyPool`** (prefer **`SyncProxyPool`** / **`AsyncProxyPool`** from **`omniproxy.pool`**) |

---

## `omniproxy.pool`

Import advanced pool types from here:

- **`SyncProxyPool`**, **`AsyncProxyPool`**, **`BaseProxyPool`**
- **`ProxyPool`** (deprecated)
- **`HealthMonitor`**
- **`PoolConfig`**, **`TokenBucket`**
- Protocols: **`BasePoolProtocol`**, **`MonitorablePoolProtocol`**, **`SyncPoolProtocol`**, **`AsyncPoolProtocol`**

For **`ANONYMITY_RANKS`**, import **`omniproxy.constants.ANONYMITY_RANKS`** (not re-exported from **`pool`**).

---

## `omniproxy.config`

- **`OmniproxyConfig`**, **`settings`**
- **`PoolConfig`**, **`HealthCheckConfig`**, strategy/structure literal types

Even if **`config.__all__`** highlights the singleton, **`PoolConfig`** and friends are **public** configuration surfaces.

---

## `omniproxy.utils`

Use when you need parsing **without** immediately constructing a **`Proxy`**, or for micro-benchmarks:

- **`OmniproxyParser`**
- **`get_formatted_proxy_string`**
- **`ALLOWED_PROTOCOLS`**

---

## Lazy attributes on the root package

**`Client`** and **`AsyncClient`** (httpx wrappers) are resolved on **attribute access** at the **`omniproxy` package** level. Import them as:

```python
from omniproxy import Client, AsyncClient
```

only **after** ensuring **`httpx`** extras are installed, or expect import-time failures when the optional stack is missing.

---

## Explicit submodule: `omniproxy.extended_proxy`

The following are **not** re-exported from **`import omniproxy`** and must be imported explicitly when needed:

- **`run_health_check`**
- **`arun_health_check`**

Example:

```python
from omniproxy.extended_proxy import run_health_check, arun_health_check
```

Everything else most applications need is already on the **`omniproxy`** root namespace.

---

## Internal modules (stability not guaranteed)

- **`omniproxy.constants`** — compiled regexes and default constants; safe to read, not a semver-stable “plugin API”.
- **`omniproxy.cli`** — argparse wiring for the **`omniproxy`** console script.
- **`adapter`** — legacy integration (commented / absent; do not depend on it).

---

## CLI entrypoint

Console script name: **`omniproxy`** → **`omniproxy.cli:main`** (see `pyproject.toml` **`[project.scripts]`**).

See [CLI](../cli.md).
