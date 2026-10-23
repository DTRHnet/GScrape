"""Secret references, deliberately separate from persisted secret values."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets


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


class LocalFileSecretStore(SecretStore):
    """Single-user local store with opaque references and mode 0600 files."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _path(self, secret_ref: SecretRef) -> Path:
        if secret_ref.backend != "local-file" or not secret_ref.id or not all(
            character.isalnum() or character in "-_" for character in secret_ref.id
        ):
            raise ValueError("invalid local secret reference")
        return self.root / f"{secret_ref.id}.json"

    def put(self, secret_ref: SecretRef, value: bytes) -> None:
        path = self._path(secret_ref)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(value)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    def get(self, secret_ref: SecretRef) -> bytes:
        return self._path(secret_ref).read_bytes()

    def delete(self, secret_ref: SecretRef) -> None:
        self._path(secret_ref).unlink(missing_ok=True)

    def new_ref(self) -> SecretRef:
        return SecretRef(secrets.token_hex(24), "local-file")

    def put_json(self, value: dict) -> SecretRef:
        ref = self.new_ref()
        self.put(ref, json.dumps(value, sort_keys=True).encode("utf-8"))
        return ref

    def get_json(self, ref: SecretRef) -> dict:
        value = json.loads(self.get(ref).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("stored credential payload is invalid")
        return value