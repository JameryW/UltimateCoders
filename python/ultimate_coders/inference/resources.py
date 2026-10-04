"""Durable FIFO reservations shared by workers; active writers never expire."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ultimate_coders.runtime_state import RuntimeState


class ResourceWaitExpiredError(RuntimeError):
    retryable = False


@dataclass(frozen=True)
class ResourceReservation:
    resource_id: str
    operation_id: str


class ResourceBudget:
    def __init__(self, state: RuntimeState, resource_id: str, capacity: int = 1):
        if not resource_id or type(capacity) is not int or capacity < 1:
            raise ValueError("Resource identity and positive capacity are required")
        self.state, self.resource_id, self.capacity = state, resource_id, capacity

    def inherit_reservations(self, operation_ids: list[str]) -> None:
        def inherit(old):
            holders = dict(old.get("holders", {}))
            for identity in operation_ids:
                holders.setdefault(identity, {"acquired_at": time.time(), "legacy": True})
            return {
                **old,
                "capacity": old.get("capacity", self.capacity),
                "holders": holders,
                "queue": old.get("queue", []),
                "state": "occupied" if holders else "idle",
            }

        self.state.mutate("resource_budgets", self.resource_id, inherit)

    def _take(self, operation_id: str, deadline: float) -> tuple[bool, bool]:
        now = time.time()
        newly_acquired = False

        def take(old):
            nonlocal newly_acquired
            if old and old.get("capacity") != self.capacity:
                raise ValueError("Shared resource capacity differs between workers")
            holders = dict(old.get("holders", {}))
            queue = [item for item in old.get("queue", []) if item["expires_at"] > now]
            if operation_id in holders:
                return old
            ticket = next((item for item in queue if item["operation_id"] == operation_id), None)
            if ticket is None:
                ticket = {"operation_id": operation_id, "created_at": now}
                queue.append(ticket)
            # Only unsubmitted waiting tickets expire. Active reservations have
            # no heartbeat expiry: losing a worker cannot prove its writer stopped.
            ticket["expires_at"] = min(deadline, now + 30)
            if queue[0]["operation_id"] == operation_id and len(holders) < self.capacity:
                holders[operation_id] = {"acquired_at": now}
                newly_acquired = True
                queue.pop(0)
            return {
                "capacity": self.capacity,
                "holders": holders,
                "queue": queue,
                "state": "occupied" if holders else "idle",
            }

        result = self.state.mutate("resource_budgets", self.resource_id, take)
        return operation_id in result.get("holders", {}), newly_acquired

    async def acquire(
        self, operation_id: str, *, timeout: float, poll_interval: float = 0.5
    ) -> ResourceReservation:
        deadline = time.time() + timeout
        acquired = False
        try:
            while time.time() < deadline:
                pending = asyncio.create_task(asyncio.to_thread(self._take, operation_id, deadline))
                try:
                    taken, new = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    taken, new = await pending
                    if taken and new:
                        await asyncio.to_thread(self.release, operation_id)
                    raise
                if taken:
                    acquired = True
                    return ResourceReservation(self.resource_id, operation_id)
                await asyncio.sleep(poll_interval)
            raise ResourceWaitExpiredError("Resource wait deadline exceeded before submission")
        finally:
            if not acquired:
                await asyncio.to_thread(self.cancel_wait, operation_id)

    def cancel_wait(self, operation_id: str) -> None:
        self.state.mutate(
            "resource_budgets",
            self.resource_id,
            lambda old: {
                **old,
                "queue": [
                    item for item in old.get("queue", []) if item["operation_id"] != operation_id
                ],
            },
        )

    def release(self, operation_id: str) -> None:
        def release(old):
            holders = {
                key: value for key, value in old.get("holders", {}).items() if key != operation_id
            }
            return {**old, "holders": holders, "state": "occupied" if holders else "idle"}

        self.state.mutate("resource_budgets", self.resource_id, release)
