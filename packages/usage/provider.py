import json
import re
from dataclasses import dataclass
from typing import Any

from usage.kill_switch import KillSwitch


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int = 0
    cost_usd: float = 0.0


def _tokens(value: str) -> int:
    return max(1, (len(value) + 3) // 4)


class MockProvider:
    """Deterministic local provider for development and contract testing."""

    def __init__(self, kill_switch: KillSwitch | None = None):
        self.kill_switch = kill_switch or KillSwitch()

    def complete(self, purpose: str, model: str, client_id: str, document_id: str, payload: dict[str, Any]) -> ProviderResponse:
        serialized = json.dumps(payload, sort_keys=True, default=str)
        tokens_in = _tokens(serialized)
        estimated_tokens = tokens_in + self._estimate_output_tokens(purpose, payload)
        reservation = self.kill_switch.reserve(client_id, document_id, estimated_tokens)
        try:
            output = self._generate(purpose, document_id, payload)
            text = json.dumps(output, separators=(",", ":"))
            tokens_out = _tokens(text)
            self.kill_switch.reconcile(reservation, tokens_in + tokens_out)
        except Exception:
            self.kill_switch.release(reservation)
            raise
        return ProviderResponse(text, tokens_in, tokens_out)

    @staticmethod
    def _estimate_output_tokens(purpose: str, payload: dict[str, Any]) -> int:
        base = {"summary": 8, "risk_llm": 64, "map": 256, "extract": 256, "extract_vision": 256}
        return base.get(purpose, 256)

    def _generate(self, purpose: str, document_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if purpose in {"extract", "extract_vision"}:
            return self._extract(document_id, payload)
        elif purpose == "map":
            return self._map(payload)
        elif purpose == "risk_llm":
            return self._risk(document_id, payload)
        elif purpose == "summary":
            return {"text": "deterministic-summary"}
        else:
            raise ValueError(f"Unsupported provider purpose: {purpose}")

    @staticmethod
    def _field(markdown: str, *names: str) -> str:
        names_pattern = "|".join(re.escape(name) for name in names)
        match = re.search(rf"(?im)^\s*(?:{names_pattern})\s*:\s*(.+?)\s*$", markdown)
        return match.group(1).strip() if match else ""

    def _extract(self, document_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        markdown = str(payload.get("markdown", ""))
        image_bytes = list(payload.get("page_image_bytes", []))
        if not markdown.strip() and image_bytes:
            for blob in image_bytes:
                try:
                    decoded = blob.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if decoded.strip():
                    markdown = decoded
                    break
        images = []
        document_type = "debit_note" if "DEBIT NOTE" in markdown.upper() else "credit_note" if "CREDIT NOTE" in markdown.upper() else "purchase_invoice"
        total = float(self._field(markdown, "Total") or 0)
        taxable = float(self._field(markdown, "Total Taxable") or total)
        vendor = self._field(markdown, "Vendor") or "Scanned vendor"
        gstin = self._field(markdown, "GSTIN")
        invoice_no = self._field(markdown, "Inv No", "Invoice No", "DN No", "CN No")
        date = self._field(markdown, "Date")
        rows = [line for line in markdown.splitlines() if line.strip().startswith("|")]
        line_items = []
        tax_totals = {"cgst": 0.0, "sgst": 0.0, "igst": 0.0}
        if rows:
            headers = [cell.strip().lower() for cell in rows[0].strip("|").split("|")]
            for row in rows[2:]:
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                values = dict(zip(headers, cells))
                def number(name):
                    try:
                        return float(values.get(name, "") or 0)
                    except ValueError:
                        return 0.0
                item_taxable = number("taxable")
                taxes = sum(number(name) for name in tax_totals)
                for name in tax_totals:
                    tax_totals[name] += number(name)
                line_items.append({"desc": values.get("item", ""), "hsn_sac": values.get("hsn", ""),
                    "qty": number("qty"), "rate": number("rate"), "taxable": item_taxable,
                    "tax_rate": round(taxes / item_taxable * 100, 2) if item_taxable and taxes else 0,
                    "tax_amount": taxes})
        return {"document_id": document_id, "document_type": document_type,
            "vendor": {"name": vendor, "gstin": gstin, "pan": gstin[2:12] if len(gstin) >= 12 else "", "state": ""},
            "invoice_no": invoice_no, "invoice_date": date, "taxable": taxable, "total": total,
            "cgst": tax_totals["cgst"], "sgst": tax_totals["sgst"], "igst": tax_totals["igst"],
            "line_items": line_items, "page_image_paths": images,
            "confidences": {"invoice_no": .9 if invoice_no else 0, "date": .9 if date else 0,
                "gstin": .9 if gstin else .2, "total": .9 if total else .2}}

    @staticmethod
    def _map(payload: dict[str, Any]) -> dict[str, Any]:
        invoice = payload["invoice"]
        vendor = invoice.get("vendor", {})
        known = any(chunk.get("chunk_type") in {"vendor", "memory", "posted_bill"} for chunk in payload.get("chunks", []))
        party = vendor.get("name") or "Unknown Vendor"
        return {"document_id": invoice["document_id"], "date": invoice.get("invoice_date", ""),
            "voucher_type": "purchase" if invoice.get("document_type") == "purchase_invoice" else invoice.get("document_type", "purchase"),
            "party_ledger": party if known else f"{party} (Proposed)", "party_create_proposal": not known,
            "lines": [{"ledger": "Purchases", "amount": invoice.get("taxable", invoice.get("total", 0)), "is_debit": True}],
            "gst": {"treatment": "inter" if invoice.get("igst", 0) else "intra", "input_ledgers": [], "tax_breakup": {}},
            "tds": {"applicable": False, "amount": 0},
            "bill_wise": {"ref_type": "new_ref", "ref": invoice.get("invoice_no", ""), "due_date": invoice.get("due_date")},
            "narration": f"Being purchase from {party}", "attachment_document_id": invoice["document_id"],
            "reasons": ["Deterministic mock mapping using tenant retrieval context"]}

    @staticmethod
    def _risk(document_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        text = " ".join([json.dumps(payload.get("invoice", {})), json.dumps(payload.get("draft", {}))]).lower()
        suspicious = any(term in text for term in ("urgent transfer", "new bank account", "confidential payment"))
        marks = []
        if suspicious:
            marks.append({"field": "narration", "check": "suspicious_language", "ok": False,
                          "note": "Potential social-engineering language requires human review"})
        return {"document_id": document_id, "score": 30 if suspicious else 0,
                "band": "medium" if suspicious else "low", "marks": marks}
