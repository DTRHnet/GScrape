#!/usr/bin/env python3
"""Safe, redacted triage for acquired backup artifacts.

This utility never extracts archives, emits matched values, or attempts recovery.
It records only archive metadata and counts/locations of sensitive-looking
categories. It is intentionally useful for prioritization, not content recovery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Iterator


PATTERNS = {
    "wallet": re.compile(r"(?i)\b(?:wallet(?:\.dat)?|bitcoin(?:[-_ ]?core)?|electrum|multibit|keystore|cold storage|paper wallet)\b"),
    "recovery": re.compile(r"(?i)\b(?:seed phrase|recovery phrase|mnemonic|recovery words|backup phrase|bip[- ]?(?:32|39|44|49|84))\b"),
    "private_key": re.compile(r"(?i)\b(?:private key|secret key|xprv|yprv|zprv|keystore)\b"),
    "credential": re.compile(r"(?i)\b(?:password|passwd|passphrase|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|smtp[_ -]?pass|database[_ -]?pass)\b"),
    "crypto_address": re.compile(r"(?i)(?<![a-z0-9])(?:bc1[a-z0-9]{25,90}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|0x[a-f0-9]{40}|ltc1[a-z0-9]{25,90})(?![a-z0-9])"),
    "wordpress": re.compile(r"(?i)\b(?:wp_users|wp_usermeta|wp_options|auth_key|secure_auth_key|logged_in_key|nonce_key|salts?)\b"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_lines(lines: Iterable[bytes], chunk_size: int = 4096) -> Dict[str, object]:
    counts = {name: 0 for name in PATTERNS}
    offsets = {name: [] for name in PATTERNS}
    total_bytes = 0
    carry = b""
    for block in lines:
        total_bytes += len(block)
        text = (carry + block).decode("utf-8", errors="replace")
        for category, pattern in PATTERNS.items():
            hits = [hit for hit in pattern.finditer(text) if hit.end() > len(carry)]
            counts[category] += len(hits)
            for hit in hits[:20]:
                if len(offsets[category]) < 20:
                    offsets[category].append(max(0, total_bytes - len(text) + hit.start()))
        carry = block[-chunk_size:]
    return {"bytes_scanned": total_bytes, "match_counts": counts, "sample_offsets": offsets}


def stream_zip_member(archive: Path, member: str) -> Iterator[bytes]:
    with zipfile.ZipFile(archive) as zf, zf.open(member, "r") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                return
            yield block


def analyze(artifact_dir: Path) -> Dict[str, object]:
    attachments = artifact_dir / "attachments"
    archives = sorted(attachments.glob("*.zip"))
    report: Dict[str, object] = {
        "artifact_dir": str(artifact_dir.resolve()),
        "analysis_mode": "metadata_and_redacted_stream_scan",
        "secrets_emitted": False,
        "archives": [],
    }
    for archive in archives:
        with zipfile.ZipFile(archive) as zf:
            members = [{"name": i.filename, "size": i.file_size, "compressed_size": i.compress_size}
                       for i in zf.infolist()]
        entry = {"filename": archive.name, "size": archive.stat().st_size,
                 "sha256": sha256_file(archive), "members": members, "scans": []}
        for member in members:
            name = member["name"]
            if name.lower().endswith((".sql", ".txt", ".log", ".json")):
                entry["scans"].append({"member": name, **scan_lines(stream_zip_member(archive, name))})
        report["archives"].append(entry)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Redacted metadata-only artifact triage")
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.artifact_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote redacted artifact triage: {args.output}")


if __name__ == "__main__":
    main()