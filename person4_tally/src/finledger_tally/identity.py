"""Read-only identity lookup: find the Tally voucher that carries a FinLedger narration marker.

Pure functions over raw Tally Export replies. Nothing here talks to Tally, and nothing here may be used on its own to
decide that a voucher must be (re)posted: ``not_found`` is not proof that a voucher never existed.

Shapes come from one real capture (TallyPrime EDU 7.1, tests/fixtures/tallyprime_7_1_edu_run_20261010_162316):
- UTF-8 without BOM, CRLF; ``ENVELOPE/HEADER/STATUS`` is ``1``.
- Rows are ``ENVELOPE/BODY/DATA/COLLECTION/VOUCHER`` (company list: ``.../COLLECTION/COMPANY``).
- ``GUID`` is ``<company GUID>-<8 hex digits>``; ``MASTERID`` is a space-padded number (``" 1"``).
- ``SVFROMDATE``/``SVTODATE`` did NOT filter a Voucher collection export, so callers must expect every voucher and
  must never treat a reply as date-scoped. The lookup ignores dates entirely.

Company identity: the only company identifier in a voucher row is the GUID prefix. The company list reply gives the
company's NAME and GUID (a lowercase 8-4-4-4-12 hex UUID). Voucher rows carry no company name, and the capture held a
single company, so "a reply pinned with SVCURRENTCOMPANY only holds that company's vouchers" is unproven; the lookup
therefore checks every row's GUID prefix and refuses the whole reply if any row is foreign or malformed. The GUID
suffix is never interpreted (not as MASTERID, not as an order): only the ``<company GUID>-<8 hex>`` shape is used.

Name resolution is exact, and ambiguity fails closed: a name is resolved only when exactly one loaded company has it
exactly AND no other loaded company has a name equal to it ignoring case, Unicode compatibility forms and spacing.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum

MARKER_PREFIX = "finledger:document_id="
_CANONICAL_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EXACT_MARKER = re.compile(r"(?<![\w:=-])finledger:document_id=(" + _CANONICAL_UUID.pattern + r")(?![\w-])")
# Same language as ``finledger\s*:?\s*document[_\s-]*id``, but without two adjacent ``\s*`` runs that can split one
# whitespace run in quadratically many ways: each run of spaces is now tried in linear time.
_MARKER_LIKE = re.compile(r"finledger(?:\s*:)?\s*document[_\s-]*id", re.IGNORECASE)
_COMPANY_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_VOUCHER_GUID = re.compile(r"(" + _COMPANY_GUID.pattern + r")-[0-9a-f]{8}")
_AMOUNT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")  # real export: "1180.00", "-1180.00"; ASCII digits only
_NOT_ALNUM = re.compile(r"[^0-9a-z]")


class TallyMalformedResponse(ValueError):
    """The reply is not the shape this lookup was built and tested against."""


class TallyRequestRejected(RuntimeError):
    """Tally answered, but reported an error (STATUS other than 1, or a LINEERROR)."""


class CompanyNotResolved(LookupError):
    """The requested company is not loaded in Tally, or more than one loaded company has that exact name."""


@dataclass(frozen=True)
class TallyCompany:
    name: str
    guid: str


@dataclass(frozen=True)
class VoucherRow:
    guid: str
    master_id: int
    alter_id: int | None
    date: str | None
    voucher_type: str | None
    voucher_number: str | None
    party: str | None
    reference: str | None
    amount: Decimal | None
    narration: str | None  # None means the element was absent, "" means present but empty
    is_deleted: bool | None
    remote_id: str | None = None  # VOUCHER REMOTEID attribute; recorded only (equals GUID for a hand-entered voucher)
    vch_key: str | None = None  # VOUCHER VCHKEY attribute; recorded only


class LookupStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class LookupResult:
    status: LookupStatus
    reason: str
    row: VoucherRow | None = None
    rows: tuple[VoucherRow, ...] = field(default_factory=tuple)


def identity_marker(document_id: str) -> str:
    """The exact narration marker for a document id; refuses anything but a canonical lowercase UUID."""
    if not isinstance(document_id, str) or not _CANONICAL_UUID.fullmatch(document_id):
        raise ValueError("document_id must be a canonical lowercase UUID")
    uuid.UUID(document_id)
    return MARKER_PREFIX + document_id


def _decode(raw: bytes) -> str:
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("raw Tally reply must be bytes")
    try:
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TallyMalformedResponse(f"reply is not UTF-8 or UTF-16 with BOM: {exc}") from None
    if "\x00" in text:  # e.g. UTF-16 without BOM read as UTF-8: every other character would be NUL
        raise TallyMalformedResponse("reply holds NUL characters (UTF-16 without BOM?); refused")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():  # real Tally replies carry no DTD
        raise TallyMalformedResponse("reply declares a DTD or entities; refused")
    return text


def _envelope(raw: bytes) -> ET.Element:
    return _envelope_text(_decode(raw))


def _envelope_text(text: str) -> ET.Element:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TallyMalformedResponse(f"reply is not well-formed XML: {exc}") from None
    if root.tag != "ENVELOPE":
        raise TallyMalformedResponse(f"root element is {root.tag!r}, not ENVELOPE")
    errors = [e.text or "" for e in root.iter("LINEERROR")]
    if errors:
        raise TallyRequestRejected("; ".join(errors))
    status = (root.findtext("HEADER/STATUS") or "").strip()
    if status != "1":
        raise TallyRequestRejected(f"Tally STATUS is {status!r}, expected '1'")
    return root


def _rows(root: ET.Element, tag: str) -> list[ET.Element]:
    collections = root.findall("BODY/DATA/COLLECTION")
    if len(collections) != 1:
        raise TallyMalformedResponse(f"expected one BODY/DATA/COLLECTION, found {len(collections)}")
    children = list(collections[0])
    strangers = sorted({c.tag for c in children if c.tag != tag})
    if strangers:
        raise TallyMalformedResponse(f"collection holds {strangers}, expected only {tag}")
    return children


def _text(el: ET.Element, tag: str) -> str | None:
    """The whole text of the one ``tag`` child of ``el``; None when absent. Refused when ``tag`` appears twice or the
    field holds a child element (mixed content such as ``A<X/>B`` would otherwise be truncated to ``A``)."""
    found = el.findall(tag)
    if len(found) > 1:
        raise TallyMalformedResponse(f"{tag} appears {len(found)} times in one row")
    if found and len(found[0]):
        raise TallyMalformedResponse(f"{tag} holds a child element; mixed content is refused, not truncated")
    return None if not found else (found[0].text or "")


def _number(el: ET.Element, tag: str, required: bool) -> int | None:
    """A whole number of ASCII digits (``str.isdigit`` alone would accept other scripts' digits and superscripts)."""
    value = _text(el, tag)
    if value is None or not value.strip():
        if required:
            raise TallyMalformedResponse(f"row has no {tag}")
        return None
    digits = value.strip()
    if not (digits.isascii() and digits.isdigit()):
        raise TallyMalformedResponse(f"{tag} {value!r} is not a whole number")
    try:
        return int(digits)
    except ValueError:  # beyond the interpreter's digit limit
        raise TallyMalformedResponse(f"{tag} has {len(digits)} digits") from None


def _amount(el: ET.Element) -> Decimal | None:
    """AMOUNT as an exact Decimal, never rounded. Absent or blank is None; anything but plain ASCII ``-123.45`` (NaN,
    Infinity, exponents, other scripts' digits, separators, Dr/Cr) is refused."""
    text = _text(el, "AMOUNT")
    if text is None or not text.strip():
        return None
    if not _AMOUNT.fullmatch(text.strip()):
        raise TallyMalformedResponse(f"AMOUNT {text!r} is not a plain decimal number")
    try:
        return Decimal(text.strip())
    except InvalidOperation:
        raise TallyMalformedResponse(f"AMOUNT {text!r} is not a number") from None


def _only_rows(root: ET.Element, tag: str) -> list[ET.Element]:
    """``_rows``, and refuse a ``tag`` element (tag compared ignoring case) anywhere else in the reply: a row outside
    the collection would otherwise be silently skipped. The only exemption is a leaf counter directly under
    ``BODY/DESC/CMPINFO`` whose text is a whole number (real Tally sends ``<COMPANY>1</COMPANY>``,
    ``<VOUCHER>6</VOUCHER>`` there); a full row next to or inside CMPINFO is refused like any other stray row."""
    rows = _rows(root, tag)
    counters = {e for e in root.findall("BODY/DESC/CMPINFO/*")
                if len(e) == 0 and not e.attrib and (e.text or "").strip().isascii()
                and (e.text or "").strip().isdigit()}
    found = sum(1 for e in root.iter() if isinstance(e.tag, str) and e.tag.upper() == tag and e not in counters)
    if found != len(rows):
        raise TallyMalformedResponse(f"reply holds {found - len(rows)} {tag} element(s) outside the collection rows")
    return rows


def name_key(name: str) -> str:
    """The form two company names are compared in for ambiguity: Unicode compatibility form, case-folded, runs of
    spaces collapsed. Never used to select a company, only to refuse one."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def parse_company_list(raw: bytes) -> list[TallyCompany]:
    """Companies in a Company collection Export reply (the loaded companies).

    Refused whole: a row without NAME or GUID, a NAME attribute that disagrees with the NAME element, a GUID that is
    not 8-4-4-4-12 hex, two rows with one GUID (compared lower-case), a COMPANY element outside the collection.
    """
    companies = []
    for row in _only_rows(_envelope(raw), "COMPANY"):
        name, guid = _text(row, "NAME"), _text(row, "GUID")
        if not name or not guid:
            raise TallyMalformedResponse("company row lacks NAME or GUID")
        if row.get("NAME") is not None and row.get("NAME") != name:
            raise TallyMalformedResponse("company row NAME attribute and NAME element disagree")
        guid = guid.strip().lower()
        if not _COMPANY_GUID.fullmatch(guid):
            raise TallyMalformedResponse(f"company GUID {guid!r} is not the 8-4-4-4-12 hex form seen from Tally")
        companies.append(TallyCompany(name=name, guid=guid))
    guids = [c.guid for c in companies]
    if len(set(guids)) != len(guids):
        raise TallyMalformedResponse("two loaded companies share one GUID")
    return companies


def resolve_company(companies: list[TallyCompany], name: str) -> TallyCompany:
    """The one loaded company whose NAME equals ``name`` exactly (case and spaces included).

    Refused (``CompanyNotResolved``, whose message holds only ``name`` and counts, never another company's name or
    GUID) unless exactly one loaded company has that exact name and no OTHER loaded company
    has the same name ignoring case, compatibility forms or spacing (``name_key``): with "Acme" and "ACME" both loaded,
    neither is resolved, even by its exact name, because which one Tally pins is not proven.
    """
    if not isinstance(name, str):
        raise CompanyNotResolved(f"company name is a {type(name).__name__}, not a string")
    # Messages name only the configured name and counts: on a CA's PC the other loaded companies are other clients.
    found = [c for c in companies if c.name == name]
    if len(found) != 1:
        raise CompanyNotResolved(f"{len(found)} loaded companies are named {name!r} exactly "
                                 f"({len(companies)} loaded in all)")
    similar = [c for c in companies if name_key(c.name) == name_key(name)]
    if len(similar) != 1:
        raise CompanyNotResolved(f"{len(similar)} loaded companies are named {name!r} ignoring case or spacing; "
                                 f"the name is ambiguous and is not resolved ({len(companies)} loaded in all)")
    return found[0]


def parse_voucher_export(raw: bytes) -> list[VoucherRow]:
    """Voucher rows in a Voucher collection Export reply. Fails closed on any shape it was not built for."""
    rows = []
    for el in _only_rows(_envelope(raw), "VOUCHER"):
        guid = (_text(el, "GUID") or "").strip().lower()
        if not guid:
            raise TallyMalformedResponse("voucher row has no GUID")
        amount = _amount(el)
        deleted = _text(el, "ISDELETED")
        rows.append(VoucherRow(
            guid=guid,
            master_id=_number(el, "MASTERID", required=True),
            alter_id=_number(el, "ALTERID", required=False),
            date=_text(el, "DATE"),
            voucher_type=_text(el, "VOUCHERTYPENAME"),
            voucher_number=_text(el, "VOUCHERNUMBER"),
            party=_text(el, "PARTYLEDGERNAME"),
            reference=_text(el, "REFERENCE"),
            amount=amount,
            narration=_text(el, "NARRATION"),
            is_deleted=None if deleted is None else deleted.strip().lower() == "yes",
            remote_id=el.get("REMOTEID"),
            vch_key=el.get("VCHKEY"),
        ))
    return rows


def _company_guid(company_guid: str) -> str:
    guid = company_guid.strip().lower() if isinstance(company_guid, str) else ""
    if not _COMPANY_GUID.fullmatch(guid):
        raise ValueError("company_guid must be the company's 8-4-4-4-12 hex GUID from the company list")
    return guid


def voucher_company_guid(row: VoucherRow) -> str | None:
    """The company GUID a voucher GUID starts with, or None when the GUID is not ``<company GUID>-<8 hex>``."""
    match = _VOUCHER_GUID.fullmatch(row.guid.lower())
    return match.group(1) if match else None


def belongs_to_company(row: VoucherRow, company_guid: str) -> bool:
    """True when the voucher GUID is ``<company_guid>-<8 hex digits>``, the shape seen in the real capture."""
    return voucher_company_guid(row) == _company_guid(company_guid)


def _scope_problem(rows: tuple[VoucherRow, ...], company: str) -> LookupResult | None:
    """Refuse a reply that is not provably one company's set of distinct vouchers. A foreign or unattributable row is
    only counted in the reason, never returned as a row, and its GUID (another company's) is never quoted."""
    owners = [voucher_company_guid(row) for row in rows]
    unattributable = sum(1 for owner in owners if owner is None)
    if unattributable:
        return LookupResult(LookupStatus.UNREADABLE, f"{unattributable} of {len(rows)} voucher GUIDs are not "
                            "<company GUID>-<8 hex>; the reply is not company-scoped")
    foreign = sum(1 for owner in owners if owner != company)
    if foreign:
        return LookupResult(LookupStatus.UNREADABLE, f"reply holds {foreign} of {len(rows)} vouchers of another "
                            f"company, not {company}; the reply is not company-scoped")
    for attr in ("guid", "master_id"):
        seen: dict[object, VoucherRow] = {}
        for row in rows:
            key = getattr(row, attr)
            key = key.lower() if isinstance(key, str) else key  # GUIDs differing only in case are one GUID
            if key in seen:
                return LookupResult(LookupStatus.UNREADABLE, f"two vouchers share {attr} {key!r}",
                                    rows=(seen[key], row))
            seen[key] = row
    return None


def _id_mentions(narration: str, document_id: str) -> int:
    """How often the document id's 32 hex digits appear in ``narration`` once Unicode compatibility forms and case
    are folded and every character but an ASCII letter or digit is dropped (so split, re-spaced or re-punctuated
    copies of the id still count)."""
    folded = _NOT_ALNUM.sub("", unicodedata.normalize("NFKC", narration).casefold())
    return folded.count(document_id.replace("-", ""))


def find_by_marker(rows: list[VoucherRow], document_id: str, company_guid: str) -> LookupResult:
    """Classify the export rows for one document id. Only ``found`` carries a voucher to record.

    ``company_guid`` must come from ``resolve_company`` on a company-list reply. Dates are never used: the export's
    date window is not honoured, so a match is accepted whatever its DATE. ALTERID is not identity (it changes on
    every edit); GUID and MASTERID are.

    ``unreadable`` (never ``not_found``) when any row is foreign or malformed, two rows share a GUID or MASTERID, a
    narration is missing, holds marker-like text that is not an exact marker, holds two markers, or mentions this
    document id (split, re-cased, re-punctuated) without holding its exact marker: a damaged marker of this document
    must block a resend, not read as absence. A narration with the exact marker that also repeats the id is a match.
    """
    marker = identity_marker(document_id)
    company = _company_guid(company_guid)
    rows = tuple(rows)  # an iterator would be exhausted by the scope checks and then look like "no match"
    if not all(isinstance(row, VoucherRow) for row in rows):
        raise TypeError("rows must be VoucherRow objects from parse_voucher_export")
    problem = _scope_problem(rows, company)
    if problem:
        return problem
    matches: list[VoucherRow] = []
    for row in rows:
        if row.narration is None:
            return LookupResult(LookupStatus.UNREADABLE, f"voucher {row.guid} has no NARRATION element", rows=(row,))
        exact = _EXACT_MARKER.findall(row.narration)
        like = _MARKER_LIKE.findall(row.narration)
        if len(like) != len(exact):
            return LookupResult(LookupStatus.UNREADABLE,
                                f"voucher {row.guid} has marker-like text that is not an exact marker", rows=(row,))
        if len(exact) > 1:
            return LookupResult(LookupStatus.UNREADABLE, f"voucher {row.guid} narration holds {len(exact)} markers",
                                rows=(row,))
        if document_id not in exact and _id_mentions(row.narration, document_id):
            return LookupResult(LookupStatus.UNREADABLE, f"voucher {row.guid} narration mentions {document_id} "
                                "without its exact marker (a damaged marker?)", rows=(row,))
        if exact and exact[0] == document_id:
            matches.append(row)
    if not matches:
        return LookupResult(LookupStatus.NOT_FOUND,
                            f"no voucher narration holds {marker}; this does not prove the voucher never existed")
    if len(matches) > 1:
        return LookupResult(LookupStatus.AMBIGUOUS, f"{len(matches)} vouchers hold {marker}", rows=tuple(matches))
    row = matches[0]
    if row.is_deleted:
        return LookupResult(LookupStatus.UNREADABLE, f"voucher {row.guid} is marked deleted", rows=(row,))
    if row.master_id <= 0:
        return LookupResult(LookupStatus.UNREADABLE, f"voucher {row.guid} has MASTERID {row.master_id}", rows=(row,))
    return LookupResult(LookupStatus.FOUND, f"exactly one voucher holds {marker}", row=row, rows=(row,))
