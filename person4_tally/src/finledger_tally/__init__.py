"""Person 4 Tally connector. Reads (company, masters, vouchers, bills) are Export-only. The single write path,
``posting.post_approved``, is off unless the agent is configured to post AND the job's approval rows check out.
See person4_tally/README.md."""

from .identity import (
    CompanyNotResolved,
    MARKER_PREFIX,
    LookupResult,
    LookupStatus,
    TallyCompany,
    TallyMalformedResponse,
    TallyRequestRejected,
    VoucherRow,
    belongs_to_company,
    find_by_marker,
    identity_marker,
    parse_company_list,
    parse_voucher_export,
    resolve_company,
    voucher_company_guid,
)
from .agent import AgentAlreadyRunning, AgentHost, ClaimedJob, InstanceLock, JobSource, ResultSink, TickResult
from .bills import OpenBill, OpenBills, fetch_open_bills, parse_bill_export
from .config import AgentSettings
from .connector import Health, HealthReport, TallyConnector
from .journal import FileJournal, JournalCorrupt, Phase
from .lookup import lookup_document
from .mapping import LedgerResolution, LedgerStatus, MappingTable, masters_snapshot_payloads, resolve_ledger, resolve_party
from .masters import LedgerMasters, fetch_ledger_masters, parse_ledger_export
from .posting import (
    PostAuthorization,
    PostOutcome,
    State,
    clear_for_resend,
    p3_request_hash,
    parse_import_reply,
    post_approved,
    reconcile,
    write_offline_xml,
)
from .transport import ImportOutcomeUnknown, ReplyTooLarge, RequestRefused, TallyTransport, TallyUnavailable
from .voucher_xml import DraftRejected, VoucherPreview, preview_purchase_xml

__all__ = [
    "AgentAlreadyRunning", "AgentHost", "ClaimedJob", "InstanceLock", "JobSource", "ResultSink", "TickResult",
    "OpenBill", "OpenBills", "fetch_open_bills", "parse_bill_export", "AgentSettings", "Health", "HealthReport",
    "TallyConnector", "FileJournal", "JournalCorrupt", "Phase", "lookup_document", "LedgerResolution", "LedgerStatus",
    "MappingTable", "masters_snapshot_payloads", "resolve_ledger", "resolve_party", "PostAuthorization", "PostOutcome",
    "State", "clear_for_resend", "p3_request_hash", "parse_import_reply", "post_approved", "reconcile",
    "write_offline_xml", "ImportOutcomeUnknown", "ReplyTooLarge", "RequestRefused", "TallyTransport",
    "TallyUnavailable", "fetch_ledger_masters", "parse_ledger_export",
    "DraftRejected", "LedgerMasters", "VoucherPreview", "preview_purchase_xml",
    "CompanyNotResolved", "resolve_company", "voucher_company_guid",
    "MARKER_PREFIX", "LookupResult", "LookupStatus", "TallyCompany", "TallyMalformedResponse", "TallyRequestRejected",
    "VoucherRow", "belongs_to_company", "find_by_marker", "identity_marker", "parse_company_list",
    "parse_voucher_export",
]
