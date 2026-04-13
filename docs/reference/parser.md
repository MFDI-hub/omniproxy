# Parser model: `OmniproxyParser` and `omniproxy.utils`

Low-level parsing mirrors structural fields **before** the **`Proxy`** string is canonicalized. Most applications should construct **`Proxy(...)`** directly; the parser and helpers are useful for benchmarks, tests, or custom pipelines.

---

## `OmniproxyParser`

- Implemented as a **`msgspec.Struct`** with the same structural fields the regex pipeline produces.
- Stays in sync with **`Proxy`** semantics but represents the **pre-canonical** structured view.

---

## Regex dispatch

- Module helper **`_proxy_format_groupdict(stripped)`** runs the stripped input against **`PROXY_FORMATS_REGEXP`** (from **`omniproxy.constants`**).
- It returns the **first** successful match’s **`groupdict()`**, or **`None`** if nothing matches.

This keeps format detection in **one** place so new formats do not require parallel logic in multiple constructors.

---

## `from_string(proxy_string)`

1. Strip the input string.
2. Require a regex match via **`_proxy_format_groupdict`**.
3. Delegate to **`from_match(groupdict)`** — the **only** downstream construction path from regex groups.
4. **Protocol allow-list** enforcement happens inside **`from_match`**.

If no format matches, construction fails (caller sees **`ValueError`** through **`Proxy`** / parser APIs).

---

## `from_match(groups)`

- Builds the struct from a **`groupdict()`** mapping.
- Normalizes naming (for example a captured **`url`** group may map to **`rotation_url`** per the regex design).
- Raises **`ValueError`** for **unsupported protocols**.

---

## `batch_parse(lines)`

- Iterates **non-empty stripped lines**.
- Each line uses the **same** **`_proxy_format_groupdict` + `from_match`** path as **`from_string`**.
- Avoids duplicating protocol or body parsing logic between single-line and batch entry points.

---

## `__post_init__` validation

After fields are assigned, the struct validates:

- **Port** is in the allowed numeric range.
- **IP vs hostname** rules (including rejection of bogus dotted-numeric “IPs” where applicable).
- **Bracketed IPv6** normalization.
- When **`rotation_url`** is set: it must look like a real URL (**`scheme`** and **`netloc`** present).

---

## `get_formatted_proxy_string(proxy, pattern)`

- Accepts either a **`Proxy`** or an **`OmniproxyParser`** instance.
- Accepts a **`ProxyPattern`** or a template string understood by the formatting layer.
- Performs token replacement and **collapses optional segments** when fields are empty so you do not get dangling punctuation.

---

## Other `utils` exports

- **`ALLOWED_PROTOCOLS`** — The protocol strings the parser and **`Proxy`** accept.

For how parsed data becomes a **`Proxy`**, see [Proxy](proxy.md).
