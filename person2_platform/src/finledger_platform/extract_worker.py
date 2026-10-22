"""extract_worker: claim `extract` -> guards -> Person 1 extraction -> atomic settlement -> `score` job.

Outcomes per job:
    replayed / already past 'received'  -> job done, nothing written ("skipped")
    statement                           -> document 'exception' (statement_not_extracted), no model call
    object key outside {client_id}/     -> document 'exception' (object_key_rejected), storage never read
    other ExtractFailure but transient  -> document 'exception' with the failure category
    provider_transient, store/DB errors -> job retries with backoff, then 'dead' (requeue_dead_job replays it)
    ok                                  -> document 'extracted' + workflow event by the firm's automation
                                           maker + score job carrying the CanonicalInvoice, in one transaction

The model call runs outside any database transaction. Settlement re-checks the lease inside the same
transaction as the status change, so a worker whose lease expired cannot land a late result.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from .db import tenant
from .queue import claim, enqueue, finish
from .worker import load_extract_input

log = logging.getLogger("finledger.extract")

STATEMENT_NOT_EXTRACTED = "statement_not_extracted"
OBJECT_KEY_REJECTED = "object_key_rejected"
PROVIDER_TRANSIENT = "provider_transient"

# extract(client_id=, document_id=, document_class=, markdown=, page_image_paths=, page_image_bytes=)
# returns CanonicalInvoice JSON or raises an exception carrying a safe `category` (extract.failures).
ExtractFn = Callable[..., dict[str, Any]]


class LeaseLost(RuntimeError):
    """Another worker owns the job now; this worker's result must not be settled."""


def _retry_reason(error: Exception) -> str:
    """Only fixed diagnostic categories may reach jobs.last_error or logs."""
    if getattr(error, "category", None) == PROVIDER_TRANSIENT:
        return PROVIDER_TRANSIENT
    if isinstance(error, psycopg.Error):
        return "extract database operation failed"
    if isinstance(error, OSError):
        return "extract storage read failed"
    return "extract processing failed"


def _key_ok(client_id: str, key: Any) -> bool:
    return (isinstance(key, str) and key.startswith(f"{client_id}/") and "\\" not in key
            and ".." not in key.split("/") and "\x00" not in key)


def keys_rejected(job: dict) -> bool:
    """True when the payload names another tenant, another document, or any key outside `{client_id}/`."""
    p, client_id = job["payload"], str(job["client_id"])
    if str(p.get("client_id")) != client_id or str(p.get("document_id")) != str(job["document_id"]):
        return True
    keys = [p.get("raw_path")] + [p.get(k) for k in ("raw_markdown_path", "layout_blocks_path") if p.get(k)]
    pages = p.get("page_image_paths") or []
    if not isinstance(pages, list):
        return True
    return not all(_key_ok(client_id, k) for k in keys + pages)


def _settle(conn: psycopg.Connection, job: dict, to_status: str, reason: str | None,
            invoice: dict[str, Any] | None) -> str:
    """One transaction: compare-and-set the status, audit it, hand off, and finish the job under its lease."""
    client_id, doc_id = job["client_id"], job["document_id"]
    with tenant(conn, client_id):
        moved = conn.execute("""update documents set status = %s, status_reason = %s, updated_at = now()
                                where id = %s and status = 'received' returning id""",
                             (to_status, reason, doc_id)).fetchone()
        if moved:
            maker = conn.execute("select automation_maker_id(%s) as id", (client_id,)).fetchone()["id"]
            conn.execute("""insert into workflow_events (client_id, document_id, from_status, to_status, action,
                                                         actor_id, metadata)
                            values (%s, %s, 'received', %s, %s, %s, %s)""",
                         (client_id, doc_id, to_status, "extracted" if invoice is not None else "extract_exception",
                          maker, Jsonb({"reason": reason} if reason else {})))
            if invoice is not None:
                enqueue(conn, "score", client_id, doc_id,
                        {"document_id": str(doc_id), "client_id": str(client_id), "maker_id": str(maker),
                         "canonical_invoice": invoice})
        state = finish(conn, job["id"], job["claim_token"], ok=True)
        if state is None:
            raise LeaseLost()  # rolls back the status change and the score job
    if not moved:
        return "skipped"
    return to_status if invoice is None else "extracted"


def process_extract(conn: psycopg.Connection, store, extract: ExtractFn, job: dict) -> str:
    client_id, doc_id = str(job["client_id"]), str(job["document_id"])
    with tenant(conn, client_id):
        doc = conn.execute("select status, document_class from documents where id = %s", (doc_id,)).fetchone()
    if doc is None:
        finish(conn, job["id"], job["claim_token"], ok=False, error="document row missing", permanent=True)
        return "dead"
    if doc["status"] != "received":  # replayed job: an earlier attempt already settled this document
        if finish(conn, job["id"], job["claim_token"], ok=True) is None:
            raise LeaseLost()
        return "skipped"

    if doc["document_class"] == "statement":
        return _settle(conn, job, "exception", STATEMENT_NOT_EXTRACTED, None)
    if keys_rejected(job):
        return _settle(conn, job, "exception", OBJECT_KEY_REJECTED, None)

    p = load_extract_input(conn, store, job)
    images = [store.get(k) for k in p["page_image_paths"]] if not p["raw_markdown"].strip() else []
    try:
        invoice = extract(client_id=client_id, document_id=doc_id, document_class=doc["document_class"],
                          markdown=p["raw_markdown"], page_image_paths=p["page_image_paths"],
                          page_image_bytes=images)
    except Exception as e:
        category = getattr(e, "category", None)
        if not isinstance(category, str) or category == PROVIDER_TRANSIENT:
            raise
        return _settle(conn, job, "exception", category, None)
    return _settle(conn, job, "extracted", None, invoice)


def run_once(conn: psycopg.Connection, store, extract: ExtractFn, lease_seconds: int = 300) -> str | None:
    """Keep claim, processing and settlement failures safe for the worker's stderr."""
    try:
        return _run_once(conn, store, extract, lease_seconds)
    except Exception:
        raise RuntimeError("extract worker operation failed") from None


def _run_once(conn: psycopg.Connection, store, extract: ExtractFn, lease_seconds: int) -> str | None:
    job = claim(conn, "extract", lease_seconds)
    if job is None:
        return None
    try:
        return process_extract(conn, store, extract, job)
    except LeaseLost:
        log.warning("extract job %s lost its lease; result discarded", job["id"])
        return "lease_lost"
    except Exception as e:
        reason = _retry_reason(e)
        log.error("extract job %s failed: %s", job["id"], reason)
        return finish(conn, job["id"], job["claim_token"], ok=False, error=reason) or "lease_lost"


def default_extract() -> ExtractFn:
    """Person 1's dispatcher behind ModelRouter, which refuses mock or transport-less providers when hosted."""
    from extract.contract import extract_document
    from extract.extract_worker import ExtractWorker
    from extract.vision_fallback import ExtractionDispatcher
    from usage.usage_writer import UsageWriter

    dispatcher = ExtractionDispatcher(ExtractWorker(UsageWriter()))
    return lambda **kw: extract_document(dispatcher, **kw)


def run_forever(pool, store, extract: ExtractFn, idle_sleep: float = 1.0) -> None:
    log.info("extract worker started")
    while True:
        with pool.connection() as conn:
            did = run_once(conn, store, extract)
        if did is None:
            time.sleep(idle_sleep)
