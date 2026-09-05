import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    sentry_url: str
    sentry_org: str
    sentry_token: str
    basic_auth: str | None
    lookback_days: int
    max_events_per_project: int
    rewrite_chunk_url: bool
    tmp_dir: str
    symbolicator_url: str
    system_symbols_url: str

    @property
    def configured(self) -> bool:
        return bool(self.sentry_token)


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        sentry_url=env.get("SYMBOL_PORTAL_SENTRY_URL", "http://web:9000").rstrip("/"),
        sentry_org=env.get("SYMBOL_PORTAL_SENTRY_ORG", "sentry"),
        sentry_token=env.get("SYMBOL_PORTAL_SENTRY_TOKEN", ""),
        basic_auth=env.get("SYMBOL_PORTAL_BASIC_AUTH") or None,
        lookback_days=int(env.get("SYMBOL_PORTAL_LOOKBACK_DAYS", "14")),
        max_events_per_project=int(env.get("SYMBOL_PORTAL_MAX_EVENTS", "2000")),
        # Sentry builds the chunk-upload URL from system.url-prefix (the public URL).
        # Inside the compose network we talk to `web` directly, so rewrite it by default.
        rewrite_chunk_url=env.get("SYMBOL_PORTAL_REWRITE_CHUNK_URL", "1") not in ("0", "false", "no"),
        tmp_dir=env.get("SYMBOL_PORTAL_TMP_DIR", "/tmp/symbol-portal"),
        symbolicator_url=env.get("SYMBOL_PORTAL_SYMBOLICATOR_URL", "http://symbolicator:3021").rstrip("/"),
        # Internal OS symbol server (symbol-server/); empty disables the extra source.
        system_symbols_url=env.get("SYMBOL_PORTAL_SYSTEM_SYMBOLS_URL", "http://symbol-server/"),
    )
