"""Exact ledger resolution against fetched masters, plus the P2 ``mapping_table`` and ``/agent/masters`` shapes.

Resolution never creates or alters a ledger and never picks a near match. The mission's outcome names map onto the
existing ``LedgerStatus`` (no new values: ``voucher_xml._RESOLUTION_CODES`` maps every status to a problem code):

- resolved -> ``exists``: the (mapped) name is in the masters exactly, and no other ledger or alias looks like it.
- missing -> ``missing``: no ledger by that name; a reviewer must create it in Tally (create-proposal), never this code.
- ambiguous -> ``ambiguous``: only a look-alike ledger exists (case, spacing, Unicode form, invisible characters), the
  name is an alias (an alias is never authoritative), two ledgers look alike, a mapping key differs from the name only
  in that way, or a GSTIN mapping disagrees with the draft's party ledger.
- fetch_failed -> ``masters_unavailable``: no masters were fetched (the caller passes ``None`` after a failed fetch).
- company_unverified -> ``company_unverified``: the masters belong to another company, or the GUID is not verified.
- unsupported -> no status: ``MappingTable.get`` raises ``ValueError`` for the ``tax``/``tds``/``cost_centre`` kinds,
  which P4 cannot apply (the shared VoucherDraft has no typed tax lines; drafts that need them are refused by
  ``voucher_xml`` as ``unsupported_tax`` / ``unsupported_field`` before any resolution).

``mapping_table`` (person2 migration 002): ``kind`` in party | ledger | tax | tds | cost_centre,
``our_key`` -> ``erp_value``, unique per (client, kind, our_key). ``party`` is keyed by vendor GSTIN, which the P3 post
job does not carry, so party mapping applies only when a caller supplies the GSTIN. ``tax``/``tds``/``cost_centre`` rows
are kept but never applied.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .company import check_name
from .identity import _COMPANY_GUID
from .masters import LedgerMasters

MAPPING_KINDS = ("party", "ledger", "tax", "tds", "cost_centre")  # person2 mapping_table.kind check constraint
APPLIED_KINDS = ("party", "ledger")  # what resolve_party / resolve_ledger use; the other kinds are refused by get()


class LedgerStatus(str, Enum):
    EXISTS = "exists"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    MASTERS_UNAVAILABLE = "masters_unavailable"
    COMPANY_UNVERIFIED = "company_unverified"


@dataclass(frozen=True)
class LedgerResolution:
    requested: str
    status: LedgerStatus
    tally_name: str | None
    detail: str


@dataclass(frozen=True)
class MappingTable:
    """One client's ``mapping_table`` rows, by kind: ``{kind: {our_key: erp_value}}``. Validated and read-only.

    ``client_id`` is the client every row was checked to belong to (set by ``from_rows`` when the rows or the caller
    name one), or None when that is unknown; ``belongs_to`` never vouches for a table whose client is unknown.
    """

    rows: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    client_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.rows, Mapping):
            raise TypeError("MappingTable rows must be a mapping of kind -> {our_key: erp_value}")
        frozen: dict[str, Mapping[str, str]] = {}
        for kind, pairs in self.rows.items():
            if kind not in MAPPING_KINDS:
                raise ValueError(f"mapping kind {kind!r} is not one of {MAPPING_KINDS}")
            if not isinstance(pairs, Mapping):
                raise TypeError(f"mapping {kind} rows must be a mapping of our_key -> erp_value")
            for key, value in pairs.items():
                check_name(key, f"mapping {kind} our_key")
                check_name(value, f"mapping {kind} erp_value")
            frozen[kind] = MappingProxyType(dict(pairs))
        object.__setattr__(self, "rows", MappingProxyType(frozen))
        if self.client_id is not None and (not isinstance(self.client_id, str) or not self.client_id.strip()
                                           or self.client_id != self.client_id.strip()):
            raise ValueError("MappingTable client_id must be None or a non-blank, unpadded string")

    def __reduce__(self):  # copy/deepcopy/pickle rebuild through __post_init__ (a mappingproxy cannot be pickled)
        return (type(self), ({kind: dict(pairs) for kind, pairs in self.rows.items()}, self.client_id))

    def belongs_to(self, client_id: Any) -> bool:
        """True only when this table's rows were checked to be ``client_id``'s (compared as text, like ``from_rows``).
        False for another client and for a table whose client is unknown (built without ``client_id``)."""
        return self.client_id is not None and client_id not in (None, "") and self.client_id == str(client_id)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]], *, client_id: Any = None) -> "MappingTable":
        """Build from P2 rows (``kind``, ``our_key``, ``erp_value``; ``id``/``updated_at`` ignored).

        Refuses unknown kinds, blank or padded keys and values, duplicate (kind, our_key), and rows of more than one
        client: either every row carries ``client_id`` and they all share it, or none does; when ``client_id`` is given
        every row must carry exactly that client (compared as text, like ``PostAuthorization.from_rows``), so one
        client's mapping is never applied to another's documents.
        """
        if isinstance(rows, (str, bytes, bytearray, Mapping)) or not isinstance(rows, Iterable):
            raise TypeError("mapping rows must be an iterable of row mappings")
        table: dict[str, dict[str, str]] = {k: {} for k in MAPPING_KINDS}
        clients: set[str] = set()
        unattributed = 0
        for row in rows:
            if not isinstance(row, Mapping):
                raise TypeError(f"mapping row must be a mapping (kind, our_key, erp_value), not {type(row).__name__}")
            kind, key, value = row.get("kind"), row.get("our_key"), row.get("erp_value")
            if not isinstance(kind, str) or kind not in MAPPING_KINDS:
                raise ValueError(f"mapping kind {kind!r} is not one of {MAPPING_KINDS}")
            check_name(key, f"mapping {kind} our_key")
            check_name(value, f"mapping {kind} erp_value")
            if key in table[kind]:
                raise ValueError(f"mapping {kind} {key!r} appears twice")
            if row.get("client_id") not in (None, ""):
                clients.add(str(row["client_id"]))
            else:
                unattributed += 1
            table[kind][key] = value
        if unattributed and (clients or client_id is not None):
            raise ValueError(f"{unattributed} mapping row(s) carry no client_id; their client cannot be checked")
        if client_id is not None:
            clients.add(str(client_id))
        if len(clients) > 1:
            raise ValueError("mapping rows are not all for the same client; refusing to mix clients' mappings")
        return cls(rows=table, client_id=next(iter(clients), None))

    def get(self, kind: str, key: str) -> str | None:
        """The exact ``erp_value`` for ``key``, or None. Only the applied kinds can be read: ``tax``/``tds``/
        ``cost_centre`` raise ``ValueError`` (unsupported), and so does an unknown kind (never a silent None)."""
        return self.rows.get(_applied_kind(kind), {}).get(key)

    def look_alike_keys(self, kind: str, key: str) -> list[str]:
        """Keys of ``kind`` that equal ``key`` only ignoring case, spacing, Unicode form or invisible characters."""
        return sorted(_look_alikes(key, self.rows.get(_applied_kind(kind), {})))

    def conflicting_look_alikes(self, kind: str, key: str) -> list[str]:
        """Look-alike keys of ``kind`` (see ``look_alike_keys``) that map to a value other than ``key``'s exact one.
        When ``key`` has no exact row, every look-alike key is returned."""
        exact = self.get(kind, key)
        pairs = self.rows.get(_applied_kind(kind), {})
        return [k for k in self.look_alike_keys(kind, key) if exact is None or pairs[k] != exact]


def _applied_kind(kind: str) -> str:
    if kind in APPLIED_KINDS:
        return kind
    if kind in MAPPING_KINDS:
        raise ValueError(f"mapping kind {kind!r} is unsupported: P4 cannot post tax, TDS or cost-centre lines")
    raise ValueError(f"mapping kind {kind!r} is not one of {MAPPING_KINDS}")


@lru_cache(maxsize=65536)  # masters are re-scanned per resolution; their keys repeat
def _look_alike_key(name: str) -> str:
    """The form names are compared in to REFUSE a choice, never to make one: invisible format characters (zero-width
    spaces/joiners, BOM, bidi controls, soft hyphen) dropped, Unicode compatibility form, case-folded, runs of
    whitespace (including no-break spaces) collapsed."""
    visible = "".join(ch for ch in name if unicodedata.category(ch) != "Cf")
    return " ".join(unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", visible).casefold()).split())


def _look_alikes(target: str, names: Iterable[str]) -> list[str]:
    """``names`` other than ``target`` itself that look like it (see ``_look_alike_key``)."""
    fold, key = target.casefold(), _look_alike_key(target)
    return [n for n in names if n != target and (n.casefold() == fold or _look_alike_key(n) == key)]


def _check_name_arg(value: Any, what: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string, not {type(value).__name__}")


def _check_masters(requested: str, masters: LedgerMasters | None, company_guid: str) -> LedgerResolution | None:
    if masters is None:
        return LedgerResolution(requested, LedgerStatus.MASTERS_UNAVAILABLE, None, "ledger masters were not fetched")
    if not isinstance(company_guid, str) or not _COMPANY_GUID.fullmatch(company_guid) \
            or masters.company_guid != company_guid:
        return LedgerResolution(requested, LedgerStatus.COMPANY_UNVERIFIED, None,
                                "ledger masters were not fetched from the verified company")
    return None


def _exact(requested: str, target: str, masters: LedgerMasters, via: str) -> LedgerResolution:
    """``target`` must be a ledger name exactly, with no look-alike ledger and no look-alike alias of another ledger.

    The parser already refuses replies with case-only duplicates or colliding aliases; this re-checks because masters
    can also be built by hand or loaded from the offline cache, which do not go through the parser.
    """
    twins = sorted(_look_alikes(target, masters.ledgers))
    alike = {target, *_look_alikes(target, {alias for alias, _ in masters.aliases})}
    owners = sorted({ledger for alias, ledger in masters.aliases if ledger != target and alias in alike})
    if target in masters.ledgers:
        if twins:
            return LedgerResolution(requested, LedgerStatus.AMBIGUOUS, None,
                                    f"{target!r}{via} exists, but so do look-alike ledgers {twins}; not chosen")
        if owners:
            return LedgerResolution(requested, LedgerStatus.AMBIGUOUS, None,
                                    f"{target!r}{via} is also an alias of {owners}; not chosen")
        return LedgerResolution(requested, LedgerStatus.EXISTS, target, f"exact match{via}")
    if twins:
        return LedgerResolution(requested, LedgerStatus.AMBIGUOUS, None,
                                f"{target!r}{via} only matches {twins} by case, spacing or Unicode form; not chosen")
    if owners:
        return LedgerResolution(requested, LedgerStatus.AMBIGUOUS, None,
                                f"{target!r}{via} is an alias of {owners}, not a ledger name; not chosen")
    return LedgerResolution(requested, LedgerStatus.MISSING, None,
                            f"ledger {target!r}{via} does not exist in Tally; never auto-created")


def resolve_ledger(name: str, masters: LedgerMasters | None, company_guid: str,
                   mapping: MappingTable | None = None) -> LedgerResolution:
    """A draft line ledger: apply a ``ledger`` mapping row if one exists, then require an exact master.

    A mapping row is applied once (never chained) and is authoritative: when it points at a missing ledger the result
    is ``missing`` even if a ledger named like the draft line exists. A mapping key that matches ``name`` only by case,
    spacing or Unicode form makes the result ``ambiguous`` instead of being skipped silently, and so does an exact key
    whose look-alike keys map to a DIFFERENT ledger: the exact key never silently wins over a conflicting twin.
    """
    _check_name_arg(name, "ledger name")
    problem = _check_masters(name, masters, company_guid)
    if problem:
        return problem
    mapped = mapping.get("ledger", name) if mapping else None
    near = mapping.conflicting_look_alikes("ledger", name) if mapping else []
    if near and mapped is None:
        return LedgerResolution(name, LedgerStatus.AMBIGUOUS, None,
                                f"ledger mapping keys {near} differ from {name!r} only by case, spacing or Unicode "
                                "form; not applied and not skipped")
    if near:
        return LedgerResolution(name, LedgerStatus.AMBIGUOUS, None,
                                f"ledger mapping {name!r} -> {mapped!r} has look-alike keys {near} that map to other "
                                "ledgers; neither is chosen")
    return _exact(name, mapped or name, masters, f" (mapped from {name!r})" if mapped else "")


def resolve_party(party_ledger: str, masters: LedgerMasters | None, company_guid: str,
                  mapping: MappingTable | None = None, vendor_gstin: str | None = None) -> LedgerResolution:
    """The party ledger: the draft's name, cross-checked with a ``party`` (GSTIN) mapping when a GSTIN is supplied.

    The GSTIN mapping never replaces the draft's name: it must agree with it exactly, or the result is ``ambiguous``;
    so is a GSTIN whose look-alike keys (case, spacing) map to a different ledger than its exact key.
    """
    _check_name_arg(party_ledger, "party ledger")
    if vendor_gstin is not None:
        _check_name_arg(vendor_gstin, "vendor GSTIN")
    problem = _check_masters(party_ledger, masters, company_guid)
    if problem:
        return problem
    mapped = mapping.get("party", vendor_gstin) if (mapping and vendor_gstin) else None
    if mapped is not None and mapped != party_ledger:
        return LedgerResolution(party_ledger, LedgerStatus.AMBIGUOUS, None,
                                f"GSTIN mapping says {mapped!r}, the draft says {party_ledger!r}; not chosen")
    if mapping and vendor_gstin:
        near = mapping.conflicting_look_alikes("party", vendor_gstin)
        if near:
            return LedgerResolution(party_ledger, LedgerStatus.AMBIGUOUS, None,
                                    f"party mapping keys {near} differ from GSTIN {vendor_gstin!r} only by case or "
                                    "spacing; the GSTIN cross-check cannot be skipped")
    return _exact(party_ledger, party_ledger, masters, "")


def masters_snapshot_payloads(masters: LedgerMasters) -> list[dict[str, Any]]:
    """Request bodies for P2 ``POST /agent/masters`` (``MastersIn``: kind + items), deterministic order.

    Only what was fetched is sent: the company identity and the ledgers with their parent group. Groups, vendors with
    GSTIN and cost centres are not fetched yet, so no snapshot of those kinds is produced.
    """
    if not masters.company_name or not masters.parents:
        raise ValueError("only masters built by parse_ledger_export (with company name and parents) can be sent")
    return [
        {"kind": "company", "items": [{"name": masters.company_name, "guid": masters.company_guid}]},
        {"kind": "ledger", "items": [{"name": name, "parent": parent} for name, parent in masters.parents]},
    ]
