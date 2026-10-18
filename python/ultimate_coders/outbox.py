"""Immutable outcomes, bounded replay and renewable delivery ownership."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass

from .runtime_state import RuntimeState


@dataclass(frozen=True)
class OutcomeClaim:
    key: str
    token: str
    record: dict


def outcome_fingerprint(event: dict, update: dict) -> str:
    """Content hash of one terminal outcome (message ids excluded).

    Message ids embed this hash, so it must be computed before they are
    set. Two resends of the same outcome hash identically; a retry with a
    different result does not.
    """
    body = json.dumps({"event": event, "update": update}, sort_keys=True, default=str)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def outcome_key(task_id: str, subtask_id: str) -> str:
    """Logical identity of one subtask's terminal outcome.

    Deliberately excludes the attempt number: a subtask has one current
    outcome, and a retry supersedes it rather than minting a second
    terminal record. The attempt is stored *inside* the record so the
    supersession rule stays checkable.
    """
    return f"{task_id}:{subtask_id}"


class OutcomeOutbox:
    def __init__(self, state: RuntimeState):
        self.state = state

    def store(
        self,
        key: str,
        *,
        attempt: int,
        fingerprint: str,
        event: dict,
        update: dict,
    ) -> dict:
        """Record one attempt's terminal outcome under the logical key.

        Rules, all decided inside the transaction:

        * no record -> store it (undelivered);
        * higher attempt -> replace it (the newer attempt is the truth);
        * same attempt + same fingerprint -> keep the existing record and
          its delivery state (a resend, not a new fact);
        * same attempt + different fingerprint -> keep the first (two
          different results for one attempt is a bug, not a race to win);
        * lower attempt -> ignore (a late finisher must not overwrite a
          newer outcome).

        The caller decides to publish by comparing the returned record's
        ``attempt``/``fingerprint`` with its own.
        """

        def change(old: dict) -> dict:
            if not old:
                return {
                    "event": event,
                    "update": update,
                    "delivered": False,
                    "attempt": attempt,
                    "fingerprint": fingerprint,
                }
            stored = int(old.get("attempt", -1))
            if attempt < stored:
                return old
            if attempt == stored:
                return old
            return {
                "event": event,
                "update": update,
                "delivered": False,
                "attempt": attempt,
                "fingerprint": fingerprint,
                "superseded_attempt": stored,
            }

        return self.state.mutate("result_outbox", key, change)

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
