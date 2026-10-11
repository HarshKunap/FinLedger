"""Preflight and gated execution for the ONE controlled live Import test (docs/LIVE_IMPORT_TEST_PROCEDURE.md).

Nothing in this repository calls ``execute_live_import``. It exists so that a future, explicitly authorized run is a
single, checked call instead of hand-typed steps. It is not a CLI command and has no flag that skips a check.

``live_import_preflight`` is READ-ONLY: it reads the company list, the company's vouchers and ledgers, builds the
preview twice, and checks the approval rows and the local journal. It has no code path that sends an Import. It
reports every check with its result and returns ``ready`` only when all pass. A ready report carries a confirmation
phrase bound to the company GUID, the document and the request digest.

``execute_live_import`` refuses unless posting is enabled for this process, the preflight is run again and passes,
and the caller typed that exact confirmation phrase. It then calls ``post_approved`` once with
``expected_request_sha256`` set to the confirmed digest, so a preview that differs at send time is BLOCKED
(``request_changed``) before anything leaves: the bytes the person confirmed are the bytes sent. It never retries, never clears a block and never deletes anything; an uncertain outcome
is returned as ``outcome_uncertain`` for the person to reconcile.

Designating the disposable company: the caller passes ``disposable_company_guid`` explicitly (the procedure reads it
from ``FINLEDGER_TALLY_LIVE_TEST_COMPANY_GUID``). It must equal the paired GUID, the company name must start with
``FinLedger Live Test``, it must be the only loaded company and it must hold no vouchers. These checks cannot prove a
company is not a client's; the person running the test confirms that, and the summary says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .company import list_companies, pinned_export
from .config import AgentSettings
from .identity import (
    _CANONICAL_UUID,
    CompanyNotResolved,
    LookupStatus,
    TallyMalformedResponse,
    TallyRequestRejected,
    parse_voucher_export,
)
from .journal import FileJournal, JournalCorrupt
from .lookup import lookup_document, voucher_request
from .masters import fetch_ledger_masters
from .posting import PostAuthorization, PostOutcome, _draft_date, check_authorization, post_approved
from .transport import TallyTransport, TallyUnavailable
from .voucher_xml import DraftRejected, preview_purchase_xml

LIVE_TEST_NAME_PREFIX = "FinLedger Live Test"
_READ_ERRORS = (TallyUnavailable, TallyMalformedResponse, TallyRequestRejected, CompanyNotResolved)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    ready: bool
    checks: tuple[Check, ...]
    document_id: str | None = None
    revision: int | None = None
    company_guid: str | None = None
    reference: str | None = None
    amount: str | None = None
    request_sha256: str | None = None
    confirmation: str | None = None  # only on a ready report

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.ok)

    def summary(self) -> str:
        """Shown to the person before any submission. Holds identifiers and the digest, not ledger names."""
        lines = ["FinLedger live Import preflight (read-only; nothing has been sent)",
                 f"  document  {self.document_id}  revision {self.revision}",
                 f"  company   {self.company_guid}",
                 f"  reference {self.reference}  amount {self.amount}",
                 f"  request   sha256 {self.request_sha256}", ""]
        lines += [f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in self.checks]
        if self.ready:
            lines += ["", "READY. This sends ONE voucher Import, once, with no retry. You must also confirm yourself",
                      "that this company is disposable and holds no client data. To proceed, type exactly:",
                      f"  {self.confirmation}"]
        else:
            lines += ["", f"NOT READY: {len(self.failed)} check(s) failed. Nothing may be sent."]
        return "\n".join(lines)


def confirmation_phrase(company_guid: str, document_id: str, request_sha256: str) -> str:
    return f"IMPORT ONE VOUCHER {document_id} INTO {company_guid} SHA256 {request_sha256[:16]}"


def live_import_preflight(job: Mapping[str, Any], auth: PostAuthorization, settings: AgentSettings, *,
                          disposable_company_guid: str | None, transport: TallyTransport | None = None,
                          posting_expected: bool = False) -> PreflightReport:
    """Every check the procedure requires, read-only. ``posting_expected`` is False for the dry preflight (posting
    must still be off) and True only inside ``execute_live_import``."""
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str) -> bool:
        checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    if not isinstance(settings, AgentSettings) or not isinstance(job, Mapping):
        add("inputs", False, "settings must be AgentSettings and the job an object")
        return PreflightReport(False, tuple(checks))
    doc, rev = job.get("document_id"), job.get("revision")
    guid = settings.company_guid
    designated = isinstance(disposable_company_guid, str) and _CANONICAL_UUID.fullmatch(disposable_company_guid)
    add("disposable_company_designated", designated and disposable_company_guid == guid,
        "FINLEDGER_TALLY_LIVE_TEST_COMPANY_GUID is set and equals the paired company GUID" if designated
        and disposable_company_guid == guid else "the disposable company GUID is missing, malformed or not the paired one")
    add("posting_state", settings.posting_enabled is posting_expected,
        f"posting is {'enabled' if settings.posting_enabled else 'disabled'}; this step requires it "
        f"{'enabled for this one process' if posting_expected else 'still disabled'}")
    add("company_name_convention", isinstance(settings.company_name, str)
        and settings.company_name.startswith(LIVE_TEST_NAME_PREFIX),
        f"the paired company name must start with {LIVE_TEST_NAME_PREFIX!r}")

    problems = check_authorization(job, auth, settings)
    allowed = set() if posting_expected else {"posting_disabled"}
    blocking = [code for code, _ in problems if code not in allowed]
    add("authorization", not blocking, "approval rows match this exact job" if not blocking
        else f"refused: {sorted(set(blocking))}")
    human = isinstance(auth, PostAuthorization) and isinstance(auth.approvals, tuple) and ("human", rev) in auth.approvals
    add("human_approval", human, "a human approval for this revision exists (policy approval is not enough for the "
        "live test)" if human else "no human approval row for this revision")

    try:
        events = FileJournal(settings.journal_dir).events(doc) if isinstance(doc, str) else None
        add("fresh_document", events == [], "no journal entry exists for this document" if events == []
            else "this document already has journal entries; use a new document")
    except (JournalCorrupt, OSError, ValueError) as exc:
        add("fresh_document", False, f"journal unreadable: {type(exc).__name__}")

    transport = transport or settings.transport()
    try:
        companies = list_companies(transport)
    except _READ_ERRORS as exc:
        add("tally_reachable", False, f"{type(exc).__name__}: {exc}"[:300])
        return PreflightReport(False, tuple(checks), doc, rev, guid)
    add("single_company_loaded", len(companies) == 1, f"{len(companies)} companies are loaded; exactly one is required")
    if not (settings.company_name and guid) or len(companies) != 1:
        return PreflightReport(False, tuple(checks), doc, rev, guid)

    try:
        company, raw = pinned_export(transport, settings.company_name, lambda c: _all_vouchers(c, job), guid)
        rows = parse_voucher_export(raw)
    except _READ_ERRORS as exc:
        add("company_verified", False, f"{type(exc).__name__}: {exc}"[:300])
        return PreflightReport(False, tuple(checks), doc, rev, guid)
    add("company_verified", True, "the loaded company has the paired name and GUID (checked before and after)")
    add("company_has_no_vouchers", not rows, f"{len(rows)} voucher(s) in the company; a fresh disposable company has 0")

    try:
        masters = fetch_ledger_masters(settings.company_name, expected_guid=guid, transport=transport)
        first = preview_purchase_xml(job, company=company, expected_company_name=settings.company_name, masters=masters)
        second = preview_purchase_xml(job, company=company, expected_company_name=settings.company_name,
                                      masters=masters)
    except DraftRejected as exc:
        add("preview", False, f"draft refused: {sorted(exc.codes)}")
        return PreflightReport(False, tuple(checks), doc, rev, guid)
    except _READ_ERRORS as exc:
        add("preview", False, f"masters not read: {type(exc).__name__}")
        return PreflightReport(False, tuple(checks), doc, rev, guid)
    add("preview", True, "purchase voucher built from the verified company's ledgers")
    add("preview_deterministic", first.xml == second.xml, "two builds produced identical bytes"
        if first.xml == second.xml else "two builds differed; refusing")

    found, _ = lookup_document(transport, settings.company_name, doc, _draft_date(job), guid)
    add("marker_absent", found.status is LookupStatus.NOT_FOUND, f"lookup: {found.status.value}")

    ready = all(c.ok for c in checks)
    return PreflightReport(ready, tuple(checks), doc, rev, guid, first.reference, f"{first.party_amount:.2f}",
                           first.sha256, confirmation_phrase(guid, doc, first.sha256) if ready else None)


def _all_vouchers(company, job):
    day = _draft_date(job)
    if day is None:
        raise TallyMalformedResponse("job has no valid voucher date")  # refused before any request is built
    return voucher_request(company, day, day)  # Tally ignores the window (real capture); every voucher comes back


def execute_live_import(job: Mapping[str, Any], auth: PostAuthorization, settings: AgentSettings, *,
                        disposable_company_guid: str | None, confirmation: str,
                        transport: TallyTransport | None = None) -> tuple[PreflightReport, PostOutcome | None]:
    """Re-run the preflight with posting enabled; send ONE Import only if it passes and ``confirmation`` matches."""
    transport = transport or settings.transport()
    report = live_import_preflight(job, auth, settings, disposable_company_guid=disposable_company_guid,
                                   transport=transport, posting_expected=True)
    if not report.ready:
        return report, None
    if not isinstance(confirmation, str) or confirmation != report.confirmation:
        bad = Check("confirmation", False, "the typed confirmation does not match this exact request; nothing sent")
        return PreflightReport(False, report.checks + (bad,), report.document_id, report.revision,
                               report.company_guid, report.reference, report.amount, report.request_sha256), None
    return report, post_approved(job, auth, settings, transport=transport,
                                 expected_request_sha256=report.request_sha256)


__all__ = ["Check", "LIVE_TEST_NAME_PREFIX", "PreflightReport", "confirmation_phrase", "execute_live_import",
           "live_import_preflight"]
