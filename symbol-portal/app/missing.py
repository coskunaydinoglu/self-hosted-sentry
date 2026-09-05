"""Aggregate "missing debug file" processing errors across recent events into one report.

Sentry only shows these per event. Countly users are used to a per-app list of
missing mapping files, so we rebuild that view from the events API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

ERROR_KINDS = {
    "native_missing_dsym": "required",
    "native_bad_dsym": "bad",
    "native_missing_optionally_bundled_dsym": "optional",
    "proguard_missing_mapping": "proguard",
    "proguard_missing_lineno": "proguard",
}


@dataclass
class MissingEntry:
    debug_id: str
    kind: str
    image_path: str | None = None
    event_count: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    releases: set[str] = field(default_factory=set)
    sample_events: list[str] = field(default_factory=list)
    uploaded: bool | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["releases"] = sorted(self.releases)
        return data


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _release_of(event: dict) -> str | None:
    release = event.get("release")
    if isinstance(release, dict):
        return release.get("version") or release.get("shortVersion")
    if isinstance(release, str):
        return release
    for tag in event.get("tags") or []:
        if isinstance(tag, dict) and tag.get("key") == "release":
            return tag.get("value")
    return None


def _debug_id_of(error: dict) -> str | None:
    data = error.get("data") or {}
    for key in ("image_uuid", "mapping_uuid", "debug_id"):
        value = data.get(key)
        if value:
            return str(value).lower()
    return None


def collect_missing(events: list[dict]) -> dict[str, MissingEntry]:
    entries: dict[str, MissingEntry] = {}
    for event in events:
        release = _release_of(event)
        date = event.get("dateCreated") or event.get("dateReceived")
        event_id = event.get("eventID") or event.get("id")
        seen_in_event: set[str] = set()
        for error in event.get("errors") or []:
            kind = ERROR_KINDS.get(error.get("type", ""))
            if not kind:
                continue
            debug_id = _debug_id_of(error)
            if not debug_id or debug_id in seen_in_event:
                continue
            seen_in_event.add(debug_id)
            entry = entries.setdefault(debug_id, MissingEntry(debug_id=debug_id, kind=kind))
            entry.event_count += 1
            if entry.kind != "required" and kind == "required":
                entry.kind = kind
            data = error.get("data") or {}
            entry.image_path = entry.image_path or data.get("image_path") or data.get("image_name")
            if release:
                entry.releases.add(release)
            if event_id and len(entry.sample_events) < 3:
                entry.sample_events.append(str(event_id))
            if date:
                if entry.first_seen is None or date < entry.first_seen:
                    entry.first_seen = date
                if entry.last_seen is None or date > entry.last_seen:
                    entry.last_seen = date
    return entries


def sort_entries(entries: dict[str, MissingEntry]) -> list[MissingEntry]:
    order = {"required": 0, "bad": 1, "proguard": 2, "optional": 3}
    return sorted(entries.values(), key=lambda e: (order.get(e.kind, 9), -e.event_count, e.debug_id))
