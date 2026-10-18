#!/usr/bin/env python3
"""Paginated Gmail API search with in-memory variant matching.

Enhanced with additional analysis, reporting, scoring, extraction,
export, and operational helper functions. All new functions are
heavily commented to explain purpose and behavior.
"""

import argparse
import base64
import csv
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


API_ROOT = "https://gmail.googleapis.com/gmail/v1/users"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DEFAULT_QUERY = 'gourl OR "go url" OR "gourl.io" OR "gourl dot io"'
IDENTITY_TERMS = ("jstacey", "jonstacey", "jstacy", "jonstacy", "johnstacy", "johnstacey")
CATEGORY_PATTERNS = {
    "gourl": re.compile(r"(?i)(?<![a-z0-9])(?:g\s*[-_.]?\s*o\s*[-_.]?\s*u\s*[-_.]?\s*r\s*[-_.]?\s*l)\s*(?:\.|\(\s*dot\s*\)|\[\s*dot\s*\]|\bdot\b)\s*i\s*o\b"),
    "wallet": re.compile(r"(?i)\b(?:wallet(?:\.dat)?|bitcoin[-_ ]?qt|electrum|multibit|crypto(?:currency)?|blockchain|hardware wallet|cold storage|exchange account|coinbase|kraken|binance|bitcoin core|armory|exodus|ethereum|litecoin|dogecoin|monero|ripple|xrp|trezor|ledger)\b"),
    "seed": re.compile(r"(?i)\b(?:seed phrase|recovery phrase|mnemonic(?: phrase)?|recovery words|backup phrase|bip[- ]?(?:32|39|44|49|84)|paper wallet|secret key|private key|keystore(?:\.json)?)\b"),
    "wallet-file": re.compile(r"(?i)(?<![\w.-])[\w.-]{0,100}(?:wallet|bitcoin|litecoin|dogecoin|monero|ethereum|keystore|backup|walletbackup)[\w.-]*\.(?:dat|wallet|key|json|backup|bak|old|db|sqlite|aes|txt)(?!\w)|(?<![\w.-])wal[^/\\\s]{0,80}\.dat\b"),
    "crypto-address": re.compile(r"(?i)(?<![a-z0-9])(?:bc1[a-z0-9]{25,90}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|0x[a-f0-9]{40}|ltc1[a-z0-9]{25,90})(?![a-z0-9])"),
    "identity": re.compile(r"(?i)\b(?:jstacey|jonstacey|jstacy|jonstacy|johnstacy|johnstacey)\b"),
    "phone": re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?(?:\([2-9]\d{2}\)|[2-9]\d{2})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"),
}
CATEGORY_NAMES = tuple(CATEGORY_PATTERNS)
SEARCH_PASSES = {
    "gourl": ['gourl OR "go url" OR "gourl.io" OR "gourl dot io"', '"go-url" OR "go_url" OR "g o u r l" OR "gourl io"'],
    "wallet": ['wallet OR "bitcoin core" OR electrum OR multibit OR "paper wallet"', 'cryptocurrency OR blockchain OR bitcoin OR ethereum OR litecoin OR dogecoin', 'trezor OR ledger OR exodus OR armory OR keystore OR "cold storage"', '"wallet backup" OR "wallet.dat" OR "private key" OR "public key"'],
    "seed": ['"seed phrase" OR "recovery phrase" OR mnemonic OR "recovery words"', '"12 word" OR "15 word" OR "18 word" OR "21 word" OR "24 word"', 'BIP39 OR BIP32 OR BIP44 OR "paper wallet" OR "backup phrase"'],
    "wallet-file": ['filename:wallet.dat OR filename:wallet OR filename:backup', 'filename:dat OR filename:wallet OR filename:keystore OR filename:backup', '"wallet.dat" OR "bitcoin.dat" OR "keystore.json" OR "wallet backup"'],
    "crypto-address": ['"bitcoin address" OR "BTC address" OR "ETH address" OR "deposit address"', '"transaction ID" OR txid OR "receive address" OR "public address"'],
    "identity": ["jstacey OR jonstacey OR jstacy OR jonstacy OR johnstacy OR johnstacey"],
    "phone": ['phone OR mobile OR cellphone OR telephone OR "phone number"', '"contact number" OR "call me" OR "text me"'],
}
SECRET_PATTERNS = [
    re.compile(r"(?i)\b(?:seed phrase|recovery phrase|mnemonic|private key|secret key|password|access token|refresh token)\s*[:=]\s*\S+(?:\s+\S+){0,30}"),
    re.compile(r"\b(?:[a-fA-F0-9]{64}|0x[a-fA-F0-9]{64})\b"),
    re.compile(r"\b(?:[a-z]{3,12}\s+){11,23}[a-z]{3,12}\b", re.I),
]

# ---------------------------------------------------------------------------
# Category weighting used by relevance scoring
# Higher weight = more interesting / higher-risk match
# ---------------------------------------------------------------------------
CATEGORY_WEIGHTS = {
    "seed": 10.0,
    "wallet-file": 9.0,
    "identity": 8.0,
    "phone": 5.0,
    "gourl": 7.0,
    "crypto-address": 6.0,
    "wallet": 4.0,
}


class GmailAPIError(RuntimeError):
    """A redacted Gmail API error with enough context for safe recovery."""

    def __init__(self, code: int, reason: str = "", message: str = ""):
        self.code = code
        self.reason = reason
        self.api_message = message
        detail = f"Gmail API request failed with HTTP {code}"
        if reason:
            detail += f" ({reason})"
        if code == 403 and reason in {"insufficientPermissions", "forbidden"}:
            detail += "; re-authorize the account so the OAuth grant includes gmail.readonly"
        elif code == 403 and reason == "accessNotConfigured":
            detail += "; enable the Gmail API in the Google Cloud project used by this OAuth client"
        elif code == 403 and reason in {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"}:
            detail += "; Gmail API quota or rate limit was reached"
        super().__init__(detail)


class AdaptiveRateLimiter:
    """Bound Gmail request pacing and adapt cooldown after rate errors."""

    def __init__(self, requests_per_second: float = 1.0, max_delay: float = 60.0,
                 clock=time.monotonic, sleeper=time.sleep):
        if requests_per_second < 0:
            raise ValueError("requests_per_second must be non-negative")
        if max_delay <= 0:
            raise ValueError("max_delay must be positive")
        self.requests_per_second = float(requests_per_second)
        self.interval = 1.0 / self.requests_per_second if self.requests_per_second else 0.0
        self.max_delay = float(max_delay)
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._cooldown = 0.0

    def wait(self) -> float:
        """Wait until the next request is allowed and return waited seconds."""
        with self._lock:
            now = self._clock()
            delay = max(0.0, self._next_request_at - now)
            if delay:
                self._sleep(delay)
                now += delay
            self._next_request_at = now + self.interval
            return delay

    def penalize(self, delay: Optional[float] = None) -> float:
        """Schedule a bounded cooldown after a rate-limit response."""
        with self._lock:
            if delay is None:
                delay = 1.0 if not self._cooldown else min(self._cooldown * 2.0, self.max_delay)
            delay = max(0.0, min(float(delay), self.max_delay))
            self._cooldown = delay
            self._next_request_at = max(self._next_request_at, self._clock() + delay)
            return delay

    def reset(self) -> None:
        with self._lock:
            self._cooldown = 0.0


DEFAULT_RATE_LIMITER = AdaptiveRateLimiter()


# =============================================================================
# Core utility helpers (original + enhanced)
# =============================================================================

def load_dotenv(path: Path) -> Dict[str, str]:
    """Load key=value pairs from a .env-style file into a dict.

    Skips comments and blank lines. Does not overwrite already-set
    environment variables (callers should merge with os.environ themselves).
    Returns an empty dict if the file does not exist.
    """
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            values[name] = value
    return values


def text_from_part(part: Dict[str, Any]) -> str:
    """Recursively extract plain-text content from a Gmail message part tree.

    Handles both simple body payloads and nested multipart messages.
    Base64url-decodes the body data when present. Never fetches attachment
    bytes—only the textual body is returned.
    """
    body = part.get("body", {}).get("data")
    if body:
        try:
            decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            return decoded.decode("utf-8", errors="replace")
        except (ValueError, UnicodeError):
            return ""
    return "".join(text_from_part(child) for child in part.get("parts", []))


def normalized_text(value: str) -> str:
    """Normalize visible text for safer pattern matching.

    Performs Unicode NFKC normalization, HTML entity unescaping, strips
    <script>/<style> blocks and remaining tags, removes zero-width and
    bidirectional control characters, then collapses whitespace.
    The result is intended for in-memory matching only and is never persisted.
    """
    value = unicodedata.normalize("NFKC", html.unescape(value))
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\u200b", "").replace("\ufeff", "")
    value = re.sub(r"[\u2000-\u200f\u202a-\u202e\u2060]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def attachment_names(part: Dict[str, Any]) -> List[str]:
    """Return a flat list of attachment filenames from a message part tree.

    Only filenames are collected; attachment content is never downloaded.
    This keeps the tool metadata-only and reduces risk of handling binary
    wallet files or keystores.
    """
    names = [str(part["filename"])] if part.get("filename") else []
    for child in part.get("parts", []):
        names.extend(attachment_names(child))
    return names


def redact_metadata(value: Any) -> str:
    """Redact obvious secrets from short metadata fields (subject, from, etc.).

    Applies the global SECRET_PATTERNS and truncates to 500 characters so
    that log records remain useful while avoiding accidental leakage of
    seed phrases, private keys, or long token strings.
    """
    value = str(value or "")
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value[:500]


def matched_categories(text: str, filenames: Sequence[str] = ()) -> List[str]:
    """Return the list of category names that match the given text + filenames.

    Builds a single normalized haystack from body text and attachment names,
    then tests every pattern in CATEGORY_PATTERNS. Order of the returned list
    follows the definition order of CATEGORY_PATTERNS.
    """
    haystack = normalized_text(text + " " + " ".join(filenames))
    return [name for name, pattern in CATEGORY_PATTERNS.items() if pattern.search(haystack)]


COMMON_ATTACHMENT_TYPES = {
    "documents": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".csv"},
    "spreadsheets": {".xls", ".xlsx", ".ods", ".csv"},
    "archives": {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2"},
    "images": {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp"},
    "wallets": {".dat", ".wallet", ".key", ".json", ".backup", ".bak", ".db", ".sqlite"},
    "email": {".eml", ".msg"},
}


def normalize_attachment_extensions(values: Optional[Sequence[str]] = None) -> List[str]:
    result = []
    for value in values or ():
        extension = str(value).strip().lower()
        if extension and not extension.startswith("."):
            extension = "." + extension
        if extension and re.fullmatch(r"\.[a-z0-9][a-z0-9._-]{0,19}", extension):
            result.append(extension)
        elif extension:
            raise ValueError("attachment extensions must be simple values such as .pdf or .dat")
    return list(dict.fromkeys(result))


def attachment_filter_matches(filenames: Sequence[str], extensions=(), filename_regex="") -> bool:
    """Match attachment metadata only; attachment content is never downloaded."""
    names = [str(name) for name in filenames]
    if not names:
        return False
    selected = set(normalize_attachment_extensions(extensions))
    pattern = re.compile(filename_regex, re.I) if filename_regex else None
    return any((not selected or Path(name).suffix.lower() in selected)
               and (pattern is None or pattern.search(Path(name).name)) for name in names)


def normalize_category_selection(
    categories: Optional[Sequence[str]] = None,
    category: str = "all",
) -> List[str]:
    """Normalize legacy single-category and new multi-category inputs."""
    if categories:
        selected = [str(item).strip() for item in categories if str(item).strip()]
    elif category and category != "all":
        selected = [category]
    else:
        selected = list(CATEGORY_NAMES)
    if not selected:
        raise ValueError("select at least one discovery category")
    invalid = sorted(set(selected) - set(CATEGORY_NAMES))
    if invalid:
        raise ValueError(f"unsupported category: {invalid[0]}")
    return [name for name in CATEGORY_NAMES if name in selected]


def compile_category_regexes(raw: Optional[Dict[str, str]] = None) -> Dict[str, re.Pattern]:
    """Compile optional expert overrides without exposing pattern matches."""
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("category regex overrides must be an object")
    compiled = {}
    for category, expression in (raw or {}).items():
        if category not in CATEGORY_PATTERNS:
            raise ValueError(f"unsupported category: {category}")
        if expression:
            compiled[category] = re.compile(expression, re.I)
    return compiled


def header(message: Dict[str, Any], name: str) -> str:
    """Extract a single header value (case-insensitive) from a Gmail message.

    Returns an empty string when the header is absent.
    """
    for item in message.get("payload", {}).get("headers", []):
        if item.get("name", "").lower() == name.lower():
            return item.get("value", "")
    return ""


# =============================================================================
# New / creative utility functions
# =============================================================================

def extract_email_addresses(text: str) -> List[str]:
    """Extract likely email addresses from free-form text.

    Uses a pragmatic regex that catches most real-world addresses while
    avoiding common false positives. Returns a de-duplicated list preserving
    first-seen order. Useful for discovering related accounts or reply-to
    addresses that appear inside message bodies.
    """
    # Conservative pattern: local-part @ domain with at least one dot
    pattern = re.compile(
        r"(?<![a-zA-Z0-9._%+\-])"
        r"[a-zA-Z0-9._%+\-]+@"
        r"[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
        r"(?![a-zA-Z0-9.\-])"
    )
    seen: Set[str] = set()
    results: List[str] = []
    for match in pattern.finditer(text or ""):
        addr = match.group(0).lower()
        if addr not in seen:
            seen.add(addr)
            results.append(addr)
    return results


def parse_email_date(date_header: str) -> Optional[datetime]:
    """Parse an RFC 2822 Date header into a timezone-aware datetime.

    Returns None on failure. Useful for sorting matches chronologically or
    for building time-based reports without relying on Gmail's internal
    internalDate field (which is still available if needed).
    """
    if not date_header:
        return None
    try:
        dt = parsedate_to_datetime(date_header)
        # Ensure timezone-aware; treat naïve as UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError, IndexError):
        return None


def score_match_relevance(
    categories: Sequence[str],
    body: str = "",
    filenames: Sequence[str] = (),
) -> float:
    """Compute a simple relevance / risk score for a match.

    Scoring model:
      - Base score = sum of CATEGORY_WEIGHTS for each matched category
      - Bonus +2.0 if a high-value category (seed / wallet-file) co-occurs
        with identity
      - Bonus +1.5 if any attachment filename looks like a wallet/keystore
      - Small bonus for presence of multiple distinct categories

    The score is purely heuristic and intended to help prioritise review;
    it is not a security assertion.
    """
    if not categories:
        return 0.0
    score = sum(CATEGORY_WEIGHTS.get(c, 1.0) for c in categories)
    cats = set(categories)
    if "identity" in cats and cats & {"seed", "wallet-file", "gourl"}:
        score += 2.0
    if any(re.search(r"(?i)wallet|keystore|backup|\.dat$", name or "") for name in filenames):
        score += 1.5
    if len(cats) >= 3:
        score += 1.0
    # Tiny body-length dampener so very short matches don't dominate
    if body and len(body) < 40:
        score *= 0.85
    return round(score, 2)


def looks_like_seed_phrase(text: str, min_words: int = 12, max_words: int = 24) -> bool:
    """Heuristic check whether a stretch of text resembles a BIP-39 style seed.

    Looks for sequences of 12–24 common English words (3–12 letters) separated
    by whitespace. This is intentionally conservative and will produce false
    positives on ordinary prose; it is meant only as a secondary signal after
    the primary CATEGORY_PATTERNS have already flagged the message.
    """
    if not text:
        return False
    # Find candidate runs of lowercase words
    words = re.findall(r"\b[a-z]{3,12}\b", text.lower())
    if len(words) < min_words:
        return False
    # Sliding window looking for contiguous runs of the right length
    for i in range(len(words) - min_words + 1):
        window = words[i : i + max_words]
        if min_words <= len(window) <= max_words:
            # Very rough: most BIP-39 words are common; accept the window
            return True
    return False


def extract_crypto_addresses(text: str) -> List[str]:
    """Extract Bitcoin, Ethereum and Litecoin-style addresses from text.

    Re-uses the same regex family as CATEGORY_PATTERNS["crypto-address"] but
    returns the actual matched strings (de-duplicated). Useful for building
    a quick inventory of addresses that appeared in the scanned corpus.
    """
    pattern = CATEGORY_PATTERNS["crypto-address"]
    seen: Set[str] = set()
    results: List[str] = []
    for match in pattern.finditer(text or ""):
        addr = match.group(0)
        if addr not in seen:
            seen.add(addr)
            results.append(addr)
    return results


def extract_possible_filenames(text: str) -> List[str]:
    """Pull likely filename-like tokens from free text (not just attachments).

    Looks for tokens that end in common wallet/backup extensions. Helps catch
    cases where a user wrote "I sent you wallet.dat" inside the body rather
    than attaching the file.
    """
    pattern = re.compile(
        r"(?<![\w.-])"
        r"[\w.-]{1,80}\.(?:dat|wallet|key|json|backup|bak|old|db|sqlite|aes|txt)"
        r"(?!\w)",
        re.I,
    )
    seen: Set[str] = set()
    results: List[str] = []
    for match in pattern.finditer(text or ""):
        name = match.group(0)
        if name.lower() not in seen:
            seen.add(name.lower())
            results.append(name)
    return results


def extract_wallet_and_recovery_suspects(
    body: str,
    filenames: Sequence[str] = (),
    *,
    max_phrase_samples: int = 3,
    context_chars: int = 80,
) -> Dict[str, Any]:
    """Detect suspected wallet files and recovery/seed phrases in a message.

    This is a higher-signal extractor than the coarse category patterns. It
    returns structured findings that are safe to log:

    Returns a dict with:
      wallet_files: list of suspected wallet/backup filenames
        - source: "attachment" | "body_mention"
        - name: filename string (redacted if it looks like it embeds a secret)
        - confidence: "high" | "medium" | "low"
        - reason: short explanation
      recovery_phrases: list of suspected mnemonic/seed evidence
        - word_count: 12/15/18/21/24 when detectable
        - confidence: "high" | "medium" | "low"
        - cue: nearby keyword that triggered the hit (seed/recovery/...)
        - preview: heavily redacted sample (first 2 + last 2 words only)
        - context_snippet: short surrounding text with the phrase itself redacted
      signals: flat list of human-readable reasons the message was flagged
      has_high_confidence: bool — True if any high-confidence wallet file or
        recovery phrase was found

    Design goals:
      1. Prefer attachment filenames and explicit keyword+word-count combos.
      2. Never return a full recovery phrase — only counts and redacted previews.
      3. Reduce false positives from ordinary English prose and marketing email.
      4. Stay metadata-oriented; no attachment bytes are read.
    """
    body = body or ""
    filenames = list(filenames or [])
    norm = normalized_text(body)

    wallet_files: List[Dict[str, Any]] = []
    recovery_phrases: List[Dict[str, Any]] = []
    signals: List[str] = []
    seen_names: Set[str] = set()

    # ------------------------------------------------------------------
    # 1. Wallet / keystore / backup files from attachment names
    # ------------------------------------------------------------------
    # High confidence when the filename itself strongly implies a wallet.
    attachment_wallet_re = re.compile(
        r"(?:^|[_\-.])"
        r"(?:wallet|bitcoin|litecoin|dogecoin|monero|ethereum|eth|btc|ltc|"
        r"keystore|keyfile|walletbackup|backup|electrum|armory|exodus|"
        r"multibit|trezor|ledger)"
        r"(?:[_\-.]|$)|"
        r"\.(?:dat|wallet|key|keystore|backup|bak|aes)$",
        re.I,
    )
    # Medium: generic backup-looking names that often accompany wallets
    attachment_backup_re = re.compile(
        r"(?:backup|export|dump|keys?|seed|mnemonic|recovery)"
        r".*\.(?:dat|json|txt|bak|old|wallet|aes|zip|gpg|asc)$|"
        r"\.(?:dat|wallet|keystore)$",
        re.I,
    )

    for name in filenames:
        if not name or name.lower() in seen_names:
            continue
        low = name.lower()
        if attachment_wallet_re.search(name):
            conf = "high"
            reason = "attachment filename matches wallet/keystore pattern"
        elif attachment_backup_re.search(name):
            conf = "medium"
            reason = "attachment filename looks like a backup/export of keys"
        else:
            continue
        seen_names.add(low)
        wallet_files.append(
            {
                "source": "attachment",
                "name": redact_metadata(name)[:120],
                "confidence": conf,
                "reason": reason,
            }
        )
        signals.append(f"attachment:{redact_metadata(name)[:60]}")

    # ------------------------------------------------------------------
    # 2. Wallet filenames mentioned only in the body text
    # ------------------------------------------------------------------
    body_name_re = re.compile(
        r"(?<![\w.-])"
        r"([\w.-]{1,100}(?:wallet|bitcoin|litecoin|dogecoin|monero|ethereum|"
        r"keystore|backup|walletbackup|electrum)[\w.-]*"
        r"\.(?:dat|wallet|key|json|backup|bak|old|db|sqlite|aes|txt))"
        r"(?!\w)",
        re.I,
    )
    for match in body_name_re.finditer(body):
        name = match.group(1)
        low = name.lower()
        if low in seen_names:
            continue
        seen_names.add(low)
        wallet_files.append(
            {
                "source": "body_mention",
                "name": redact_metadata(name)[:120],
                "confidence": "medium",
                "reason": "filename-like token mentioned in message body",
            }
        )
        signals.append(f"body-filename:{redact_metadata(name)[:60]}")

    # Generic "wallet.dat" / "keystore.json" style mentions (even without brand)
    generic_file_re = re.compile(
        r"(?i)\b(wallet\.dat|bitcoin\.dat|keystore\.json|wallet\.backup|"
        r"wallet-backup\.(?:dat|json|zip)|electrum\.dat)\b"
    )
    for match in generic_file_re.finditer(body):
        name = match.group(1)
        low = name.lower()
        if low in seen_names:
            continue
        seen_names.add(low)
        wallet_files.append(
            {
                "source": "body_mention",
                "name": name,
                "confidence": "high",
                "reason": "classic wallet filename mentioned in body",
            }
        )
        signals.append(f"classic-name:{redact_metadata(name)}")

    # ------------------------------------------------------------------
    # 3. Recovery / seed phrase detection (never return full phrase)
    # ------------------------------------------------------------------
    # Strong cue words that make a nearby word-run much more credible
    cue_re = re.compile(
        r"(?i)\b("
        r"seed\s*phrase|recovery\s*phrase|mnemonic(?:\s*phrase)?|"
        r"recovery\s*words|backup\s*phrase|secret\s*phrase|"
        r"bip[- ]?39|write\s*(?:these|down)|backup\s*these\s*words|"
        r"12\s*words?|15\s*words?|18\s*words?|21\s*words?|24\s*words?"
        r")\b"
    )
    def _redacted_preview(words: List[str]) -> str:
        """Return a shape-only preview; never expose mnemonic words."""
        if len(words) <= 4:
            return "[REDACTED] […] [REDACTED]"
        return "[REDACTED] [REDACTED] […] [REDACTED] [REDACTED]"

    def _snippet_around(start: int, end: int) -> str:
        """Return safe context without retaining neighboring mnemonic words.

        A phrase may be longer than the selected scoring window. Returning
        arbitrary surrounding text could therefore leak adjacent seed words,
        so the persisted context is intentionally cue-oriented only.
        """
        cue = nearest_cue((start + end) // 2)
        return (
            f"near '{redact_metadata(cue[0])}': "
            "<<RECOVERY_PHRASE_REDACTED>>"
            if cue else "<<RECOVERY_PHRASE_REDACTED>>"
        )

    # Build contiguous runs of short dictionary-like words. A run breaks when
    # two tokens are separated by more than a couple of spaces/punctuation.
    token_re = re.compile(r"[A-Za-z]{3,12}")
    raw_tokens = [(m.group(0).lower(), m.start(), m.end()) for m in token_re.finditer(norm)]

    runs: List[List[Tuple[str, int, int]]] = []
    current: List[Tuple[str, int, int]] = []
    for tok in raw_tokens:
        if not current:
            current = [tok]
            continue
        gap = tok[1] - current[-1][2]
        # allow a little punctuation/space between words in a phrase
        if gap <= 3:
            current.append(tok)
        else:
            if len(current) >= 12:
                runs.append(current)
            current = [tok]
    if len(current) >= 12:
        runs.append(current)

    cue_spans = [(m.start(), m.end(), m.group(1).lower()) for m in cue_re.finditer(norm)]
    found_phrase_hashes: Set[str] = set()
    covered_spans: List[Tuple[int, int]] = []
    bip_lengths = (24, 21, 18, 15, 12)

    def nearest_cue(mid: int):
        best = None
        best_dist = 10**9
        for cs, ce, cue in cue_spans:
            dist = min(abs(mid - cs), abs(mid - ce))
            if dist < best_dist:
                best_dist = dist
                best = (cue, best_dist)
        return best

    def overlaps_covered(start: int, end: int) -> bool:
        for cs, ce in covered_spans:
            overlap = max(0, min(end, ce) - max(start, cs))
            shorter = min(end - start, ce - cs)
            if shorter > 0 and overlap / shorter > 0.5:
                return True
        return False

    # Cue vocabulary to exclude from phrase windows when possible
    cue_words = {
        "seed", "phrase", "recovery", "mnemonic", "backup", "words", "word",
        "secret", "private", "key", "bip", "write", "these", "down", "carefully",
        "here", "keep", "safe", "please", "find", "attached", "my", "the", "and",
    }

    for run in runs:
        best = None  # (score, length, i, confidence, cue)

        for length in bip_lengths:
            if len(run) < length:
                continue
            for i in range(0, len(run) - length + 1):
                window = run[i : i + length]
                start_i, end_i = window[0][1], window[-1][2]
                mid = (start_i + end_i) // 2
                if overlaps_covered(start_i, end_i):
                    continue

                words = [t[0] for t in window]
                # Penalize windows that still contain cue/prose filler words
                filler = sum(1 for w in words if w in cue_words)
                if filler > max(2, length // 6):
                    continue

                cue_info = nearest_cue(mid)
                if cue_info is not None:
                    cue, dist = cue_info
                else:
                    cue, dist = None, 10**9

                if cue is not None and dist <= 220:
                    confidence = "high" if length in (12, 24) else "medium"
                    score = 80 - min(dist, 200) // 4 + length * 5
                elif cue is not None and dist <= 500:
                    confidence = "medium"
                    score = 30 - dist // 20 + length * 5
                else:
                    if length not in (12, 24):
                        continue
                    confidence = "low"
                    cue = None
                    score = length * 5

                # Strongly prefer windows without cue/prose filler words
                score -= filler * 40
                # Prefer windows that begin after the nearest cue ends
                if cue_info is not None:
                    cue_end = None
                    for cs, ce, c in cue_spans:
                        if c == cue:
                            cue_end = ce
                            break
                    if cue_end is not None and start_i >= cue_end - 2:
                        score += 20

                if best is None or score > best[0]:
                    best = (score, length, i, confidence, cue, words, start_i, end_i)

        if best is None:
            continue

        _, length, i, confidence, cue, words, start_i, end_i = best
        fingerprint = f"{length}:{words[0]}:{words[-1]}:{start_i}"
        if fingerprint in found_phrase_hashes:
            continue
        found_phrase_hashes.add(fingerprint)
        covered_spans.append((start_i, end_i))

        recovery_phrases.append(
            {
                "word_count": length,
                "confidence": confidence,
                "cue": cue,
                "preview": _redacted_preview(words),
                "context_snippet": _snippet_around(start_i, end_i),
            }
        )
        signals.append(
            f"recovery:{length}-words" + (f" near '{cue}'" if cue else " (no cue)")
        )
        if len(recovery_phrases) >= max_phrase_samples:
            break

    # Also flag messages that talk about seed/recovery even if no word-run matched
    if not recovery_phrases and cue_spans:
        for _, _, cue in cue_spans[:2]:
            recovery_phrases.append(
                {
                    "word_count": None,
                    "confidence": "low",
                    "cue": cue,
                    "preview": None,
                    "context_snippet": None,
                    "note": "cue present but no clear 12–24 word run detected",
                }
            )
            signals.append(f"cue-only:{cue}")

    has_high = any(w["confidence"] == "high" for w in wallet_files) or any(
        r["confidence"] == "high" for r in recovery_phrases
    )

    return {
        "wallet_files": wallet_files,
        "recovery_phrases": recovery_phrases,
        "signals": signals,
        "has_high_confidence": has_high,
        "wallet_file_count": len(wallet_files),
        "recovery_phrase_count": len(recovery_phrases),
    }





def human_readable_size(num_bytes: int) -> str:
    """Convert a byte count into a short human-readable string (KiB, MiB…).

    Purely cosmetic helper used by reporting functions.
    """
    if num_bytes < 1024:
        return f"{num_bytes} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        num_bytes /= 1024.0
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
    return f"{num_bytes:.1f} PiB"


def build_gmail_search_url(
    query: str,
    profile_index: str = "0",
    after: Optional[str] = None,
    before: Optional[str] = None,
) -> str:
    """Construct a clickable Gmail web-search URL for the same query.

    Useful for manually verifying results in the browser. The URL opens the
    Gmail search UI with the query pre-filled; date operators are appended
    when supplied.
    """
    parts = [query]
    if after:
        parts.append(f"after:{after}")
    if before:
        parts.append(f"before:{before}")
    encoded = urllib.parse.quote(" ".join(parts))
    return f"https://mail.google.com/mail/u/{profile_index}/#search/{encoded}"


def suggest_refined_queries(base_category: str) -> List[str]:
    """Return a short list of alternative / refined Gmail search queries.

    Intended as a creative helper when the primary SEARCH_PASSES for a
    category produce too much noise or too few hits. The suggestions are
    static knowledge, not dynamically generated from results.
    """
    suggestions = {
        "gourl": [
            '"gourl.io" OR "go-url.io"',
            "subject:(gourl OR \"go url\")",
            "from:(gourl OR support@gourl)",
        ],
        "wallet": [
            "has:attachment filename:wallet.dat",
            "(\"bitcoin core\" OR electrum) (backup OR restore)",
            "subject:(wallet OR keystore) has:attachment",
        ],
        "seed": [
            "\"12 words\" OR \"24 words\" (seed OR recovery OR mnemonic)",
            "subject:(seed OR recovery OR mnemonic)",
            "\"write these words\" OR \"recovery phrase\"",
        ],
        "wallet-file": [
            "filename:wallet.dat OR filename:keystore.json",
            "has:attachment (wallet OR backup) (dat OR json OR bak)",
        ],
        "crypto-address": [
            "\"bc1\" OR \"0x\" (address OR deposit OR receive)",
            "txid OR \"transaction id\" OR \"tx id\"",
        ],
        "identity": [
            "jstacey OR jonstacey OR \"jon stacey\"",
            "from:(jstacey OR jonstacey)",
        ],
        "phone": [
            "phone OR mobile OR cellphone OR telephone",
            "\"phone number\" OR \"contact number\" OR \"call me\"",
        ],
    }
    return suggestions.get(base_category, [])


def rate_limit_sleep(attempt: int, base: float = 1.0, cap: float = 30.0) -> None:
    """Sleep with exponential backoff, capped at `cap` seconds.

    Used by the request helper and available for any caller that wants a
    consistent backoff policy. `attempt` is zero-based.
    """
    delay = min(base * (2 ** attempt), cap)
    time.sleep(delay)


def safe_json_loads(raw: str, default: Any = None) -> Any:
    """Parse JSON and return `default` on any failure instead of raising.

    Handy when reading log files that may contain truncated or corrupted
    lines.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def normalize_gmail_date(value: str, *, inclusive_end: bool = False) -> str:
    """Normalize UI/CLI dates to Gmail's YYYY/MM/DD format.

    The dashboard presents the ``before`` field as an inclusive date, while
    Gmail's ``before:`` operator is exclusive. A date-only inclusive end is
    therefore advanced by one day.
    """
    value = str(value or "").strip()
    if not value:
        return ""
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if not match:
        raise ValueError("dates must use YYYY/MM/DD or YYYY-MM-DD")
    year, month, day = map(int, match.groups())
    try:
        parsed = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError(f"invalid date: {value}") from error
    if inclusive_end:
        parsed += timedelta(days=1)
    return parsed.strftime("%Y/%m/%d")


# =============================================================================
# Reporting & analysis functions
# =============================================================================

def load_match_records(log_path: Path) -> List[Dict[str, Any]]:
    """Load all 'gmail-crypto-discovery-match' records from a JSONL log file.

    Skips audit lines and any unparseable rows. Returns a list of dicts
    ready for further analysis or export.
    """
    records: List[Dict[str, Any]] = []
    if not log_path.exists():
        return records
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = safe_json_loads(line)
            if isinstance(obj, dict) and obj.get("event") == "gmail-crypto-discovery-match":
                records.append(obj)
    return records


def load_audit_records(log_path: Path) -> List[Dict[str, Any]]:
    """Load the audit / summary records written at the end of each search run."""
    records: List[Dict[str, Any]] = []
    if not log_path.exists():
        return records
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            obj = safe_json_loads(line.strip())
            if isinstance(obj, dict) and obj.get("event") == "gmail-crypto-search-audit":
                records.append(obj)
    return records


def summarize_matches(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Produce a high-level statistical summary of a list of match records.

    Returns a dict containing:
      - total_matches
      - category_counts (Counter)
      - top_senders
      - date_range (earliest / latest parsed Date headers)
      - high_score_count (how many scored >= 12)
      - unique_message_ids
    """
    if not records:
        return {
            "total_matches": 0,
            "category_counts": {},
            "top_senders": [],
            "date_range": None,
            "high_score_count": 0,
            "unique_message_ids": 0,
        }

    cat_counter: Counter = Counter()
    sender_counter: Counter = Counter()
    dates: List[datetime] = []
    high_score = 0
    ids: Set[str] = set()

    for rec in records:
        cats = rec.get("categories") or []
        cat_counter.update(cats)
        sender = (rec.get("sender") or "").strip()
        if sender:
            sender_counter[sender] += 1
        dt = parse_email_date(rec.get("message_date") or "")
        if dt:
            dates.append(dt)
        # Re-score on the fly if the record already has categories
        score = score_match_relevance(cats)
        if score >= 12.0:
            high_score += 1
        # message_location contains the id; extract it roughly
        loc = rec.get("message_location") or ""
        if "/#all/" in loc:
            ids.add(loc.rsplit("/#all/", 1)[-1])

    date_range = None
    if dates:
        dates.sort()
        date_range = {
            "earliest": dates[0].isoformat(),
            "latest": dates[-1].isoformat(),
        }

    return {
        "total_matches": len(records),
        "category_counts": dict(cat_counter.most_common()),
        "top_senders": sender_counter.most_common(15),
        "date_range": date_range,
        "high_score_count": high_score,
        "unique_message_ids": len(ids) or len(records),
    }


def print_summary_report(summary: Dict[str, Any], title: str = "Match Summary") -> None:
    """Pretty-print a summary dict produced by summarize_matches() to stdout.

    Purely presentational; does not write any files.
    """
    print(f"\n=== {title} ===")
    print(f"Total matches          : {summary.get('total_matches', 0)}")
    print(f"Unique message IDs     : {summary.get('unique_message_ids', 0)}")
    print(f"High-score matches     : {summary.get('high_score_count', 0)}")
    dr = summary.get("date_range")
    if dr:
        print(f"Date range             : {dr['earliest']} → {dr['latest']}")
    print("\nCategory breakdown:")
    for cat, count in (summary.get("category_counts") or {}).items():
        print(f"  {cat:18s} {count}")
    print("\nTop senders:")
    for sender, count in (summary.get("top_senders") or [])[:10]:
        print(f"  {count:3d}  {sender[:80]}")
    print()


def export_matches_csv(records: Sequence[Dict[str, Any]], csv_path: Path) -> int:
    """Export match records to a CSV file suitable for spreadsheets.

    Columns: message_date, sender, subject, categories, score,
    attachment_names, message_location, matched_search_passes.
    Returns the number of rows written.
    """
    fieldnames = [
        "message_date",
        "sender",
        "subject",
        "categories",
        "score",
        "attachment_names",
        "message_location",
        "matched_search_passes",
    ]
    count = 0
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            cats = rec.get("categories") or []
            row = {
                "message_date": rec.get("message_date", ""),
                "sender": rec.get("sender", ""),
                "subject": rec.get("subject", ""),
                "categories": "|".join(cats),
                "score": rec.get("relevance_score", score_match_relevance(cats)),
                "attachment_names": "|".join(rec.get("attachment_names") or []),
                "message_location": rec.get("message_location", ""),
                "matched_search_passes": "|".join(rec.get("matched_search_passes") or []),
            }
            writer.writerow(row)
            count += 1
    return count


def export_matches_json(records: Sequence[Dict[str, Any]], json_path: Path, pretty: bool = True) -> int:
    """Write the full list of match records as a single JSON array.

    Adds a computed 'relevance_score' field to each record for convenience.
    Returns the number of records written.
    """
    enriched = [public_match_record(rec) for rec in records]
    with json_path.open("w", encoding="utf-8") as fh:
        if pretty:
            json.dump(enriched, fh, indent=2, ensure_ascii=False, sort_keys=True)
        else:
            json.dump(enriched, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    return len(enriched)


def public_match_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return only approved non-secret fields for reports and exports."""
    fields = (
        "event", "provider", "account", "message_date", "message_location",
        "sender", "subject", "categories", "matched_search_passes",
        "attachment_names", "relevance_score", "wallet_files",
        "recovery_phrases", "suspect_signals", "has_high_confidence_suspects",
    )
    result = {field: record[field] for field in fields if field in record}
    result["relevance_score"] = record.get(
        "relevance_score",
        score_match_relevance(
            result.get("categories") or [],
            filenames=result.get("attachment_names") or [],
        ),
    )
    return result


def group_matches_by_category(records: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group match records by every category they matched.

    Returns a dict mapping category name → list of records. Records that
    matched multiple categories appear under every matching category.
    """
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        for cat in rec.get("categories") or []:
            groups[cat].append(rec)
    return dict(groups)


def find_overlapping_matches(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return only those records that matched two or more categories.

    Multi-category hits are often the most interesting (e.g. identity + seed).
    """
    return [r for r in records if len(r.get("categories") or []) >= 2]


# =============================================================================
# Network / Gmail API helpers (original + small enhancements)
# =============================================================================

def request_json(
    url: str,
    api_key: Optional[str],
    access_token: str,
    params: Optional[Dict[str, Any]] = None,
    retries: int = 4,
    rate_limiter: Optional[AdaptiveRateLimiter] = None,
) -> Dict[str, Any]:
    """Perform a GET against the Gmail API and return the parsed JSON body.

    Automatically injects the API key (if supplied) and the Bearer token.
    Applies client-side pacing and adaptive cooldowns for rate-limit responses.
    Implements exponential backoff for transient HTTP errors (408, 429, 5xx)
    and network failures. Raises RuntimeError with a clear message on
    permanent auth or permission failures.
    """
    query = dict(params or {})
    if api_key:
        query["key"] = api_key
    request_url = f"{url}?{urllib.parse.urlencode(query)}"
    last_error: Optional[Exception] = None
    limiter = rate_limiter or DEFAULT_RATE_LIMITER
    for attempt in range(retries + 1):
        limiter.wait()
        request = urllib.request.Request(
            request_url, headers={"Authorization": f"Bearer {access_token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                limiter.reset()
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                error_payload = json.loads(error.read().decode("utf-8", errors="replace"))
            except (OSError, ValueError):
                error_payload = {}
            error_info = error_payload.get("error") if isinstance(error_payload, dict) else {}
            if not isinstance(error_info, dict):
                error_info = {}
            error_entries = error_info.get("errors") or []
            first_error = error_entries[0] if error_entries and isinstance(error_entries[0], dict) else {}
            reason = str(first_error.get("reason") or error_info.get("status") or "").strip()
            message = str(error_info.get("message") or "").strip()
            if error.code == 401:
                raise GmailAPIError(error.code, reason or "unauthorized", message) from error
            rate_limited = error.code == 403 and reason in {
                "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "backendError"
            }
            if rate_limited and attempt < retries:
                last_error = error
                retry_after = error.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and str(retry_after).isdigit() else None
                limiter.penalize(delay)
                rate_limit_sleep(attempt, base=1.0, cap=60.0)
                continue
            if error.code == 403:
                raise GmailAPIError(error.code, reason or "forbidden", message) from error
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt >= retries:
                raise GmailAPIError(error.code, reason, message) from error
            last_error = error
            retry_after = error.headers.get("Retry-After")
            delay = (
                min(30.0, float(retry_after))
                if retry_after and str(retry_after).isdigit()
                else min(2 ** attempt, 20)
            )
            time.sleep(delay)
        except urllib.error.URLError as error:
            if attempt >= retries:
                raise RuntimeError("Gmail API request could not reach Google") from error
            last_error = error
            rate_limit_sleep(attempt)
    raise RuntimeError("Gmail API request failed after retries") from last_error


def refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> str:
    """Exchange a long-lived refresh token for a fresh short-lived access token.

    Raises RuntimeError with diagnostic detail when the refresh fails
    (revoked token, wrong client credentials, etc.).
    """
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        detail = oauth_error_detail(error)
        raise RuntimeError(
            "OAuth refresh failed; verify GMAIL_REFRESH_TOKEN and OAuth client settings" + detail
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            "OAuth refresh failed; verify GMAIL_REFRESH_TOKEN and OAuth client settings"
        ) from error
    token = data.get("access_token")
    if not token:
        raise RuntimeError("OAuth refresh response did not contain an access token")
    return token


def oauth_error_detail(error: urllib.error.HTTPError) -> str:
    """Extract a human-readable detail string from an OAuth error response body."""
    try:
        payload = json.loads(error.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return f" (HTTP {error.code})"
    error_code = payload.get("error")
    description = payload.get("error_description")
    if error_code and description:
        return f" ({error_code}: {description})"
    if error_code:
        return f" ({error_code})"
    return f" (HTTP {error.code})"


def set_dotenv_value(path: Path, name: str, value: str) -> None:
    """Create or update a single key in a .env file, preserving other lines.

    Sets file mode to 0o600 when possible so the refresh token is not
    world-readable.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f'{name}="{value}"'
    found = False
    updated = []
    for line in lines:
        if line.strip().startswith(f"{name}="):
            updated.append(replacement)
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(replacement)
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def authorize(
    args: argparse.Namespace,
    client_id: str,
    client_secret: str,
    configured_redirect_uri: Optional[str] = None,
) -> str:
    """Run the local OAuth 2.0 authorization-code + PKCE flow.

    Opens the system browser, spins up a one-shot HTTP server on the
    configured redirect port, exchanges the code for tokens, and persists
    the refresh token into the .env file. Returns the refresh token.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = secrets.token_urlsafe(32)
    result: Dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({key: values[0] for key, values in query.items() if values})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")

        def log_message(self, format_string, *args):  # noqa: A003
            return

    redirect_uri = configured_redirect_uri or f"http://127.0.0.1:{args.oauth_port}/"
    parsed_redirect = urllib.parse.urlsplit(redirect_uri)
    if (
        parsed_redirect.scheme != "http"
        or parsed_redirect.hostname not in {"127.0.0.1", "localhost"}
        or parsed_redirect.path != "/"
        or parsed_redirect.query
        or parsed_redirect.fragment
        or parsed_redirect.port is None
    ):
        raise RuntimeError(
            "GOOGLE_REDIRECT_URI must be an exact local URI like http://127.0.0.1:8765/"
        )
    try:
        server = HTTPServer(("127.0.0.1", parsed_redirect.port), CallbackHandler)
    except OSError as error:
        raise RuntimeError(
            f"OAuth callback port {args.oauth_port} is unavailable; choose another with --oauth-port"
        ) from error
    server.timeout = 1
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(auth_params)
    print("Opening Google OAuth consent in the browser.", flush=True)
    print(f"Authorization URL (contains no token): {auth_url}", flush=True)
    webbrowser.open(auth_url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()
    if result.get("error"):
        error = result["error"]
        description = result.get("error_description", "").strip()
        if error == "access_denied":
            detail = (
                "Google rejected this account because it is not an approved test user. "
                "In Google Cloud, open Google Auth Platform > Audience for the project "
                "belonging to this OAuth client, set the app to External/Testing, and "
                "add the Gmail address under Test users. Then retry authorization."
            )
            if description:
                detail = f"{detail} Google said: {description}"
            raise RuntimeError(detail)
        detail = f"Google OAuth authorization failed: {error}"
        if description:
            detail += f" ({description})"
        raise RuntimeError(detail)
    if result.get("state") != state:
        raise RuntimeError("Google OAuth state validation failed")
    payload = urllib.parse.urlencode(
        {
            "code": result.get("code", ""),
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            tokens = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError("Google OAuth code exchange failed" + oauth_error_detail(error)) from error
    except urllib.error.URLError as error:
        raise RuntimeError("Google OAuth code exchange failed") from error
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Google OAuth response did not contain a refresh token")
    set_dotenv_value(args.env_file, "GMAIL_REFRESH_TOKEN", refresh_token)
    return refresh_token


def get_profile(access_token: str, user: str = "me") -> Dict[str, Any]:
    """Fetch the Gmail profile (emailAddress, messagesTotal, threadsTotal, historyId).

    Useful as a quick connectivity / token-validity check and for logging
    which account is being searched.
    """
    url = f"{API_ROOT}/{urllib.parse.quote(user, safe='')}/profile"
    return request_json(url, api_key=None, access_token=access_token)


def list_labels(access_token: str, user: str = "me") -> List[Dict[str, Any]]:
    """Return the list of labels for the given Gmail account.

    Can be used to discover custom label IDs if you later want to restrict
    searches to particular labels.
    """
    url = f"{API_ROOT}/{urllib.parse.quote(user, safe='')}/labels"
    data = request_json(url, api_key=None, access_token=access_token)
    return data.get("labels") or []


# =============================================================================
# Core search logic
# =============================================================================

def append_record(path: Path, record: Dict[str, Any]) -> None:
    """Append a single JSON object as a line to the JSONL log file."""
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        log.flush()


def append_progress_match(path: Optional[Path], run_id: str, record: Dict[str, Any]) -> None:
    """Write a redacted match snapshot for live dashboard progress."""
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event": "gmail-crypto-progress-match",
        "run_id": run_id,
        "record": public_match_record(record),
    }
    append_record(path, payload)


def search(args: argparse.Namespace, api_key: Optional[str], access_token: str) -> Tuple[int, int, int]:
    """Execute the multi-pass paginated search and write match + audit records.

    Returns (pages_scanned, unique_messages_scanned, match_count).
    """
    base_url = f"{API_ROOT}/{urllib.parse.quote(args.user, safe='')}/messages"
    priority = ["gourl", "identity", "wallet-file", "seed", "wallet", "crypto-address", "phone"]
    categories = normalize_category_selection(
        getattr(args, "categories", None),
        getattr(args, "category", "all"),
    ) if not getattr(args, "attachment_discovery", False) or getattr(args, "categories", None) else []
    categories = [category for category in priority if category in categories]
    category_regexes = compile_category_regexes(
        json.loads(args.category_regex) if getattr(args, "category_regex", "") else {}
    )
    attachment_extensions = normalize_attachment_extensions(getattr(args, "attachment_extensions", []))
    attachment_filename_regex = getattr(args, "attachment_filename_regex", "")
    if attachment_filename_regex:
        re.compile(attachment_filename_regex, re.I)
    rate_limiter = AdaptiveRateLimiter(
        args.requests_per_second,
        args.rate_limit_max_delay,
    )
    date_parts = []
    if not args.no_date_filter:
        if args.after:
            date_parts.append(f"after:{normalize_gmail_date(args.after)}")
        if args.before:
            date_parts.append(f"before:{normalize_gmail_date(args.before, inclusive_end=True)}")
    date_filter = f" {' '.join(date_parts)}" if date_parts else ""
    query_passes = (
        [("custom", args.query)]
        if args.query
        else [(category, query) for category in categories for query in SEARCH_PASSES[category]]
    )
    if not query_passes and getattr(args, "attachment_discovery", False):
        query_passes = [("attachment", "has:attachment")]
    if getattr(args, "verbose", False):
        print(
            f"[search] categories={','.join(categories)} passes={len(query_passes)} "
            f"max_messages={args.max_messages}",
            file=sys.stderr,
            flush=True,
        )
    seen_ids: Set[str] = set()
    scanned_ids: Set[str] = set()
    match_records: Dict[str, Dict[str, Any]] = {}
    progress_file = getattr(args, "progress_file", None)
    pages = api_calls = errors = listed_messages = fetch_attempts = 0

    for pass_category, pass_query in query_passes:
        page_token = None
        while len(scanned_ids) < args.max_messages:
            params = {
                "q": (pass_query + date_filter).strip(),
                "maxResults": min(100, args.max_messages - len(scanned_ids)),
            }
            if page_token:
                params["pageToken"] = page_token
            page = request_json(
                base_url, api_key, access_token, params,
                retries=args.retries, rate_limiter=rate_limiter,
            )
            api_calls += 1
            pages += 1
            if getattr(args, "verbose", False):
                print(
                    f"[search] pass={pass_category} page={pages} "
                    f"returned={listed_messages} fetched={len(scanned_ids)} "
                    f"matches={len(match_records)}",
                    file=sys.stderr,
                    flush=True,
                )
            for item in page.get("messages", []):
                message_id = item.get("id")
                if not message_id or message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
                listed_messages += 1
                if len(scanned_ids) >= args.max_messages:
                    break
                fetch_attempts += 1
                try:
                    message = request_json(
                        f"{base_url}/{urllib.parse.quote(message_id, safe='')}",
                        api_key,
                        access_token,
                        {"format": "full"},
                        retries=args.retries,
                        rate_limiter=rate_limiter,
                    )
                    api_calls += 1
                except GmailAPIError as error:
                    if error.code in {401, 403}:
                        raise
                    errors += 1
                    continue
                except RuntimeError:
                    errors += 1
                    continue
                scanned_ids.add(message_id)
                payload = message.get("payload", {})
                body = normalized_text(text_from_part(payload))
                filenames = attachment_names(payload)
                attachment_match = attachment_filter_matches(
                    filenames, attachment_extensions, attachment_filename_regex,
                )
                found = matched_categories(body, filenames)
                haystack = normalized_text(body + " " + " ".join(filenames))
                for category, pattern in category_regexes.items():
                    if pattern.search(haystack) and category not in found:
                        found.append(category)
                found = [category for category in found if category in categories]
                if getattr(args, "attachment_discovery", False):
                    if attachment_match:
                        found.append("attachment")
                    elif not found:
                        continue
                if not found:
                    continue
                suspects = extract_wallet_and_recovery_suspects(body, filenames)
                record = match_records.setdefault(
                    message_id,
                    {
                        "event": "gmail-crypto-discovery-match",
                        "run_id": args.run_id,
                        "provider": "Gmail API",
                        "account": args.user,
                        "message_date": redact_metadata(header(message, "Date")),
                        "message_location": (
                            f"https://mail.google.com/mail/u/{args.profile_index}/#all/{message_id}"
                        ),
                        "sender": redact_metadata(header(message, "From")),
                        "subject": redact_metadata(header(message, "Subject")),
                        "categories": found,
                        "matched_search_passes": [],
                        "attachment_names": [redact_metadata(n) for n in filenames[:30]],
                        "relevance_score": score_match_relevance(found, body, filenames),
                        "wallet_files": suspects["wallet_files"],
                        "recovery_phrases": suspects["recovery_phrases"],
                        "suspect_signals": suspects["signals"],
                        "has_high_confidence_suspects": suspects["has_high_confidence"],
                    },
                )
                for category in found:
                    if category not in record["categories"]:
                        record["categories"].append(category)
                if pass_category not in record["matched_search_passes"]:
                    record["matched_search_passes"].append(pass_category)
                # Refresh score after possible category growth. Structured
                # high-confidence findings are a review-priority boost only.
                base = score_match_relevance(record["categories"], body, filenames)
                if record.get("has_high_confidence_suspects"):
                    base = round(base + 3.0, 2)
                elif record.get("wallet_files") or record.get("recovery_phrases"):
                    base = round(base + 1.5, 2)
                record["relevance_score"] = base
                append_progress_match(progress_file, args.run_id, record)
            page_token = page.get("nextPageToken")
            if not page_token or len(scanned_ids) >= args.max_messages:
                break

    for record in match_records.values():
        append_record(args.log_file, record)
    append_record(
        args.log_file,
        {
            "event": "gmail-crypto-search-audit",
            "run_id": args.run_id,
            "provider": "Gmail API",
            "account": args.user,
            "date_window": (
                "unrestricted"
                if args.no_date_filter or not date_parts
                else {
                    "after": normalize_gmail_date(args.after) if args.after else "",
                    "before": normalize_gmail_date(args.before, inclusive_end=True)
                    if args.before else "",
                }
            ),
            "categories": categories,
            "search_passes": len(query_passes),
            "pages_scanned": pages,
            "api_calls": api_calls,
            "unique_messages_scanned": len(scanned_ids),
            "messages_returned": listed_messages,
            "message_fetch_attempts": fetch_attempts,
            "matches": len(match_records),
            "message_fetch_errors": errors,
            "max_messages_reached": len(scanned_ids) >= args.max_messages,
        },
    )
    if getattr(args, "verbose", False):
        print(
            f"[search] complete pages={pages} scanned={len(scanned_ids)} "
            f"returned={listed_messages} matches={len(match_records)} errors={errors}",
            file=sys.stderr,
            flush=True,
        )
    return pages, len(scanned_ids), len(match_records)


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search paginated Gmail messages and log redacted metadata matches."
    )
    parser.add_argument("--query", help="single custom Gmail query; otherwise run multiple discovery passes")
    parser.add_argument(
        "--category",
        choices=["all", *sorted(CATEGORY_PATTERNS)],
        default="all",
    )
    parser.add_argument(
        "--categories",
        help="comma-separated categories; overrides --category",
    )
    parser.add_argument(
        "--category-regex",
        default="",
        help="JSON object of expert local regex overrides keyed by category",
    )
    parser.add_argument("--attachment-extensions", default="", help="comma-separated attachment extensions, e.g. pdf,dat,zip")
    parser.add_argument("--attachment-filename-regex", default="", help="case-insensitive regex matched against attachment filenames")
    parser.add_argument("--attachment-discovery", action="store_true", help="treat attachment filename discovery as a standalone search mode")
    parser.add_argument("--after", default="", help="Optional Gmail after: date; omitted by default")
    parser.add_argument("--before", default="", help="Optional Gmail before: date; omitted by default")
    parser.add_argument("--no-date-filter", action="store_true", help="disable any supplied date filters")
    parser.add_argument("--retries", type=int, default=4, help="transient API retry count")
    parser.add_argument(
        "--requests-per-second", type=float, default=1.0,
        help="maximum proactive Gmail request rate; 0 disables pacing (default: 1)",
    )
    parser.add_argument(
        "--rate-limit-max-delay", type=float, default=60.0,
        help="maximum adaptive cooldown in seconds after a rate-limit response (default: 60)",
    )
    parser.add_argument("--user", default="me", help="Gmail user resource, usually me")
    parser.add_argument("--profile-index", default="0")
    parser.add_argument("--max-messages", type=int, default=1000)
    parser.add_argument("--log-file", type=Path, default=Path("email-wallet-search.log"))
    parser.add_argument("--run-id", default="", help="local dashboard run identifier for audit correlation")
    parser.add_argument(
        "--progress-file",
        type=Path,
        help="append redacted match snapshots for live dashboard progress",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="emit non-secret progress details to stderr",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="complete local OAuth consent and save a refresh token to the env file",
    )
    parser.add_argument(
        "--oauth-port",
        type=int,
        default=8765,
        help="fixed local OAuth callback port; register http://127.0.0.1:<port>/ in Google Cloud",
    )
    parser.add_argument(
        "--redirect-uri",
        help="exact registered local OAuth callback URI; overrides GOOGLE_REDIRECT_URI",
    )
    # New reporting / analysis flags
    parser.add_argument(
        "--report",
        action="store_true",
        help="after search (or alone) load the log file and print a summary report",
    )
    parser.add_argument(
        "--export-csv",
        type=Path,
        help="export match records from the log file to the given CSV path",
    )
    parser.add_argument(
        "--export-json",
        type=Path,
        help="export match records from the log file to the given JSON path",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="report/export existing log records without contacting Gmail",
    )
    parser.add_argument(
        "--profile-check",
        action="store_true",
        help="fetch and print the Gmail profile (email, message counts) then exit",
    )
    parser.add_argument(
        "--list-labels",
        action="store_true",
        help="list Gmail labels for the account then exit",
    )
    parser.add_argument(
        "--suggest-queries",
        choices=sorted(CATEGORY_PATTERNS),
        help="print refined query suggestions for a category and exit",
    )
    args = parser.parse_args()
    if args.max_messages <= 0:
        parser.error("--max-messages must be positive")
    if not 0 <= args.retries <= 10:
        parser.error("--retries must be between 0 and 10")
    if not 0 <= args.requests_per_second <= 10:
        parser.error("--requests-per-second must be between 0 and 10")
    if not 1 <= args.rate_limit_max_delay <= 300:
        parser.error("--rate-limit-max-delay must be between 1 and 300")
    if not 1024 <= args.oauth_port <= 65535:
        parser.error("--oauth-port must be between 1024 and 65535")
    try:
        args.categories = (
            [item.strip() for item in args.categories.split(",") if item.strip()]
            if args.categories else None
        )
        normalize_category_selection(args.categories, args.category)
        if args.category_regex:
            compile_category_regexes(json.loads(args.category_regex))
        args.attachment_extensions = normalize_attachment_extensions(
            [item for item in args.attachment_extensions.split(",") if item.strip()]
        )
        if args.attachment_filename_regex:
            re.compile(args.attachment_filename_regex, re.I)
    except (ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return args


def main() -> None:
    args = parse_args()

    # Pure local helpers that need no credentials
    if args.suggest_queries:
        print(f"Suggested refined queries for category '{args.suggest_queries}':")
        for q in suggest_refined_queries(args.suggest_queries):
            print(f"  {q}")
        return

    # Offline reporting/export must not require credentials or contact Gmail.
    if args.report_only:
        if not (args.report or args.export_csv or args.export_json):
            raise SystemExit("--report-only requires --report, --export-csv, or --export-json")
        records = load_match_records(args.log_file)
        if args.report:
            summary = summarize_matches(records)
            print_summary_report(summary, title=f"Report from {args.log_file}")
        if args.export_csv:
            n = export_matches_csv(records, args.export_csv)
            print(f"Exported {n} matches to {args.export_csv}")
        if args.export_json:
            n = export_matches_json(records, args.export_json)
            print(f"Exported {n} matches to {args.export_json}")
        return

    dotenv = load_dotenv(args.env_file)
    api_key = os.environ.get("GOOGLE_API_KEY", dotenv.get("GOOGLE_API_KEY"))
    access_token = os.environ.get("GMAIL_ACCESS_TOKEN", dotenv.get("GMAIL_ACCESS_TOKEN"))
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN", dotenv.get("GMAIL_REFRESH_TOKEN"))
    client_id = os.environ.get("GOOGLE_CLIENT_ID", dotenv.get("GOOGLE_CLIENT_ID"))
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", dotenv.get("GOOGLE_CLIENT_SECRET"))
    redirect_uri = args.redirect_uri or os.environ.get(
        "GOOGLE_REDIRECT_URI", dotenv.get("GOOGLE_REDIRECT_URI")
    )

    if args.authorize:
        if not client_id or not client_secret:
            raise SystemExit(
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required for --authorize"
            )
        try:
            authorize(args, client_id, client_secret, redirect_uri)
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        print(json.dumps({"authorized": True, "refresh_token_saved": True}))
        return

    if access_token and access_token.startswith("AIza"):
        raise SystemExit(
            "GMAIL_ACCESS_TOKEN contains an API key; use an OAuth access token or refresh token"
        )
    if not access_token and refresh_token and client_id and client_secret:
        access_token = refresh_access_token(refresh_token, client_id, client_secret)
    if not access_token:
        raise SystemExit(
            "GMAIL_ACCESS_TOKEN or GMAIL_REFRESH_TOKEN with GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET is required; run with --authorize to create one. "
            "GOOGLE_API_KEY alone cannot read private Gmail"
        )

    # Quick diagnostic commands
    if args.profile_check:
        try:
            profile = get_profile(access_token, args.user)
            print(json.dumps(profile, indent=2))
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        return

    if args.list_labels:
        try:
            labels = list_labels(access_token, args.user)
            for lab in labels:
                print(f"{lab.get('id', ''):40s}  {lab.get('name', '')}")
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        return

    # Full search path
    try:
        pages, scanned, matches = search(args, api_key, access_token)
    except RuntimeError as error:
        if (
            "expired, invalid" in str(error)
            and refresh_token
            and client_id
            and client_secret
        ):
            access_token = refresh_access_token(refresh_token, client_id, client_secret)
            pages, scanned, matches = search(args, api_key, access_token)
        else:
            raise SystemExit(str(error)) from error

    print(json.dumps({"pages_scanned": pages, "messages_scanned": scanned, "matches": matches}))

    # Optional post-search report
    if args.report:
        records = load_match_records(args.log_file)
        summary = summarize_matches(records)
        print_summary_report(summary, title="Post-search summary")


if __name__ == "__main__":
    main()
