"""Built‑in Prometheus metrics exporter (optional)."""

from __future__ import annotations

from typing import Any


class PrometheusExporter:
    """Thin adapter for prometheus_client.  Install it and pass into PoolConfig."""

    def __init__(self, registry: Any = None) -> None:
        try:
            import prometheus_client
        except ImportError:
            raise ImportError("prometheus-client is required for PrometheusExporter") from None
        self._registry = registry or prometheus_client.REGISTRY
        self._gauges: dict[str, Any] = {}
        self._counters: dict[str, Any] = {}

    def emit_gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        import prometheus_client
        key = name + str(sorted(tags.items()) if tags else "")
        if key not in self._gauges:
            self._gauges[key] = prometheus_client.Gauge(
                name, f"Omniproxy {name}", labelnames=tags.keys() if tags else [], registry=self._registry
            )
        gauge = self._gauges[key]
        if tags:
            gauge.labels(**tags).set(value)
        else:
            gauge.set(value)

    def emit_counter(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        import prometheus_client
        key = name + str(sorted(tags.items()) if tags else "")
        if key not in self._counters:
            self._counters[key] = prometheus_client.Counter(
                name, f"Omniproxy {name}", labelnames=tags.keys() if tags else [], registry=self._registry
            )
        counter = self._counters[key]
        if tags:
            counter.labels(**tags).inc(value)
        else:
            counter.inc(value)

    def close(self) -> None:
        # Nothing to do
        pass


__all__: list[str] = ["PrometheusExporter"]