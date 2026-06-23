"""Built‑in Prometheus metrics exporter (optional)."""

from __future__ import annotations

from typing import Any


class PrometheusExporter:
    """Thin :class:`~omniproxy.config.MetricsExporter` adapter for ``prometheus_client``.

    The class lazily imports ``prometheus_client`` so installations that do
    not ship the dependency can still import the rest of omniproxy.

    Attributes:
        _registry (Any): ``prometheus_client`` registry used for created
            collectors.
        _gauges (dict[str, Any]): Cache of created ``Gauge`` instances keyed
            by name + sorted tag tuple.
        _counters (dict[str, Any]): Cache of created ``Counter`` instances
            keyed the same way.

    Version:
        Added in 4.0.0.
    """

    def __init__(self, registry: Any = None) -> None:
        """Build a Prometheus-backed metrics exporter.

        Args:
            registry (Any): Optional ``prometheus_client`` registry. When
                ``None``, the global default registry is used.

        Raises:
            ImportError: If ``prometheus_client`` is not installed.

        Version:
            Added in 4.0.0.
        """
        try:
            import prometheus_client
        except ImportError:
            raise ImportError("prometheus-client is required for PrometheusExporter") from None
        self._registry = registry or prometheus_client.REGISTRY
        self._gauges: dict[str, Any] = {}
        self._counters: dict[str, Any] = {}

    def emit_gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """Set the value of a Prometheus gauge.

        A gauge is created lazily the first time a given ``(name, tag-set)``
        combination is observed.

        Args:
            name (str): Gauge name.
            value (float): Value to publish.
            tags (dict[str, str] | None): Optional Prometheus labels.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
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
        """Increment a Prometheus counter.

        A counter is created lazily the first time a given ``(name, tag-set)``
        combination is observed.

        Args:
            name (str): Counter name.
            value (float): Increment amount (Prometheus requires ``>= 0``).
            tags (dict[str, str] | None): Optional Prometheus labels.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
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
        """No-op close hook for the :class:`MetricsExporter` protocol.

        ``prometheus_client`` registries are process-wide and do not need
        explicit release.

        Returns:
            None

        Version:
            Added in 4.0.0.
        """
        return None


__all__: list[str] = ["PrometheusExporter"]