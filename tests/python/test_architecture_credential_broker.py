"""Tests for host-side credential leases and sandbox network policy."""

from __future__ import annotations

import base64
import json

import pytest
from ultimate_coders.architecture.credential_broker import (
    MAX_OUTSTANDING_LEASES,
    CredentialBroker,
    CredentialLease,
    SandboxNetworkPolicy,
    credential_broker_enabled,
)


class FakeClock:
    """Deterministic epoch clock for lease expiry tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_broker(clock: FakeClock | None = None, **kwargs) -> CredentialBroker:
    return CredentialBroker(
        "upstream-secret",
        ttl_seconds=60,
        now=clock if clock is not None else FakeClock(),
        **kwargs,
    )


def decode_payload(token: str) -> dict:
    encoded = token.split(".", 1)[0]
    padded = encoded + "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_issue_and_verify_round_trip() -> None:
    clock = FakeClock()
    broker = make_broker(clock, broker_url="https://broker.test")

    lease = broker.issue("repo:read")

    assert isinstance(lease, CredentialLease)
    assert lease.lease_id
    assert lease.capability == "repo:read"
    assert lease.expires_at == 1_060.0
    assert lease.broker_url == "https://broker.test"
    assert lease.token.count(".") == 1
    assert "upstream-secret" not in lease.token
    assert lease.expired(now=1_059.0) is False

    payload = broker.verify(lease.token)
    assert payload is not None
    assert payload["lease_id"] == lease.lease_id
    assert payload["capability"] == "repo:read"
    assert payload["exp"] == 1_060.0
    assert broker.verify(lease.token, capability="repo:read") is not None
    assert broker.verify(lease.token, capability="repo:write") is None


def test_token_payload_is_signed_json_without_the_secret() -> None:
    broker = make_broker()

    payload = decode_payload(broker.issue("repo:read").token)

    assert set(payload) == {"lease_id", "capability", "iat", "exp"}
    assert "upstream-secret" not in json.dumps(payload)


def test_verify_rejects_tampered_malformed_and_wrong_type() -> None:
    broker = make_broker()
    lease = broker.issue("repo:read")

    assert broker.verify(lease.token[:-2] + "AA") is None
    assert broker.verify("not-a-token") is None
    assert broker.verify("a.b.c") is None
    assert broker.verify(lease.token + "x") is None
    assert broker.verify(123) is None  # type: ignore[arg-type]


def test_other_broker_with_different_secret_rejects_token() -> None:
    lease = make_broker().issue("repo:read")

    assert CredentialBroker("different-secret").verify(lease.token) is None


def test_bytes_secret_works() -> None:
    broker = CredentialBroker(b"binary-secret", now=FakeClock())

    assert broker.verify(broker.issue("repo:read").token) is not None


def test_expiry_invalidates_verification() -> None:
    clock = FakeClock()
    broker = make_broker(clock)
    lease = broker.issue("repo:read")

    clock.advance(29)
    assert broker.verify(lease.token) is not None

    clock.advance(31)
    assert broker.verify(lease.token) is None
    assert lease.expired(now=clock.value) is True


def test_revoke_invalidates_token_and_is_idempotent() -> None:
    broker = make_broker()
    lease = broker.issue("repo:read")

    broker.revoke(lease.lease_id)
    broker.revoke(lease.lease_id)
    broker.revoke("unknown-lease")

    assert broker.verify(lease.token) is None


def test_per_lease_ttl_override() -> None:
    clock = FakeClock()
    broker = make_broker(clock)

    lease = broker.issue("net:connect", ttl_seconds=5)

    assert lease.expires_at == 1_005.0
    with pytest.raises(ValueError):
        broker.issue("net:connect", ttl_seconds=0)


@pytest.mark.parametrize("secret", ["", "   ", b""])
def test_rejects_empty_secret(secret) -> None:
    with pytest.raises(ValueError):
        CredentialBroker(secret)


def test_rejects_invalid_constructor_arguments() -> None:
    with pytest.raises(TypeError):
        CredentialBroker(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CredentialBroker("secret", ttl_seconds=0)


@pytest.mark.parametrize("capability", ["", "   ", None])
def test_rejects_empty_capability(capability) -> None:
    with pytest.raises(ValueError):
        make_broker().issue(capability)  # type: ignore[arg-type]


def test_issued_leases_are_unique() -> None:
    broker = make_broker()

    leases = [broker.issue("repo:read") for _ in range(3)]

    assert len({lease.lease_id for lease in leases}) == 3
    assert len({lease.token for lease in leases}) == 3
    assert all(broker.verify(lease.token) is not None for lease in leases)


def test_issue_many_issues_in_order() -> None:
    broker = make_broker()

    leases = broker.issue_many(["repo:read", "repo:write"])

    assert [lease.capability for lease in leases] == ["repo:read", "repo:write"]
    assert all(broker.verify(lease.token) is not None for lease in leases)


def test_issue_many_validates_the_batch_before_issuing() -> None:
    broker = make_broker()

    with pytest.raises(ValueError):
        broker.issue_many(["repo:read", ""])

    leases = broker.issue_many(["repo:read"] * MAX_OUTSTANDING_LEASES)
    assert len(leases) == MAX_OUTSTANDING_LEASES


def test_outstanding_lease_limit_and_expiry_slot_reuse() -> None:
    clock = FakeClock()
    broker = make_broker(clock)

    leases = [broker.issue("repo:read") for _ in range(MAX_OUTSTANDING_LEASES)]
    assert len(leases) == MAX_OUTSTANDING_LEASES

    with pytest.raises(RuntimeError):
        broker.issue("repo:read")

    clock.advance(60)
    fresh = broker.issue("repo:read")
    assert broker.verify(fresh.token) is not None


def test_lease_env_exposes_only_broker_handles() -> None:
    broker = make_broker(broker_url="https://broker.test")
    lease = broker.issue("repo:read")

    env = lease.to_env()

    assert env == {
        "UC_CREDENTIAL_LEASE": lease.token,
        "UC_CREDENTIAL_BROKER": "https://broker.test",
    }
    assert "upstream-secret" not in json.dumps(env)


def test_default_network_policy_is_deny_all() -> None:
    env = SandboxNetworkPolicy().to_env()

    assert env == {
        "UC_NETWORK_MODE": "none",
        "UC_NETWORK_ALLOWLIST": "",
        "UC_NETWORK_ALLOW_CONNECT_BACK": "0",
    }


def test_network_policy_never_inherits_upstream_secrets() -> None:
    policy = SandboxNetworkPolicy(
        allow_egress=True,
        allow_connect_back=False,
        allowed_hosts=(" api.example.com ", "api.example.com", "", "cdn.example.com"),
        broker_env={
            "UC_CREDENTIAL_LEASE": "lease-token",
            "UC_CREDENTIAL_BROKER": "https://broker.test",
            "OPENAI_API_KEY": "sk-live-value",
            "GITHUB_TOKEN": "ghp_live_value",
            "DB_SECRET": "hunter2",
            "SAFE_FLAG": "yes",
        },
    )

    env = policy.to_env()

    assert policy.allowed_hosts == ("api.example.com", "cdn.example.com")
    assert env["UC_CREDENTIAL_LEASE"] == "lease-token"
    assert env["UC_CREDENTIAL_BROKER"] == "https://broker.test"
    assert env["SAFE_FLAG"] == "yes"
    assert env["UC_NETWORK_MODE"] == "allowlist"
    assert env["UC_NETWORK_ALLOWLIST"] == "api.example.com,cdn.example.com"
    assert env["UC_NETWORK_ALLOW_CONNECT_BACK"] == "0"
    assert not any(
        key.upper().endswith(("_KEY", "_TOKEN", "_SECRET")) for key in env
    )
    assert "sk-live-value" not in env.values()
    assert "ghp_live_value" not in env.values()
    assert "hunter2" not in env.values()


def test_network_policy_egress_without_allowlist_and_connect_back() -> None:
    env = SandboxNetworkPolicy(allow_egress=True, allow_connect_back=True).to_env()

    assert env["UC_NETWORK_MODE"] == "egress"
    assert env["UC_NETWORK_ALLOWLIST"] == ""
    assert env["UC_NETWORK_ALLOW_CONNECT_BACK"] == "1"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        (" YES ", True),
        ("on", True),
        ("https://broker.test", True),
        ("http://broker.local:8080", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_credential_broker_enabled_flag(monkeypatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("UC_CREDENTIAL_BROKER", value)

    assert credential_broker_enabled() is expected


def test_credential_broker_enabled_without_env(monkeypatch) -> None:
    monkeypatch.delenv("UC_CREDENTIAL_BROKER", raising=False)

    assert credential_broker_enabled() is False
