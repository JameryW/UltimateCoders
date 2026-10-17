"""DeepSeek official Search/Fetch HTTP client (Approach C).

Thin httpx wrapper around:

* ``POST {base}/v1/tools/search``
* ``POST {base}/v1/tools/fetch``

Default base is ``https://api.deepseek.com``. When ``DEEPSEEK_BASE_URL`` is
set, trailing slashes are stripped and ``/v1/tools/...`` is appended.

Enablement is key-gated: tools are advertised when ``DEEPSEEK_API_KEY`` is
present. Calls fail closed (no silent empty success) when the key is missing
or the API returns 401/402/403/404/429/5xx.

Fetch URLs are prechecked in UC before any DeepSeek call: https-only, no
userinfo, and only globally routable resolved addresses (blocks localhost /
RFC1918 / CGNAT / link-local / unique-local / common cloud-metadata; fail
closed on unresolved or non-global targets).

This module is independent of ``deepseek-harness`` / ``dsh`` headless coding.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

#: Env vars sandbox agents need so in-process ``engine_mcp`` DeepSeek tools
#: can authenticate. Shared infrastructure (like hy-memory), not exclusive to
#: the ``deepseek-harness`` coding agent.
DEEPSEEK_WEB_ENV_ALLOWLIST: tuple[str, ...] = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")

#: Account QPS for Search and Fetch (independent quotas).
SEARCH_QPS = 5
FETCH_QPS = 5

#: USD per 1_000 successful HTTP-200 requests (empty search / failures unpaid).
PRICE_PER_1K_SUCCESS = 0.75

#: Grace period for agent-embedded Search/Fetch ends (Beijing time).
BILLING_GRACE_ENDS = "2026-11-01"

TOOL_SEARCH = "deepseek_web_search"
TOOL_FETCH = "deepseek_web_fetch"

# Hostnames that must never be fetched (before DNS).
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
    }
)

# Explicit cloud-metadata IPs (also covered by link-local / reserved checks).
_BLOCKED_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS / GCP / Azure IMDS
        ipaddress.ip_address("169.254.170.2"),  # AWS ECS task metadata
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
    }
)


class DeepSeekWebError(RuntimeError):
    """Actionable DeepSeek Search/Fetch failure (never a silent empty success)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SearchResultItem:
    rerank_score: float
    title: str
    url: str
    snippet: str
    summary: str
    content: str
    site_name: str | None
    site_icon: str | None
    date: str | None
    authority: int


@dataclass(frozen=True)
class SearchResponse:
    results: list[SearchResultItem] = field(default_factory=list)


@dataclass(frozen=True)
class FetchResponse:
    title: str
    content: str


def deepseek_api_key() -> str | None:
    """Return stripped ``DEEPSEEK_API_KEY`` or None when unset/blank."""
    raw = os.environ.get("DEEPSEEK_API_KEY")
    if raw is None:
        return None
    key = raw.strip()
    return key or None


def is_configured() -> bool:
    """True when Search/Fetch tools should be registered/advertised."""
    return deepseek_api_key() is not None


def resolve_base_url(base_url: str | None = None) -> str:
    """Return DeepSeek API base without trailing slash."""
    if base_url is None:
        base_url = os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL
    return base_url.strip().rstrip("/")


def require_api_key() -> str:
    key = deepseek_api_key()
    if key is None:
        raise DeepSeekWebError(
            "DEEPSEEK_API_KEY is not set. Set it to enable DeepSeek Search/Fetch "
            f"(QPS {SEARCH_QPS}/{FETCH_QPS}; ${PRICE_PER_1K_SUCCESS}/1k successful "
            f"calls; agent-embedded grace ends {BILLING_GRACE_ENDS})."
        )
    return key


def _is_blocked_hostname(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    if host in _BLOCKED_HOSTNAMES:
        return True
    if host.endswith(".localhost") or host.endswith(".local"):
        return True
    return False


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # IPv4-mapped IPv6 (::ffff:x.x.x.x) — classify the embedded v4 address.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_blocked_ip(addr.ipv4_mapped)
    if addr in _BLOCKED_IPS:
        return True
    # Fail closed: only globally routable addresses are allowed. This covers
    # RFC1918, loopback, link-local, unique-local, reserved/multicast,
    # unspecified, CGNAT (100.64/10), and cloud-metadata ranges that are
    # non-global (e.g. 100.100.100.200). Python's is_private alone misses CGNAT.
    return not addr.is_global


def validate_fetch_url(url: str, *, resolver: Any = None) -> str:
    """Validate a fetch URL against UC SSRF policy.

    Returns the original URL when safe. Raises ``DeepSeekWebError`` otherwise.
    ``resolver`` is injectable for tests; signature matches ``socket.getaddrinfo``.
    """
    if not isinstance(url, str) or not url.strip():
        raise DeepSeekWebError("Fetch URL is required")
    if len(url) > 8192:
        raise DeepSeekWebError("Fetch URL must be at most 8192 characters")

    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise DeepSeekWebError("Fetch URL must use https: (http: is not allowed)")
    if not parsed.hostname:
        raise DeepSeekWebError("Fetch URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise DeepSeekWebError("Fetch URL must not contain username or password")

    hostname = parsed.hostname
    if _is_blocked_hostname(hostname):
        raise DeepSeekWebError(f"Fetch URL host is blocked: {hostname}")

    # Literal IP in the URL — check without DNS.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and _is_blocked_ip(literal):
        raise DeepSeekWebError(f"Fetch URL targets a private/metadata address: {hostname}")

    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DeepSeekWebError(
            f"Fetch URL host could not be resolved (fail closed): {hostname}"
        ) from exc
    if not infos:
        raise DeepSeekWebError(
            f"Fetch URL host resolved to no addresses (fail closed): {hostname}"
        )

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            raise DeepSeekWebError(
                f"Fetch URL host resolution was ambiguous (fail closed): {hostname}"
            )
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except (ValueError, TypeError) as exc:
            raise DeepSeekWebError(
                f"Fetch URL host resolution was ambiguous (fail closed): {hostname}"
            ) from exc
        if _is_blocked_ip(addr):
            raise DeepSeekWebError(
                f"Fetch URL resolves to a private/metadata address ({addr})"
            )
    return url


def _map_http_error(status: int, body: Any) -> DeepSeekWebError:
    detail = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("code") or "")
        elif err is not None:
            detail = str(err)
        elif body.get("message"):
            detail = str(body["message"])
    suffix = f": {detail}" if detail else ""

    messages = {
        400: f"DeepSeek Search/Fetch bad request (400){suffix}",
        401: (
            "DeepSeek API key missing or invalid (401). "
            "Check DEEPSEEK_API_KEY" + (f" — {detail}" if detail else "")
        ),
        402: (
            "DeepSeek account balance insufficient (402). "
            "Top up the account before retrying" + (f" — {detail}" if detail else "")
        ),
        403: f"DeepSeek Fetch rejected by security policy (403){suffix}",
        404: f"DeepSeek Fetch could not get usable content (404){suffix}",
        408: f"DeepSeek request body read timeout (408){suffix}",
        413: f"DeepSeek request body too large (413){suffix}",
        429: (
            f"DeepSeek Search/Fetch QPS exceeded (429; limit {SEARCH_QPS}/{FETCH_QPS}). "
            "Retry with backoff" + (f" — {detail}" if detail else "")
        ),
        500: f"DeepSeek server error (500){suffix}",
        504: f"DeepSeek downstream timeout (504){suffix}",
    }
    if status in messages:
        return DeepSeekWebError(messages[status], status_code=status)
    if status >= 500:
        return DeepSeekWebError(f"DeepSeek server error ({status}){suffix}", status_code=status)
    return DeepSeekWebError(f"DeepSeek Search/Fetch failed ({status}){suffix}", status_code=status)


def _parse_search_response(data: Any) -> SearchResponse:
    if not isinstance(data, dict):
        raise DeepSeekWebError("DeepSeek Search response was not a JSON object")
    raw_results = data.get("results")
    if raw_results is None:
        raise DeepSeekWebError("DeepSeek Search response missing 'results'")
    if not isinstance(raw_results, list):
        raise DeepSeekWebError("DeepSeek Search 'results' must be a list")
    items: list[SearchResultItem] = []
    for row in raw_results:
        if not isinstance(row, dict):
            raise DeepSeekWebError("DeepSeek Search result item must be an object")
        try:
            authority_raw = row.get("authority")
            authority = int(authority_raw) if authority_raw is not None else -1
            items.append(
                SearchResultItem(
                    rerank_score=float(row.get("rerank_score") or 0),
                    title=str(row.get("title") or ""),
                    url=str(row.get("url") or ""),
                    snippet=str(row.get("snippet") or ""),
                    summary=str(row.get("summary") or ""),
                    content=str(row.get("content") or ""),
                    site_name=(
                        str(row["site_name"]) if row.get("site_name") is not None else None
                    ),
                    site_icon=(
                        str(row["site_icon"]) if row.get("site_icon") is not None else None
                    ),
                    date=str(row["date"]) if row.get("date") is not None else None,
                    authority=authority,
                )
            )
        except (TypeError, ValueError) as exc:
            raise DeepSeekWebError(
                "DeepSeek Search result item had invalid field types"
            ) from exc
    return SearchResponse(results=items)


def _parse_fetch_response(data: Any) -> FetchResponse:
    if not isinstance(data, dict):
        raise DeepSeekWebError("DeepSeek Fetch response was not a JSON object")
    if "title" not in data or "content" not in data:
        raise DeepSeekWebError("DeepSeek Fetch response missing title/content")
    return FetchResponse(title=str(data.get("title") or ""), content=str(data.get("content") or ""))


class DeepSeekWebClient:
    """Synchronous client for DeepSeek Search/Fetch APIs."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        resolver: Any = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = resolve_base_url(base_url)
        self._client = client
        self._owns_client = client is None
        self.timeout = timeout
        self._resolver = resolver

    def _headers(self) -> dict[str, str]:
        key = self._api_key if self._api_key is not None else require_api_key()
        if not key:
            raise DeepSeekWebError(
                "DEEPSEEK_API_KEY is not set. Set it to enable DeepSeek Search/Fetch."
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> DeepSeekWebClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self._http().post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise DeepSeekWebError(
                f"DeepSeek request timed out calling {path}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DeepSeekWebError(
                f"DeepSeek transport error calling {path}: {type(exc).__name__}"
            ) from exc

        body: Any
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {"message": (response.text or "")[:200]}

        if response.status_code != 200:
            raise _map_http_error(response.status_code, body)
        return body

    def search(
        self,
        query: str,
        *,
        count: int = 10,
        ori_question: str | None = None,
        localization: str = "",
        need_content: bool = False,
    ) -> SearchResponse:
        if not isinstance(query, str) or not query.strip():
            raise DeepSeekWebError("Search query is required (1–128 non-whitespace characters)")
        if len(query) > 128:
            raise DeepSeekWebError("Search query must be at most 128 characters")
        if type(count) is not int or count < 1 or count > 50:
            raise DeepSeekWebError("Search count must be an integer between 1 and 50")
        payload: dict[str, Any] = {
            "query": query,
            "count": count,
            "localization": localization or "",
            "need_content": bool(need_content),
        }
        if ori_question is not None:
            if (
                not isinstance(ori_question, str)
                or not ori_question.strip()
                or len(ori_question) > 128
            ):
                raise DeepSeekWebError(
                    "ori_question must be 1–128 characters when provided"
                )
            payload["ori_question"] = ori_question
        return _parse_search_response(self._post(SEARCH_PATH, payload))

    def fetch(self, url: str) -> FetchResponse:
        validate_fetch_url(url, resolver=self._resolver)
        return _parse_fetch_response(self._post(FETCH_PATH, {"url": url}))


def format_search_for_tool(response: SearchResponse) -> str:
    if not response.results:
        return "(no search results)"
    lines: list[str] = []
    for i, item in enumerate(response.results, 1):
        site = f" [{item.site_name}]" if item.site_name else ""
        date = f" ({item.date})" if item.date else ""
        lines.append(
            f"{i}. [{item.rerank_score:.2f}|auth={item.authority}] "
            f"{item.title}{site}{date}\n"
            f"   {item.url}\n"
            f"   {item.snippet or item.summary}"
        )
        if item.content:
            lines.append(f"   content: {item.content[:500]}")
    return "\n".join(lines)


def format_fetch_for_tool(response: FetchResponse) -> str:
    title = response.title or "(untitled)"
    content = response.content or "(empty content)"
    if len(content) > 8000:
        content = content[:8000] + "\n... (truncated)"
    return f"# {title}\n\n{content}"


def search_tool_text(**kwargs: Any) -> str:
    """Worker/OMP tool entry: search and return LLM-facing text (raises on error)."""
    with DeepSeekWebClient() as client:
        return format_search_for_tool(client.search(**kwargs))


def fetch_tool_text(url: str) -> str:
    """Worker/OMP tool entry: fetch and return LLM-facing text (raises on error)."""
    with DeepSeekWebClient() as client:
        return format_fetch_for_tool(client.fetch(url))


def _cli_result(ok: bool, **payload: Any) -> int:
    print(json.dumps({"ok": ok, **payload}, ensure_ascii=False))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """CLI used by the OMP bridge (JSON on stdout)."""
    parser = argparse.ArgumentParser(description="DeepSeek Search/Fetch CLI for UC bridges")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="POST /v1/tools/search")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--count", type=int, default=10)
    p_search.add_argument("--ori-question", default=None)
    p_search.add_argument("--localization", default="")
    p_search.add_argument("--need-content", action="store_true")

    p_fetch = sub.add_parser("fetch", help="POST /v1/tools/fetch")
    p_fetch.add_argument("--url", required=True)

    sub.add_parser("configured", help="Exit 0 if DEEPSEEK_API_KEY is set")

    args = parser.parse_args(argv)
    if args.command == "configured":
        return _cli_result(True, configured=is_configured())

    try:
        if args.command == "search":
            text = search_tool_text(
                query=args.query,
                count=args.count,
                ori_question=args.ori_question,
                localization=args.localization,
                need_content=args.need_content,
            )
            return _cli_result(True, text=text)
        if args.command == "fetch":
            text = fetch_tool_text(args.url)
            return _cli_result(True, text=text)
    except DeepSeekWebError as exc:
        return _cli_result(False, error=str(exc), status_code=exc.status_code)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        return _cli_result(False, error=f"{type(exc).__name__}: {exc}")
    return _cli_result(False, error=f"Unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
