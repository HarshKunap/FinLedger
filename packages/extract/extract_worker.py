import json
import os
import re
from pydantic import ValidationError
from contracts.schemas import CanonicalInvoice
from extract.failures import ExtractFailure, INVALID_MODEL_OUTPUT
from extract.input_policy import minimize_invoice_markdown
from usage.model_router import ModelRouter
from usage.usage_writer import UsageWriter

class ExtractWorker:
    def __init__(self, usage_writer: UsageWriter, router: ModelRouter | None = None):
        self.usage_writer = usage_writer
        self.router = router or ModelRouter()
        self.prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "extract_digital.txt")

    def _call_llm(self, model: str, prompt: str, markdown: str) -> str:
        minimized = minimize_invoice_markdown(markdown)
        response = self.router.provider.complete(
            "extract",
            model,
            self._client_id,
            self._document_id,
            {"prompt": prompt, "markdown": minimized},
        )
        self._provider_response = response
        return response.text

    def _parse_invoice_json(self, response_text: str) -> CanonicalInvoice:
        clean_json = re.sub(r'```json|```', '', response_text).strip()
        try:
            data = json.loads(clean_json)
        except json.JSONDecodeError:
            raise ExtractFailure(INVALID_MODEL_OUTPUT)
        try:
            return CanonicalInvoice(**data)
        except ValidationError:
            raise ExtractFailure(INVALID_MODEL_OUTPUT)

    def extract(self, client_id: str, document_id: str, markdown: str) -> CanonicalInvoice:
        model = self.router.resolve_model("extract")
        self._document_id = document_id
        self._client_id = client_id
        
        with open(self.prompt_path, "r") as f:
            prompt_template = f.read()
            
        response_text = self._call_llm(model, prompt_template, markdown)
        
        self.usage_writer.log_usage(
            client_id=client_id,
            doc_id=document_id,
            model=model,
            tokens_in=getattr(getattr(self, "_provider_response", None), "tokens_in", max(1, len(markdown) // 4)),
            tokens_out=getattr(getattr(self, "_provider_response", None), "tokens_out", max(1, len(response_text) // 4)),
            latency_ms=getattr(getattr(self, "_provider_response", None), "latency_ms", 1000),
            cost_usd=getattr(getattr(self, "_provider_response", None), "cost_usd", 0.0),
            purpose="extract"
        )
        
        invoice = self._parse_invoice_json(response_text)
        
        total_line_taxable = sum(item.taxable for item in invoice.line_items)
        if abs(total_line_taxable - invoice.taxable) > 0.01:
            invoice.confidences.total = 0.5
            
        return invoice
