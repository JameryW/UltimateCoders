"""Unit tests for DeepSeek Search/Fetch client (no live paid calls)."""

from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any

import httpx
import pytest
from ultimate_coders.agent import deepseek_web as dw


def _public_resolver(host: str, port: int, *args: Any, **kwargs: Any):
    return [(2, 1, 6, "", ("93.184.216.34", port))]  # example.com public


def _private_resolver(host: str, port: int, *args: Any, **kwargs: Any):
    return [(2, 1, 6, "", ("10.0.0.5", port))]


def _link_local_resolver(host: str, port: int, *args: Any, **kwargs: Any):
    return [(2, 1, 6, "", ("169.254.169.254", port))]


def _unresolved(host: str, port: int, *args: Any, **kwargs: Any):
    raise socket.gaierror("Name or service not known")


class TestConfigAndBaseUrl:
    def test_is_configured_when_key_set(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        assert dw.is_configured() is True
        assert dw.deepseek_api_key() == "sk-test"

    def test_is_configured_false_when_blank(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "  ")
        assert dw.is_configured() is False
        assert dw.deepseek_api_key() is None

    def test_is_configured_false_when_missing(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        assert dw.is_configured() is False

    def test_require_api_key_fail_closed(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(dw.DeepSeekWebError, match="DEEPSEEK_API_KEY is not set"):
            dw.require_api_key()

    def test_default_base_url(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        assert dw.resolve_base_url() == "https://api.deepseek.com"

    def test_base_url_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://proxy.example/deepseek/")
        assert dw.resolve_base_url() == "https://proxy.example/deepseek"


class TestFetchUrlPolicy:
    def test_rejects_http(self):
        with pytest.raises(dw.DeepSeekWebError, match="https"):
            dw.validate_fetch_url("http://example.com/a", resolver=_public_resolver)

    def test_rejects_userinfo(self):
        with pytest.raises(dw.DeepSeekWebError, match="username or password"):
            dw.validate_fetch_url(
                "https://user:pass@example.com/a", resolver=_public_resolver
            )

    def test_rejects_localhost(self):
        with pytest.raises(dw.DeepSeekWebError, match="blocked"):
            dw.validate_fetch_url("https://localhost/admin", resolver=_public_resolver)

    def test_rejects_rfc1918_literal(self):
        with pytest.raises(dw.DeepSeekWebError, match="private/metadata"):
            dw.validate_fetch_url("https://10.1.2.3/", resolver=_public_resolver)

    def test_rejects_cgnat_literal(self):
        # 100.64/10 is non-global but not is_private in Python — must still block.
        with pytest.raises(dw.DeepSeekWebError, match="private/metadata"):
            dw.validate_fetch_url("https://100.64.0.1/", resolver=_public_resolver)

    def test_rejects_alibaba_metadata_literal(self):
        with pytest.raises(dw.DeepSeekWebError, match="private/metadata"):
            dw.validate_fetch_url("https://100.100.100.200/", resolver=_public_resolver)

    def test_rejects_link_local_metadata_literal(self):
        with pytest.raises(dw.DeepSeekWebError, match="private/metadata"):
            dw.validate_fetch_url("https://169.254.169.254/latest/", resolver=_public_resolver)

    def test_rejects_unique_local_literal(self):
        with pytest.raises(dw.DeepSeekWebError, match="private/metadata"):
            dw.validate_fetch_url("https://[fd12::1]/", resolver=_public_resolver)

    def test_rejects_resolved_private(self):
        with pytest.raises(dw.DeepSeekWebError, match="private/metadata"):
            dw.validate_fetch_url("https://evil.example/", resolver=_private_resolver)

    def test_rejects_resolved_metadata(self):
        with pytest.raises(dw.DeepSeekWebError, match="private/metadata"):
            dw.validate_fetch_url("https://evil.example/", resolver=_link_local_resolver)

    def test_rejects_unresolved(self):
        with pytest.raises(dw.DeepSeekWebError, match="could not be resolved"):
            dw.validate_fetch_url("https://no-such-host.invalid/", resolver=_unresolved)

    def test_allows_public_https(self):
        assert (
            dw.validate_fetch_url("https://example.com/page", resolver=_public_resolver)
            == "https://example.com/page"
        )


class TestClientHttp:
    def test_search_request_construction(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "rerank_score": 2.4,
                            "title": "T",
                            "url": "https://example.com",
                            "snippet": "snip",
                            "summary": "sum",
                            "content": "",
                            "site_name": "Ex",
                            "site_icon": None,
                            "date": "2026-06-01T00:00:00+00:00",
                            "authority": 3,
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as http:
            client = dw.DeepSeekWebClient(client=http, resolver=_public_resolver)
            resp = client.search("python release", count=5, need_content=False)

        assert captured["url"] == "https://api.deepseek.com/v1/tools/search"
        assert captured["auth"] == "Bearer sk-test"
        assert captured["body"] == {
            "query": "python release",
            "count": 5,
            "localization": "",
            "need_content": False,
        }
        assert len(resp.results) == 1
        assert resp.results[0].title == "T"
        assert resp.results[0].authority == 3

    def test_search_honors_custom_base_url(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://proxy.example/")

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://proxy.example/v1/tools/search"
            return httpx.Response(200, json={"results": []})

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = dw.DeepSeekWebClient(client=http)
            resp = client.search("q")
        assert resp.results == []

    def test_fetch_request_and_response(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://api.deepseek.com/v1/tools/fetch"
            assert json.loads(request.content.decode()) == {
                "url": "https://example.com/article"
            }
            return httpx.Response(
                200, json={"title": "Example article", "content": "# Example article\n\nHi"}
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = dw.DeepSeekWebClient(client=http, resolver=_public_resolver)
            resp = client.fetch("https://example.com/article")
        assert resp.title == "Example article"
        assert "Hi" in resp.content

    def test_fetch_refuses_private_before_http(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        called = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={"title": "x", "content": "y"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = dw.DeepSeekWebClient(client=http, resolver=_private_resolver)
            with pytest.raises(dw.DeepSeekWebError, match="private/metadata"):
                client.fetch("https://evil.example/")
        assert called["n"] == 0

    def test_missing_key_at_call_time(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": []})

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = dw.DeepSeekWebClient(client=http)
            with pytest.raises(dw.DeepSeekWebError, match="DEEPSEEK_API_KEY is not set"):
                client.search("q")

    @pytest.mark.parametrize(
        "status,match",
        [
            (401, "invalid"),
            (402, "balance"),
            (403, "security policy"),
            (404, "usable content"),
            (429, "QPS"),
            (500, "server error"),
            (504, "timeout"),
        ],
    )
    def test_error_mapping(self, monkeypatch, status, match):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status, json={"error": {"message": "boom", "type": "x", "param": None}}
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = dw.DeepSeekWebClient(client=http, resolver=_public_resolver)
            with pytest.raises(dw.DeepSeekWebError, match=match) as excinfo:
                client.search("q")
            assert excinfo.value.status_code == status


class TestCliAndFormatting:
    def test_cli_configured(self, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        code = dw.main(["configured"])
        assert code == 0
        assert json.loads(capsys.readouterr().out)["configured"] is True

    def test_cli_fetch_rejects_http(self, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        code = dw.main(["fetch", "--url", "http://example.com"])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "https" in payload["error"]

    def test_cli_search_with_mock_transport(self, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "rerank_score": 1.5,
                            "title": "Hi",
                            "url": "https://example.com",
                            "snippet": "s",
                            "summary": "sum",
                            "content": "",
                            "site_name": None,
                            "site_icon": None,
                            "date": None,
                            "authority": 2,
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client

        def factory(*args: Any, **kwargs: Any):
            kwargs = {**kwargs, "transport": transport}
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)
        code = dw.main(["search", "--query", "hello", "--count", "3"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert "Hi" in payload["text"]

    def test_format_search_empty(self):
        assert dw.format_search_for_tool(dw.SearchResponse(results=[])) == "(no search results)"

    def test_format_fetch_truncates(self):
        text = dw.format_fetch_for_tool(dw.FetchResponse(title="T", content="x" * 9000))
        assert text.startswith("# T\n")
        assert "... (truncated)" in text

    def test_blocked_ip_helpers(self):
        assert dw._is_blocked_ip(ipaddress.ip_address("127.0.0.1"))
        assert dw._is_blocked_ip(ipaddress.ip_address("192.168.1.1"))
        assert dw._is_blocked_ip(ipaddress.ip_address("169.254.169.254"))
        assert dw._is_blocked_ip(ipaddress.ip_address("100.64.0.1"))
        assert dw._is_blocked_ip(ipaddress.ip_address("100.100.100.200"))
        assert dw._is_blocked_ip(ipaddress.ip_address("::ffff:10.0.0.1"))
        assert not dw._is_blocked_ip(ipaddress.ip_address("93.184.216.34"))
        assert not dw._is_blocked_ip(ipaddress.ip_address("8.8.8.8"))
