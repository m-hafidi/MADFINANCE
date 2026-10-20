"""HTTP layer shared by all sources.

Uses ``curl_cffi`` with Chrome TLS-impersonation — the Casablanca site is
behind plain nginx but Medias24 sits behind a Cloudflare JS-challenge that
rejects default ``requests`` fingerprints with a 403.

Policy: bounded retries with exponential backoff on 403/429/5xx and network
errors; callers are expected to cache and to implement source-level fallbacks
rather than hammering any endpoint.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from curl_cffi import requests as _creq

from ._exceptions import SourceError

DEFAULT_TIMEOUT = 25
_RETRY_STATUSES = {403, 429, 500, 502, 503, 504}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Hosts whose certificate chain cannot be verified (casablanca-bourse.com
#: serves an incomplete chain — no intermediate in the bundle; browsers hide
#: this via AIA fetching, curl cannot). Only public, read-only pages are
#: requested from these hosts and no credentials ever travel over them, so
#: verification is relaxed host-side rather than globally.
_INSECURE_HOSTS = frozenset({"casablanca-bourse.com", "www.casablanca-bourse.com"})

_session: Optional[_creq.Session] = None


def session() -> _creq.Session:
    """Shared impersonating session (created lazily)."""
    global _session
    if _session is None:
        _session = _creq.Session(impersonate="chrome")
        _session.headers.update({"User-Agent": _USER_AGENT, "Accept-Language": "fr,en;q=0.8"})
    return _session


def reset_session() -> None:
    """Drop the shared session (used by tests)."""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:
            pass
    _session = None


def _request(
    method: str,
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    data: Optional[Any] = None,
    json: Optional[Any] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
) -> str:
    last_error: Optional[str] = None
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    insecure = host in _INSECURE_HOSTS or any(
        host.endswith("." + h) for h in _INSECURE_HOSTS)
    for attempt in range(retries + 1):
        try:
            resp = session().request(
                method, url, params=params, data=data, json=json,
                headers=dict(headers) if headers else None, timeout=timeout,
                verify=not insecure,
            )
            if resp.status_code == 200:
                return resp.text
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code not in _RETRY_STATUSES:
                break
        except Exception as exc:  # curl_cffi raises transport errors
            last_error = str(exc) or type(exc).__name__
        if attempt < retries:
            time.sleep(0.6 * (2 ** attempt))
    raise SourceError(f"{method} {url} failed: {last_error}")


def get_text(
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
) -> str:
    """GET ``url`` and return the response body; raises :class:`SourceError`."""
    return _request("GET", url, params=params, headers=headers,
                    timeout=timeout, retries=retries)


def get_json(
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
) -> Any:
    """GET ``url`` and decode JSON; raises :class:`SourceError` on failure."""
    import json as _json

    body = get_text(url, params=params, headers=headers, timeout=timeout, retries=retries)
    try:
        return _json.loads(body)
    except ValueError as exc:
        raise SourceError(f"Non-JSON response from {url}: {exc}") from exc


def post_json(
    url: str,
    payload: Any,
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 1,
) -> Any:
    """POST JSON ``payload`` and decode the JSON response."""
    import json as _json

    hdrs = {"Content-Type": "application/json", "Referer": "https://www.tradingview.com/"}
    if headers:
        hdrs.update(headers)
    body = _request("POST", url, data=_json.dumps(payload).encode("utf-8"),
                    headers=hdrs, timeout=timeout, retries=retries)
    try:
        return _json.loads(body)
    except ValueError as exc:
        raise SourceError(f"Non-JSON response from {url}: {exc}") from exc
