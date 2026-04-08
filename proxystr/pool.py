"""Managed proxy groups with rotation and cooldown blacklisting."""

from __future__ import annotations

import random
import time
from typing import Literal

from .extended_proxy import Proxy

Strategy = Literal["round_robin", "random"]


class ProxyPool:
    def __init__(
        self,
        proxies: list[Proxy | str],
        strategy: Strategy = "round_robin",
        cooldown: float = 60.0,
    ) -> None:
        self._prototypes = [
            Proxy(p) if not isinstance(p, Proxy) else p for p in proxies
        ]
        self.proxies = list(self._prototypes)
        self.strategy = strategy
        self.cooldown = cooldown
        self._index = 0
        self._cooldown_until: dict[str, float] = {}

    @staticmethod
    def _key(p: Proxy) -> str:
        return p.url

    def _purge_cooldown(self) -> None:
        now = time.time()
        for k, until in list(self._cooldown_until.items()):
            if until <= now:
                del self._cooldown_until[k]
        active_keys = {self._key(p) for p in self.proxies}
        for p in self._prototypes:
            k = self._key(p)
            if k not in self._cooldown_until and k not in active_keys:
                self.proxies.append(p)

    def get_next(self) -> Proxy | None:
        self._purge_cooldown()
        if not self.proxies:
            return None
        if self.strategy == "random":
            return random.choice(self.proxies)
        p = self.proxies[self._index % len(self.proxies)]
        self._index += 1
        return p

    def mark_failed(self, proxy: Proxy | str) -> None:
        p = Proxy(proxy) if not isinstance(proxy, Proxy) else proxy
        k = self._key(p)
        self._cooldown_until[k] = time.time() + self.cooldown
        self.proxies = [x for x in self.proxies if self._key(x) != k]

    def reset(self) -> None:
        self.proxies = list(self._prototypes)
        self._cooldown_until.clear()
        self._index = 0
