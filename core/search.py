"""Provider-neutral Phase 2 search planning and normalized result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class SearchPlan:
    domains: tuple[str, ...]
    query: str = ""
    after: str = ""
    before: str = ""
    sender: str = ""
    subject: str = ""
    mime: str = ""
    max_messages: int = 1000

    def preview(self) -> dict[str, Any]:
        return {"domains": list(self.domains), "provider_query": build_gmail_query(self), "max_messages": self.max_messages}


def build_gmail_query(plan: SearchPlan) -> str:
    parts = []
    if plan.query:
        parts.append(f"({plan.query})")
    if plan.sender:
        parts.append(f"from:{plan.sender}")
    if plan.subject:
        parts.append(f"subject:{plan.subject}")
    if plan.mime:
        parts.append(f"filename:{plan.mime.lstrip('.')}")
    if plan.after:
        parts.append(f"after:{plan.after}")
    if plan.before:
        parts.append(f"before:{plan.before}")
    return " ".join(parts)


@dataclass(frozen=True)
class NormalizedResult:
    provider: str
    message_id: str
    message_date: str = ""
    sender: str = ""
    subject: str = ""
    domains: tuple[str, ...] = ()
    evidence: tuple[Mapping[str, Any], ...] = ()
    attachments: tuple[Mapping[str, Any], ...] = ()


def normalize_result(record: Mapping[str, Any]) -> NormalizedResult:
    return NormalizedResult(
        provider=str(record.get("provider", "")), message_id=str(record.get("message_id", record.get("id", ""))),
        message_date=str(record.get("message_date", "")), sender=str(record.get("sender", "")),
        subject=str(record.get("subject", "")), domains=tuple(str(x) for x in record.get("domains", record.get("categories", ()))),
        evidence=tuple(record.get("evidence", ())), attachments=tuple(record.get("attachments", ())),
    )