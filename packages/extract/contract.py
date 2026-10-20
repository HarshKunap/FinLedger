"""Extraction contract for Person 2 extract-worker integration (library boundary)."""

from __future__ import annotations

from typing import Any

from contracts.schemas import CanonicalInvoice
from extract.failures import ExtractFailure, FAILURE_CATEGORIES
from extract.vision_fallback import ExtractionDispatcher
from extract.extract_worker import ExtractWorker

__all__ = ["ExtractFailure", "FAILURE_CATEGORIES", "extract_document"]


def extract_document(
    dispatcher: ExtractionDispatcher,
    *,
    client_id: str,
    document_id: str,
    document_class: str,
    markdown: str = "",
    page_image_paths: list[str] | None = None,
    page_image_bytes: list[bytes] | None = None,
) -> dict[str, Any]:
    """Return CanonicalInvoice JSON or raise ExtractFailure with a stable category string."""
    invoice = dispatcher.extract(
        client_id,
        document_id,
        markdown,
        page_image_paths=page_image_paths,
        page_image_bytes=page_image_bytes,
        document_class=document_class,
    )
    return invoice.model_dump(mode="json")
