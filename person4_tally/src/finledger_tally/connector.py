"""The Tally adapter behind the shared connector contract (person4 ARCHITECTURE.md):

    connect() / fetch_masters() / post_invoice(...) / post_credit_debit(...) / fetch_open_bills() / post_payment(...) /
    health()

No Python interface for this contract exists elsewhere in the repository, so these are the method names from the
architecture document. ``post_invoice`` takes the P3 post job (HANDOFFS 2026-10-05, which wraps the VoucherDraft and
its idempotency key) plus the approval record, because a draft alone cannot prove it was approved.

Guarantees of the post methods: every refusal is a ``PostOutcome`` (never an exception for a malformed job), and a
refusal is reportable to P3 (``erp_result()`` not None) only after the job's approval rows check out. An unsupported
voucher type with a missing or foreign approval, or on an agent that is not configured to post, is ``not_authorized``
and is never reported, so an unapproved job cannot move a document to the exception tray. Nothing here sends a request
before those checks; only ``post_invoice`` of a purchase job reaches ``post_approved``.

``health()`` never raises for a Tally fault and its text fields carry no reply text (Tally error lines, ledger or
company names): exception type names, counts and fixed phrases only, because operators print and log the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .bills import OpenBills, fetch_open_bills
from .company import current_company, list_companies
from .config import AgentSettings
from .identity import CompanyNotResolved, TallyCompany, TallyMalformedResponse, TallyRequestRejected, name_key
from .mapping import MappingTable, masters_snapshot_payloads
from .masters import LedgerMasters, fetch_ledger_masters
from .posting import PostAuthorization, PostOutcome, State, _job_ids, check_authorization, post_approved
from .transport import HOST, TallyUnavailable

_DETAIL_LIMIT = 300


class Health:
    TALLY_UNREACHABLE = "tally_unreachable"
    MALFORMED_REPLY = "malformed_reply"
    NO_COMPANY_LOADED = "no_company_loaded"
    COMPANY_NOT_CONFIGURED = "company_not_configured"
    COMPANY_UNRESOLVED = "company_unresolved"  # no loaded company has the exact configured name
    COMPANY_AMBIGUOUS = "company_ambiguous"  # two loaded companies share it (exactly, or ignoring case or spacing)
    COMPANY_GUID_MISMATCH = "company_guid_mismatch"
    COMPANY_VERIFIED = "company_verified"


@dataclass(frozen=True)
class HealthReport:
    status: str
    tally_reachable: bool
    companies_loaded: int | None
    company_verified: bool
    company_guid: str | None  # the resolved company's GUID (record it at pairing)
    masters: str  # ok | failed: <exception type>[ (<fixed phrase>)] | not_checked
    ledger_count: int | None
    read_only_available: bool
    posting: str  # disabled | enabled | enabled_but_not_ready
    detail: str


class TallyConnector:
    def __init__(self, settings: AgentSettings):
        if not isinstance(settings, AgentSettings):
            raise TypeError("settings must be AgentSettings")
        self.settings = settings

    @property
    def transport(self):
        return self.settings.transport()

    def connect(self) -> TallyCompany:
        """Verify the paired company is loaded (name, and GUID when configured). Read-only."""
        if not self.settings.company_name:
            raise CompanyNotResolved("no company name is configured")
        return current_company(self.transport, self.settings.company_name, self.settings.company_guid)

    def health(self, *, check_masters: bool = True) -> HealthReport:
        s = self.settings

        def report(status, *, reachable=True, loaded=None, guid=None, masters="not_checked", ledgers=None, detail=""):
            verified = status == Health.COMPANY_VERIFIED
            read_only = verified and not masters.startswith("failed")
            if not s.posting_enabled:
                posting = "disabled"
            elif read_only and s.client_id and s.company_guid:
                posting = "enabled"
            else:
                posting = "enabled_but_not_ready"
            return HealthReport(status, reachable, loaded, verified, guid, masters, ledgers, read_only, posting, detail)

        try:
            companies = list_companies(self.transport)
        except TallyUnavailable as exc:
            return report(Health.TALLY_UNREACHABLE, reachable=False, detail=_unreachable_detail(exc, s.port))
        except (TallyMalformedResponse, TallyRequestRejected) as exc:  # their text can quote the reply
            return report(Health.MALFORMED_REPLY, detail=f"{type(exc).__name__}: the company list reply was refused")
        if not companies:
            return report(Health.NO_COMPANY_LOADED, loaded=0, detail="Tally answered but no company is loaded")
        if not s.company_name:
            return report(Health.COMPANY_NOT_CONFIGURED, loaded=len(companies), detail="no company name configured")
        matches = [c for c in companies if c.name == s.company_name]
        if not matches:
            return report(Health.COMPANY_UNRESOLVED, loaded=len(companies),
                          detail="no loaded company has the configured name (exact, case-sensitive match)")
        similar = [c for c in companies if name_key(c.name) == name_key(s.company_name)]
        if len(matches) != 1 or len(similar) != 1:  # the same rule as identity.resolve_company; nothing is pinned
            return report(Health.COMPANY_AMBIGUOUS, loaded=len(companies),
                          masters="failed: CompanyNotResolved (company ambiguous; masters not read)"
                          if check_masters else "not_checked",
                          detail=f"{len(similar)} loaded companies have the configured name ignoring case or spacing "
                                 f"({len(matches)} exactly); which one Tally would pin is not proven")
        company = matches[0]
        if s.company_guid and company.guid != s.company_guid:
            return report(Health.COMPANY_GUID_MISMATCH, loaded=len(companies), guid=company.guid,
                          detail="a company with the configured name is loaded, but its GUID is not the paired one")
        if not check_masters:
            return report(Health.COMPANY_VERIFIED, loaded=len(companies), guid=company.guid)
        try:
            masters = fetch_ledger_masters(s.company_name, expected_guid=company.guid, transport=self.transport)
        except (TallyUnavailable, TallyMalformedResponse, TallyRequestRejected, CompanyNotResolved) as exc:
            return report(Health.COMPANY_VERIFIED, loaded=len(companies), guid=company.guid,
                          masters=f"failed: {type(exc).__name__}")  # the message can name ledgers or companies
        return report(Health.COMPANY_VERIFIED, loaded=len(companies), guid=company.guid, masters="ok",
                      ledgers=len(masters.ledgers))

    def fetch_masters(self) -> LedgerMasters:
        if not self.settings.company_name:
            raise CompanyNotResolved("no company name is configured")
        return fetch_ledger_masters(self.settings.company_name, expected_guid=self.settings.company_guid,
                                    transport=self.transport)

    def masters_snapshot(self) -> list[dict[str, Any]]:
        """Bodies for person2 ``POST /agent/masters``; refreshing never changes Tally."""
        return masters_snapshot_payloads(self.fetch_masters())

    def fetch_open_bills(self) -> OpenBills:
        if not self.settings.company_name:
            raise CompanyNotResolved("no company name is configured")
        return fetch_open_bills(self.settings.company_name, expected_guid=self.settings.company_guid,
                                transport=self.transport)

    def post_invoice(self, job: Mapping[str, Any], auth: PostAuthorization, *, mapping: MappingTable | None = None,
                     expected_total: Decimal | None = None) -> PostOutcome:
        """The only method that can send an Import: a purchase job goes to ``post_approved`` (all its checks), anything
        else is refused here with no network call. A ``mapping`` that is neither None nor a ``MappingTable`` is
        ``blocked`` (``invalid_argument``, never reported, nothing sent): it is the caller's fault, not the job's."""
        draft = job.get("voucher_draft") if isinstance(job, Mapping) else None
        voucher_type = draft.get("voucher_type") if isinstance(draft, Mapping) else None
        if voucher_type != "purchase":
            return self._unsupported(job, auth, "post_invoice handles purchase vouchers only, not "
                                                f"{_shown(voucher_type)}")
        refused = self._config_refusal(job)
        if refused is not None:
            return refused
        if mapping is not None and not isinstance(mapping, MappingTable):  # a caller bug, not a fault of the job
            doc, rev, _ = _job_ids(job)
            why = f"mapping must be a MappingTable or None, not {type(mapping).__name__}; nothing sent"
            return PostOutcome(State.BLOCKED, doc, rev, why, problems=(("invalid_argument", why),))
        return post_approved(job, auth, self.settings, transport=self.transport, mapping=mapping,
                             expected_total=expected_total)

    def post_credit_debit(self, job: Mapping[str, Any], auth: PostAuthorization, **_: Any) -> PostOutcome:
        return self._unsupported(job, auth, "debit/credit notes are blocked: the shared VoucherDraft cannot say which "
                                            "original invoice a note is against, nor which party issued it "
                                            "(docs/CONTRACT_GAPS.md)")

    def post_payment(self, job: Mapping[str, Any], auth: PostAuthorization, **_: Any) -> PostOutcome:
        return self._unsupported(job, auth, "payments are out of scope until invoice posting is live-verified")

    def _config_refusal(self, job: Any) -> PostOutcome | None:
        """``not_authorized`` (never reported, nothing sent) while ``settings.posting_problems()`` reports anything."""
        problems = self.settings.posting_problems()
        if not problems:
            return None
        doc, rev, _ = _job_ids(job)
        return PostOutcome(State.NOT_AUTHORIZED, doc, rev, "; ".join(m for _, m in problems), problems=problems)

    def _unsupported(self, job: Any, auth: Any, why: str) -> PostOutcome:
        """A job P4 will never post. Reported (``validation_rejected``) only when its approval rows and this agent's
        configuration check out; otherwise ``not_authorized``, which is never reported. No I/O either way."""
        doc, rev, key = _job_ids(job)
        problems = list(self.settings.posting_problems()) + check_authorization(job, auth, self.settings)
        if not problems and (doc is None or rev is None or key is None):
            problems = [("invalid_job", "job lacks a canonical document_id, revision or idempotency key")]
        if problems:
            seen: dict[str, str] = {}
            for code, message in problems + [("unsupported_voucher_type", why)]:
                seen.setdefault(code, message)
            return PostOutcome(State.NOT_AUTHORIZED, doc, rev, "; ".join(seen.values()), problems=tuple(seen.items()))
        return PostOutcome(State.VALIDATION_REJECTED, doc, rev, why, problems=(("unsupported_voucher_type", why),))


_UNREACHABLE = re.compile(r"Tally did not (answer|accept a connection) on [0-9.]+:[0-9]+: ([A-Za-z][A-Za-z0-9_]{0,59})")


def _unreachable_detail(exc: TallyUnavailable, port: int) -> str:
    """The health detail for an unreachable Tally, built from fixed phrases, the configured port and the underlying
    error's type name only (read from the transport's own message format). Nothing else of the exception's text is
    kept, so a message that ever quoted a company name or reply bytes cannot reach the report."""
    found = _UNREACHABLE.match(str(exc))
    verb, kind = found.groups() if found else ("answer", type(exc).__name__)
    return f"Tally did not {verb} on {HOST}:{port}: {kind}"[:_DETAIL_LIMIT]


def _shown(value: Any) -> str:
    """A draft value quoted in an outcome that P3 may store: bounded, never the whole value."""
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."
