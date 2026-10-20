from __future__ import annotations

import uuid

import psycopg

from usage.usage_writer import UsageRow, UsageWriter


class PostgresUsageWriter(UsageWriter):
    def __init__(self, conn: psycopg.Connection):
        super().__init__()
        self._conn = conn

    def log_usage(
        self,
        client_id: str,
        doc_id: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        cost_usd: float,
        purpose: str,
    ) -> UsageRow:
        row = super().log_usage(
            client_id, doc_id, model, tokens_in, tokens_out, latency_ms, cost_usd, purpose
        )
        with self._conn.transaction():
            self._conn.execute(
                """insert into llm_usage_rows
                   (client_id, document_id, reservation_id, purpose, model, tokens_in, tokens_out, latency_ms, cost_usd)
                   values (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s)""",
                (
                    client_id,
                    doc_id,
                    str(uuid.uuid4()),
                    purpose,
                    model,
                    tokens_in,
                    tokens_out,
                    latency_ms,
                    cost_usd,
                ),
            )
        return row
