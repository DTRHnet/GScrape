"""Protected artifact capture contracts.

Discovery may identify sensitive material, but plaintext must never be written
to SQLite, logs, or ordinary result payloads.  This module provides the
policy/classification layer and a deliberately fail-closed storage boundary.
An authenticated-encryption backend can be supplied by the deployment without
changing the scanner contract.
"""

from __future__ import annotations

import hashlib
import base64
import json
import secrets
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secrets import LocalFileSecretStore


PROMOTION_MARKERS = re.compile(
    r"(?i)\b(?:unsubscribe|manage preferences|view in browser|newsletter|marketing|"
    r"limited[- ]time offer|bonus|referral|airdrop|presale|weekly market update|"
    r"trade more|promo(?:tion| code)?|sponsored)\b"
)
HARD_BULK_MARKERS = re.compile(
    r"(?i)\b(?:unsubscribe|manage preferences|view in browser|newsletter|marketing|"
    r"sponsored|advertisement|advertising|casino|bonus code|refer a friend|"
    r"buy now|limited[- ]time offer|airdrop|presale|promo(?:tion| code)?)\b"
)
STRONG_MARKERS = re.compile(
    r"(?i)\b(?:wallet\.dat|keystore(?:\.json)?|seed phrase|recovery phrase|"
    r"mnemonic|private key|secret key|backup|restore|export|import|recover|"
    r"(?:12|15|18|21|24)[ -]words?)\b"
)
TRANSACTIONAL_MARKERS = re.compile(
    r"(?i)\b(?:withdrawal|deposit|transaction|confirmation|security alert|"
    r"account recovery|new login|two[- ]factor|verification|wallet export)\b"
)


@dataclass(frozen=True)
class RelevanceAssessment:
    score: int
    classification: str
    suppressed: bool
    reasons: tuple[str, ...]


class EncryptedArtifactBackend(Protocol):
    """Deployment-provided authenticated-encryption boundary."""

    def put(self, payload: bytes, *, metadata: Mapping[str, Any]) -> str:
        """Encrypt and persist payload, returning an opaque artifact reference."""


class LocalAESGCMArtifactBackend:
    """AES-GCM artifact backend using the existing mode-0600 local secret store.

    The key is generated once and retained through ``LocalFileSecretStore``;
    ciphertext is kept outside SQLite. The returned reference is opaque.
    """

    def __init__(self, root: str, key_store: Optional[LocalFileSecretStore] = None):
        self.root = __import__("pathlib").Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.key_store = key_store or LocalFileSecretStore(self.root / ".keys")
        self._key_ref = self._load_or_create_key()

    def _load_or_create_key(self):
        from .secrets import SecretRef
        ref = SecretRef("crypto-artifact-master-key", "local-file")
        try:
            key = self.key_store.get(ref)
            if len(key) != 32:
                raise ValueError("invalid artifact encryption key")
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            self.key_store.put(ref, key)
        return ref

    def _key(self) -> bytes:
        key = self.key_store.get(self._key_ref)
        if len(key) != 32:
            raise ValueError("invalid artifact encryption key")
        return key

    def put(self, payload: bytes, *, metadata: Mapping[str, Any]) -> str:
        artifact_ref = secrets.token_hex(24)
        nonce = secrets.token_bytes(12)
        aad = json.dumps(dict(metadata), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ciphertext = AESGCM(self._key()).encrypt(nonce, payload, aad)
        envelope = {
            "version": 1,
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "aad": base64.b64encode(aad).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        target = self.root / f"{artifact_ref}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)
        target.chmod(0o600)
        return artifact_ref

    def get(self, artifact_ref: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{48}", artifact_ref):
            raise ValueError("invalid artifact reference")
        envelope = json.loads((self.root / f"{artifact_ref}.json").read_text(encoding="utf-8"))
        if envelope.get("version") != 1 or envelope.get("algorithm") != "AES-256-GCM":
            raise ValueError("unsupported artifact envelope")
        nonce = base64.b64decode(envelope["nonce"])
        aad = base64.b64decode(envelope["aad"])
        ciphertext = base64.b64decode(envelope["ciphertext"])
        return AESGCM(self._key()).decrypt(nonce, ciphertext, aad)


def assess_relevance(record: Mapping[str, Any]) -> RelevanceAssessment:
    """Rank a candidate without returning or persisting sensitive content."""
    subject = str(record.get("subject", ""))
    sender = str(record.get("sender", ""))
    text = " ".join((subject, sender, str(record.get("safe_context", ""))))
    attachments = tuple(str(x) for x in record.get("attachment_names", ()))
    combined = " ".join((text, *attachments))
    score = 0
    reasons: list[str] = []
    promotional = bool(PROMOTION_MARKERS.search(combined))
    hard_bulk = bool(HARD_BULK_MARKERS.search(combined))
    strong = bool(STRONG_MARKERS.search(combined))
    transactional = bool(TRANSACTIONAL_MARKERS.search(combined))
    if strong:
        score += 10
        reasons.append("strong_wallet_or_recovery_signal")
    if transactional:
        score += 4
        reasons.append("transactional_or_security_signal")
    if any(re.search(r"(?i)(wallet|keystore|seed|backup|mnemonic).+\.(?:dat|json|txt|key|bak)$", name) for name in attachments):
        score += 8
        reasons.append("wallet_like_attachment_filename")
    if re.search(r"(?i)\b(?:from:me|to:me|sent|draft)\b", str(record.get("mail_context", ""))):
        score += 5
        reasons.append("self_authored_mail_context")
    if promotional:
        score -= 7
        reasons.append("promotional_or_bulk_signal")
    if hard_bulk:
        score -= 4
        reasons.append("advertisement_or_bulk_language")
    if strong and promotional:
        return RelevanceAssessment(score, "strong_candidate", False, tuple(reasons))
    if score >= 8:
        return RelevanceAssessment(score, "high", False, tuple(reasons))
    if score >= 3:
        return RelevanceAssessment(score, "medium", False, tuple(reasons))
    if promotional or hard_bulk:
        return RelevanceAssessment(score, "promotional", True, tuple(reasons))
    return RelevanceAssessment(score, "low", False, tuple(reasons))


def artifact_digest(payload: bytes) -> str:
    """Return a non-secret integrity identifier for protected payloads."""
    return hashlib.sha256(payload).hexdigest()


def capture_payload(
    backend: Optional[EncryptedArtifactBackend],
    payload: bytes,
    *,
    metadata: Mapping[str, Any],
    mode: str = "metadata-only",
) -> dict[str, Any]:
    """Capture through an approved encrypted backend, failing closed otherwise."""
    if mode not in {"metadata-only", "capture-selected", "capture-high-confidence"}:
        raise ValueError("unsupported artifact capture mode")
    result = {"status": "metadata-only", "sha256": artifact_digest(payload), "size_bytes": len(payload)}
    if mode == "metadata-only":
        return result
    if backend is None:
        raise RuntimeError("secure encrypted artifact backend is required for capture")
    result.update({"status": "captured", "artifact_ref": backend.put(payload, metadata=metadata)})
    return result