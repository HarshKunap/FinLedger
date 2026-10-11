"""Contract compatibility pins: what Person 4 relies on (or works around) in the Person 2 / Person 3 / shared contracts,
read from the repository itself. No database and no Tally.

Each test names its gap in docs/CONTRACT_GAPS.md. A failure here means an upstream contract CHANGED: that is good news
for an open gap, but Person 4's code and the gap's status must be updated together before the test is changed.
"""

from __future__ import annotations

import ast
import re
import sys
import typing
from decimal import Decimal
from pathlib import Path

import pytest

from finledger_tally.posting import POSTING_APPROVAL_KINDS, POSTABLE_DOCUMENT_STATUSES, p3_request_hash

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "person2_platform" / "src" / "finledger_platform" / "migrations"
P3_SERVICE = ROOT / "person3_control_ui" / "src" / "finledger_control" / "service.py"
P3_BOUNDARY = ROOT / "person3_control_ui" / "src" / "finledger_control" / "boundary.py"
P2_API = ROOT / "person2_platform" / "src" / "finledger_platform" / "api.py"
SHARED_DRAFT = ROOT / "packages" / "contracts" / "schemas" / "voucher_draft.py"


def sql() -> str:
    return "\n".join(p.read_text() for p in sorted(MIGRATIONS.glob("*.sql")))


def last_definition(text: str, start: str, end: str = "$$;") -> str:
    i = text.rindex(start)
    return text[i:text.index(end, i)]


def check_values(text: str, table_marker: str, column: str) -> set[str]:
    """The quoted values of the check ``<column> in (...)`` now in force for the table: the one in the LAST
    ``table_marker`` definition, unless a later ``alter table <table> ... <column> in (...)`` replaced it."""
    found = re.findall(rf"{column}\s+in\s*\(([^)]*)\)", text[text.rindex(table_marker):])
    assert found, f"no check on {column} after {table_marker!r}"
    table = table_marker.split()[-1]
    altered = re.findall(rf"alter\s+table\s+(?:only\s+)?{table}\b[^;]*?\b{column}\s+in\s*\(([^)]*)\)", text,
                         re.IGNORECASE)
    return set(re.findall(r"'([^']+)'", altered[-1] if altered else found[0]))


def test_gap1_posting_attempt_results_have_no_uncertain_state():
    values = check_values(sql(), "create table posting_attempts", "result")
    assert values == {"pending", "posted", "failed"}, "gap #1 changed upstream: map outcome_uncertain to it"


def test_gap2_posting_attempts_have_no_evidence_columns():
    table = last_definition(sql(), "create table posting_attempts", ");")
    text = sql()
    for column in ("request_sha256", "response_sha256", "response_excerpt"):
        assert column not in table, f"gap #2 changed upstream: fill {column} from PostOutcome"
        assert not re.search(rf"alter\s+table\s+(?:only\s+)?posting_attempts\b[^;]*add\s+column\s+(?:if\s+not\s+"
                             rf"exists\s+)?{column}\b", text, re.IGNORECASE), f"gap #2 changed upstream: fill {column}"


def test_gap3_erp_links_have_no_company_guid():
    text = sql()
    assert "company_guid" not in last_definition(text, "create table erp_links", ");")
    assert not re.search(r"alter table erp_links add column company_guid", text), "gap #3 changed upstream"


def _p3_enqueue_payload_keys() -> set[str]:
    tree = ast.parse(P3_SERVICE.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_enqueue_post")
    payload = next(n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   and getattr(n.targets[0], "id", None) == "payload")
    return {k.value for k in payload.keys}


def test_gap4_post_job_has_no_invoice_total_or_vendor_gstin():
    assert _p3_enqueue_payload_keys() == {"document_id", "client_id", "revision", "idempotency_key", "voucher_draft"}


def test_p3_request_hash_formula_is_the_one_p4_recomputes():
    src = P3_SERVICE.read_text()
    assert 'json.dumps(job["payload"], sort_keys=True, separators=(",", ":"))' in src
    assert p3_request_hash({"b": 1, "a": "x"}) == __import__("hashlib").sha256(b'{"a":"x","b":1}').hexdigest()


def _shared():
    sys.path.insert(0, str(ROOT / "packages"))
    try:
        from contracts.schemas import voucher_draft  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    return voucher_draft


def test_gap5_shared_amounts_are_floats_while_p3_boundary_uses_decimal():
    shared = _shared()
    assert typing.get_type_hints(shared.VoucherLine)["amount"] is float, "gap #5 changed upstream (shared)"
    assert typing.get_type_hints(shared.TdsDetails)["amount"] is float
    boundary = P3_BOUNDARY.read_text()
    assert re.search(r"^\s+amount: Decimal$", boundary, re.M), "P3 boundary no longer Decimal: revisit gap #5"
    assert Decimal("1180.10") != Decimal(1180.10)  # why P4 re-reads every amount as an exact decimal string


def test_gaps_6_7_tax_and_tds_are_untyped():
    shared = _shared()
    hints = typing.get_type_hints(shared.GstDetails)
    assert hints["tax_breakup"] == typing.Dict[str, float] and set(hints) == {"treatment", "input_ledgers",
                                                                             "tax_breakup"}
    assert set(typing.get_type_hints(shared.TdsDetails)) == {"applicable", "section", "ledger", "amount"}


def test_gap8_bill_wise_cannot_name_an_original_invoice_or_issuer():
    fields = set(typing.get_type_hints(_shared().BillWise))
    assert fields == {"ref_type", "ref", "due_date"}, "gap #8 changed upstream"


def test_gap10_there_is_no_agent_job_route():
    routes = re.findall(r'@app\.(?:get|post|put)\("([^"]+)"', P2_API.read_text())
    agent = sorted(r for r in routes if r.startswith("/agent/"))
    assert agent == ["/agent/mapping", "/agent/masters", "/agent/open-bills"], f"gap #10 changed upstream: {agent}"


def test_gap11_claim_job_re_leases_an_expired_running_job_but_finish_is_fenced():
    text = sql()
    claim = last_definition(text, "create or replace function claim_job")
    assert "state='running' and locked_until<now()" in claim.replace(" = ", "="), "gap #11 changed upstream"
    finish = last_definition(text, "create function finish_job")
    assert "claim_token=p_claim_token" in finish  # upstream already fences a stale finisher (claim_token, 005)


def test_gap13_voucher_type_values_differ_between_shared_and_p3():
    shared = set(typing.get_args(typing.get_type_hints(_shared().VoucherDraft)["voucher_type"]))
    p3 = set(re.findall(r'"(\w+)"', re.search(r"voucher_type: Literal\[([^\]]+)\]", P3_BOUNDARY.read_text())[1]))
    assert shared == {"purchase", "debit_note", "credit_note", "payment"}
    assert p3 == {"purchase", "credit_note", "debit_note", "journal"}, "gap #13 changed upstream"


def test_gap14_approval_kinds_and_supersession_match_what_p4_reads():
    text = sql()
    kinds = check_values(text, "create table approvals", "kind")
    assert kinds == {"human", "policy_clear", "payment_release"}
    assert set(POSTING_APPROVAL_KINDS) < kinds and "payment_release" not in POSTING_APPROVAL_KINDS
    assert "alter table approvals add column superseded_at" in text  # P4 from_rows requires this column


def test_p3_policy_clear_approval_is_written_for_revision_1():
    src = P3_SERVICE.read_text()
    assert "values(%s,%s,'policy_clear',1)" in src and "self._enqueue_post(client_id, document_id, 1)" in src


def test_p3_posted_transition_starts_from_the_statuses_p4_accepts():
    assert set(POSTABLE_DOCUMENT_STATUSES) == {"approved", "scheduled"}


def test_gap16_p2_open_bill_amounts_are_floats_and_dates_optional():
    src = P2_API.read_text()
    block = src[src.index("class OpenBill(BaseModel):"):src.index("class OpenBillsIn")]
    assert "amount: float" in block and "pending_amount: float" in block
    assert "due_date: str | None = None" in block  # P4 sends None: Tally's credit period is not a due date


@pytest.mark.parametrize("state", ["cancelled"])
def test_jobs_may_be_cancelled_upstream(state):
    assert state in check_values(sql(), "alter table jobs add constraint jobs_state_check", "state")
