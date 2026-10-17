"""Secret references, deliberately separate from persisted secret values."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SecretRef:
    """A database-safe pointer to secret material held elsewhere."""

    id: str
    backend: str


class SecretStore:
    """Minimal backend contract for future encrypted/keyring stores."""

    def put(self, secret_ref: SecretRef, value: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, secret_ref: SecretRef) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def delete(self, secret_ref: SecretRef) -> None:  # pragma: no cover - interface
        raise NotImplementedError