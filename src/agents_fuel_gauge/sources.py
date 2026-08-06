"""Credential discovery and quota fetching for each agent CLI.

Two deliberate choices in here:

1. Credentials are re-read from disk on *every* poll rather than cached at
   startup. Claude Code and Codex both rewrite their credential files in place
   when they refresh, so a long-running gauge inherits fresh tokens for free.
   Caching the bearer would strand the app after roughly eight hours.

2. Anthropic's per-model numbers are read from the self-describing `limits[]`
   array, never from the `seven_day_opus` / `seven_day_omelette` top-level keys.
   Those legacy keys still exist in the payload but are `null` on current
   accounts — reading them is why several third-party trackers silently show no
   per-model data at all.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import cache
from .models import Gauge, ProviderSnapshot, derive_severity, mask_email

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_BETA = "oauth-2025-04-20"

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

TIMEOUT = httpx.Timeout(15.0)

# Anthropic tags each limit with a `kind`; map it to the window it covers.
_CLAUDE_WINDOW_BY_KIND = {
    "session": "5h",
    "weekly_all": "7d",
    "weekly_scoped": "7d",
}

# Anthropic reports a reset time but not a window length, so the lengths are
# supplied here. Codex sends `limit_window_seconds` and needs no such table.
_CLAUDE_WINDOW_SECONDS = {
    "session": 5 * 3_600,
    "weekly_all": 7 * 86_400,
    "weekly_scoped": 7 * 86_400,
}


class SourceError(RuntimeError):
    """A fetch failed in a way worth showing the user verbatim."""


class RateLimited(SourceError):
    """A 429, or a standing backoff from a previous one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


async def _cached_get(
    client: httpx.AsyncClient,
    provider: str,
    url: str,
    headers: dict,
    max_age: float,
) -> tuple[dict, float]:
    """Fetch through the cache, honouring any standing backoff.

    Returns (payload, age_seconds). Age is 0 for a live response, and non-zero
    when the payload came from disk — the caller uses that to mark the reading
    as stale rather than passing off an old number as current.
    """
    fresh, _ = cache.load(provider, max_age)
    if fresh is not None:
        return fresh, 0.0 if max_age <= 0 else cache.load_stale(provider)[1]

    # Never knock on a door the server has explicitly closed. Retrying inside
    # the backoff is what turns a single 429 into a continuous stream of them.
    waiting = cache.blocked_for(provider)
    if waiting > 0:
        stale, age = cache.load_stale(provider)
        if stale is not None:
            return stale, age
        raise RateLimited(
            f"rate limited — retrying in {int(waiting)}s", retry_after=waiting
        )

    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        stale, age = cache.load_stale(provider)
        if stale is not None:
            return stale, age
        raise

    if response.status_code == 429:
        retry = response.headers.get("retry-after")
        seconds = float(retry) if (retry or "").replace(".", "", 1).isdigit() else None
        cache.block(provider, seconds)
        stale, age = cache.load_stale(provider)
        if stale is not None:
            return stale, age
        suffix = f" — retrying in {int(seconds)}s" if seconds else ""
        raise RateLimited(f"rate limited (429){suffix}", retry_after=seconds)

    payload = _raise_for_status(response, provider)
    cache.store(provider, payload)
    return payload, 0.0


def tidy_path(path: Path) -> str:
    """Render `$HOME/...` as `~/...` so messages never expose a home directory."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def claude_credentials_path() -> Path:
    """Where Claude Code keeps its OAuth tokens.

    `CLAUDE_CONFIG_DIR` is Claude Code's own override; `AFG_CLAUDE_CREDENTIALS`
    is ours, for anyone with a genuinely unusual layout.
    """
    if override := os.environ.get("AFG_CLAUDE_CREDENTIALS"):
        return Path(override).expanduser()
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(config_dir).expanduser() / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


def codex_auth_path() -> Path:
    """Where Codex keeps its tokens (`CODEX_HOME` is Codex's own override)."""
    if override := os.environ.get("AFG_CODEX_AUTH"):
        return Path(override).expanduser()
    if home := os.environ.get("CODEX_HOME"):
        return Path(home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _read_json(path: Path, cli: str) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise SourceError(
            f"not signed in — run `{cli}` and sign in "
            f"(looked in {tidy_path(path)})"
        ) from None
    except PermissionError:
        raise SourceError(f"cannot read {tidy_path(path)} — check permissions") from None
    except json.JSONDecodeError:
        raise SourceError(f"{tidy_path(path)} is not valid JSON") from None
    except OSError as exc:
        raise SourceError(f"could not read {tidy_path(path)}: {exc.strerror}") from None


def _duration_label(seconds: int | None) -> str:
    """Codex identifies windows by length, not name: 604800 -> 7d, 18000 -> 5h."""
    if not seconds:
        return "?"
    if seconds >= 86_400 and seconds % 86_400 == 0:
        return f"{seconds // 86_400}d"
    if seconds >= 3_600:
        return f"{seconds // 3_600}h"
    return f"{seconds // 60}m"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_epoch(value: int | float | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _plan_from_tier(tier: str | None) -> str | None:
    """`default_claude_max_20x` -> `Max 20x`."""
    if not tier:
        return None
    stem = tier.removeprefix("default_claude_").removeprefix("default_")
    parts = [p for p in stem.split("_") if p]
    if not parts:
        return None
    return " ".join(p if p[0].isdigit() else p.capitalize() for p in parts)


def _raise_for_status(response: httpx.Response, provider: str) -> dict:
    if response.status_code == 401:
        cli = "claude" if provider == "claude" else "codex"
        raise SourceError(f"token rejected (401) — run `{cli}` once to refresh it")
    if response.status_code == 429:
        # The usage endpoints are themselves rate-limited; polling too fast
        # trips this long before your actual quota runs out.
        retry = response.headers.get("retry-after")
        seconds = int(retry) if (retry or "").isdigit() else 0
        suffix = f" — retry in {seconds}s" if seconds > 0 else " — poll less often"
        raise SourceError(f"rate limited (429){suffix}")
    if response.status_code >= 400:
        raise SourceError(f"HTTP {response.status_code} from {provider}")
    try:
        return response.json()
    except ValueError:
        raise SourceError(f"{provider} returned a non-JSON body") from None


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #


def _claude_gauges(payload: dict) -> list[Gauge]:
    gauges: list[Gauge] = []
    for entry in payload.get("limits") or []:
        kind = entry.get("kind") or "unknown"
        scope = (entry.get("scope") or {}).get("model") or {}
        gauges.append(
            Gauge(
                window=_CLAUDE_WINDOW_BY_KIND.get(kind, kind),
                scope=scope.get("display_name") or "all models",
                percent=float(entry.get("percent") or 0.0),
                resets_at=_parse_iso(entry.get("resets_at")),
                severity=entry.get("severity") or derive_severity(
                    float(entry.get("percent") or 0.0)
                ),
                active_limit=bool(entry.get("is_active")),
                window_seconds=_CLAUDE_WINDOW_SECONDS.get(kind),
            )
        )
    if gauges:
        return gauges

    # Degrade to the flat top-level windows rather than showing an empty panel.
    # These two keys predate `limits[]` and are the most stable thing in the
    # payload, which makes them a safe last resort — just an incomplete one.
    for key, window, length in (
        ("five_hour", "5h", 5 * 3_600),
        ("seven_day", "7d", 7 * 86_400),
    ):
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        percent = float(block.get("utilization") or block.get("used_percentage") or 0.0)
        gauges.append(
            Gauge(
                window=window,
                scope="all models",
                percent=percent,
                resets_at=_parse_iso(block.get("resets_at")),
                severity=derive_severity(percent),
                window_seconds=length,
            )
        )
    return gauges


async def fetch_claude(
    client: httpx.AsyncClient, max_age: float = cache.DEFAULT_MAX_AGE
) -> ProviderSnapshot:
    snapshot = ProviderSnapshot(
        key="claude", display_name="Claude", captured_at=datetime.now(timezone.utc)
    )
    try:
        path = claude_credentials_path()
        creds = _read_json(path, "claude").get("claudeAiOauth", {})
        token = creds.get("accessToken")
        if not token:
            raise SourceError(
                f"no OAuth token in {tidy_path(path)} — this tool needs a "
                "Pro/Max subscription login, not an API key"
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": CLAUDE_BETA,
            "Content-Type": "application/json",
        }
        usage, age = await _cached_get(
            client, "claude", CLAUDE_USAGE_URL, headers, max_age
        )
        snapshot.gauges = _claude_gauges(usage)
        if age:
            snapshot.stale = True
            snapshot.captured_at = datetime.now(timezone.utc) - timedelta(seconds=age)
        snapshot.plan = _plan_from_tier(creds.get("rateLimitTier"))

        # The account label is a nicety; never let it fail the whole panel.
        try:
            # The profile is effectively static; cache it hard so it never
            # costs a request that the usage endpoint needs.
            profile, _ = await _cached_get(
                client, "claude-profile", CLAUDE_PROFILE_URL, headers, 86_400
            )
            account = profile.get("account") or {}
            snapshot.account = mask_email(account.get("email"))
            snapshot.plan = (
                _plan_from_tier((profile.get("organization") or {}).get("rate_limit_tier"))
                or snapshot.plan
            )
        except (SourceError, httpx.HTTPError):
            pass
    except SourceError as exc:
        snapshot.error = str(exc)
    except httpx.HTTPError as exc:
        snapshot.error = f"network error: {exc.__class__.__name__}"
    return snapshot


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #


def _codex_windows(rate_limit: dict | None, scope: str) -> list[Gauge]:
    """Codex nests up to two windows per limit; either may be absent."""
    out: list[Gauge] = []
    if not isinstance(rate_limit, dict):
        return out
    for slot in ("primary_window", "secondary_window"):
        window = rate_limit.get(slot)
        if not isinstance(window, dict):
            continue
        percent = float(window.get("used_percent") or 0.0)
        length = window.get("limit_window_seconds")
        out.append(
            Gauge(
                window=_duration_label(length),
                scope=scope,
                percent=percent,
                resets_at=_parse_epoch(window.get("reset_at")),
                severity=derive_severity(percent),
                window_seconds=int(length) if length else None,
            )
        )
    return out


def _codex_gauges(payload: dict) -> list[Gauge]:
    gauges = _codex_windows(payload.get("rate_limit"), "all models")
    for extra in payload.get("additional_rate_limits") or []:
        gauges.extend(
            _codex_windows(extra.get("rate_limit"), extra.get("limit_name") or "scoped")
        )

    # Anthropic reports which limit is currently active; OpenAI does not, and
    # guessing from "whichever bar is fullest" was wrong: a meter filled days
    # ago and idle since looks identical to one filling right now. Better to
    # flag nothing than to flag the wrong thing.
    return gauges


async def fetch_codex(
    client: httpx.AsyncClient, max_age: float = cache.DEFAULT_MAX_AGE
) -> ProviderSnapshot:
    snapshot = ProviderSnapshot(
        key="codex", display_name="Codex", captured_at=datetime.now(timezone.utc)
    )
    try:
        path = codex_auth_path()
        auth = _read_json(path, "codex")
        tokens = auth.get("tokens") or {}
        token = tokens.get("access_token")
        if not token:
            # Codex can authenticate with a raw API key instead of a ChatGPT
            # login, but usage quotas only exist for the subscription path.
            if auth.get("OPENAI_API_KEY"):
                raise SourceError(
                    "signed in with an API key — quota data needs a ChatGPT "
                    "subscription login (`codex login`)"
                )
            raise SourceError(f"no access token in {tidy_path(path)} — run `codex login`")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if tokens.get("account_id"):
            headers["chatgpt-account-id"] = tokens["account_id"]

        usage, age = await _cached_get(
            client, "codex", CODEX_USAGE_URL, headers, max_age
        )
        snapshot.gauges = _codex_gauges(usage)
        if age:
            snapshot.stale = True
            snapshot.captured_at = datetime.now(timezone.utc) - timedelta(seconds=age)
        plan = usage.get("plan_type")
        snapshot.plan = plan.capitalize() if isinstance(plan, str) else None
        snapshot.account = mask_email(usage.get("email"))
    except SourceError as exc:
        snapshot.error = str(exc)
    except httpx.HTTPError as exc:
        snapshot.error = f"network error: {exc.__class__.__name__}"
    return snapshot


async def fetch_all(
    max_age: float = cache.DEFAULT_MAX_AGE,
) -> list[ProviderSnapshot]:
    """Both providers in parallel — neither failure mode blocks the other.

    `max_age` is served from the shared on-disk cache, so several `afg`
    processes running at once cost one request between them rather than one
    each. Pass 0 to force a live fetch.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        return list(
            await asyncio.gather(
                fetch_claude(client, max_age), fetch_codex(client, max_age)
            )
        )
