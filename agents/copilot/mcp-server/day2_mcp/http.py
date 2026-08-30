"""A read-only HTTP client. GET is the only verb it can construct.

This module exists so that "the MCP server cannot write to anything" is a
property a reviewer can check by reading one short file, rather than a claim
that has to be re-verified at every call site.

The enforcement is structural, not conventional:

* `urllib.request.Request` is built here and only here, with `method="GET"`
  hardcoded. There is no parameter to override it, so no caller can pass one.
* Request bodies are impossible — `urlopen` is called without `data`, which is
  what makes urllib send a GET rather than a POST in the first place.
* The URL is checked against an allowlist of *hosts we configured*, so a
  crafted query string cannot redirect the client at the Kubernetes API, the
  EC2 metadata endpoint (`169.254.169.254` — the classic SSRF target on a
  cloud node, and the reason this check exists at all), or anything else.
* Redirects are refused. A backend that answers a GET with a 302 to somewhere
  else is either misconfigured or hostile; following it would step around the
  host allowlist that is the point of this module.

Prometheus and Loki both expose their query APIs over GET, so nothing here is
a workaround — the read-only surface is the natural one. Prometheus's admin
API (`/api/v1/admin/*`, delete-series and friends) is POST/PUT-only and is
therefore unreachable from this client by construction, but it is *also* named
in `FORBIDDEN_PATH_FRAGMENTS` below, because defence that depends on remembering
one fact about someone else's API is not defence.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from day2_mcp.limits import clamp_timeout

USER_AGENT = "day2-observability-copilot/1.0 (read-only)"

# Path fragments refused before a request is built, whatever the verb would be.
# Every one of these is a mutating or privilege-revealing endpoint on a backend
# we legitimately talk to.
FORBIDDEN_PATH_FRAGMENTS: tuple[str, ...] = (
    "/api/v1/admin",  # Prometheus TSDB admin: delete_series, clean_tombstones
    "/-/reload",  # Prometheus/Alertmanager config reload
    "/-/quit",
    "/loki/api/v1/delete",  # Loki log deletion
    "/api/v1/silences",  # Alertmanager: creating a silence is a write
    "/config",  # backend configuration, may echo scrape credentials
    "/api/v1/status/config",
    "/api/v1/status/flags",
)


class HttpError(RuntimeError):
    """A read request failed. Carries no response body — it may hold secrets."""


class ForbiddenRequest(HttpError):
    """The request was refused locally, before any network call."""


@dataclass(frozen=True)
class ReadOnlyHttp:
    """GET-only JSON client, pinned to one base URL."""

    base_url: str
    timeout: float = 10.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in ("http", "https"):
            raise ForbiddenRequest(
                f"base_url must be http(s), got {parsed.scheme!r} in {self.base_url!r}"
            )
        if not parsed.hostname:
            raise ForbiddenRequest(f"base_url has no host: {self.base_url!r}")

    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Issue one GET and parse the JSON body."""
        url = self._build_url(path, params)
        request = urllib.request.Request(
            url,
            method="GET",  # the only verb this module can produce
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(
                request, timeout=clamp_timeout(timeout or self.timeout)
            ) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Deliberately does not include the body: an error page from a
            # misconfigured proxy can echo request headers back, and those may
            # carry an Authorization value.
            raise HttpError(f"GET {path} failed: HTTP {exc.code} {exc.reason}") from None
        except urllib.error.URLError as exc:
            raise HttpError(f"GET {path} failed: {exc.reason}") from None
        except TimeoutError:
            raise HttpError(f"GET {path} timed out after {self.timeout}s") from None

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(f"GET {path} returned a non-JSON body: {exc}") from None
        if not isinstance(parsed, dict):
            raise HttpError(
                f"GET {path} returned {type(parsed).__name__}, expected an object"
            )
        return parsed

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        if not path.startswith("/"):
            raise ForbiddenRequest(f"path must be absolute, got {path!r}")
        lowered = path.lower()
        for fragment in FORBIDDEN_PATH_FRAGMENTS:
            if fragment in lowered:
                raise ForbiddenRequest(
                    f"refusing {path!r}: {fragment!r} is a mutating or "
                    "configuration endpoint and is not readable by this server"
                )

        base = self.base_url.rstrip("/")
        query = ""
        if params:
            # Drop Nones so callers can pass optional params positionally.
            cleaned = {k: v for k, v in params.items() if v is not None}
            query = "?" + urllib.parse.urlencode(cleaned, doseq=True)
        url = f"{base}{path}{query}"

        # Re-check the assembled URL: a path containing "../" or an absolute
        # URL smuggled through `path` would otherwise change the host.
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname != urllib.parse.urlparse(self.base_url).hostname:
            raise ForbiddenRequest(
                f"refusing {path!r}: it would leave the configured host "
                f"{urllib.parse.urlparse(self.base_url).hostname!r}"
            )
        return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects rather than follow them off the allowed host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ForbiddenRequest(
            f"refusing to follow a {code} redirect to {newurl!r}; "
            "the host allowlist is the point of this client"
        )
