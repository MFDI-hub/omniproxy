# API Reference

Auto-generated from docstrings using [mkdocstrings](https://mkdocstrings.github.io/) and the Python handler. Narrative background for the same concepts lives under [Reference](reference/overview.md).

---

## `omniproxy` package

::: omniproxy

---

## `omniproxy.pool`

::: omniproxy.pool
    options:
      filters:
        - "!^_"

---

## `omniproxy.io`

::: omniproxy.io

---

## `omniproxy.config`

Configuration singleton (**`settings`**) and pool/check dataclasses.

::: omniproxy.config
    options:
      filters:
        - "!^_"

---

## `omniproxy.utils`

Parser struct and formatting helpers.

::: omniproxy.utils
    options:
      filters:
        - "!^_"

---

## `omniproxy.extended_proxy` (health check drivers)

Not re-exported from **`import omniproxy`**; import explicitly when you call these from application code.

::: omniproxy.extended_proxy
    options:
      members:
        - run_health_check
        - arun_health_check
      filters:
        - "!^_"
