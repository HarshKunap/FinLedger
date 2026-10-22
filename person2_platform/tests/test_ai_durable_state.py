"""Migration 012 without the test overlay: rag upserts, usage/reservations under RLS, token functions, automation maker."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib import resources

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from conftest import APP_PASSWORD
from finledger_platform import tenancy
from finledger_platform.db import firm, migrate, tenant


def new_doc(conn, client_id):
    with tenant(conn, client_id):
        return conn.execute(
            """insert into documents (client_id, channel, object_path, content_hash, mime, size_bytes)
               values (%s, 'portal', %s, repeat('a', 64), 'application/pdf', 10) returning id""",
            (client_id, f"{client_id}/raw/{uuid.uuid4()}.pdf")).fetchone()["id"]


def reserve(conn, client_id, doc_id, tokens, cap=100):
    rid = uuid.uuid4()
    with tenant(conn, client_id):
        conn.execute("select llm_reserve_tokens(%s, %s, %s, %s, %s)", (client_id, doc_id, rid, tokens, cap))
    return rid


def test_rag_chunks_upsert_on_client_ref_and_fill_empty_refs(world, conn):
    a = world.a["id"]
    upsert = """insert into rag_chunks (client_id, chunk_type, ref, content) values (%s, 'memory', %s, %s)
                on conflict (client_id, ref) do update set content = excluded.content"""
    with tenant(conn, a):
        conn.execute(upsert, (a, "memory:1", "first"))
        conn.execute(upsert, (a, "memory:1", "second"))
        conn.execute("insert into rag_chunks (client_id, chunk_type, content) values (%s, 'ledger', 'x')", (a,))
        conn.execute("insert into rag_chunks (client_id, chunk_type, content) values (%s, 'ledger', 'y')", (a,))
        rows = conn.execute("select ref, content from rag_chunks order by id").fetchall()
    assert rows[0] == {"ref": "memory:1", "content": "second"}
    assert len(rows) == 3 and all(r["ref"].startswith("auto:") for r in rows[1:])


def test_reservations_enforce_cap_reconcile_and_release(world, conn):
    a = world.a["id"]
    doc = new_doc(conn, a)
    first = reserve(conn, a, doc, 60)
    with pytest.raises(psycopg.Error, match="ai_budget_exceeded"):
        reserve(conn, a, doc, 50)
    with tenant(conn, a):
        conn.execute("select llm_release_tokens(%s)", (first,))
        conn.execute("select llm_release_tokens(%s)", (first,))  # tolerant
    second = reserve(conn, a, doc, 50)
    with tenant(conn, a):
        conn.execute("select llm_reconcile_tokens(%s)", (second,))
        conn.execute("""insert into llm_usage_rows (client_id, document_id, reservation_id, purpose, model,
                        tokens_in, tokens_out, latency_ms) values (%s, %s, %s, 'extract', 'mock', 30, 10, 1)""",
                     (a, doc, second))
        with pytest.raises(psycopg.Error, match="unknown_reservation"):
            with conn.transaction():
                conn.execute("select llm_reconcile_tokens(%s)", (second,))
    reserve(conn, a, doc, 60)  # 40 used + 60 held == cap
    with pytest.raises(psycopg.Error, match="ai_budget_exceeded"):
        reserve(conn, a, doc, 1)


def test_concurrent_reservations_on_one_bill_admit_one(world, conn, pool):
    a = world.a["id"]
    doc = new_doc(conn, a)

    def attempt(_):
        with pool.connection() as c:
            try:
                reserve(c, a, doc, 70)
                return "ok"
            except psycopg.Error as e:
                return "cap" if "ai_budget_exceeded" in str(e) else repr(e)

    with ThreadPoolExecutor(4) as ex:
        results = sorted(ex.map(attempt, range(4)))
    assert results == ["cap", "cap", "cap", "ok"]


def test_cross_tenant_reads_and_calls_are_denied(world, conn):
    a, c = world.a["id"], world.c["id"]
    doc = new_doc(conn, a)
    reserve(conn, a, doc, 5)
    with tenant(conn, a):
        conn.execute("""insert into llm_usage_rows (client_id, document_id, reservation_id, purpose, model,
                        tokens_in, tokens_out, latency_ms) values (%s, %s, %s, 'extract', 'mock', 1, 1, 1)""",
                     (a, doc, uuid.uuid4()))
    with tenant(conn, c):
        assert conn.execute("select count(*) from llm_usage_rows").fetchone()["count"] == 0
        assert conn.execute("select count(*) from llm_token_reservations").fetchone()["count"] == 0
    with pytest.raises(psycopg.Error, match="tenant_mismatch"):
        with tenant(conn, c):
            conn.execute("select llm_reserve_tokens(%s, %s, %s, 1, 100)", (a, doc, uuid.uuid4()))
    with pytest.raises(psycopg.Error, match="tenant_mismatch"):
        with tenant(conn, c):
            conn.execute("select automation_maker_id(%s)", (a,))


def test_app_role_without_worker_membership_has_no_token_path(world, conn, owner, owner_dsn):
    owner.execute("drop role if exists fl_plain")
    owner.execute(f"create role fl_plain login password '{APP_PASSWORD}' in role finledger_app")
    info = conninfo_to_dict(owner_dsn)
    info.update(user="fl_plain", password=APP_PASSWORD)
    a = world.a["id"]
    doc = new_doc(conn, a)
    with psycopg.connect(make_conninfo(**info), row_factory=dict_row, autocommit=True) as plain:
        for sql, args in [("select llm_reserve_tokens(%s, %s, %s, 1, 100)", (a, doc, uuid.uuid4())),
                          ("select automation_maker_id(%s)", (a,)),
                          ("insert into llm_token_reservations (reservation_id, client_id, document_id, estimated_tokens)"
                           " values (%s, %s, %s, 1)", (uuid.uuid4(), a, doc)),
                          ("insert into llm_usage_rows (client_id, document_id, reservation_id, purpose, model, tokens_in,"
                           " tokens_out, latency_ms) values (%s, %s, %s, 'extract', 'm', 1, 1, 1)", (a, doc, uuid.uuid4()))]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with tenant(plain, a):
                    plain.execute(sql, args)
        with tenant(plain, a):
            assert plain.execute("select count(*) from llm_usage_rows").fetchone()["count"] == 0
    owner.execute("drop role fl_plain")


def test_one_non_login_automation_maker_per_firm(world, conn, owner):
    makers = owner.execute("""select firm_id, firm_admin, api_token_hash, auth_subject from users
                              where automation order by email""").fetchall()
    assert {m[0] for m in makers} == {world.firm1, world.firm2} and len(makers) == 2
    assert all(m[1] is False and m[2] is None and m[3] is None for m in makers)
    maker = owner.execute("select id from users where firm_id = %s and automation", (world.firm1,)).fetchone()[0]
    with tenant(conn, world.a["id"]):
        assert conn.execute("select automation_maker_id(%s) as id", (world.a["id"],)).fetchone()["id"] == maker
    for sql in ["update users set firm_admin = true where id = %s",
                "update users set api_token_hash = '\\x01' where id = %s",
                "update users set auth_subject = gen_random_uuid() where id = %s"]:
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(sql, (maker,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        owner.execute("insert into users (firm_id, email, automation) values (%s, 'second@x.invalid', true)",
                      (world.firm1,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="client roles"):
        with firm(conn, world.firm1):
            tenancy.add_member(conn, world.a["id"], maker, "approver")


@pytest.fixture
def scratch_db(owner_dsn):
    name = f"fl_upgrade_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(owner_dsn, autocommit=True) as c:
        c.execute(f"create database {name}")
    info = conninfo_to_dict(owner_dsn)
    info["dbname"] = name
    yield make_conninfo(**info)
    with psycopg.connect(owner_dsn, autocommit=True) as c:
        c.execute(f"drop database {name} with (force)")


def _apply_through_011(dsn):
    files = sorted((f for f in resources.files("finledger_platform.migrations").iterdir()
                    if f.name.endswith(".sql") and f.name < "012"), key=lambda f: f.name)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("create table if not exists schema_migrations (name text primary key, applied_at timestamptz not null default now())")
        for f in files:
            with c.transaction():
                c.execute(f.read_text(encoding="utf-8"))
                c.execute("insert into schema_migrations (name) values (%s) on conflict do nothing", (f.name,))


def _seed_old_rows(dsn, refs):
    with psycopg.connect(dsn, autocommit=True) as c:
        firm_id, _, _ = tenancy.create_firm_with_admin(c, "Legacy Firm", "legacy@test.example")
        client_id = c.execute("insert into clients (firm_id, name, public_token, inbound_slug) values (%s, 'Old', 'tok', 'old') returning id",
                              (firm_id,)).fetchone()[0]
        for ref in refs:
            c.execute("insert into rag_chunks (client_id, chunk_type, ref, content) values (%s, 'ledger', %s, 'x')",
                      (client_id, ref))
    return firm_id


def test_upgrade_backfills_refs_and_maker_for_existing_firms(scratch_db):
    _apply_through_011(scratch_db)
    firm_id = _seed_old_rows(scratch_db, ["", "", "memory:1"])
    assert migrate(scratch_db) == ["012_ai_durable_state.sql"]
    with psycopg.connect(scratch_db) as c:
        refs = sorted(r[0] for r in c.execute("select ref from rag_chunks"))
        assert refs[-1] == "memory:1" and all(r.startswith("legacy:") for r in refs[:2])
        assert c.execute("select count(*) from users where firm_id = %s and automation", (firm_id,)).fetchone()[0] == 1
    assert migrate(scratch_db) == []


def test_upgrade_refuses_duplicate_refs_instead_of_deleting(scratch_db):
    _apply_through_011(scratch_db)
    _seed_old_rows(scratch_db, ["memory:1", "memory:1"])
    with pytest.raises(psycopg.Error, match="duplicate"):
        migrate(scratch_db)
    with psycopg.connect(scratch_db) as c:
        assert c.execute("select count(*) from rag_chunks").fetchone()[0] == 2
        assert c.execute("select count(*) from schema_migrations where name like '012%'").fetchone()[0] == 0
