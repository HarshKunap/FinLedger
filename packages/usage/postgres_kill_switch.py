from __future__ import annotations

import uuid

import psycopg

from usage.kill_switch import KillSwitch, Reservation, TokenCapExceededException


class PostgresKillSwitch(KillSwitch):
    """Durable token reservations on Postgres (ephemeral overlay SQL in tests)."""

    def __init__(self, conn: psycopg.Connection, limit: int = 20000, enabled: bool = True):
        super().__init__(limit=limit, enabled=enabled)
        self._conn = conn

    def reserve(self, client_id: str, document_id: str, estimated_tokens: int) -> Reservation:
        if not self.enabled:
            return Reservation(str(uuid.uuid4()), client_id, document_id, estimated_tokens)
        reservation = Reservation(str(uuid.uuid4()), client_id, document_id, estimated_tokens)
        try:
            with self._conn.transaction():
                self._conn.execute(
                    "select llm_reserve_tokens(%s::uuid, %s::uuid, %s::uuid, %s, %s)",
                    (client_id, document_id, reservation.reservation_id, estimated_tokens, self.limit),
                )
        except psycopg.Error as exc:
            if "ai_budget_exceeded" in str(exc):
                raise TokenCapExceededException(
                    f"Token cap exceeded for client {client_id}, doc {document_id}. Limit: {self.limit}"
                ) from exc
            raise
        return reservation

    def reconcile(self, reservation: Reservation, actual_tokens: int):
        if not self.enabled:
            return 0
        with self._conn.transaction():
            self._conn.execute("select llm_reconcile_tokens(%s::uuid)", (reservation.reservation_id,))
        return actual_tokens

    def release(self, reservation: Reservation):
        if not self.enabled:
            return
        with self._conn.transaction():
            self._conn.execute("select llm_release_tokens(%s::uuid)", (reservation.reservation_id,))
