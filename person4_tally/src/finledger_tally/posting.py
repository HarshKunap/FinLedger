"""Authorized posting of an approved P3 post job, with no blind resend. OFF unless explicitly enabled.

``post_approved(job, auth, settings)`` is the only path that sends an Import. It takes the P3 post job (never XML).
Its checks run in this order; the first that fails ends the call, and nothing before step 4 touches the network:
1. P3 already recorded an ERP id for exactly this approved attempt: ``already_posted``, no Tally call.
2. Authorization (no I/O): posting enabled; ``settings`` names the paired client, company name AND company GUID,
   and an absolute state folder (the journal must not move with the working directory);
   ``auth`` (the person2/person3 rows) is a real approval record for this job's document and client: a non-superseded
   ``human``/``policy_clear`` approval of this revision (``payment_release`` approves another action), document
   ``approved``/``scheduled``, attempt ``pending`` for this revision and idempotency key, and ``request_hash`` equal
   to P3's hash of this exact job payload (so an edited draft or another job cannot ride an old approval).
3. The document's lock (one post, reconciliation or clearance at a time), then the local journal (journal.py): no
   unresolved send, no offline file, no recorded doubt, no confirmed post (a voucher of the paired company returns
   ``already_posted``), not a revision Tally already rejected. An unreadable journal blocks.
4. (read-only) The company by exact name AND GUID, then its ledger masters (pinned and re-checked).
5. (no I/O) The preview: job schema, voucher type, ledger mappings, amounts, dates. It is built twice; both builds
   must be byte-identical and match their sha256; when the preflight passed ``expected_request_sha256`` the digest
   must equal it (``request_changed``). ``preview_ready`` is journaled.
6. (read-only) The marker lookup, pinned to the same verified company and re-checked right after the read; a voucher
   already carrying the marker blocks the send.
7. The journal is read again and must still allow this send (``journal_changed`` otherwise); the request bytes are
   saved and read back (they must equal the preview and its digest), ``send_started`` is
   fsync'd, and ONE Import of exactly those bytes is sent. Its exact reply is saved with its sha256, parsed
   conservatively, and the marker is looked up again.

Outcomes (``PostOutcome.state``) and what they mean for a resend:
- ``posted``: reply said created AND exactly one voucher of this company carries the marker with this reference and
  amount. ``erp_id`` = that voucher's GUID. A repeat call returns the same id without sending.
- ``tally_rejected``: reply said not created (errors) AND the lookup did not see it. Same revision is never re-sent;
  a corrected revision goes through P3 again.
- ``outcome_uncertain``: anything else after the Import may have left (timeout, closed connection, unknown or partial
  reply, created-but-not-found, rejected-but-found, any failure after the Import may have left). Never re-sent
  automatically. ``reconcile`` re-checks read-only; ``not_found`` is not proof, so only a person can clear it
  (``clear_for_resend``).
- ``validation_rejected`` / ``blocked`` / ``not_authorized`` / ``tally_unavailable`` / ``offline_xml``: nothing sent.

Exactly-once delivery is NOT guaranteed: Tally's XML Import has no idempotency key. What this code guarantees is at
most one Import per call and no automatic resend while any earlier send is in doubt (only a send that provably never
left, a definite rejection of that exact request, a confirmed post or a person settles a send), plus a marker lookup
before every send. A voucher imported by hand from an offline file between that lookup and a later live send can still
duplicate; that is why an issued offline file blocks live posting until a person clears it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .company import current_company
from .identity import (
    _CANONICAL_UUID,
    CompanyNotResolved,
    LookupResult,
    LookupStatus,
    TallyCompany,
    TallyMalformedResponse,
    TallyRequestRejected,
    VoucherRow,
    _decode,
)
from .journal import DocumentBusy, FileJournal, Gate, JournalCorrupt, Phase, gate, private_dir
from .lookup import lookup_document
from .mapping import MappingTable
from .masters import LedgerMasters, fetch_ledger_masters
from .config import AgentSettings
from .transport import ImportOutcomeUnknown, RequestRefused, TallyTransport, TallyUnavailable
from .voucher_xml import DraftRejected, VoucherPreview, preview_purchase_xml

log = logging.getLogger("finledger_tally.posting")

POSTABLE_DOCUMENT_STATUSES = ("approved", "scheduled")  # person3 workflow: POSTED is allowed only from these
POSTING_APPROVAL_KINDS = ("human", "policy_clear")  # P2 approvals.kind that approve the voucher itself
# ('payment_release' approves a payment, never an invoice posting)
_ERROR_TEXT_LIMIT = 1000
# Blocks a person must act on go to the exception tray; transient ones (Tally down, wrong company open, document busy)
# do not.
_REPORTED_BLOCKS = ("reconciliation_required", "marker_already_in_tally", "already_posted_other", "journal_unreadable",
                    "preview_not_deterministic", "request_digest_mismatch")
_UNCERTAIN_PREFIX = "outcome_uncertain (reconciliation required; FinLedger will not resend)"
_SAFE_KIND = re.compile(r"[A-Za-z]{1,40}")  # an exception type name, quoted in a lookup reason before its ':'
_ERP_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-[0-9a-f]{8}")  # voucher GUID


class State:
    NOT_AUTHORIZED = "not_authorized"
    BLOCKED = "blocked"
    VALIDATION_REJECTED = "validation_rejected"
    TALLY_UNAVAILABLE = "tally_unavailable"
    OFFLINE_XML = "offline_xml"
    SEND_NOT_DELIVERED = "send_not_delivered"
    POSTED = "posted"
    ALREADY_POSTED = "already_posted"
    TALLY_REJECTED = "tally_rejected"
    OUTCOME_UNCERTAIN = "outcome_uncertain"
    RECONCILED = "reconciled"


@dataclass(frozen=True)
class PostAuthorization:
    """The platform rows that prove a human (or valid policy) approved THIS job. Read by the caller from P2/P3."""

    client_id: str
    document_status: str  # documents.status
    attempt_result: str  # posting_attempts.result
    attempt_revision: int  # posting_attempts.revision
    attempt_idempotency_key: str  # posting_attempts.idempotency_key
    attempt_request_hash: str  # posting_attempts.request_hash
    attempt_erp_id: str | None = None  # posting_attempts.erp_id, set by P3 erp_result once posted
    approvals: tuple[tuple[str, int], ...] | None = None  # P2 approvals rows (kind, revision); None = not supplied
    document_id: str | None = None  # the document all of these rows belong to; None = not supplied (refused)
    superseded: tuple[tuple[str, int], ...] = ()  # approvals rows P3 marked superseded_at (an edit after approval)

    @classmethod
    def from_rows(cls, client_id: Any, document: Mapping[str, Any], attempt: Mapping[str, Any],
                  approvals: Iterable[Mapping[str, Any]]) -> "PostAuthorization":
        """From P2 rows ``documents`` (id, client_id, status), ``posting_attempts`` (client_id, document_id, result,
        revision, idempotency_key, request_hash, erp_id) and the document's ``approvals`` (client_id, document_id,
        kind, revision, superseded_at), read by the caller under the client's tenant. A superseded approval (P3
        ``edit_field`` after approval) is kept in ``superseded`` and never counts as approval.

        Raises ValueError when a column is missing or the rows do not all belong to one document of one client: a row
        of another document or client is never silently dropped or counted."""
        approvals = list(approvals)
        try:
            doc_ids = {str(document["id"]), str(attempt["document_id"])} | {str(a["document_id"]) for a in approvals}
            clients = ({str(client_id), str(document["client_id"]), str(attempt["client_id"])}
                       | {str(a["client_id"]) for a in approvals})
            fields = dict(document_status=document["status"], attempt_result=attempt["result"],
                          attempt_revision=attempt["revision"], attempt_idempotency_key=attempt["idempotency_key"],
                          attempt_request_hash=attempt["request_hash"], attempt_erp_id=attempt["erp_id"],
                          approvals=tuple((str(a["kind"]), a["revision"]) for a in approvals
                                          if a["superseded_at"] is None),
                          superseded=tuple((str(a["kind"]), a["revision"]) for a in approvals
                                           if a["superseded_at"] is not None))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"authorization rows lack a required column: {exc}") from None
        if len(doc_ids) != 1:
            raise ValueError("document, posting attempt and approvals rows are not for the same document")
        if len(clients) != 1:
            raise ValueError("document, posting attempt and approvals rows are not for the same client")
        return cls(client_id=str(client_id), document_id=doc_ids.pop(), **fields)


@dataclass(frozen=True)
class PostOutcome:
    state: str
    document_id: str | None
    revision: int | None
    detail: str
    sent: bool = False  # True when Import bytes may have reached Tally
    erp_id: str | None = None
    master_id: int | None = None
    alter_id: int | None = None
    request_sha256: str | None = None
    response_sha256: str | None = None
    problems: tuple[tuple[str, str], ...] = ()
    tally_errors: tuple[str, ...] = ()
    artifact: Path | None = None
    # The text erp_result() sends to P3 in place of ``detail`` when ``detail`` quotes Tally's company list, a lookup
    # reason or the journal (any of which can name OTHER companies loaded on this PC). ``detail`` stays local.
    report_detail: str | None = None

    def erp_result(self) -> dict[str, Any] | None:
        """Keyword arguments for person3 ``ControlService.erp_result`` (plus job/ids from the caller), or None.

        None means "do not report": nothing reached Tally and the document should stay approved (posting disabled,
        Tally down, offline file issued, connection refused before sending, a transient unreadable lookup). The error
        text is Tally's own line errors, else ``report_detail`` when set, else ``detail``; it never quotes a lookup
        reason or another company's name. A block that stands on a send in doubt (``send_in_doubt``) carries the same
        stable ``outcome_uncertain`` prefix as the uncertain send itself, so a report that arrives late means the same.
        """
        if self.state in (State.POSTED, State.ALREADY_POSTED, State.RECONCILED):
            return {"ok": True, "erp_id": self.erp_id, "error": None}
        reportable_block = self.state == State.BLOCKED and any(code in _REPORTED_BLOCKS for code, _ in self.problems)
        if self.state in (State.VALIDATION_REJECTED, State.TALLY_REJECTED, State.OUTCOME_UNCERTAIN) or reportable_block:
            if self.tally_errors:
                text = "; ".join(self.tally_errors)
            else:
                text = self.detail if self.report_detail is None else self.report_detail
            prefix = {State.OUTCOME_UNCERTAIN: _UNCERTAIN_PREFIX, State.TALLY_REJECTED: "tally_rejected",
                      State.VALIDATION_REJECTED: "validation_rejected", State.BLOCKED: "blocked"}[self.state]
            if self.state == State.BLOCKED and any(code == "send_in_doubt" for code, _ in self.problems):
                prefix = _UNCERTAIN_PREFIX
            return {"ok": False, "erp_id": None, "error": f"{prefix}: {text}"[:_ERROR_TEXT_LIMIT]}
        return None


# ---------- authorization ----------


def p3_request_hash(job: Mapping[str, Any]) -> str:
    """person3 service._enqueue_post: sha256 of the job payload JSON with sorted keys and compact separators."""
    return hashlib.sha256(json.dumps(job, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_authorization(job: Any, auth: Any, settings: AgentSettings) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    if not isinstance(settings, AgentSettings):
        return [("not_authorized", "settings must be AgentSettings")]
    if settings.posting_enabled is not True:
        problems.append(("posting_disabled", "posting is not enabled on this agent (FINLEDGER_TALLY_POSTING)"))
    if not (settings.client_id and settings.company_name and settings.company_guid):
        problems.append(("not_paired", "client id, company name and company GUID must all be configured"))
    if settings.posting_enabled is True and not Path(settings.state_dir).is_absolute():
        problems.append(("state_dir_not_absolute", "posting is enabled but the state folder is not an absolute path; "
                                                   "the resend journal would depend on the working directory"))
    if not isinstance(auth, PostAuthorization):
        return problems + [("not_authorized", "no approval record was supplied")]
    if not isinstance(job, Mapping):
        return problems + [("invalid_job", "post job must be an object")]
    if auth.document_id is None or job.get("document_id") != auth.document_id:
        problems.append(("document_mismatch", "the approval rows are not for this job's document"))
    if auth.approvals is None or not isinstance(auth.approvals, tuple):
        problems.append(("approval_missing", "no approvals rows were supplied; an attempt row alone is not approval"))
    elif not all(_approval_row(a) for a in auth.approvals):
        problems.append(("approval_missing", "approvals rows are malformed; none is read as an approval"))
    elif not any(kind in POSTING_APPROVAL_KINDS and rev == job.get("revision") for kind, rev in auth.approvals):
        kinds = sorted({k for k, r in auth.approvals if r == job.get("revision")})
        problems.append(("approval_missing", f"no human/policy approval for revision {job.get('revision')!r}"
                                             + (f" (only {kinds})" if kinds else "")))
    if not isinstance(auth.superseded, tuple) or not all(_approval_row(a) for a in auth.superseded):
        problems.append(("approval_superseded", "superseded approvals rows are malformed; whether this revision's "
                                                "approval still stands cannot be told"))
    elif any(rev == job.get("revision") for _, rev in auth.superseded):
        problems.append(("approval_superseded", "the approval for this revision was superseded by a later edit"))
    if auth.document_status not in POSTABLE_DOCUMENT_STATUSES:
        problems.append(("not_approved", f"document status is {auth.document_status!r}, not approved/scheduled"))
    if auth.attempt_result != "pending":
        problems.append(("not_pending", f"posting attempt result is {auth.attempt_result!r}, not 'pending'"))
    if job.get("client_id") != auth.client_id or auth.client_id != settings.client_id:
        problems.append(("client_mismatch", "job, approval and agent are not for the same client"))
    if job.get("revision") != auth.attempt_revision:
        problems.append(("revision_mismatch", "approved revision differs from the job revision"))
    if job.get("idempotency_key") != auth.attempt_idempotency_key:
        problems.append(("idempotency_mismatch", "approved idempotency key differs from the job"))
    try:
        digest = p3_request_hash(job)
    except (TypeError, ValueError, RecursionError):
        digest = None
    if digest is None or digest != auth.attempt_request_hash:
        problems.append(("job_changed", "job payload does not match the approved request_hash"))
    return problems


def _approval_row(row: Any) -> bool:
    """A (kind, revision) pair as ``PostAuthorization.from_rows`` builds it."""
    return isinstance(row, tuple) and len(row) == 2 and isinstance(row[0], str)


def _recorded_erp_id(job: Any, auth: Any, settings: AgentSettings) -> str | None:
    """The erp_id P3 already holds for exactly this approved attempt, else None (and the normal checks decide)."""
    if not isinstance(auth, PostAuthorization) or auth.attempt_result != "posted" or not auth.attempt_erp_id:
        return None
    problems = [c for c, _ in check_authorization(job, auth, settings)]
    # only the "already posted" facts may differ; any other mismatch is not this attempt
    if set(problems) - {"not_pending", "not_approved", "posting_disabled"}:
        return None
    return auth.attempt_erp_id


# ---------- Tally Import reply ----------

_COUNTERS = ("CREATED", "ALTERED", "DELETED", "LASTVCHID", "LASTMID", "COMBINED", "IGNORED", "ERRORS", "CANCELLED",
             "EXCEPTIONS")
_MUST_BE_ZERO = ("ALTERED", "DELETED", "COMBINED", "IGNORED", "ERRORS", "CANCELLED", "EXCEPTIONS")


@dataclass(frozen=True)
class ImportReply:
    kind: str  # created | rejected | unclear
    reason: str
    counters: tuple[tuple[str, int], ...] = ()
    errors: tuple[str, ...] = ()


def parse_import_reply(raw: bytes) -> ImportReply:
    """Classify a Tally Import reply. Only an exact, unambiguous shape is ``created`` or ``rejected``; never raises.

    Shapes accepted (documented by Tally; NOT yet seen from the real TallyPrime 7.1 here): ``<RESPONSE>`` with counter
    children, or ``<ENVELOPE>`` with one ``HEADER/STATUS`` and at most one ``IMPORTRESULT`` (at ``BODY/DATA``) holding
    the counters; ``LINEERROR`` anywhere. ``unclear`` on any doubt: a counter repeated or outside its holder, a count
    that is not plain ASCII digits, two STATUS or IMPORTRESULT elements, errors reported next to ``ERRORS`` 0.
    """
    try:
        root = ET.fromstring(_decode(raw))
    except (ET.ParseError, TallyMalformedResponse, TypeError, ValueError) as exc:
        return ImportReply("unclear", f"reply is not readable XML: {exc}")
    errors = tuple((e.text or "").strip() for e in root.iter("LINEERROR"))
    if root.tag == "RESPONSE":
        holder, status = root, None
    elif root.tag == "ENVELOPE":
        holders, statuses = root.findall("BODY/DATA/IMPORTRESULT"), root.findall("HEADER/STATUS")
        if len(holders) > 1 or len(statuses) > 1 or sum(1 for _ in root.iter("IMPORTRESULT")) != len(holders):
            return ImportReply("unclear", "reply holds more than one STATUS or IMPORTRESULT, or one out of place",
                               errors=errors)
        holder = holders[0] if holders else None
        status = (statuses[0].text or "").strip() if statuses else ""
    else:
        return ImportReply("unclear", f"unexpected reply root {root.tag!r}", errors=errors)
    counters: dict[str, int] = {}
    if holder is not None:
        for name in _COUNTERS:
            found = holder.findall(name)
            if len(found) > 1:
                return ImportReply("unclear", f"{name} appears {len(found)} times", errors=errors)
            if found:
                text = (found[0].text or "").strip()
                if not (text.isascii() and text.isdigit()) or len(text) > 9:  # int() also reads other scripts' digits
                    return ImportReply("unclear", f"{name} {text[:20]!r} is not a plain whole number", errors=errors)
                counters[name] = int(text)
    stray = sum(1 for e in root.iter() if e.tag in _COUNTERS) - len(counters)
    if stray:
        return ImportReply("unclear", f"{stray} counter element(s) outside the import result", errors=errors)
    pairs = tuple(sorted(counters.items()))
    if (not errors and status in (None, "1") and counters.get("CREATED") == 1 and "ERRORS" in counters
            and all(counters.get(n, 0) == 0 for n in _MUST_BE_ZERO)):
        return ImportReply("created", "reply says exactly one voucher was created and nothing else changed", pairs)
    if errors and counters.get("ERRORS") == 0:
        return ImportReply("unclear", "reply reports errors but an ERRORS count of 0", pairs, errors)
    nothing_changed = all(counters.get(n, 0) == 0 for n in ("CREATED", "ALTERED", "DELETED", "COMBINED"))
    said_error = bool(errors) or counters.get("ERRORS", 0) > 0 or counters.get("EXCEPTIONS", 0) > 0
    if said_error and nothing_changed and (counters or status == "0"):
        return ImportReply("rejected", "reply says nothing was created and reports errors", pairs, errors)
    return ImportReply("unclear", "reply is neither a clean create nor a clean rejection", pairs, errors)


# ---------- masters cache (offline XML only) ----------


def save_masters_cache(path: Path, masters: LedgerMasters) -> None:
    """Write the cache owner-only (file 0o600, missing folders 0o700 on POSIX), replacing the previous one whole."""
    data = {"company_name": masters.company_name, "company_guid": masters.company_guid,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "parents": [list(p) for p in masters.parents], "aliases": [list(a) for a in masters.aliases]}
    private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if hasattr(os, "fchmod"):  # a .tmp left by an older version keeps its mode; owner-only from here on
            os.fchmod(fh.fileno(), 0o600)
        fh.write(json.dumps(data, sort_keys=True, indent=1))
    tmp.replace(path)


def load_masters_cache(path: Path, company: TallyCompany) -> tuple[LedgerMasters, str] | None:
    """Cached masters for exactly this company, with their fetch time; None when absent or for another company."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("company_guid") != company.guid or data.get("company_name") != company.name:
        return None
    parents = tuple((str(a), str(b)) for a, b in data.get("parents", []))
    if not parents:
        return None
    aliases = tuple((str(a), str(b)) for a, b in data.get("aliases", []))
    masters = LedgerMasters(company.guid, frozenset(n for n, _ in parents), parents, company.name, aliases)
    return masters, str(data.get("fetched_at"))


# ---------- the post operation ----------


def _job_ids(job: Any) -> tuple[str | None, int | None, str | None]:
    if not isinstance(job, Mapping):
        return None, None, None
    doc, rev, key = job.get("document_id"), job.get("revision"), job.get("idempotency_key")
    doc = doc if isinstance(doc, str) and _CANONICAL_UUID.fullmatch(doc) else None
    rev = rev if isinstance(rev, int) and not isinstance(rev, bool) and rev > 0 else None
    return doc, rev, key if isinstance(key, str) else None


def _draft_date(job: Mapping[str, Any]) -> date | None:
    try:
        return date.fromisoformat(job["voucher_draft"]["date"])
    except (KeyError, TypeError, ValueError):
        return None


def _matches(row: VoucherRow, reference: str | None, party_amount: str | None) -> bool:
    """The found voucher is the one we sent: same REFERENCE and the same absolute AMOUNT (real export: party total)."""
    if reference is None or party_amount is None or row.reference != reference or row.amount is None:
        return False
    try:
        return row.amount.copy_abs() == Decimal(party_amount)  # abs() would round to the 28-digit context
    except (InvalidOperation, ValueError, TypeError):  # an unreadable recorded amount never matches
        return False


def _lookup_summary(result: LookupResult) -> str:
    """A lookup outcome as text that may leave this PC: its status, the kind of failure (a fixed phrase or an exception
    type name) and a voucher count. Never the reason itself, which can quote Tally's company list (other clients)."""
    text = result.status.value
    if result.status is LookupStatus.UNREADABLE:
        head = result.reason.split(":", 1)[0] if isinstance(result.reason, str) else ""
        if head == "company not verified" or _SAFE_KIND.fullmatch(head):
            text += f" ({head})"
    if result.rows:
        text += f", {len(result.rows)} voucher(s)"
    return text


def _blocked_summary(current: Gate, events: list) -> str:
    """A journal block as text that may leave this PC: counts and the last phase, never a recorded detail."""
    last = events[-1]
    return (f"the journal holds {len(current.unresolved)} unresolved send(s) or offline file(s); last recorded event "
            f"r{last.revision} {last.phase}; reconcile, or a person must clear it")


def _may_send(current: Gate, rev: int) -> bool:
    """What the journal must say for a send of revision ``rev``: clear, or only an OLDER revision rejected."""
    return current.status == "clear" or (current.status == "rejected" and current.rejected.revision < rev)


def _erp_company(erp_id: Any) -> str | None:
    """The company GUID an ERP id (voucher GUID ``<company GUID>-<8 hex>``) belongs to, or None."""
    match = _ERP_ID.fullmatch(erp_id) if isinstance(erp_id, str) else None
    return match.group(1) if match else None


def post_approved(job: Mapping[str, Any], auth: PostAuthorization, settings: AgentSettings, *,
                  transport: TallyTransport | None = None, mapping: MappingTable | None = None,
                  expected_total: Decimal | None = None, expected_request_sha256: str | None = None) -> PostOutcome:
    """Post one approved purchase job to the paired Tally company. Sends at most one Import per call and never
    resends after doubt; it does not (cannot) guarantee exactly-once delivery. Checks and their order: module doc.

    ``expected_request_sha256`` (the live-test preflight): when given, the preview built now must have exactly this
    sha256, or the call returns ``blocked`` (``request_changed``) before any ``send_started`` record or Import.
    None (the default) changes nothing. The document lock is held for the whole call; a second caller on the same
    state folder gets ``blocked`` (``document_busy``, not reported) and sends nothing."""
    doc, rev, key = _job_ids(job)
    recorded = _recorded_erp_id(job, auth, settings)
    if recorded is not None:  # ARCHITECTURE.md: PostingJob already has an erp_id -> return it, never import again
        return PostOutcome(State.ALREADY_POSTED, doc, rev, "P3 already recorded this attempt as posted; not sent",
                           erp_id=recorded)
    problems = check_authorization(job, auth, settings)
    if problems or doc is None or rev is None or key is None:
        problems = problems or [("invalid_job", "job lacks a canonical document_id, revision or idempotency key")]
        log.warning("post refused before any Tally call: %s", [c for c, _ in problems])
        return PostOutcome(State.NOT_AUTHORIZED, doc, rev, "; ".join(m for _, m in problems), problems=tuple(problems))

    journal = FileJournal(settings.journal_dir)
    try:
        held = journal.lock(doc)
    except DocumentBusy as exc:
        return PostOutcome(State.BLOCKED, doc, rev, str(exc), problems=(("document_busy", str(exc)),))
    except OSError as exc:
        return _not_journaled(doc, rev, exc, "the document lock")
    with held:
        return _post_locked(job, auth, settings, journal, doc, rev, key, transport, mapping, expected_total,
                            expected_request_sha256)


def _build_preview(job, company, settings, masters, expected_total, mapping) -> VoucherPreview:
    return preview_purchase_xml(job, company=company, expected_company_name=settings.company_name, masters=masters,
                                expected_total=expected_total, mapping=mapping)


def _unreproducible(preview: VoucherPreview, rebuild: Callable[[], VoucherPreview]) -> str | None:
    """Why ``preview`` cannot be trusted as the exact bytes to send, or None. The bytes approved for sending must be
    a pure function of the job and the verified masters, and must match the digest the journal records."""
    if hashlib.sha256(preview.xml).hexdigest() != preview.sha256:
        return "the preview's sha256 does not match its bytes"
    try:
        again = rebuild()
    except DraftRejected:
        return "a second build of the same job was refused"
    if (again.xml, again.sha256) != (preview.xml, preview.sha256):
        return "a second build of the same job produced different bytes"
    return None


def _post_locked(job, auth, settings, journal, doc, rev, key, transport, mapping, expected_total,
                 expected_sha) -> PostOutcome:
    job_hash = auth.attempt_request_hash
    base = {"revision": rev, "idempotency_key": key, "job_hash": job_hash}
    try:
        events = journal.events(doc)
        current = gate(events)
    except JournalCorrupt as exc:
        return PostOutcome(State.BLOCKED, doc, rev, str(exc), problems=(("journal_unreadable", str(exc)),))
    if current.status == "posted":
        ev = current.posted
        if _erp_company(ev.erp_id) != settings.company_guid:
            detail = (f"the journal records this document as posted as {ev.erp_id!r}, which is not a voucher of the "
                      "paired company; not posting again")
            return PostOutcome(State.BLOCKED, doc, rev, detail, problems=(("already_posted_other", detail),))
        if ev.revision == rev and ev.job_hash == job_hash:
            return PostOutcome(State.ALREADY_POSTED, doc, rev, current.reason, erp_id=ev.erp_id,
                               master_id=ev.master_id, alter_id=ev.alter_id, request_sha256=ev.request_sha256)
        return PostOutcome(State.BLOCKED, doc, rev, f"document already posted under another revision or payload "
                           f"({current.reason}); not posting again",
                           problems=(("already_posted_other", current.reason),))
    if current.status == "blocked":
        problems = (("reconciliation_required", current.reason),)
        if any(Phase(e.phase) is Phase.SEND_STARTED for e in current.unresolved):  # an Import may have reached Tally
            problems += (("send_in_doubt", "an earlier Import of this document may have reached Tally"),)
        return PostOutcome(State.BLOCKED, doc, rev, current.reason, problems=problems,
                           report_detail=_blocked_summary(current, events))
    if current.status == "rejected" and current.rejected.revision >= rev:
        ev = current.rejected
        return PostOutcome(State.TALLY_REJECTED, doc, rev, current.reason, tally_errors=(ev.detail,),
                           request_sha256=ev.request_sha256)

    transport = transport or settings.transport()
    expected = TallyCompany(settings.company_name, settings.company_guid)

    # ---- read-only checks: company, masters, preview ----
    try:
        company = current_company(transport, settings.company_name, settings.company_guid)
        masters = fetch_ledger_masters(settings.company_name, expected_guid=settings.company_guid, transport=transport)
    except TallyUnavailable as exc:
        if settings.offline_xml_enabled:
            return _offline(job, expected, settings, journal, base, mapping, expected_total, str(exc))
        return PostOutcome(State.TALLY_UNAVAILABLE, doc, rev, f"Tally unavailable; nothing sent: {exc}")
    except (CompanyNotResolved, TallyMalformedResponse, TallyRequestRejected, RequestRefused) as exc:
        return PostOutcome(State.BLOCKED, doc, rev, f"company or masters not verified; nothing sent: {exc}",
                           problems=(("company_unverified", str(exc)),),
                           report_detail=f"company or masters not verified ({type(exc).__name__}); nothing sent")
    try:
        save_masters_cache(settings.masters_cache, masters)
    except OSError as exc:  # the cache only feeds offline XML; an older cache keeps its own fetch time
        log.warning("masters cache not saved (%s); continuing", type(exc).__name__)
    try:
        preview = _build_preview(job, company, settings, masters, expected_total, mapping)
    except DraftRejected as exc:
        problems = exc.reasons
        try:
            journal.append(doc, Phase.VALIDATION_REJECTED, **base, detail=str(exc)[:_ERROR_TEXT_LIMIT])
        except (OSError, JournalCorrupt) as write_exc:
            problems = problems + (("journal_write_failed", str(write_exc)),)
        return PostOutcome(State.VALIDATION_REJECTED, doc, rev, str(exc), problems=problems)
    why = _unreproducible(preview, lambda: _build_preview(job, company, settings, masters, expected_total, mapping))
    if why:
        log.error("preview not reproducible; nothing sent: document=%s", doc)
        return PostOutcome(State.BLOCKED, doc, rev, f"{why}; nothing sent",
                           problems=(("preview_not_deterministic", why),))
    if expected_sha is not None and preview.sha256 != expected_sha:
        detail = "the request built now is not the one the preflight checked (sha256 differs); nothing sent"
        return PostOutcome(State.BLOCKED, doc, rev, detail, problems=(("request_changed", detail),),
                           request_sha256=preview.sha256)
    try:
        journal.append(doc, Phase.PREVIEW_READY, **base, request_sha256=preview.sha256)
    except (OSError, JournalCorrupt) as exc:
        return _not_journaled(doc, rev, exc)

    # ---- the marker must not already be in Tally; the company is re-checked right after this read ----
    before, checked = lookup_document(transport, settings.company_name, doc, _draft_date(job), settings.company_guid)
    if before.status in (LookupStatus.FOUND, LookupStatus.AMBIGUOUS):
        detail = f"before sending: {before.reason}; a voucher already carries this document's marker"
        problems = (("marker_already_in_tally", detail),)
        try:
            journal.append(doc, Phase.RECONCILIATION_REQUIRED, **base, request_sha256=preview.sha256, detail=detail,
                           erp_id=before.row.guid if before.row else None)
        except (OSError, JournalCorrupt) as exc:  # nothing is sent either way; the marker lookup repeats next time
            problems = problems + (("journal_write_failed", str(exc)),)
        return PostOutcome(State.BLOCKED, doc, rev, detail, problems=problems,
                           report_detail=f"before sending: lookup {_lookup_summary(before)}; a voucher already carries "
                                         "this document's marker")
    if before.status is not LookupStatus.NOT_FOUND:
        return PostOutcome(State.BLOCKED, doc, rev, f"pre-send lookup unreadable; nothing sent: {before.reason}",
                           report_detail=f"pre-send lookup {_lookup_summary(before)}; nothing sent")
    if checked != company:
        detail = "the company checked by the pre-send lookup is not the one the preview was built for; nothing sent"
        return PostOutcome(State.BLOCKED, doc, rev, detail, problems=(("company_changed", detail),))

    # ---- send once: exactly the saved, digested bytes, and only while the journal still allows it ----
    try:
        now = journal.events(doc)
    except JournalCorrupt as exc:
        return PostOutcome(State.BLOCKED, doc, rev, f"{exc}; nothing sent",
                           problems=(("journal_unreadable", str(exc)),))
    except OSError as exc:
        return _not_journaled(doc, rev, exc, "send_started")
    again = gate(now)
    if not _may_send(again, rev):  # only a writer that ignores the document lock can change it; never send past it
        detail = f"the journal changed during the pre-send checks ({again.status}: {again.reason}); nothing sent"
        log.error("journal changed before sending; nothing sent: document=%s", doc)
        return PostOutcome(State.BLOCKED, doc, rev, detail, problems=(("journal_changed", detail),),
                           report_detail=f"the journal changed during the pre-send checks ({again.status}); nothing "
                                         "sent")
    try:
        seq = len(now) + 1
        path, saved_sha = journal.save_bytes(doc, f"{seq:04d}_request.xml", preview.xml)
        if saved_sha != preview.sha256 or path.read_bytes() != preview.xml:
            detail = "the saved request bytes do not read back as the preview and its digest; nothing sent"
            return PostOutcome(State.BLOCKED, doc, rev, detail, problems=(("request_digest_mismatch", detail),))
        journal.append(doc, Phase.SEND_STARTED, **base, request_sha256=preview.sha256, reference=preview.reference,
                       party_amount=f"{preview.party_amount:.2f}")
    except (OSError, JournalCorrupt) as exc:
        return _not_journaled(doc, rev, exc, "send_started")
    log.info("import sending: document=%s revision=%s request_sha256=%s", doc, rev, preview.sha256)
    try:
        raw = transport.import_vouchers(preview.xml)
    except (TallyUnavailable, RequestRefused) as exc:
        try:
            journal.append(doc, Phase.SEND_NOT_DELIVERED, **base, request_sha256=preview.sha256, detail=str(exc))
        except (OSError, JournalCorrupt) as write_exc:  # send_started stays the last word: blocked until cleared
            log.error("journal write failed after an undelivered send: document=%s", doc)
            return PostOutcome(State.BLOCKED, doc, rev, f"Import never left this machine ({exc}), but the journal "
                               f"could not record that ({write_exc}); the document stays blocked until a person "
                               "clears it", problems=(("journal_write_failed", str(write_exc)),),
                               request_sha256=preview.sha256)
        return PostOutcome(State.SEND_NOT_DELIVERED, doc, rev, f"Import never left this machine: {exc}",
                           request_sha256=preview.sha256)
    except Exception as exc:  # noqa: BLE001 - ImportOutcomeUnknown, or anything once the Import may have left
        why = str(exc) if isinstance(exc, ImportOutcomeUnknown) else f"{type(exc).__name__} while sending the Import"
        try:
            journal.append(doc, Phase.OUTCOME_UNCERTAIN, **base, request_sha256=preview.sha256,
                           detail=why[:_ERROR_TEXT_LIMIT])
            return _settle_after_unknown(transport, settings, journal, doc, rev, base, preview, _draft_date(job), why)
        except (OSError, JournalCorrupt) as write_exc:
            return _unrecorded(doc, rev, preview, f"{why}; then {write_exc}")
        except Exception as later:  # noqa: BLE001
            return _uncertain_after_send(journal, doc, rev, base, preview, later)
    try:
        return _after_reply(raw, transport, settings, journal, doc, rev, base, preview, _draft_date(job))
    except Exception as exc:  # noqa: BLE001 - after the Import may have left, every failure is uncertain
        return _uncertain_after_send(journal, doc, rev, base, preview, exc)


def _not_journaled(doc, rev, exc, what="the pre-send record") -> PostOutcome:
    """A journal write before ``send_started`` failed: nothing was sent, nothing is reported."""
    log.error("journal write failed before sending; nothing sent: document=%s", doc)
    return PostOutcome(State.BLOCKED, doc, rev, f"could not record {what} durably; nothing sent: {exc}",
                       problems=(("journal_write_failed", str(exc)),))


def _unrecorded(doc, rev, preview, why) -> PostOutcome:
    """The Import was sent but its outcome could not be established and recorded. send_started is durable, so the
    gate stays blocked for this document; the result is never reported as posted."""
    log.error("outcome not recorded after sending: document=%s revision=%s", doc, rev)
    return PostOutcome(State.OUTCOME_UNCERTAIN, doc, rev, f"Import sent, but the outcome could not be established and "
                       f"recorded ({why}); reconciliation required", sent=True, request_sha256=preview.sha256)


def _uncertain_after_send(journal, doc, rev, base, preview, exc) -> PostOutcome:
    """Anything that fails after the Import may have left. A disk or journal fault is left as it is (send_started is
    durable and keeps the document blocked); any other fault is recorded as outcome_uncertain when the journal still
    accepts writes. Only the exception type is logged or recorded for an unexpected fault."""
    if isinstance(exc, (OSError, JournalCorrupt)):
        return _unrecorded(doc, rev, preview, str(exc))
    log.error("unexpected %s after sending: document=%s", type(exc).__name__, doc)
    why = f"{type(exc).__name__} after the Import was sent"
    try:
        journal.append(doc, Phase.OUTCOME_UNCERTAIN, **base, request_sha256=preview.sha256, detail=why)
    except (OSError, JournalCorrupt):
        pass
    return _unrecorded(doc, rev, preview, why)


def _after_reply(raw, transport, settings, journal, doc, rev, base, preview, around) -> PostOutcome:
    _, response_sha = journal.save_bytes(doc, f"{journal.next_seq(doc):04d}_response.xml", raw)
    journal.append(doc, Phase.RESPONSE_RECEIVED, **base, request_sha256=preview.sha256, response_sha256=response_sha)
    reply = parse_import_reply(raw)
    after, _ = lookup_document(transport, settings.company_name, doc, around, settings.company_guid)
    common = dict(request_sha256=preview.sha256, response_sha256=response_sha)

    if reply.kind == "created" and after.status is LookupStatus.FOUND and _matches(
            after.row, preview.reference, f"{preview.party_amount:.2f}"):
        row = after.row
        journal.append(doc, Phase.POSTED, **base, **common, erp_id=row.guid, master_id=row.master_id,
                       alter_id=row.alter_id, detail=reply.reason)
        log.info("posted: document=%s revision=%s erp_id=%s", doc, rev, row.guid)
        return PostOutcome(State.POSTED, doc, rev, f"{reply.reason}; {after.reason}", sent=True, erp_id=row.guid,
                           master_id=row.master_id, alter_id=row.alter_id, **common)
    if reply.kind == "rejected" and after.status is LookupStatus.NOT_FOUND:
        detail = "; ".join(reply.errors) or reply.reason
        journal.append(doc, Phase.TALLY_REJECTED, **base, **common, detail=detail[:_ERROR_TEXT_LIMIT])
        log.info("tally rejected: document=%s revision=%s", doc, rev)
        return PostOutcome(State.TALLY_REJECTED, doc, rev, reply.reason, sent=True, tally_errors=reply.errors,
                           **common)
    detail = f"reply {reply.kind} ({reply.reason}); lookup {after.status.value} ({after.reason})"
    journal.append(doc, Phase.OUTCOME_UNCERTAIN, **base, **common, detail=detail[:_ERROR_TEXT_LIMIT])
    log.warning("outcome uncertain: document=%s revision=%s", doc, rev)
    return PostOutcome(State.OUTCOME_UNCERTAIN, doc, rev, detail, sent=True, tally_errors=reply.errors, **common,
                       report_detail=f"reply {reply.kind} ({reply.reason}); lookup {_lookup_summary(after)}")


def _settle_after_unknown(transport, settings, journal, doc, rev, base, preview, around, why) -> PostOutcome:
    """One read-only lookup straight after an unknown Import outcome. Never sends."""
    found, _ = lookup_document(transport, settings.company_name, doc, around, settings.company_guid)
    if found.status is LookupStatus.FOUND and _matches(found.row, preview.reference, f"{preview.party_amount:.2f}"):
        row = found.row
        journal.append(doc, Phase.RECONCILED, **base, request_sha256=preview.sha256, erp_id=row.guid,
                       master_id=row.master_id, alter_id=row.alter_id, detail=f"after unknown outcome: {found.reason}")
        return PostOutcome(State.RECONCILED, doc, rev, f"{why}; then {found.reason}", sent=True, erp_id=row.guid,
                           master_id=row.master_id, alter_id=row.alter_id, request_sha256=preview.sha256)
    return PostOutcome(State.OUTCOME_UNCERTAIN, doc, rev, f"{why}; lookup {found.status.value}: {found.reason}",
                       sent=True, request_sha256=preview.sha256,
                       report_detail=f"{why}; lookup {_lookup_summary(found)}")


def _offline(job, company, settings, journal, base, mapping, expected_total, why) -> PostOutcome:
    doc, rev = job["document_id"], job["revision"]
    cached = load_masters_cache(settings.masters_cache, company)
    if cached is None:
        return PostOutcome(State.TALLY_UNAVAILABLE, doc, rev,
                           f"Tally unavailable and no cached masters for the paired company; no offline XML: {why}")
    masters, fetched_at = cached
    try:
        preview = preview_purchase_xml(job, company=company, expected_company_name=settings.company_name,
                                       masters=masters, expected_total=expected_total, mapping=mapping)
    except DraftRejected as exc:
        problems = exc.reasons
        try:
            journal.append(doc, Phase.VALIDATION_REJECTED, **base, detail=str(exc)[:_ERROR_TEXT_LIMIT])
        except (OSError, JournalCorrupt) as write_exc:
            problems = problems + (("journal_write_failed", str(write_exc)),)
        return PostOutcome(State.VALIDATION_REJECTED, doc, rev, str(exc), problems=problems)
    name = offline_xml_name(preview)
    try:  # block live posting FIRST: a file that exists must never coexist with an unblocked journal
        journal.append(doc, Phase.OFFLINE_ARTIFACT_ISSUED, **base, request_sha256=preview.sha256,
                       reference=preview.reference, party_amount=f"{preview.party_amount:.2f}",
                       detail=f"offline XML {name}; ledgers checked against masters cached {fetched_at}")
    except (OSError, JournalCorrupt) as exc:
        return PostOutcome(State.BLOCKED, doc, rev, f"could not record the offline artifact; none written: {exc}",
                           problems=(("journal_write_failed", str(exc)),))
    try:
        path = write_offline_xml(settings.offline_dir, preview, masters_fetched_at=fetched_at)
    except OSError as exc:
        return PostOutcome(State.BLOCKED, doc, rev, f"offline XML could not be written ({exc}); live posting for this "
                           "document stays blocked until a person checks and clears it",
                           problems=(("artifact_write_failed", str(exc)),), request_sha256=preview.sha256)
    return PostOutcome(State.OFFLINE_XML, doc, rev, f"Tally unavailable ({why}); manual-import XML written, NOT "
                       "imported; live posting for this document is blocked until a person clears it",
                       request_sha256=preview.sha256, artifact=path)


def offline_xml_name(preview: VoucherPreview) -> str:
    return f"r{preview.revision}_{preview.sha256[:12]}.xml"


def write_offline_xml(folder: Path, preview: VoucherPreview, *, masters_fetched_at: str) -> Path:
    """Write the preview bytes for a CA to import by hand, next to a metadata file. Never overwrites.

    The metadata holds identifiers and digests only (no party, amount or narration). Neither file is, or says it is,
    an imported or posted voucher. Both files are created owner-only (0o600) and missing folders 0o700 on POSIX.
    """
    folder = private_dir(Path(folder) / preview.document_id)
    xml_path = folder / offline_xml_name(preview)
    stem = xml_path.stem
    meta = {"status": "NOT IMPORTED: manual-import file generated while Tally was unavailable",
            "document_id": preview.document_id, "revision": preview.revision,
            "idempotency_key": preview.idempotency_key, "sha256": preview.sha256,
            "company_guid": preview.company_guid, "masters_fetched_at": masters_fetched_at,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "warning": "Import at most once. Tell FinLedger after importing; live posting stays blocked until then."}
    if xml_path.exists():
        if xml_path.read_bytes() != preview.xml:
            raise FileExistsError(f"{xml_path} exists with different bytes")
        return xml_path
    with os.fdopen(_create_private(xml_path), "wb") as fh:
        fh.write(preview.xml)
    with os.fdopen(_create_private(folder / f"{stem}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, sort_keys=True, indent=1)
    return xml_path


def _create_private(path: Path) -> int:
    """Exclusive create, owner read/write only (0o600 before the umask); FileExistsError if ``path`` exists."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)


# ---------- reconciliation and human clearance ----------


def reconcile(document_id: str, around: date, settings: AgentSettings, *,
              transport: TallyTransport | None = None) -> PostOutcome:
    """Read-only: try to identify the voucher of a document's one unresolved send (or offline file).

    One marker lookup, pinned to the paired company's name AND GUID and re-checked after the read; never an Import,
    never a retry. ``reconciled`` is recorded only when the journal holds exactly one unresolved send or offline file
    and exactly one voucher of that company carries the document's marker with that send's reference and amount; the
    record carries that send's revision, idempotency key, job hash and digest, and is written only when no later
    revision has been journaled since (otherwise ``outcome_uncertain``, ``revision_changed``: the voucher of an earlier
    revision is never recorded as a later revision's posting). ``not_found`` (never proof), ``ambiguous`` and
    ``unreadable`` leave the document blocked. Text for P3 (``erp_result``) never quotes the lookup reason. A block
    with no FinLedger send behind it (a marker already in Tally before sending) is never settled here: a person
    decides. ``around`` only fills the request's date window, which Tally does not honour.
    """
    if not isinstance(settings, AgentSettings) or not (settings.company_name and settings.company_guid):
        detail = "company name and GUID must be configured; a voucher is never identified by company name alone"
        return PostOutcome(State.BLOCKED, document_id, None, detail, problems=(("not_paired", detail),))
    if not isinstance(around, date):
        detail = "around must be a date (the draft date); nothing was read"
        return PostOutcome(State.BLOCKED, document_id, None, detail, problems=(("invalid_date", detail),))
    journal = FileJournal(settings.journal_dir)
    try:
        held = journal.lock(document_id)
    except DocumentBusy as exc:
        return PostOutcome(State.BLOCKED, document_id, None, str(exc), problems=(("document_busy", str(exc)),))
    except OSError as exc:
        return PostOutcome(State.BLOCKED, document_id, None, f"could not take the document lock; nothing read: {exc}",
                           problems=(("journal_write_failed", str(exc)),))
    with held:
        return _reconcile(journal, document_id, around, settings, transport)


def _reconcile(journal, document_id, around, settings, transport) -> PostOutcome:
    try:
        events = journal.events(document_id)
    except JournalCorrupt as exc:
        return PostOutcome(State.BLOCKED, document_id, None, str(exc), problems=(("journal_unreadable", str(exc)),))
    current = gate(events)
    if current.status == "posted":
        ev = current.posted
        if _erp_company(ev.erp_id) != settings.company_guid:
            detail = f"the journal records {ev.erp_id!r}, which is not a voucher of the paired company"
            return PostOutcome(State.BLOCKED, document_id, ev.revision, detail,
                               problems=(("already_posted_other", detail),))
        return PostOutcome(State.ALREADY_POSTED, document_id, ev.revision, current.reason, erp_id=ev.erp_id,
                           master_id=ev.master_id, alter_id=ev.alter_id)
    if current.status != "blocked":
        return PostOutcome(State.BLOCKED, document_id, None, f"nothing to reconcile ({current.reason})")
    # A send a person cleared may still have reached Tally; it is a candidate only when nothing else is unresolved.
    candidates = current.unresolved or current.cleared
    if len(candidates) != 1:
        detail = (f"no FinLedger send or offline file is unresolved ({current.reason}); a person must decide"
                  if not candidates else
                  f"{len(candidates)} sends are unresolved; a person must decide which voucher is which")
        return PostOutcome(State.OUTCOME_UNCERTAIN, document_id, events[-1].revision, detail,
                           problems=(("unresolved", detail),), report_detail=_blocked_summary(current, events))
    expected = candidates[0]
    result, _ = lookup_document(transport or settings.transport(), settings.company_name, document_id, around,
                                settings.company_guid)
    base = {"revision": expected.revision, "idempotency_key": expected.idempotency_key, "job_hash": expected.job_hash}
    if result.status is LookupStatus.FOUND and _matches(result.row, expected.reference, expected.party_amount):
        row = result.row
        newest = events[-1].revision
        if newest != expected.revision:  # the voucher may be that send's or a later revision's: a person decides
            detail = (f"voucher {row.guid} matches the r{expected.revision} send, but the document has moved on to "
                      f"r{newest}; it is not recorded as the posting of either revision; a person must decide")
            return PostOutcome(State.OUTCOME_UNCERTAIN, document_id, newest, detail,
                               problems=(("revision_changed", detail),))
        try:
            journal.append(document_id, Phase.RECONCILED, **base, request_sha256=expected.request_sha256,
                           erp_id=row.guid, master_id=row.master_id, alter_id=row.alter_id, detail=result.reason)
        except (OSError, JournalCorrupt) as exc:  # found but not recorded: stays blocked, never reported as posted
            detail = f"{result.reason}; but the journal could not record it ({exc}); run reconcile again"
            return PostOutcome(State.OUTCOME_UNCERTAIN, document_id, expected.revision, detail,
                               problems=(("journal_write_failed", str(exc)),),
                               report_detail=f"lookup found; but the journal could not record it "
                                             f"({type(exc).__name__}); run reconcile again")
        return PostOutcome(State.RECONCILED, document_id, expected.revision, result.reason, erp_id=row.guid,
                           master_id=row.master_id, alter_id=row.alter_id, request_sha256=expected.request_sha256)
    if result.status is LookupStatus.FOUND:
        detail = (f"a voucher carries the marker but does not match the recorded send ({result.reason}); "
                  "a person must decide")
    elif result.status is LookupStatus.NOT_FOUND:
        detail = f"{result.reason}; still unresolved: not_found is not proof that nothing was posted"
    else:
        detail = f"lookup {result.status.value}: {result.reason}"
    return PostOutcome(State.OUTCOME_UNCERTAIN, document_id, expected.revision, detail,
                       problems=(("unresolved", detail),),
                       report_detail=f"lookup {_lookup_summary(result)}; still unresolved (not_found is not proof); "
                                     "a person must decide")


def clear_for_resend(document_id: str, settings: AgentSettings, *, actor: str, note: str) -> PostOutcome:
    """A person attests that a blocked (uncertain / offline) attempt left no voucher in Tally. Never sends.

    Raises ``ValueError`` (bad input, or the document is not blocked), ``JournalCorrupt`` (unreadable journal),
    ``DocumentBusy`` (a post or reconciliation of it is running) or ``OSError`` (not recorded); in every case nothing
    is cleared. The next ``post_approved`` still runs the pre-send marker lookup.
    """
    if not isinstance(actor, str) or not actor.strip() or not isinstance(note, str) or len(note.strip()) < 10:
        raise ValueError("actor and a note of at least 10 characters are required")
    journal = FileJournal(settings.journal_dir)
    with journal.lock(document_id):
        events = journal.events(document_id)
        current = gate(events)
        if current.status != "blocked":
            raise ValueError(f"document is not blocked ({current.status}: {current.reason})")
        last = events[-1]
        journal.append(document_id, Phase.HUMAN_CLEARED, revision=last.revision, idempotency_key=last.idempotency_key,
                       job_hash=last.job_hash, actor=actor.strip(), detail=note.strip()[:_ERROR_TEXT_LIMIT])
    return PostOutcome(State.BLOCKED, document_id, last.revision,
                       "cleared by a person; a new post still re-checks Tally")
