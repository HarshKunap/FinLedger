"""Ephemeral Postgres helpers for Person 1 durable-state tests (not hosted apply)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

SQL_PATH = Path(__file__).parent / "sql" / "ai_durable_ephemeral.sql"
APP_PASSWORD = "fl_app_test_pw"


def _pg_runner(data: Path) -> list[str]:
    if os.name == "nt" or os.geteuid() != 0:
        return []
    import pwd

    pg = pwd.getpwnam("postgres")
    os.chown(data, pg.pw_uid, pg.pw_gid)
    return ["runuser", "-u", "postgres", "--"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def ai_owner_dsn():
    if dsn := os.environ.get("FINLEDGER_TEST_OWNER_DSN"):
        yield dsn
        return
    initdb = shutil.which("initdb")
    if not initdb:
        for candidate in (Path("/usr/lib/postgresql/16/bin/initdb"), Path("/usr/lib/postgresql/15/bin/initdb")):
            if candidate.exists():
                initdb = str(candidate)
                break
    if not initdb:
        pytest.skip("no Postgres: set FINLEDGER_TEST_OWNER_DSN or put initdb on PATH")
    bindir = Path(initdb).parent
    data = Path(tempfile.mkdtemp(prefix="flpg_ai_"))
    port = _free_port()
    run_as = _pg_runner(data)
    subprocess.run(
        [*run_as, str(bindir / "initdb"), "-D", str(data), "-U", "postgres", "-A", "trust", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    options = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories={data}"
    subprocess.run(
        [*run_as, str(bindir / "pg_ctl"), "-D", str(data), "-o", options, "-l", str(data / "log.txt"), "-w", "start"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(
            [*run_as, str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(data, ignore_errors=True)


@pytest.fixture(scope="session")
def ai_app_dsn(ai_owner_dsn):
    from finledger_platform.db import migrate

    migrate(ai_owner_dsn)
    overlay = SQL_PATH.read_text(encoding="utf-8")
    with psycopg.connect(ai_owner_dsn, autocommit=True) as owner:
        owner.execute(overlay)
        owner.execute("drop role if exists fl_app")
        owner.execute(f"create role fl_app login password '{APP_PASSWORD}' in role finledger_app")
        owner.execute(
            "grant finledger_worker_extract, finledger_worker_score, finledger_worker_map to fl_app"
        )
    info = conninfo_to_dict(ai_owner_dsn)
    info.update(user="fl_app", password=APP_PASSWORD)
    return make_conninfo(**info)
