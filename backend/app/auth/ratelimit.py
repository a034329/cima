"""Rate-limit + lockout de login (ADR-003, Fase D).

Anti fuerza bruta sin dependencias: ventana deslizante en memoria por clave
`(ip, email)`. Tras N fallos en la ventana se bloquea ese par durante un tiempo.

Se clava por (ip, email) — no sólo por email — para que un atacante no pueda
bloquear la cuenta de un tercero a base de fallos (DoS de lockout): el bloqueo
afecta a su IP frente a esa cuenta, no a la víctima desde su propia IP.

En memoria: vale para una instancia (el despliegue actual). Multi-worker o
multi-réplica necesitaría un backend compartido (Redis) — anotado para cuando
escale. Reinicios limpian el estado (aceptable: la ventana es de minutos).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    fails: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class LoginRateLimiter:
    def __init__(self, max_fails: int, window_s: int, lockout_s: int) -> None:
        self.max_fails = max_fails
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _now(self) -> float:
        return time.time()

    def retry_after(self, key: str) -> int:
        """Segundos restantes de bloqueo para `key`, o 0 si está permitido."""
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                return 0
            rem = b.locked_until - self._now()
            return int(rem) + 1 if rem > 0 else 0

    def record_fail(self, key: str) -> None:
        now = self._now()
        with self._lock:
            b = self._buckets.setdefault(key, _Bucket())
            b.fails = [t for t in b.fails if now - t < self.window_s]
            b.fails.append(now)
            if len(b.fails) >= self.max_fails:
                b.locked_until = now + self.lockout_s
                b.fails = []

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def clear(self) -> None:
        """Vacía todo el estado (tests)."""
        with self._lock:
            self._buckets.clear()


def make_key(ip: str | None, email: str) -> str:
    return f"{ip or '-'}|{email.strip().lower()}"
