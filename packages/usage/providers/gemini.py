from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from usage.kill_switch import KillSwitch
from usage.provider import ProviderResponse, _tokens
from usage.provider_errors import ProviderTransientError
from usage.providers.transport import GeminiTransport, TransportError


@dataclass(frozen=True)
class GeminiSettings:
    model: str
    location: str
    max_request_bytes: int = 4_194_304
    timeout_seconds: float = 30.0
    max_retries: int = 2


class GeminiProvider:
    """Approved-provider adapter behind complete(...). Requires an explicit transport (fake in CI)."""

    def __init__(
        self,
        settings: GeminiSettings,
        transport: GeminiTransport,
        kill_switch: KillSwitch | None = None,
    ):
        if not settings.model or not settings.location:
            raise ValueError("model and location are required for the gemini provider")
        self.settings = settings
        self.transport = transport
        self.kill_switch = kill_switch or KillSwitch()

    def complete(
        self,
        purpose: str,
        model: str,
        client_id: str,
        document_id: str,
        payload: dict[str, Any],
    ) -> ProviderResponse:
        self._enforce_payload_bounds(payload)
        serialized = json.dumps(payload, sort_keys=True, default=str)
        tokens_in = _tokens(serialized)
        estimated_tokens = tokens_in + 256
        reservation = self.kill_switch.reserve(client_id, document_id, estimated_tokens)
        try:
            text, tokens_in, tokens_out, latency_ms = self._call_with_retries(
                purpose=purpose, model=model, payload=payload, tokens_in=tokens_in
            )
            self.kill_switch.reconcile(reservation, tokens_in + tokens_out)
        except Exception:
            self.kill_switch.release(reservation)
            raise
        cost_usd = 0.0
        return ProviderResponse(text, tokens_in, tokens_out, latency_ms, cost_usd)

    def _enforce_payload_bounds(self, payload: dict[str, Any]) -> None:
        size = len(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
        if size > self.settings.max_request_bytes:
            raise TransportError("payload_too_large", "payload_too_large")
        for blob in payload.get("page_image_bytes", []) or []:
            if len(blob) > self.settings.max_request_bytes:
                raise TransportError("payload_too_large", "payload_too_large")

    def _call_with_retries(self, *, purpose: str, model: str, payload: dict[str, Any], tokens_in: int) -> tuple[str, int, int, int]:
        attempts = 0
        last_error: Exception | None = None
        while attempts <= self.settings.max_retries:
            attempts += 1
            try:
                started = time.perf_counter()
                response = self.transport.generate(
                    model=model or self.settings.model,
                    location=self.settings.location,
                    purpose=purpose,
                    payload=payload,
                )
                latency_ms = response.latency_ms or int((time.perf_counter() - started) * 1000)
                return response.text, response.tokens_in or tokens_in, response.tokens_out, latency_ms
            except TransportError as exc:
                last_error = exc
                if exc.code not in {"timeout", "http_429", "http_5xx"}:
                    raise
                if attempts > self.settings.max_retries:
                    raise ProviderTransientError()
        assert last_error is not None
        if last_error.code in {"timeout", "http_429", "http_5xx"}:
            raise ProviderTransientError()
        raise last_error
