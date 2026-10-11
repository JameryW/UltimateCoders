"""Explicit native Ollama profile; model aliases are checked against actual digests."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE = 4 * 1024 * 1024
CODING_CLIENT = "/opt/uc/ultimate_coders/metainfer_service/coding.py"
logger = logging.getLogger(__name__)


def native_client_identity() -> dict[str, str]:
    package = Path(__file__).parent
    files = {
        name: package / name
        for name in ("coding.py", "ollama.py", "gpu_probe.py", "observations.py")
    }
    files["process.py"] = package.parent / "inference" / "process.py"
    return {
        name: hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for name, path in files.items()
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Ollama redirects are unsupported")


@dataclass(frozen=True)
class OllamaProfile:
    url: str
    model: str
    digest: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in ("http", "https")
            or len(self.url) > 2048
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", self.model)
            or not re.fullmatch(r"[a-f0-9]{64}", self.digest)
        ):
            raise ValueError("Invalid explicit Ollama profile")
        object.__setattr__(self, "url", self.url.rstrip("/"))

    @classmethod
    def from_environment(cls, environment: dict[str, str]) -> OllamaProfile:
        return cls(
            environment.get("UC_OLLAMA_URL", ""),
            environment.get("UC_OLLAMA_MODEL", ""),
            environment.get("UC_OLLAMA_DIGEST", ""),
        )

    def _exchange(self, path: str, payload: dict | None, timeout: float) -> dict:
        if timeout <= 0:
            raise TimeoutError("Ollama request deadline exceeded before launch")
        content = json.dumps(
            {
                "url": self.url,
                "model": self.model,
                "digest": self.digest,
                "path": path,
                "payload": payload,
                "timeout": timeout,
            }
        ).encode()
        if len(content) > MAX_RESPONSE + 8192:
            raise ValueError("Ollama request exceeds the conversation bound")
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--exchange"],
                input=content,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # A dedicated request process makes header/body trickling and DNS
            # stalls cancellable; socket idle timeouts cannot provide this bound.
            raise TimeoutError("Ollama request deadline exceeded") from exc
        if result.returncode:
            logger.error(
                "Ollama exchange failed: %s", result.stderr[-4000:].decode(errors="replace")
            )
            raise ValueError("Ollama protocol exchange failed")
        return json.loads(result.stdout)

    def identity(self, *, timeout: float = 2) -> dict:
        inventory = self._exchange("/api/tags", None, timeout)
        if (
            not isinstance(inventory, dict)
            or not isinstance(inventory.get("models"), list)
            or any(not isinstance(item, dict) for item in inventory["models"])
        ):
            raise ValueError("Ollama inventory is malformed")
        matches = [item for item in inventory["models"] if item.get("name") == self.model]
        if len(matches) != 1 or matches[0].get("digest") != self.digest:
            raise ValueError("Ollama model digest changed or is unavailable")
        return {"provider": "ollama", "model": self.model, "digest": self.digest}

    def chat(self, messages: list, *, tools: list, timeout: float = 600) -> dict:
        deadline = time.monotonic() + timeout
        self.identity(timeout=min(2, max(0, deadline - time.monotonic())))
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "think": False,
            "stream": True,
            "options": {"temperature": 0.1, "num_ctx": 16384, "num_predict": 4096},
        }
        content = json.dumps(payload).encode()
        if len(content) > MAX_RESPONSE:
            raise ValueError("Ollama request exceeds the conversation bound")
        result = self._exchange("/api/chat", payload, deadline - time.monotonic())
        self.identity(timeout=min(2, max(0, deadline - time.monotonic())))
        return result


def http_exchange(profile: OllamaProfile, path: str, payload: dict | None, timeout: float) -> dict:
    """Runs only in a deadline-owned process; emits one bounded protocol result."""
    if path not in ("/api/tags", "/api/chat"):
        raise ValueError("Unsupported Ollama protocol endpoint")
    content = None if payload is None else json.dumps(payload).encode()
    request = Request(
        profile.url + path, data=content, headers={"Content-Type": "application/json"}
    )
    parts, thinking, calls, size = [], [], [], 0
    with build_opener(ProxyHandler({}), NoRedirect()).open(
        request, timeout=min(180, timeout)
    ) as response:
        if path == "/api/tags":
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise ValueError("Ollama inventory exceeds the response bound")
            return json.loads(body)
        while True:
            line = response.readline(MAX_RESPONSE - size + 1)
            if not line:
                raise ValueError("Ollama stream ended without completion")
            size += len(line)
            if size > MAX_RESPONSE:
                raise ValueError("Ollama stream exceeds the response bound")
            chunk = json.loads(line)
            if (
                not isinstance(chunk, dict)
                or chunk.get("model") != profile.model
                or chunk.get("error")
            ):
                raise ValueError("Ollama response model changed or inference failed")
            message = chunk.get("message", {})
            if (
                not isinstance(message, dict)
                or not isinstance(message.get("content", ""), str)
                or not isinstance(message.get("thinking", ""), str)
                or not isinstance(message.get("tool_calls", []), list)
                or any(not isinstance(call, dict) for call in message.get("tool_calls", []))
            ):
                raise ValueError("Ollama stream message is malformed")
            parts.append(message.get("content", ""))
            thinking.append(message.get("thinking", ""))
            calls.extend(message.get("tool_calls", []))
            if chunk.get("done") is True:
                return {
                    **chunk,
                    "message": {
                        "role": "assistant",
                        "content": "".join(parts),
                        "thinking": "".join(thinking),
                        "tool_calls": calls,
                    },
                }


if __name__ == "__main__":
    if sys.argv[1:] != ["--exchange"]:
        raise SystemExit("Use the explicit native coding client")
    raw = sys.stdin.buffer.read(MAX_RESPONSE + 8193)
    if len(raw) > MAX_RESPONSE + 8192:
        raise ValueError("Ollama request exceeds the conversation bound")
    exchange = json.loads(raw)
    profile = OllamaProfile(exchange["url"], exchange["model"], exchange["digest"])
    result = http_exchange(profile, exchange["path"], exchange["payload"], exchange["timeout"])
    response = json.dumps(result).encode()
    if len(response) > MAX_RESPONSE:
        raise ValueError("Ollama result exceeds the response bound")
    sys.stdout.buffer.write(response)
