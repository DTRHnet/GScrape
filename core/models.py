"""Provider-neutral models used by the GScrape control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Account:
    id: str
    provider_id: str
    email: str
    label: str
    status: str = "pending"


@dataclass(frozen=True)
class OAuthSession:
    id: str
    provider_id: str
    state_hash: str
    redirect_uri: str
    status: str = "pending"
    account_hint: Optional[str] = None


@dataclass(frozen=True)
class Job:
    id: str
    account_id: str
    status: str
    config_snapshot_json: str