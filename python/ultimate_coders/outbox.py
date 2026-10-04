"""Immutable outcomes, bounded replay and renewable delivery ownership."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .runtime_state import RuntimeState


@dataclass(frozen=True)
class OutcomeClaim:
    key: str
    token: str
    record: dict


class OutcomeOutbox:
    def __init__(self, state: RuntimeState):
        self.state = state

    def pending(self, limit: int = 100) -> list[dict]:
        return self.state.query("result_outbox", status="pending", limit=limit, due=True)

    def claim(self, key: str, *, ttl: float = 120) -> OutcomeClaim | None:
        token, now = uuid.uuid4().hex, time.time()

        def claim(old):
            if (
                not old
                or old.get("delivered")
                or old.get("next_retry_at", 0) > now
                or old.get("delivery_claim", {}).get("expires_at", 0) > now
            ):
                return old
            return {
                **old,
                "delivery_claim": {"token": token, "expires_at": now + ttl},
                "next_retry_at": now + ttl,
            }

        record = self.state.mutate("result_outbox", key, claim)
        if record.get("delivery_claim", {}).get("token") != token:
            return None
        return OutcomeClaim(key, token, record)

    def renew(self, claim: OutcomeClaim, *, ttl: float = 120) -> None:
        def renew(old):
            if old.get("delivery_claim", {}).get("token") != claim.token:
                raise RuntimeError("Outcome delivery ownership changed")
            return {
                **old,
                "delivery_claim": {"token": claim.token, "expires_at": time.time() + ttl},
                "next_retry_at": time.time() + ttl,
            }

        self.state.mutate("result_outbox", claim.key, renew)

    def finish(self, claim: OutcomeClaim, *, delivered: bool) -> None:
        def finish(old):
            if old.get("delivery_claim", {}).get("token") != claim.token:
                return old
            attempts = old.get("delivery_attempts", 0) + 1
            result = {
                **old,
                "delivery_claim": {},
                "delivery_attempts": attempts,
                "next_retry_at": 0 if delivered else time.time() + min(300, 2 ** min(attempts, 8)),
            }
            if delivered:
                result.update(delivered=True, delivered_at=time.time())
            return result

        self.state.mutate("result_outbox", claim.key, finish)
