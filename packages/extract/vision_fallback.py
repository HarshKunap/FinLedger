from contracts.schemas import CanonicalInvoice
from extract.extract_worker import ExtractWorker
from extract.failures import ExtractFailure, MISSING_EXTRACT_INPUT, VISION_PAGE_CAP
from extract.input_policy import DEFAULT_MAX_IMAGE_BYTES, validate_page_image_bytes


class ExtractionDispatcher:
    def __init__(self, worker: ExtractWorker, page_cap: int = 10, image_byte_cap: int | None = None):
        if page_cap < 1:
            raise ValueError("page_cap must be positive")
        self.worker = worker
        self.page_cap = page_cap
        self.image_byte_cap = image_byte_cap if image_byte_cap is not None else DEFAULT_MAX_IMAGE_BYTES

    def extract(
        self,
        client_id: str,
        document_id: str,
        markdown: str,
        page_image_paths: list[str] | None = None,
        page_image_bytes: list[bytes] | None = None,
    ) -> CanonicalInvoice:
        paths = list(page_image_paths or [])
        image_bytes = list(page_image_bytes or [])

        if markdown.strip():
            return self.worker.extract(client_id, document_id, markdown)

        if not image_bytes:
            raise ExtractFailure(MISSING_EXTRACT_INPUT)
        if len(image_bytes) > self.page_cap:
            raise ExtractFailure(VISION_PAGE_CAP)
        try:
            validate_page_image_bytes(image_bytes, self.image_byte_cap)
        except ValueError:
            raise ExtractFailure(VISION_PAGE_CAP)

        model = self.worker.router.resolve_model("extract_vision")
        response = self.worker.router.provider.complete(
            "extract_vision",
            model,
            client_id,
            document_id,
            {
                "prompt": "Extract the invoice from page images",
                "markdown": "",
                "page_image_bytes": image_bytes,
            },
        )
        invoice = self.worker._parse_invoice_json(response.text)
        self.worker.usage_writer.log_usage(
            client_id,
            document_id,
            model,
            response.tokens_in,
            response.tokens_out,
            response.latency_ms,
            response.cost_usd,
            "extract_vision",
        )
        if paths:
            invoice = invoice.model_copy(update={"page_image_paths": paths})
        return invoice
