"""Deterministic, local-only analysis of raw RFC822 email headers."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any


AUTH_FIELDS = ("spf", "dkim", "dmarc")
IP_PATTERN = re.compile(r"\[?((?:\d{1,3}\.){3}\d{1,3}|[0-9A-Fa-f:]{3,})\]?")
RESULT_PATTERN = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z][a-z0-9_-]*)", re.I)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (TypeError, ValueError):
        return value


def _domain(address: str) -> str:
    value = address.rsplit("@", 1)[-1].strip().strip("<>[]").lower()
    return value if "@" in address and "." in value else ""


def _addresses(message, header: str) -> list[dict[str, str]]:
    result = []
    for name, address in getaddresses(message.get_all(header, [])):
        address = address.strip()
        if address:
            result.append({"name": _decode(name), "address": address, "domain": _domain(address)})
    return result


def _received_hops(values: list[str]) -> list[dict[str, Any]]:
    hops = []
    for index, value in enumerate(reversed(values), 1):
        ips = []
        for candidate in IP_PATTERN.findall(value):
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if candidate not in ips:
                ips.append(candidate)
        hops.append({"hop": index, "raw": value.strip(), "ips": ips})
    return hops


def _authentication(values: list[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for value in values:
        for field, result in RESULT_PATTERN.findall(value):
            key = field.lower()
            results.setdefault(key, result.lower())
    return results


def _finding(code: str, severity: str, title: str, detail: str, evidence: list[str]) -> dict[str, Any]:
    return {"code": code, "severity": severity, "title": title, "detail": detail, "evidence": evidence}


def analyze_email_headers(raw_rfc822: bytes | bytearray | memoryview | None) -> dict[str, Any]:
    """Analyze stored headers without changing or returning the original bytes."""
    if not raw_rfc822:
        return {"available": False, "risk_level": "unknown", "score": 0, "findings": [], "fields": {}}

    message = BytesParser(policy=policy.default).parsebytes(bytes(raw_rfc822))
    from_addresses = _addresses(message, "From")
    reply_addresses = _addresses(message, "Reply-To")
    return_addresses = _addresses(message, "Return-Path")
    to_addresses = _addresses(message, "To")
    cc_addresses = _addresses(message, "Cc")
    received = [str(value) for value in message.get_all("Received", [])]
    auth_values = [str(value) for value in message.get_all("Authentication-Results", [])]
    authentication = _authentication(auth_values)
    findings: list[dict[str, Any]] = []
    score = 0

    from_domain = from_addresses[0]["domain"] if from_addresses else ""
    reply_domain = reply_addresses[0]["domain"] if reply_addresses else ""
    return_domain = return_addresses[0]["domain"] if return_addresses else ""
    if from_domain and reply_domain and from_domain != reply_domain:
        findings.append(_finding("from-reply-domain-mismatch", "high", "From and Reply-To domains differ", f"Visible sender domain is {from_domain}, but replies target {reply_domain}.", [f"From: {from_addresses[0]['address']}", f"Reply-To: {reply_addresses[0]['address']}"]))
        score += 35
    if from_domain and return_domain and from_domain != return_domain:
        findings.append(_finding(
            "from-return-path-domain-mismatch",
            "medium",
            "From and Return-Path domains differ",
            f"Visible sender domain is {from_domain}, but the envelope return path uses {return_domain}.",
            [f"From: {from_addresses[0]['address']}", f"Return-Path: {return_addresses[0]['address']}"],
        ))
        score += 20

    for field in AUTH_FIELDS:
        result = authentication.get(field)
        if result in {"fail", "softfail", "permerror", "temperror", "neutral"}:
            severity = "high" if result in {"fail", "permerror"} else "medium"
            score += 35 if severity == "high" else 15
            findings.append(_finding(f"{field}-{result}", severity, f"{field.upper()} result is {result}", f"Authentication-Results reported {field}={result}.", [f"{field.upper()}={result}"]))
    if authentication.get("dmarc") == "pass":
        score = max(0, score - 10)
    if not authentication:
        findings.append(_finding("no-authentication-results", "low", "No Authentication-Results header", "The message does not contain a standard Authentication-Results header to evaluate.", []))
        score += 5

    date_value = str(message.get("Date") or "")
    date_iso = ""
    if date_value:
        try:
            parsed_date = parsedate_to_datetime(date_value)
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)
            date_iso = parsed_date.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            findings.append(_finding("invalid-date", "medium", "Invalid Date header", "The Date header could not be parsed as an RFC822 date.", [date_value]))
            score += 15

    message_id = _decode(str(message.get("Message-ID") or ""))
    message_id_domain = _domain(message_id.strip("<>")) if "@" in message_id else ""
    if from_domain and message_id_domain and message_id_domain != from_domain:
        findings.append(_finding("message-id-domain-mismatch", "low", "Message-ID domain differs from From domain", f"Message-ID uses {message_id_domain}, while From uses {from_domain}.", [f"Message-ID: {message_id}", f"From domain: {from_domain}"]))
        score += 8

    if len(received) == 0:
        findings.append(_finding("no-received-chain", "low", "No Received headers", "No delivery path was available in the stored headers.", []))
        score += 3

    if score >= 60:
        risk_level = "high"
    elif score >= 25:
        risk_level = "medium"
    elif score > 0:
        risk_level = "low"
    else:
        risk_level = "none"

    return {
        "available": True,
        "risk_level": risk_level,
        "score": min(score, 100),
        "findings": findings,
        "authentication": authentication,
        "received_hops": _received_hops(received),
        "fields": {
            "from": from_addresses,
            "reply_to": reply_addresses,
            "return_path": return_addresses,
            "to": to_addresses,
            "cc": cc_addresses,
            "subject": _decode(str(message.get("Subject") or "")),
            "date": date_value,
            "date_utc": date_iso,
            "message_id": message_id,
            "x_originating_ip": [str(value) for value in message.get_all("X-Originating-IP", [])],
        },
    }