from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class TransportError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class TransportResponse:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int = 0


class GeminiTransport(Protocol):
    def generate(
        self,
        *,
        model: str,
        location: str,
        purpose: str,
        payload: dict[str, Any],
    ) -> TransportResponse: ...


class FakeGeminiTransport:
    """In-process transport for contract tests (no network)."""

    def __init__(self, responses: list[Any] | None = None):
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def push(self, item: Any) -> None:
        self._responses.append(item)

    def generate(self, *, model: str, location: str, purpose: str, payload: dict[str, Any]) -> TransportResponse:
        self.calls.append({"model": model, "location": location, "purpose": purpose, "payload": payload})
        if not self._responses:
            raise TransportError("empty_queue", "no fake response queued")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, TransportResponse):
            return item
        if isinstance(item, str):
            return TransportResponse(item, 10, 10, 1)
        raise TypeError(f"unsupported fake response: {type(item)!r}")
