import json
from unittest.mock import patch

import pytest

from extract.extract_worker import ExtractWorker
from extract.failures import (
    ExtractFailure,
    INVALID_MODEL_OUTPUT,
    MISSING_EXTRACT_INPUT,
    STATEMENT_NOT_EXTRACTED,
    VISION_PAGE_CAP,
)
from extract.vision_fallback import ExtractionDispatcher
from usage.kill_switch import KillSwitch, TokenCapExceededException
from usage.model_router import ModelRouter, Settings
from usage.provider import MockProvider, ProviderResponse
from usage.usage_writer import UsageWriter

CANARY = "FINLEDGER_PII_CANARY_9f3c2a1b"
FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"
REGRESSION_FIXTURES = __import__("pathlib").Path(__file__).parent / "regression_fixtures"


def _worker_with_captured_payload():
    captured: list[dict] = []
    router = ModelRouter(Settings(provider="mock"))
    original = router.provider.complete

    def capture(purpose, model, client_id, document_id, payload):
        captured.append({"purpose": purpose, "payload": payload})
        return original(purpose, model, client_id, document_id, payload)

    router.provider.complete = capture  # type: ignore[method-assign]
    return ExtractWorker(UsageWriter(), router), captured


@pytest.mark.parametrize(
    "response_text",
    [
        "not json at all",
        "```json\n{\"document_id\": \"x\"}\n```",
        json.dumps({"document_id": "x", "document_type": "not_a_real_type", "vendor": {"name": "V"}, "total": 1}),
    ],
)
def test_malformed_model_output_raises_invalid_category(response_text):
    worker = ExtractWorker(UsageWriter(), ModelRouter(Settings(provider="mock")))
    with patch.object(worker, "_call_llm", return_value=response_text):
        with pytest.raises(ExtractFailure) as exc:
            worker.extract("client-a", "doc-bad", "Invoice No: 1\nTotal: 10")
    assert exc.value.category == INVALID_MODEL_OUTPUT
    assert CANARY not in str(exc.value)


def test_vision_malformed_output_same_category():
    worker = ExtractWorker(UsageWriter(), ModelRouter(Settings(provider="mock")))
    dispatcher = ExtractionDispatcher(worker, page_cap=2, image_byte_cap=10_000)
    with patch.object(
        worker.router.provider,
        "complete",
        return_value=ProviderResponse("still-not-json", 1, 1),
    ):
        with pytest.raises(ExtractFailure) as exc:
            dispatcher.extract(
                "client-a",
                "doc-vision-bad",
                "",
                page_image_bytes=[b"Invoice No: V1\nTotal: 1"],
            )
    assert exc.value.category == INVALID_MODEL_OUTPUT
    assert "still-not-json" not in str(exc.value)


def test_statement_document_class_refuses_provider():
    worker = ExtractWorker(UsageWriter(), ModelRouter(Settings(provider="mock")))
    dispatcher = ExtractionDispatcher(worker, page_cap=2, image_byte_cap=10_000)
    with pytest.raises(ExtractFailure) as exc:
        dispatcher.extract(
            "client-a",
            "doc-stmt",
            "Bank statement line 1",
            document_class="statement",
        )
    assert exc.value.category == STATEMENT_NOT_EXTRACTED


def test_bounded_images_page_and_byte_caps():
    worker = ExtractWorker(UsageWriter(), ModelRouter(Settings(provider="mock")))
    dispatcher = ExtractionDispatcher(worker, page_cap=2, image_byte_cap=8)
    with pytest.raises(ExtractFailure) as exc:
        dispatcher.extract("c", "d", "", page_image_bytes=[b"a", b"b", b"c"])
    assert exc.value.category == VISION_PAGE_CAP

    with pytest.raises(ExtractFailure) as exc:
        dispatcher.extract("c", "d", "", page_image_bytes=[b"0123456789"])
    assert exc.value.category == VISION_PAGE_CAP

    with pytest.raises(ExtractFailure) as exc:
        dispatcher.extract("c", "d", "  ", page_image_bytes=[])
    assert exc.value.category == MISSING_EXTRACT_INPUT


def test_provider_payload_uses_bytes_not_paths():
    worker, captured = _worker_with_captured_payload()
    dispatcher = ExtractionDispatcher(worker, page_cap=2, image_byte_cap=10_000)
    dispatcher.extract(
        "client-a",
        "doc-bytes",
        "",
        page_image_paths=["client-a/doc/scan.png", "https://evil.example/x.png"],
        page_image_bytes=[b"Invoice No: BYTE-1\nTotal: 50"],
    )
    vision_calls = [item for item in captured if item["purpose"] == "extract_vision"]
    assert len(vision_calls) == 1
    payload = vision_calls[0]["payload"]
    assert "page_image_paths" not in payload
    assert payload["page_image_bytes"] == [b"Invoice No: BYTE-1\nTotal: 50"]
    serialized = json.dumps(payload, default=str)
    assert "scan.png" not in serialized
    assert "https://" not in serialized


def test_minimized_inputs_drop_canary_from_provider_payload():
    markdown = (REGRESSION_FIXTURES / "pii_canary.md").read_text(encoding="utf-8")
    worker, captured = _worker_with_captured_payload()
    worker.extract("client-a", "doc-canary", markdown)
    assert captured
    payload = captured[0]["payload"]
    minimized = payload["markdown"]
    assert CANARY not in minimized
    assert "priya.sharma@example.com" not in minimized
    assert "29ABCDE1234F1Z5" in minimized
    assert CANARY not in json.dumps(payload)
    writer = worker.usage_writer
    assert all(CANARY not in repr(row) for row in writer.logs)


def test_markdown_path_does_not_send_images():
    worker, captured = _worker_with_captured_payload()
    dispatcher = ExtractionDispatcher(worker, page_cap=2)
    dispatcher.extract(
        "client-a",
        "doc-digital",
        "Invoice No: ONLY-MD\nTotal: 10",
        page_image_paths=["should-not-send.png"],
        page_image_bytes=[b"ignored"],
    )
    assert len(captured) == 1
    assert captured[0]["purpose"] == "extract"
    assert "page_image_bytes" not in captured[0]["payload"]


def test_invoice_free_diagnostics_categories():
    worker = ExtractWorker(UsageWriter(), ModelRouter(Settings(provider="mock")))
    dispatcher = ExtractionDispatcher(worker, page_cap=1, image_byte_cap=4)
    rich = (REGRESSION_FIXTURES / "pii_canary.md").read_text(encoding="utf-8")

    with pytest.raises(ExtractFailure) as exc:
        dispatcher.extract("c", "d", "", page_image_bytes=[b"012345678"])
    assert exc.value.category == VISION_PAGE_CAP
    assert CANARY not in str(exc.value)

    with pytest.raises(ExtractFailure) as exc:
        dispatcher.extract("c", "d", "   ", page_image_bytes=[])
    assert exc.value.category == MISSING_EXTRACT_INPUT
    assert CANARY not in str(exc.value)

    with patch.object(worker, "_call_llm", return_value="```json\n{}\n```"):
        with pytest.raises(ExtractFailure) as exc:
            worker.extract("c", "d", rich)
    assert exc.value.category == INVALID_MODEL_OUTPUT
    assert CANARY not in str(exc.value)


def test_token_cap_exception_has_no_invoice_canary():
    kill_switch = KillSwitch(limit=50, enabled=True)
    router = ModelRouter(Settings(provider="mock", per_bill_token_cap=50, kill_switch_enabled=True))
    router.provider = MockProvider(kill_switch)
    worker = ExtractWorker(UsageWriter(), router)
    rich = (REGRESSION_FIXTURES / "pii_canary.md").read_text(encoding="utf-8")
    with pytest.raises(TokenCapExceededException) as exc:
        worker.extract("client-a", "doc-cap", rich)
    assert CANARY not in str(exc.value)


@patch("extract.extract_worker.ExtractWorker._call_llm")
def test_credit_note_fixture_document_type(mock_call_llm):
    expected = json.loads((FIXTURES / "credit_note.json").read_text(encoding="utf-8"))
    mock_call_llm.return_value = json.dumps(expected)
    invoice = ExtractWorker(UsageWriter()).extract(
        "client-a",
        "doc-cn",
        (FIXTURES / "credit_note.md").read_text(encoding="utf-8"),
    )
    assert invoice.document_type == "credit_note"
    assert invoice.invoice_no == "CN-44"
