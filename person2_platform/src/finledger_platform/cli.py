"""finledger-platform migrate | create-firm | serve | worker | extract-worker | outbox-worker | isolation-check | s3-smoke"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from uuid import UUID

import psycopg

from .config import Settings


def app():
    """ASGI factory: uvicorn finledger_platform.cli:app --factory"""
    from .api import create_app
    from .db import make_pool
    from .store import make_store

    s = Settings()
    return create_app(s, make_pool(s.database_url), make_store(s))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="finledger-platform")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations (owner DSN)")
    f = sub.add_parser("create-firm", help="bootstrap a firm and its first admin; prints the API token once")
    f.add_argument("--name", required=True)
    f.add_argument("--admin-email", required=True)
    sv = sub.add_parser("serve", help="run the API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sub.add_parser("worker", help="run the ingest worker")
    sub.add_parser("extract-worker", help="run the extract worker (provider settings come from Person 1's router)")
    mail = sub.add_parser("outbox-worker", help="deliver one client's queued email replies")
    mail.add_argument("--client-id", required=True, type=UUID)
    sub.add_parser("isolation-check", help="rollback-only two-tenant RLS proof (owner DSN; writes nothing)")
    sub.add_parser("s3-smoke", help="put/get/presign/delete one synthetic object in the configured S3 bucket")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.cmd == "migrate":
        from .db import migrate

        s = Settings()
        print("applied:", migrate(s.owner_database_url) or "nothing new")
    elif args.cmd == "create-firm":
        from .tenancy import create_firm_with_admin

        s = Settings()
        with psycopg.connect(s.owner_database_url) as conn:
            firm_id, user_id, token = create_firm_with_admin(conn, args.name, args.admin_email)
        print(f"firm_id={firm_id}\nuser_id={user_id}\napi_token={token}   (shown once)")
    elif args.cmd == "serve":
        import uvicorn

        # proxy_headers: behind a load balancer, request.client.host becomes the real vendor IP for rate limits.
        # Invitation/upload/download URL paths contain credentials. Never write them to access logs.
        uvicorn.run("finledger_platform.cli:app", factory=True, host=args.host, port=args.port,
                    proxy_headers=True, access_log=False)
    elif args.cmd == "worker":
        from .db import make_pool
        from .store import make_store
        from .worker import run_forever

        s = Settings()
        run_forever(make_pool(s.database_url, min_size=1, max_size=2), make_store(s), s)
    elif args.cmd == "extract-worker":
        from .db import make_pool
        from .extract_worker import default_extract, run_forever
        from .store import make_store

        s = Settings()
        run_forever(make_pool(s.database_url, min_size=1, max_size=2), make_store(s), default_extract())
    elif args.cmd == "outbox-worker":
        from .db import make_pool
        from .outbox_worker import ResendSender, run_forever

        sender = ResendSender(os.environ.get("RESEND_API_KEY", ""), os.environ.get("RESEND_FROM_EMAIL", ""))
        s = Settings()
        with make_pool(s.database_url, min_size=1, max_size=2) as pool:
            run_forever(pool, args.client_id, sender)
    elif args.cmd == "isolation-check":
        from .isolation_check import run

        _report(run(os.environ.get("FINLEDGER_OWNER_DATABASE_URL") or sys.exit("set FINLEDGER_OWNER_DATABASE_URL")))
    elif args.cmd == "s3-smoke":
        from .s3_smoke import run
        from .store import S3Store, make_store

        s = Settings()
        store = make_store(s)
        if not isinstance(store, S3Store):
            sys.exit("s3-smoke needs FINLEDGER_STORE=s3")
        _report(run(store, s.s3_endpoint_url))


def _report(rep) -> None:
    for label in rep.passed:
        print(f"PASS  {label}")
    for label in rep.failed:
        print(f"FAIL  {label}")
    sys.exit(0 if rep.ok else 1)


if __name__ == "__main__":
    main()
