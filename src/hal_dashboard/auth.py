"""Static-token authentication and browser origin policy.

Authentication is a single shared admin token supplied in the ``X-Hal-Token``
header and compared with :func:`secrets.compare_digest`. There are no
cookies, no query-string auth, and no per-user roles: the token grants full
admin. For mutating requests (POST/PUT), a browser-supplied ``Origin`` must
exactly match one of the configured allowed origins or the Host-derived
same origin.

Token authentication is the secure default. It can be deliberately turned off
for a deployment that sits behind a trusted reverse proxy that authenticates
every request; see :func:`load_auth_disabled` and ``HAL_DASHBOARD_AUTH_DISABLED``.
That flag is a deployment switch only: it trusts the network boundary, never
``X-Forwarded-*`` headers, and grants full admin to anyone who can reach the
backend directly.
"""

from __future__ import annotations

import os
import secrets

__all__ = [
    "AUTH_DISABLED_ENV",
    "MIN_TOKEN_LENGTH",
    "ORIGINS_ENV",
    "TOKEN_ENV",
    "TOKEN_HEADER",
    "TokenError",
    "load_auth_disabled",
    "load_token",
    "origin_allowed",
    "parse_allowed_origins",
    "token_matches",
]

TOKEN_ENV = "HAL_DASHBOARD_TOKEN"
ORIGINS_ENV = "HAL_DASHBOARD_ALLOWED_ORIGINS"
#: Explicit opt-in switch that disables the dashboard's own token auth so it can
#: run behind a reverse proxy that authenticates every request. Absent/false by
#: default (token auth on); accepted exactly (case-insensitive) as the true set
#: below. Any other value is rejected at startup.
AUTH_DISABLED_ENV = "HAL_DASHBOARD_AUTH_DISABLED"
#: Case-insensitive values accepted as "disable token auth".
_AUTH_DISABLED_TRUE = frozenset({"1", "true", "yes", "on"})
#: Case-insensitive values accepted as "keep token auth" (including empty/absent).
_AUTH_DISABLED_FALSE = frozenset({"", "0", "false", "no", "off"})
#: Minimum accepted length (after stripping) for the shared admin token.
MIN_TOKEN_LENGTH = 32
#: The one and only accepted authentication header (HTTP header names are
#: case-insensitive on the wire; Starlette exposes them lower-cased).
TOKEN_HEADER = "X-Hal-Token"

_EXACT_EXAMPLE_TOKENS = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "replaceme",
        "replace-me",
        "replace_me",
        "example",
        "example-token",
        "example_token",
        "your-token",
        "your_token",
        "your-token-here",
        "your_token_here",
        "password",
        "secret",
        "token",
        "test-token",
        "dev",
    }
)
# Substrings that mark a value as an unfilled template rather than a real secret.
_PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "replace",
    "__",
    "your_token",
    "your-token",
    "example",
)


class TokenError(RuntimeError):
    """Raised when the runtime token is missing, too short, or an obvious placeholder."""


def load_token(env: dict[str, str] | None = None) -> str:
    """Read and sanity-check ``HAL_DASHBOARD_TOKEN`` from the environment.

    The token must be non-empty, at least :data:`MIN_TOKEN_LENGTH` characters
    after stripping, and not look like an example/placeholder value.
    """
    source = os.environ if env is None else env
    raw = source.get(TOKEN_ENV, "")
    token = raw.strip()
    if not token:
        raise TokenError(f"{TOKEN_ENV} is required and must be a non-empty secret")
    if len(token) < MIN_TOKEN_LENGTH:
        raise TokenError(
            f"{TOKEN_ENV} is too short: at least {MIN_TOKEN_LENGTH} characters are required"
        )
    lowered = token.lower()
    if lowered in _EXACT_EXAMPLE_TOKENS or any(
        marker in lowered for marker in _PLACEHOLDER_MARKERS
    ):
        raise TokenError(
            f"{TOKEN_ENV} looks like an example/placeholder token; generate a real secret"
        )
    return token


def load_auth_disabled(env: dict[str, str] | None = None) -> bool:
    """Read and sanity-check ``HAL_DASHBOARD_AUTH_DISABLED`` from the environment.

    Returns ``True`` only when the value is an accepted truthy token (case-
    insensitive ``1``, ``true``, ``yes``, ``on``). Returns ``False`` for the
    secure default: the variable absent, empty, or one of ``0``, ``false``,
    ``no``, ``off``. Any other value is a configuration error and is rejected
    at startup so a typo can never silently disable authentication.
    """
    source = os.environ if env is None else env
    value = source.get(AUTH_DISABLED_ENV, "").strip().lower()
    if value in _AUTH_DISABLED_FALSE:
        return False
    if value in _AUTH_DISABLED_TRUE:
        return True
    accepted = ", ".join(sorted(_AUTH_DISABLED_TRUE | _AUTH_DISABLED_FALSE - {""}))
    raise TokenError(
        f"{AUTH_DISABLED_ENV} must be one of [{accepted}] (or unset); got {value!r}"
    )


def token_matches(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of the presented header value."""
    return secrets.compare_digest((provided or "").encode("utf-8"), expected.encode("utf-8"))


def parse_allowed_origins(raw: str | None) -> frozenset[str]:
    """Parse the comma-separated exact-origin allow-list from the environment."""
    if not raw:
        return frozenset()
    origins = {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}
    return frozenset(origins)


def same_origin_candidates(host_header: str) -> frozenset[str]:
    """Host-derived same-origin candidates (http and https on the Host value)."""
    host = host_header.strip().rstrip("/")
    if not host:
        return frozenset()
    return frozenset({f"http://{host}", f"https://{host}"})


def origin_allowed(origin: str, allowed: frozenset[str], host_header: str) -> bool:
    """True when the Origin exactly matches an allowed origin or the Host-derived origin."""
    candidate = origin.strip().rstrip("/")
    if not candidate:
        # Requests without an Origin (curl, healthchecks) are gated by the token alone.
        return True
    if candidate in allowed:
        return True
    return candidate in same_origin_candidates(host_header)
