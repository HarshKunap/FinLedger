import json

import pytest

from usage.kill_switch import KillSwitch
from usage.model_router import ModelRouter, Settings
from usage.provider_errors import ProviderTransientError
from usage.providers.gemini import GeminiProvider, GeminiSettings
from usage.providers.transport import FakeGeminiTransport, TransportError, TransportResponse

CANARY = "FINLEDGER_PII_CANARY_9f3c2a1b"


def _provider(transport: FakeGeminiTransport) -> GeminiProvider:
    return GeminiProvider(
        GeminiSettings(model="gemini-1.5-flash", location="asia-south1", max_request_bytes=128, max_retries=1),
        transport,
        KillSwitch(limit=50_000, enabled=True),
    )


def test_fake_transport_success_without_network():
    transport = FakeGeminiTransport([TransportResponse('{"ok":true}', 3, 4, 2)])
    response = _provider(transport).complete("extract", "gemini-1.5-flash", "c1", "d1", {"markdown": "Invoice No: 1\nTotal: 1"})
    assert json.loads(response.text)["ok"] is True
    assert CANARY not in json.dumps(transport.calls)


@pytest.mark.parametrize("code", ["timeout", "http_429", "http_5xx"])
def test_transient_errors_retry_then_provider_transient(code):
    transport = FakeGeminiTransport([TransportError(code), TransportError(code)])
    with pytest.raises(ProviderTransientError):
        _provider(transport).complete("extract", "gemini-1.5-flash", "c1", "d1", {"markdown": "x"})
    assert len(transport.calls) == 2


def test_malformed_json_response_is_returned_as_text():
    transport = FakeGeminiTransport([TransportResponse("not-json", 1, 1)])
    response = _provider(transport).complete("extract", "gemini-1.5-flash", "c1", "d1", {"markdown": "x"})
    assert response.text == "not-json"


def test_oversize_image_rejected():
    transport = FakeGeminiTransport([TransportResponse("{}", 1, 1)])
    with pytest.raises(TransportError, match="payload_too_large"):
        _provider(
            transport
        ).complete(
            "extract_vision",
            "gemini-1.5-pro",
            "c1",
            "d1",
            {"markdown": "", "page_image_bytes": [b"x" * 200]},
        )
    assert transport.calls == []


def test_router_rejects_gemini_without_transport():
    with pytest.raises(ValueError, match="explicit transport"):
        ModelRouter(Settings(provider="gemini", gemini_location="asia-south1"))


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_gemini_provider_blocked_in_hosted_environment(monkeypatch, environment):
    monkeypatch.setenv("FINLEDGER_ENV", environment)
    with pytest.raises(RuntimeError, match="gemini LLM provider"):
        ModelRouter(Settings(provider="gemini", gemini_location="asia-south1"), gemini_transport=FakeGeminiTransport())
