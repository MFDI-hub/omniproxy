# Command-line interface

The **`omniproxy`** console script is declared in **`pyproject.toml`**:

```toml
[project.scripts]
omniproxy = "omniproxy.cli:main"
```

After **`pip install omniproxy`** (or **`uv sync`** in this repo), the **`omniproxy`** executable should appear on your **`PATH`**. Entry point implementation: **`omniproxy.cli:main`**.

---

## Top-level usage

```bash
omniproxy --help
```

**`argparse`** requires a **subcommand**. There is no default action: you must pass **`check`** or **`scrape`**.

```bash
omniproxy check --help
omniproxy scrape --help
```

---

## `omniproxy check`

Validate every non-empty line in a text file as a **`Proxy`**, run the same bulk check stack as **`check_proxies`**, print aggregate counts, and optionally persist working proxies.

### Invocation

```bash
omniproxy check <FILE> [options]
```

### Positional argument

| Argument | Description |
|----------|-------------|
| **`FILE`** | Path to a newline-separated proxy list. Each line is parsed with **`Proxy(line)`** after **`strip()`**; blank lines are ignored (see **`read_proxies`** in [File and network I/O](reference/io.md)). |

### Options

| Option | Dest / type | Default | Description |
|--------|-------------|---------|-------------|
| **`--backend NAME`** | `str`, optional | `None` | Forwarded to **`check_proxies`** as **`backend=`**. Must be one of the names **`supported_backends()`** returns **in this environment**. If omitted, the library uses **`settings.default_backend`**. |
| **`--timeout SECONDS`** | `float`, optional | `None` | Forwarded as **`timeout=`**; library falls back to **`settings.default_timeout`** when `None`. |
| **`--anonymity`** | `store_true` | `False` | Sets **`detect_anonymity=True`** on **`check_proxies`**. Triggers the slower headers-based anonymity probe. |
| **`-o PATH`**, **`--output-good PATH`** | `str`, optional | `None` | When set, **`save_proxies`** writes **only proxies classified as working** to **`PATH`**, one per line. |
| **`--sync`** | `use_async=False` | async path | Forces **`check_proxies(..., use_async=False)`** so work runs in a **`ThreadPoolExecutor`** instead of the default async path. |

### Hidden compatibility flag

The **`check`** parser also defines **`--no-async`**, marked **`argparse.SUPPRESS`** in **`cli.py`**. It is an alias for forcing the synchronous/threaded path (same as **`--sync`**). Prefer **`--sync`** in scripts and documentation.

### Standard output

On success the command prints a single summary line:

```text
ok=<N> fail=<M>
```

where **`N`** is the number of **good** results and **`M`** the number of **bad** results returned by **`check_proxies`**.

### Exit code

- **`0`** — **`main()`** returned zero (no uncaught exception). Inspect **`ok`/`fail`** for business outcome.

### Implementation mapping

1. **`read_proxies(args.file)`** loads **`list[Proxy]`**.
2. **`check_proxies(proxies, backend=..., detect_anonymity=..., use_async=..., timeout=...)`** performs the work.
3. If **`args.output_good`** is set, **`save_proxies`** writes the good side of the partition. The CLI normalizes entries that might be tuples to the proxy object before saving (mirrors internal **`check_proxies`** return shape).

For semantics of backends, retries, URL rotation, and anonymity, see [Checking](reference/checking.md) and [Backends](reference/backends.md).

### Examples

```bash
omniproxy check proxies.txt --backend httpx --timeout 8 --output-good good.txt
omniproxy check proxies.txt --backend httpx --anonymity -o verified.txt
omniproxy check proxies.txt --sync --timeout 15
```

---

## `omniproxy scrape`

Download a URL over a **direct** (non-proxied) connection, run regex extraction with the default **`PROXY_LINE_PATTERN`**, and print how many **`Proxy`** instances were recovered.

### Invocation

```bash
omniproxy scrape <URL> [options]
```

### Positional argument

| Argument | Description |
|----------|-------------|
| **`URL`** | Passed verbatim to **`fetch_proxies(url)`**. The implementation uses **`urllib.request`** with the process SSL context (see [File and network I/O](reference/io.md)). |

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| **`-o PATH`**, **`--output PATH`** | `str`, optional | `None` | When set, **`save_proxies`** writes every extracted proxy to **`PATH`**. |
| **`--timeout SECONDS`** | `float`, optional | `None` | Forwarded to **`fetch_proxies`**; defaults to **`settings.default_timeout`** when omitted. |

### Standard output

Always prints:

```text
found=<N>
```

where **`N`** is **`len(fetch_proxies(...))`** after parsing and optional deduplication.

### Exit code

- **`0`** on normal completion.

### Examples

```bash
omniproxy scrape https://example.com/proxies -o scraped.txt
omniproxy scrape https://example.com/list --timeout 20
```

If **`-o` / `--output`** is omitted, results are counted and reported only—nothing is written to disk.

---

## Discovering backends at runtime

```python
from omniproxy import supported_backends

print(supported_backends())
```

Install the matching extra before passing **`--backend`** (for example **`pip install "omniproxy[httpx]"`**) or **`check_proxies`** will fail when resolving the backend.

---

## Related topics

- Library quick start: [Home](index.md)
- Checks, URLs, retries: [Checking](reference/checking.md)
- File helpers and `fetch_proxies`: [File and network I/O](reference/io.md)
- Python API signatures: [API Reference](api.md)
