# File and network I/O

The **`omniproxy.io`** module (re-exported from **`import omniproxy`**) handles **line-oriented files**, **streaming large files**, and **scraping** proxy-like strings from HTTP(S) resources.

---

## `read_proxies`

```text
read_proxies(
    filepath: str | Path,
    *,
    encoding: str = "utf-8",
    on_invalid: Literal["raise", "skip"] = "raise",
    errors_out: list[tuple[int, str, str]] | None = None,
) -> list[Proxy]
```

| Parameter | Meaning |
|-----------|---------|
| **`filepath`** | Path to a UTF-8 (by default) text file. |
| **`encoding`** | Passed through to **`open()`**. |
| **`on_invalid`** | **`"raise"`** — first bad line raises **`ValueError`** with line context. **`"skip"`** — invalid lines are skipped. |
| **`errors_out`** | If provided, append **`(lineno, line_text, error_message)`** for each failure without necessarily raising (depending on **`on_invalid`**). |

Behavior:

- Reads **one proxy per non-empty line**.
- Blank lines are ignored.
- Each line is parsed with **`Proxy(line_text)`**, so all [supported string shapes](proxy.md#parsing-pipeline) apply.

---

## `iter_proxies_from_file`

```text
iter_proxies_from_file(
    filepath: str | Path,
    *,
    encoding: str = "utf-8",
    on_invalid: Literal["raise", "skip"] = "raise",
    errors_out: list[tuple[int, str, str]] | None = None,
) -> Iterable[Proxy]
```

Same semantics as **`read_proxies`**, but returns a **lazy generator** suitable for very large files so you do not materialize the entire list at once.

---

## `save_proxies`

```text
save_proxies(
    filepath: str | Path,
    proxies: Iterable[str | Proxy],
    *,
    mode: str = "w",
    encoding: str = "utf-8",
) -> None
```

| Parameter | Meaning |
|-----------|---------|
| **`filepath`** | Output path. |
| **`proxies`** | Any iterable of **`str`** or **`Proxy`**; each item is written as **`str(item).strip()`**. |
| **`mode`** | File open mode (default truncates then writes). |
| **`encoding`** | Text encoding. |

Blank lines after coercion are skipped; each proxy is written **on its own line**.

---

## `fetch_proxies`

```text
fetch_proxies(
    url: str,
    *,
    timeout: float | None = None,
    pattern: re.Pattern[str] | None = None,
    unique: bool = True,
    user_agent: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> list[Proxy]
```

| Parameter | Meaning |
|-----------|---------|
| **`url`** | Page to download (**direct** connection — not via a proxy). |
| **`timeout`** | Socket timeout; defaults to **`settings.default_timeout`** when `None`. |
| **`pattern`** | Regex with a named group **`raw`**; when `None`, uses the built-in **`PROXY_LINE_PATTERN`** from **`omniproxy.constants`**, tuned to prefer explicit schemes and conservative **`host:port`** style lines. |
| **`unique`** | When **`True`**, deduplicate by raw capture and by **canonical proxy URL** so duplicates collapse. |
| **`user_agent`** | Override **`User-Agent`**; otherwise a default fetch UA is used unless **`headers`** already set one. |
| **`headers`** | Extra request headers merged into the request. |

Behavior:

- Uses **`urllib.request`** with the process default SSL context.
- Decodes the body as **UTF-8** with replacement on errors.
- Every regex match must parse successfully as a **`Proxy`**; malformed candidates are skipped.

This is what the **`omniproxy scrape`** CLI subcommand calls under the hood.

---

## CLI mapping

| CLI command | I/O functions used |
|-------------|-------------------|
| **`omniproxy check FILE`** | **`read_proxies`**, optionally **`save_proxies`** |
| **`omniproxy scrape URL`** | **`fetch_proxies`**, optionally **`save_proxies`** |

See [CLI](../cli.md) for flags and stdout summaries.
