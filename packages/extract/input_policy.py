import os
import re

# Lines dropped before any provider call (GSTIN and invoice table rows are kept).
_PII_LINE_PREFIXES = (
    "contact",
    "email",
    "e-mail",
    "phone",
    "mobile",
    "tel",
    "bank",
    "account",
    "ifsc",
    "aadhaar",
    "swift",
    "iban",
    "beneficiary",
)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-()]{7,}\d)")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_CANARY_RE = re.compile(r"FINLEDGER_PII_CANARY_[A-Za-z0-9]+")
_INVOICE_FIELD_PREFIXES = (
    "vendor",
    "gstin",
    "date",
    "inv no",
    "invoice no",
    "dn no",
    "cn no",
    "total taxable",
    "total",
)

DEFAULT_MAX_IMAGE_BYTES = int(os.getenv("FINLEDGER_VISION_MAX_IMAGE_BYTES", "4194304"))


def _is_invoice_structure_line(stripped: str) -> bool:
    if stripped.startswith("|") or stripped.startswith("#"):
        return True
    lower = stripped.lower()
    return any(lower.startswith(f"{prefix}:") for prefix in _INVOICE_FIELD_PREFIXES)


def minimize_invoice_markdown(markdown: str) -> str:
    kept: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if _CANARY_RE.search(stripped):
            continue
        lower = stripped.lower()
        if any(lower.startswith(f"{prefix}:") or lower.startswith(f"{prefix} ") for prefix in _PII_LINE_PREFIXES):
            continue
        if not _is_invoice_structure_line(stripped):
            if _EMAIL_RE.search(stripped) or _PHONE_RE.search(stripped) or _URL_RE.search(stripped):
                continue
        kept.append(line)
    return "\n".join(kept)


def validate_page_image_bytes(page_image_bytes: list[bytes], max_bytes: int) -> None:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    for index, blob in enumerate(page_image_bytes):
        if len(blob) > max_bytes:
            raise ValueError(f"page image {index} exceeds byte cap ({len(blob)} > {max_bytes})")
