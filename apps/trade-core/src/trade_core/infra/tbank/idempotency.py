"""Request order id generation and idempotency mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4


@dataclass(slots=True)
class OrderIdempotencyStore:
    """In-memory mapping from semantic client keys to UUID request ids."""

    _request_ids: dict[str, UUID] = field(default_factory=dict)

    def get_or_create(self, key: str | None) -> UUID:
        if key is None:
            return uuid4()
        if key not in self._request_ids:
            self._request_ids[key] = uuid4()
        return self._request_ids[key]

    def remember(self, key: str, request_order_id: UUID) -> UUID:
        existing = self._request_ids.get(key)
        if existing is not None:
            return existing
        self._request_ids[key] = request_order_id
        return request_order_id

    def get(self, key: str) -> UUID | None:
        return self._request_ids.get(key)
