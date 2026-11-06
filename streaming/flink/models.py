from datetime import datetime

from pydantic import BaseModel


class MastodonAccount(BaseModel):
    """Mastodon account projection from stream payload."""

    id: str
    username: str
    display_name: str | None = None
    url: str | None = None
    avatar: str | None = None


class MastodonPayload(BaseModel):
    """Raw Mastodon event payload used by the streaming job."""

    id: str
    created_at: datetime
    content: str
    account: MastodonAccount
    visibility: str
    sensitive: bool
    language: str | None = None
    uri: str
    url: str
    replies_count: int = 0
    reblogs_count: int = 0
    favourites_count: int = 0


class MastodonEvent(BaseModel):
    """Top-level Mastodon event envelope."""

    event_type: str
    payload: MastodonPayload
    timestamp_ms: int
    stream_type: str | None = None


class RedactedMastodonEvent(BaseModel):
    """PII-redacted Mastodon event written to staging."""

    event_id: str
    event_type: str
    account_id: str
    account_username_hash: str
    content_redacted: str
    visibility: str
    sensitive: bool
    language: str | None
    uri: str
    url: str
    replies_count: int
    reblogs_count: int
    favourites_count: int
    timestamp_ms: int
    processed_at: datetime
    pii_detected: bool
    pii_types: list[str] = []
