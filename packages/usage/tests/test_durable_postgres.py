from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.rows import dict_row

from finledger_platform import tenancy
from finledger_platform.db import firm, tenant
from rag.postgres_indexer import PostgresIndexer
from rag.retriever import Retriever
from usage.kill_switch import TokenCapExceededException
from usage.postgres_kill_switch import PostgresKillSwitch
from usage.postgres_usage_writer import PostgresUsageWriter
from testing.pg_harness import ai_app_dsn, ai_owner_dsn


def _seed_client(owner_dsn: str) -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:8]
    with psycopg.connect(owner_dsn, autocommit=True) as owner:
        firm_id, _, _ = tenancy.create_firm_with_admin(owner, f"AI Firm {suffix}", f"ai-{suffix}@test.example")
        with firm(owner, firm_id) as conn:
            conn.row_factory = dict_row
            client = tenancy.create_client(
                conn,
                f"Client A {suffix}",
                gstins=["27AAPFU0939F1ZV"],
                inbound_slug=f"client-{suffix}",
            )
            client_id = str(client["id"])
            doc_id = str(
                conn.execute(
                    """insert into documents (client_id, channel, object_path, content_hash, mime, size_bytes, document_class)
                       values (%s::uuid, 'portal', %s, repeat('a', 64), 'application/pdf', 10, 'invoice')
                       returning id""",
                    (client_id, f"{client_id}/raw/test.pdf"),
                ).fetchone()["id"]
            )
    return str(firm_id), client_id, doc_id


def test_restart_persists_chunks_and_usage(ai_owner_dsn, ai_app_dsn):
    _, client_id, doc_id = _seed_client(ai_owner_dsn)
    with psycopg.connect(ai_app_dsn, row_factory=dict_row) as conn:
        with tenant(conn, client_id):
            indexer = PostgresIndexer(conn)
            indexer.upsert(client_id, "memory:1", "memory", "vendor gstin memory", {"gstin": "27AAPFU0939F1ZV"})
            PostgresUsageWriter(conn).log_usage(client_id, doc_id, "gemini-1.5-flash", 10, 5, 1, 0.0, "extract")
    with psycopg.connect(ai_app_dsn, row_factory=dict_row) as conn2:
        with tenant(conn2, client_id):
            chunks = PostgresIndexer(conn2).get_namespace_chunks(client_id)
            assert len(chunks) == 1
            usage_count = conn2.execute(
                "select count(*) from llm_usage_rows where client_id = %s::uuid", (client_id,)
            ).fetchone()["count"]
            assert usage_count == 1
            ranked = Retriever(PostgresIndexer(conn2)).retrieve(client_id, "27AAPFU0939F1ZV", "", [], "", "0", "invoice")
            assert ranked


def test_cross_tenant_denial(ai_owner_dsn, ai_app_dsn):
    _, client_a, doc_a = _seed_client(ai_owner_dsn)
    _, client_b, _ = _seed_client(ai_owner_dsn)
    with psycopg.connect(ai_app_dsn) as conn:
        with tenant(conn, client_a):
            PostgresIndexer(conn).upsert(client_a, "memory:1", "memory", "secret-a", {})
            PostgresUsageWriter(conn).log_usage(client_a, doc_a, "gemini-1.5-flash", 1, 1, 1, 0.0, "extract")
    with psycopg.connect(ai_app_dsn) as conn:
        with tenant(conn, client_b):
            assert PostgresIndexer(conn).get_namespace_chunks(client_b) == []
            count = conn.execute("select count(*) from llm_usage_rows").fetchone()["count"]
            assert count == 0


def test_concurrent_reservations_enforce_cap(ai_owner_dsn, ai_app_dsn):
    _, client_id, doc_id = _seed_client(ai_owner_dsn)

    def attempt():
        with psycopg.connect(ai_app_dsn, row_factory=dict_row) as conn:
            with tenant(conn, client_id):
                switch = PostgresKillSwitch(conn, limit=20, enabled=True)
                try:
                    switch.reserve(client_id, doc_id, 15)
                    return "ok"
                except TokenCapExceededException:
                    return "cap"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count("ok") == 1
    assert results.count("cap") == 1


def test_kill_switch_disabled_skips_reserve(ai_owner_dsn, ai_app_dsn):
    _, client_id, doc_id = _seed_client(ai_owner_dsn)
    with psycopg.connect(ai_app_dsn, row_factory=dict_row) as conn:
        with tenant(conn, client_id):
            switch = PostgresKillSwitch(conn, limit=1, enabled=False)
            reservation = switch.reserve(client_id, doc_id, 10_000)
            switch.reconcile(reservation, 10_000)
            held = conn.execute(
                "select count(*) from llm_token_reservations where state = 'held'"
            ).fetchone()["count"]
            assert held == 0
