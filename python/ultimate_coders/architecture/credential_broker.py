"""Host-side short-lived credential leases and one-way sandbox network policy.

The broker is the only component that ever sees an upstream secret.  Sandbox
children receive a capability-scoped, short-lived bearer token instead; the
token is self-contained (base64url JSON payload + HMAC-SHA256 signature) and
never written to disk.  This module is deliberately host-side only: it
performs no OS or kernel sandbox calls, it just defines the contract
(:meth:`CredentialLease.to_env`, :meth:`SandboxNetworkPolicy.to_env`) that a
sandbox launcher can apply.

Leases are bounded to :data:`MAX_OUTSTANDING_LEASES` live entries per broker
so a runaway child cannot grow broker memory without bound.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

_BROKER_ENABLED_ENV = "UC_CREDENTIAL_BROKER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

MAX_OUTSTANDING_LEASES = 256
_MAX_TOKEN_CHARS = 4096
_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET")


@dataclass(frozen=True)
class CredentialLease:
    """A capability-scoped bearer token handed to a sandboxed child."""

    lease_id: str
    capability: str
    token: str
    expires_at: float
    broker_url: str

    def expired(self, now: float | None = None) -> bool:
        """Return whether the lease is expired at ``now`` (epoch seconds)."""

        current = time.time() if now is None else float(now)
        return current >= self.expires_at

    def to_env(self) -> dict[str, str]:
        """Return only the two env vars a child may see, never upstream secrets."""

        return {
            "UC_CREDENTIAL_LEASE": self.token,
            "UC_CREDENTIAL_BROKER": self.broker_url,
        }


class CredentialBroker:
    """Issue and verify in-memory credential leases from one upstream secret."""

    def __init__(
        self,
        secret: str | bytes,
        ttl_seconds: int = 60,
        now: Callable[[], float] = time.time,
        broker_url: str = "",
    ) -> None:
        if isinstance(secret, str):
            if not secret.strip():
                raise ValueError("credential broker secret must not be empty")
            secret_bytes = secret.encode("utf-8")
        elif isinstance(secret, bytes):
            if not secret:
                raise ValueError("credential broker secret must not be empty")
            secret_bytes = secret
        else:
            raise TypeError("credential broker secret must be str or bytes")
        if int(ttl_seconds) <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._secret = secret_bytes
        self._ttl_seconds = int(ttl_seconds)
        self._now = now
        self._broker_url = str(broker_url)
        self._leases: dict[str, CredentialLease] = {}
        self._revoked: dict[str, float] = {}

    def issue(self, capability: str, *, ttl_seconds: int | None = None) -> CredentialLease:
        """Mint a lease for ``capability``; raises when the live-lease cap is reached."""

        if not isinstance(capability, str) or not capability.strip():
            raise ValueError("capability must be a non-empty string")
        ttl = self._ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = float(self._now())
        self._prune_expired(now)
        if len(self._leases) >= MAX_OUTSTANDING_LEASES:
            raise RuntimeError(
                f"credential broker lease limit reached ({MAX_OUTSTANDING_LEASES})"
            )
        lease_id = uuid.uuid4().hex
        expires_at = now + ttl
        token = self._sign(
            {
                "lease_id": lease_id,
                "capability": capability,
                "iat": now,
                "exp": expires_at,
            }
        )
        lease = CredentialLease(
            lease_id=lease_id,
            capability=capability,
            token=token,
            expires_at=expires_at,
            broker_url=self._broker_url,
        )
        self._leases[lease_id] = lease
        return lease

    def issue_many(self, capabilities: Iterable[str]) -> list[CredentialLease]:
        """Issue one lease per capability, validating the whole batch first."""

        requested = list(capabilities)
        for capability in requested:
            if not isinstance(capability, str) or not capability.strip():
                raise ValueError("capability must be a non-empty string")
        now = float(self._now())
        self._prune_expired(now)
        if len(self._leases) + len(requested) > MAX_OUTSTANDING_LEASES:
            raise RuntimeError(
                f"credential broker lease limit reached ({MAX_OUTSTANDING_LEASES})"
            )
        return [self.issue(capability) for capability in requested]

    def verify(self, token: str, *, capability: str | None = None) -> dict[str, Any] | None:
        """Return the signed payload, or ``None`` for invalid/expired/revoked tokens."""

        if not isinstance(token, str) or len(token) > _MAX_TOKEN_CHARS:
            return None
        parts = token.split(".")
        if len(parts) != 2:
            return None
        encoded, signature = parts
        try:
            provided_signature = _b64url_decode(signature)
            payload_bytes = _b64url_decode(encoded)
            expected_signature = hmac.new(
                self._secret, encoded.encode("ascii"), hashlib.sha256
            ).digest()
        except (binascii.Error, UnicodeEncodeError, ValueError):
            return None
        if not hmac.compare_digest(provided_signature, expected_signature):
            return None
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        lease_id = payload.get("lease_id")
        payload_capability = payload.get("capability")
        expires_at = payload.get("exp")
        if not isinstance(lease_id, str) or not isinstance(payload_capability, str):
            return None
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            return None
        if capability is not None and payload_capability != capability:
            return None
        if float(self._now()) >= float(expires_at):
            return None
        if lease_id in self._revoked:
            return None
        return dict(payload)

    def revoke(self, lease_id: str) -> None:
        """Revoke one lease id; revoking an unknown or already-revoked id is a no-op."""

        if not isinstance(lease_id, str) or not lease_id:
            return
        lease = self._leases.pop(lease_id, None)
        expiry = lease.expires_at if lease is not None else float(self._now()) + self._ttl_seconds
        self._revoked[lease_id] = expiry

    def _sign(self, payload: Mapping[str, Any]) -> str:
        encoded = _b64url(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{encoded}.{_b64url(signature)}"

    def _prune_expired(self, now: float) -> None:
        stale_leases = [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease.expires_at <= now
        ]
        for lease_id in stale_leases:
            self._leases.pop(lease_id, None)
        stale_revocations = [
            lease_id for lease_id, expiry in self._revoked.items() if expiry <= now
        ]
        for lease_id in stale_revocations:
            self._revoked.pop(lease_id, None)


@dataclass(frozen=True)
class SandboxNetworkPolicy:
    """Declarative one-way network policy for a sandboxed child.

    ``to_env`` returns ``UC_NETWORK_MODE=none`` when egress is disabled.
    ``UC_CREDENTIAL_LEASE``/``UC_CREDENTIAL_BROKER`` may be supplied through
    ``broker_env``; any inherited key ending in ``_KEY``/``_TOKEN``/``_SECRET``
    is always dropped.
    """

    allow_egress: bool = False
    allow_connect_back: bool = False
    allowed_hosts: tuple[str, ...] = ()
    broker_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        hosts = tuple(
            dict.fromkeys(
                host.strip()
                for host in self.allowed_hosts
                if isinstance(host, str) and host.strip()
            )
        )
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "broker_env", dict(self.broker_env))

    def to_env(self) -> dict[str, str]:
        env = {
            str(key): str(value)
            for key, value in self.broker_env.items()
            if not _is_secret_env_key(str(key))
        }
        if not self.allow_egress:
            mode = "none"
        elif self.allowed_hosts:
            mode = "allowlist"
        else:
            mode = "egress"
        env["UC_NETWORK_MODE"] = mode
        env["UC_NETWORK_ALLOWLIST"] = ",".join(self.allowed_hosts)
        env["UC_NETWORK_ALLOW_CONNECT_BACK"] = "1" if self.allow_connect_back else "0"
        return env


def credential_broker_enabled() -> bool:
    """Return whether credential brokering is enabled via ``UC_CREDENTIAL_BROKER``.

    ``CredentialLease.to_env()`` puts the broker URL in this variable, so a URL
    is itself an enabling value; the boolean forms stay supported for launchers
    that only toggle the contract.
    """

    value = os.environ.get(_BROKER_ENABLED_ENV, "").strip()
    return value.lower() in _TRUTHY or value.startswith(("http://", "https://"))


def _is_secret_env_key(key: str) -> bool:
    return key.strip().upper().endswith(_SECRET_ENV_SUFFIXES)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)
