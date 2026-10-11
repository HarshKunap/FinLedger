"""Deterministic Tally XML PREVIEW for an approved purchase VoucherDraft. Nothing here talks to Tally.

Input is the P3 -> P4 ``post`` job payload (context/HANDOFFS.md 2026-10-05): ``document_id``, ``client_id``,
``revision``, ``idempotency_key`` and ``voucher_draft``, where the draft is the frozen shared contract
``contracts.schemas.VoucherDraft`` (shared/00_SHARED_CONTRACTS.md). P3 only creates that job after approval.

Supported today, and nothing else (everything else is refused, never guessed):
- ``voucher_type`` ``purchase``; ``bill_wise.ref_type`` ``new_ref`` with a non-empty ``ref`` (the supplier invoice no).
- ``gst.treatment`` ``exempt`` with no ``input_ledgers`` and no ``tax_breakup``: the draft carries no typed tax lines,
  rates or roles, so GST drafts stay blocked until the contract carries them.
- ``tds.applicable`` false with amount 0; no ``cost_centre``; ``party_create_proposal`` false.
- Every ledger already exists, by exact name, in the masters of the same verified company. Ledgers are never created.

Shapes taken from the real TallyPrime EDU 7.1 export (tests/fixtures/tallyprime_7_1_edu_run_20261010_162316): DATE as
YYYYMMDD, REFERENCE = supplier invoice no, the party line credited with a positive AMOUNT and ISDEEMEDPOSITIVE No, the
purchase line debited with a negative AMOUNT and ISDEEMEDPOSITIVE Yes. The Import envelope itself
(Import Data / Vouchers / TALLYMESSAGE / ACTION="Create" / ALLLEDGERENTRIES.LIST / BILLALLOCATIONS.LIST) follows
Tally's documented import format and has NOT been run against a real Tally: it is a preview only.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from typing import Any, Mapping

from pydantic import ValidationError

from contracts.schemas.voucher_draft import BillWise, GstDetails, TdsDetails, VoucherDraft, VoucherLine

from .identity import TallyCompany, _MARKER_LIKE, identity_marker
from .mapping import LedgerStatus, MappingTable, resolve_ledger, resolve_party
from .masters import LedgerMasters

_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# C0 (tab, CR, LF included: one-line values only), DEL and C1 controls, Unicode line/paragraph separators, and the
# code points XML 1.0 cannot carry at all (surrogates, U+FFFE, U+FFFF). Refused, never stripped.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff\ufffe\uffff]")
# Bidi controls (embeddings, overrides, isolates, LRM/RLM/ALM) and U+FEFF: invisible, and they can make a reference or
# a ledger name read differently from the bytes Tally receives. Refused in every written draft text; ZWJ/ZWNJ (U+200C,
# U+200D) stay allowed, Indic scripts need them.
_DISGUISE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # date.fromisoformat alone also takes 20260401 and 2026-W14-3
_PLAIN_AMOUNT = re.compile(r"-?([0-9]+)(?:\.([0-9]+))?")  # the grammar identity._AMOUNT reads Tally's amounts with
_JOB_KEYS = {"document_id", "client_id", "revision", "idempotency_key", "voucher_draft"}
_DRAFT_KEYS = set(VoucherDraft.model_fields) | {"ai_paragraph"}  # ai_paragraph: P3 review text, never written
# pydantic ignores unknown keys inside nested objects; these say which keys each nested object may hold.
_NESTED_KEYS = {"gst": set(GstDetails.model_fields), "tds": set(TdsDetails.model_fields),
                "bill_wise": set(BillWise.model_fields)}
_LINE_KEYS = set(VoucherLine.model_fields)
_CENT = Decimal("0.01")
_MAX_AMOUNT = Decimal("999999999999.99")  # person2 numeric(14,2)
_MAX_WHOLE_DIGITS = 12
# Every Decimal operation here runs in this context, whatever the caller's thread context is: amounts have at most 14
# digits, so 60 digits of precision make every sum and negation exact.
_EXACT = Context(prec=60, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999, capitals=1, clamp=0, flags=[],
                 traps=[InvalidOperation, DivisionByZero, Overflow])
_REFUSED_TYPES = {
    "debit_note": "debit notes are refused: the shared VoucherDraft cannot say which original invoice a note is "
                  "against, its own number, or which party issued it (CONTRACT_GAPS #8); the purchase template is "
                  "never reused for a note",
    "credit_note": "credit notes are refused: the shared VoucherDraft cannot say which original invoice a note is "
                   "against, its own number, or which party issued it (CONTRACT_GAPS #8); the purchase template is "
                   "never reused for a note",
    "payment": "payments are out of scope until purchase posting is verified on a real Tally",
    "journal": "P3's 'journal' is not a voucher_type of the shared VoucherDraft (CONTRACT_GAPS #13)",
}


class DraftRejected(ValueError):
    """The job/draft cannot be previewed. ``reasons`` lists every (code, message) found, not just the first."""

    def __init__(self, reasons: list[tuple[str, str]]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(f"{code}: {msg}" for code, msg in reasons))

    @property
    def codes(self) -> set[str]:
        return {code for code, _ in self.reasons}


@dataclass(frozen=True)
class VoucherPreview:
    xml: bytes
    sha256: str
    document_id: str
    revision: int
    idempotency_key: str
    company_name: str
    company_guid: str
    party_ledger: str
    party_amount: Decimal
    narration: str
    not_written: tuple[str, ...] = field(default_factory=tuple)
    voucher_type: str = "purchase"
    voucher_date: str = ""  # YYYYMMDD as written
    reference: str = ""
    lines: tuple[tuple[str, Decimal, bool], ...] = ()  # (Tally ledger, amount, is_debit) after mapping
    stage: str = "preview_ready"  # a preview is never "ready to post": posting re-checks approval, company and Tally


def _money(raw: Any, where: str, problems: list[tuple[str, str]]) -> Decimal | None:
    """``raw`` as an exact Decimal with exponent -2, or None after recording an ``invalid_amount``. Never rounds.

    int, float (read through its shortest repr: the shared contract carries floats), Decimal and str are judged by
    their text, which must be plain decimal notation: ASCII digits, an optional leading '-', an optional fraction;
    spaces (only) around a string are ignored. Refused: bool, None, NaN and infinities, exponent forms such as
    '1e3' or '1.18E+3' (P3's boundary passes them through verbatim; the agreed wire format is plain decimal, and one
    character there is a factor of a million for a reviewer), '+', '_', separators, '.5', '5.', a fraction with more
    than 2 places other than trailing zeros, and more than 12 whole digits (person2 numeric(14,2)).
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
        problems.append(("invalid_amount", f"{where} is not a number"))
        return None
    try:
        text = raw.strip(" ") if isinstance(raw, str) else str(raw)
    except ValueError:  # an int with more digits than Python will print
        problems.append(("invalid_amount", f"{where} is larger than {_MAX_AMOUNT} (numeric(14,2), the platform range)"))
        return None
    shown = repr(text)[:40]
    match = _PLAIN_AMOUNT.fullmatch(text)
    if not match:
        problems.append(("invalid_amount", f"{where} {shown} is not a plain decimal number (digits, '-' and '.' only; "
                                           "no exponent, NaN or Infinity)"))
        return None
    whole, fraction = match.group(1).lstrip("0"), (match.group(2) or "").rstrip("0")
    if len(whole) > _MAX_WHOLE_DIGITS:
        problems.append(("invalid_amount", f"{where} is larger than {_MAX_AMOUNT} (numeric(14,2), the platform range)"))
        return None
    if len(fraction) > 2:
        problems.append(("invalid_amount", f"{where} {shown} has more than 2 decimal places"))
        return None
    return Decimal(text).quantize(_CENT, context=_EXACT)  # exact: at most 2 significant decimals were checked above


def _keys(keys: Any) -> str:
    """Unknown keys for a message, e.g. ``['x', 1]``: each key's repr, sorted as text, so keys of mixed types (which
    ``sorted`` cannot compare) never raise and ``1`` stays distinguishable from ``'1'``."""
    return "[" + ", ".join(sorted(repr(k)[:60] for k in keys)) + "]"


def _text_ok(value: str, where: str, problems: list[tuple[str, str]]) -> None:
    """Records ``invalid_text`` for control characters, characters XML 1.0 cannot carry, bidi controls and U+FEFF, and
    for any leading or trailing whitespace (Unicode whitespace such as U+00A0 included). Never alters ``value``."""
    if _CONTROL.search(value):
        problems.append(("invalid_text", f"{where} contains control characters or line breaks"))
    if _DISGUISE.search(value):
        problems.append(("invalid_text", f"{where} contains invisible bidi or byte-order-mark characters "
                                         "(U+061C, U+200E, U+200F, U+202A-U+202E, U+2066-U+2069, U+FEFF)"))
    if value != value.strip():
        problems.append(("invalid_text", f"{where} has leading or trailing whitespace (U+00A0 and U+3000 included)"))


def _nested_shape(raw: Mapping[str, Any], problems: list[tuple[str, str]]) -> list[Mapping[str, Any]]:
    """The raw ``lines``, after checking that every nested object is a plain mapping holding only contract keys.

    pydantic accepts model instances and generators and drops unknown nested keys; a dropped key (a tax rate on a
    line, an original-invoice reference on ``bill_wise``) must be refused, never ignored. A shape this code cannot
    read raises at once; unknown keys are recorded as ``unsupported_field``.
    """
    shape: list[tuple[str, str]] = []
    for key, allowed in _NESTED_KEYS.items():
        value = raw.get(key)
        if key in raw and not isinstance(value, Mapping):
            shape.append(("invalid_contract", f"{key} must be an object"))
        elif isinstance(value, Mapping) and set(value) - allowed:
            problems.append(("unsupported_field",
                             f"{key} fields outside the shared contract {_keys(set(value) - allowed)}"))
    lines = raw.get("lines", [])
    if not isinstance(lines, (list, tuple)):
        shape.append(("invalid_contract", "lines must be an array of objects"))
        lines = []
    for i, line in enumerate(lines):
        if not isinstance(line, Mapping):
            shape.append(("invalid_contract", f"lines[{i}] must be an object"))
        elif set(line) - _LINE_KEYS:
            problems.append(("unsupported_field", f"lines[{i}] fields outside the shared contract "
                                                  f"{_keys(set(line) - _LINE_KEYS)}"))
    if shape:
        raise DraftRejected(problems + shape)
    return list(lines)


def _esc(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def _amount(value: Decimal) -> str:
    return f"{value:.2f}"


def preview_purchase_xml(job: Mapping[str, Any], *, company: TallyCompany, expected_company_name: str,
                         masters: LedgerMasters | None, expected_total: Decimal | None = None,
                         mapping: MappingTable | None = None) -> VoucherPreview:
    """Validate an approved post job and render its purchase voucher Import XML, without sending it anywhere.

    ``company`` must come from ``resolve_company`` on a fresh company-list reply; ``expected_company_name`` is the
    company the client is paired with; ``masters`` must have been fetched from that same company. ``expected_total``,
    when given (the invoice total), must equal the party amount exactly; the P3 post job carries no invoice total today,
    so it is optional (see docs/CONTRACT_GAPS.md). ``mapping`` (P2 ``mapping_table`` rows) translates line ledgers;
    every resulting name must exist exactly in ``masters``.

    Guarantees: the result is a function of the arguments only (same arguments, same bytes, whatever the dict order,
    hash seed or the caller's decimal context); nothing is rounded, no tax, total, due date or reference is invented;
    every refusal is a ``DraftRejected`` carrying problem codes, never XML.
    """
    with localcontext(_EXACT):
        return _preview(job, company, expected_company_name, masters, expected_total, mapping)


def _preview(job: Mapping[str, Any], company: TallyCompany, expected_company_name: str,
             masters: LedgerMasters | None, expected_total: Decimal | None,
             mapping: MappingTable | None) -> VoucherPreview:
    problems: list[tuple[str, str]] = []

    # ---- job envelope (HANDOFFS 2026-10-05) ----
    if not isinstance(job, Mapping):
        raise DraftRejected([("invalid_job", "post job must be an object")])
    extra = set(job) - _JOB_KEYS
    missing = _JOB_KEYS - set(job)
    if extra:
        problems.append(("invalid_job", f"unknown job fields {_keys(extra)}"))
    if missing:
        raise DraftRejected(problems + [("invalid_job", f"missing job fields {sorted(missing)}")])
    revision = job["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        problems.append(("invalid_job", "revision must be a whole number >= 1"))
    doc = job["document_id"]
    if not isinstance(doc, str) or not _GUID.fullmatch(doc):
        problems.append(("invalid_job", "document_id must be a canonical lowercase UUID string"))
        doc = str(doc)  # only for the comparisons below; the job is already refused
    if job["idempotency_key"] != f"{doc}:r{revision}":
        problems.append(("invalid_job", "idempotency_key must be '<document_id>:r<revision>'"))
    raw = job["voucher_draft"]
    if not isinstance(raw, Mapping):
        raise DraftRejected(problems + [("invalid_contract", "voucher_draft must be an object")])

    # ---- the shared contract, exactly ----
    unknown = set(raw) - _DRAFT_KEYS
    if unknown:
        problems.append(("unsupported_field", f"fields outside the shared VoucherDraft contract {_keys(unknown)}"))
    raw_lines = _nested_shape(raw, problems)
    ai_paragraph = raw.get("ai_paragraph")
    if ai_paragraph is not None and not isinstance(ai_paragraph, str):
        # P3 sends it (boundary.VoucherDraftBoundary.ai_paragraph: str | None); anything else is not that field
        problems.append(("invalid_contract", "ai_paragraph must be text or null"))
    vtype = raw.get("voucher_type")
    if isinstance(vtype, str) and vtype != "purchase":  # before the contract check, so P3's 'journal' is named too
        problems.append(("unsupported_voucher_type",
                         _REFUSED_TYPES.get(vtype, f"voucher_type {vtype[:40]!r} is not supported")))
    try:
        draft = VoucherDraft.model_validate(dict(raw))
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise DraftRejected(problems + [("invalid_contract", errors)]) from None
    if draft.document_id != doc:
        problems.append(("invalid_job", "voucher_draft.document_id does not match the job"))
    try:
        identity_marker(draft.document_id)
    except ValueError:
        problems.append(("invalid_contract", "document_id must be a canonical lowercase UUID"))

    # ---- company identity: exact values, never normalised ----
    company_guid = company.guid
    if not isinstance(company_guid, str) or not _GUID.fullmatch(company_guid):
        problems.append(("company_mismatch", "company GUID is not a verified lowercase 8-4-4-4-12 hex GUID"))
    if company.name != expected_company_name:
        problems.append(("company_mismatch",
                         f"loaded company {company.name!r} is not the paired company {expected_company_name!r}"))
    elif not isinstance(company.name, str) or not company.name or company.name != company.name.strip() \
            or _CONTROL.search(company.name):
        problems.append(("company_mismatch", "company name is empty, padded or holds control characters"))
    if masters is None:
        problems.append(("masters_unavailable", "ledger masters were not fetched; nothing can be checked"))
    elif masters.company_guid != company_guid:
        problems.append(("company_mismatch", "ledger masters were not fetched from the verified company"))

    # ---- supported scope ----
    gst = draft.gst
    if gst.treatment != "exempt" or gst.input_ledgers or gst.tax_breakup:
        problems.append(("unsupported_tax", "GST drafts are blocked: the contract has no typed tax lines, rates or "
                                            "roles to write; only treatment 'exempt' with no tax data is supported"))
    tds_amount = _money((raw.get("tds") or {}).get("amount", 0), "tds.amount", problems)
    if draft.tds.applicable or draft.tds.section or draft.tds.ledger or (tds_amount is not None and tds_amount != 0):
        problems.append(("unsupported_tax", "TDS is not supported yet"))
    if draft.party_create_proposal:
        problems.append(("party_create_proposal",
                         "party ledger is a create-proposal; a reviewer must create it in Tally first (never auto-created)"))
    if draft.bill_wise.ref_type != "new_ref":
        problems.append(("unsupported_bill_wise", f"bill_wise.ref_type {draft.bill_wise.ref_type!r} is not supported"))
    ref = draft.bill_wise.ref
    if not ref or not ref.strip():
        problems.append(("missing_reference", "bill_wise.ref (supplier invoice no) is empty"))
    else:
        _text_ok(ref, "bill_wise.ref", problems)

    # ---- date ----
    voucher_date = None
    if _ISO_DATE.fullmatch(draft.date):
        try:
            voucher_date = date.fromisoformat(draft.date)
        except ValueError:
            pass
    if voucher_date is None:
        problems.append(("invalid_date", f"date {draft.date[:40]!r} is not a real calendar date written YYYY-MM-DD"))
    not_written: list[str] = []
    if draft.bill_wise.due_date:
        not_written.append("bill_wise.due_date (no verified Tally field for it yet)")
    if isinstance(ai_paragraph, str) and ai_paragraph:
        not_written.append("ai_paragraph (P3 reviewer summary; never sent to Tally)")

    # ---- ledgers and amounts ----
    party = draft.party_ledger
    if not party.strip():
        problems.append(("missing_ledger", "party_ledger is empty"))
    else:
        _text_ok(party, "party_ledger", problems)
        _resolution(resolve_party(party, masters, company_guid, mapping), "party ledger", problems)
    if not draft.lines:
        problems.append(("invalid_contract", "draft has no lines"))
    debit = credit = Decimal("0")
    lines: list[tuple[str, Decimal, bool]] = []
    for i, (line, raw_line) in enumerate(zip(draft.lines, raw_lines)):
        where = f"lines[{i}]"
        if not isinstance(raw_line.get("is_debit", True), bool):
            problems.append(("invalid_contract", f"{where}.is_debit must be true or false"))
        amount = _money(raw_line.get("amount"), f"{where}.amount", problems)
        if amount is not None and amount <= 0:
            problems.append(("invalid_amount", f"{where}.amount must be greater than 0 (direction comes from is_debit)"))
            amount = None
        if line.cost_centre is not None:
            problems.append(("unsupported_field", f"{where}.cost_centre is not supported yet"))
        if not line.ledger.strip():
            problems.append(("missing_ledger", f"{where}.ledger is empty"))
            continue
        _text_ok(line.ledger, f"{where}.ledger", problems)
        tally_name = None
        if line.ledger == party:
            problems.append(("invalid_contract", f"{where} uses the party ledger; the party line is generated"))
        else:
            tally_name = _resolution(resolve_ledger(line.ledger, masters, company_guid, mapping), where, problems)
            if tally_name == party:
                problems.append(("invalid_contract", f"{where} maps to the party ledger; the party line is generated"))
            elif tally_name is not None and tally_name != line.ledger:
                _text_ok(tally_name, f"{where} mapped ledger", problems)  # the name actually written
        if amount is not None:
            if line.is_debit:
                debit += amount
            else:
                credit += amount
            lines.append((tally_name or line.ledger, amount, line.is_debit))
    party_amount = debit - credit
    if not any(is_debit for _, _, is_debit in lines) and draft.lines:
        problems.append(("invalid_amount", "a purchase needs at least one debit line"))
    if party_amount > _MAX_AMOUNT:
        problems.append(("invalid_amount", f"party amount {party_amount} is larger than {_MAX_AMOUNT}"))
    if party_amount <= 0:
        problems.append(("amount_mismatch", f"debits minus credits is {party_amount}; the party amount must be > 0"))
    if expected_total is not None:
        expected = _money(expected_total, "expected_total", problems)
        if expected is not None and expected != party_amount:
            problems.append(("amount_mismatch", f"party amount {party_amount} != invoice total {expected}"))

    # ---- narration + marker ----
    # Only ASCII spaces at the ends are dropped (the marker adds its own one space); every other character is checked
    # first and refused, never stripped: a trailing line break, U+00A0 or U+3000 is reported, not silently removed.
    narration = draft.narration.strip(" ")
    if narration:
        _text_ok(narration, "narration", problems)
    if _MARKER_LIKE.search(draft.narration):
        problems.append(("marker_conflict", "narration already contains finledger:document_id text"))

    if problems:
        raise DraftRejected(problems)

    marker = identity_marker(draft.document_id)
    full_narration = f"{narration} {marker}" if narration else marker
    xml = _render(company.name, voucher_date, party, ref, party_amount, lines, full_narration)
    return VoucherPreview(xml=xml, sha256=hashlib.sha256(xml).hexdigest(), document_id=draft.document_id,
                          revision=revision, idempotency_key=job["idempotency_key"], company_name=company.name,
                          company_guid=company_guid, party_ledger=party, party_amount=party_amount,
                          narration=full_narration, not_written=tuple(not_written), voucher_type=draft.voucher_type,
                          voucher_date=f"{voucher_date:%Y%m%d}", reference=ref, lines=tuple(lines))


_RESOLUTION_CODES = {LedgerStatus.MISSING: "missing_ledger", LedgerStatus.AMBIGUOUS: "ambiguous_ledger",
                     LedgerStatus.MASTERS_UNAVAILABLE: "masters_unavailable",
                     LedgerStatus.COMPANY_UNVERIFIED: "company_mismatch"}


def _resolution(result, where: str, problems: list[tuple[str, str]]) -> str | None:
    """The Tally name of an EXISTS result. Any other status ALWAYS records a problem for this ledger, whatever else was
    already recorded, so an unresolved ledger can never pass even if a separate masters/company check is missing."""
    if result.status is LedgerStatus.EXISTS:
        return result.tally_name
    problems.append((_RESOLUTION_CODES.get(result.status, "missing_ledger"), f"{where}: {result.detail}"))
    return None


def _render(company_name: str, voucher_date: date, party: str, ref: str, party_amount: Decimal,
            lines: list[tuple[str, Decimal, bool]], narration: str) -> bytes:
    """Adapter-owned template. Same inputs give the same bytes. No GUID, REMOTEID or VOUCHERNUMBER is sent."""
    out = [
        "<ENVELOPE>",
        " <HEADER>",
        "  <TALLYREQUEST>Import Data</TALLYREQUEST>",
        " </HEADER>",
        " <BODY>",
        "  <IMPORTDATA>",
        "   <REQUESTDESC>",
        "    <REPORTNAME>Vouchers</REPORTNAME>",
        "    <STATICVARIABLES>",
        f"     <SVCURRENTCOMPANY>{_esc(company_name)}</SVCURRENTCOMPANY>",
        "    </STATICVARIABLES>",
        "   </REQUESTDESC>",
        "   <REQUESTDATA>",
        "    <TALLYMESSAGE>",
        '     <VOUCHER VCHTYPE="Purchase" ACTION="Create" OBJVIEW="Accounting Voucher View">',
        f"      <DATE>{voucher_date:%Y%m%d}</DATE>",
        "      <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>",
        f"      <REFERENCE>{_esc(ref)}</REFERENCE>",
        f"      <PARTYLEDGERNAME>{_esc(party)}</PARTYLEDGERNAME>",
        f"      <NARRATION>{_esc(narration)}</NARRATION>",
        "      <ISINVOICE>No</ISINVOICE>",
        "      <ALLLEDGERENTRIES.LIST>",
        f"       <LEDGERNAME>{_esc(party)}</LEDGERNAME>",
        "       <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>",
        "       <ISPARTYLEDGER>Yes</ISPARTYLEDGER>",
        f"       <AMOUNT>{_amount(party_amount)}</AMOUNT>",
        "       <BILLALLOCATIONS.LIST>",
        f"        <NAME>{_esc(ref)}</NAME>",
        "        <BILLTYPE>New Ref</BILLTYPE>",
        f"        <AMOUNT>{_amount(party_amount)}</AMOUNT>",
        "       </BILLALLOCATIONS.LIST>",
        "      </ALLLEDGERENTRIES.LIST>",
    ]
    for ledger, amount, is_debit in lines:
        out += [
            "      <ALLLEDGERENTRIES.LIST>",
            f"       <LEDGERNAME>{_esc(ledger)}</LEDGERNAME>",
            f"       <ISDEEMEDPOSITIVE>{'Yes' if is_debit else 'No'}</ISDEEMEDPOSITIVE>",
            "       <ISPARTYLEDGER>No</ISPARTYLEDGER>",
            f"       <AMOUNT>{_amount(-amount if is_debit else amount)}</AMOUNT>",
            "      </ALLLEDGERENTRIES.LIST>",
        ]
    out += [
        "     </VOUCHER>",
        "    </TALLYMESSAGE>",
        "   </REQUESTDATA>",
        "  </IMPORTDATA>",
        " </BODY>",
        "</ENVELOPE>",
        "",
    ]
    return "\n".join(out).encode("utf-8")
