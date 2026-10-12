"""The local agent's run loop, without the platform transport it will eventually poll.

Person 2 has no agent route to claim post jobs or report results yet (docs/CONTRACT_GAPS.md #10), so the host takes
two small interfaces instead: a ``JobSource`` that hands over one approved job with the P2/P3 rows that authorize it,
and a ``ResultSink`` that receives person3 ``erp_result`` keyword arguments. Both are supplied by whoever wires the
agent to the platform; tests use in-memory ones.

Rules the host keeps:
- it cannot be built for a posting configuration that ``AgentSettings.posting_problems()`` refuses (not paired, or a
  state folder that depends on the working directory);
- startup runs ``health()`` and refuses to post unless the paired company is verified;
- one job per tick, one ``post_approved`` call per job, no retry inside the host (a job that could not be sent stays
  with the source; the journal gate decides what a later claim may do); ticks are serialised, so two threads calling
  ``tick`` still handle one job at a time;
- polling is bounded (``max_ticks``, a finite ``interval`` waited after EVERY tick, errors included, so a job that
  keeps failing cannot spin) and stops promptly when ``stop`` is set;
- an exception from one job is logged with ids and its type only (messages can hold ledger names), is never
  reported as success, and does not stop the loop; ids that are not canonical are not logged at all;
- after a tick that found Tally unavailable or the company unverified, health is checked again before the next claim;
- ``run`` holds an OS lock on the state folder, so a second agent on the same state cannot start (the lock is released
  by the OS if the process dies); restart safety itself comes from the journal: a document whose send started is
  blocked until reconciled, whichever process asks.

This module is the body a Windows service would run. No service wrapper is built here (see README).
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .config import AgentSettings
from .connector import Health, HealthReport, TallyConnector
from .identity import _CANONICAL_UUID
from .posting import PostAuthorization, PostOutcome, State

log = logging.getLogger("finledger_tally.agent")


@dataclass(frozen=True)
class ClaimedJob:
    job_id: Any  # P2 jobs.id, passed back to the sink untouched
    payload: Mapping[str, Any]  # the P3 post job payload
    authorization: PostAuthorization


class JobSource(Protocol):
    def claim(self) -> ClaimedJob | None: ...


class ResultSink(Protocol):
    def report(self, claimed: ClaimedJob, result: Mapping[str, Any]) -> None: ...


@dataclass
class TickResult:
    claimed: bool
    outcome: PostOutcome | None = None
    reported: bool = False
    error: str | None = None  # bounded; may quote an exception message
    error_kind: str | None = None  # ids-free label for status(): "claim failed: ConnectionError", ...


class AgentAlreadyRunning(RuntimeError):
    """Another agent process holds the lock on this state folder."""


class InstanceLock:
    """An exclusive OS lock on ``<state_dir>/agent.lock``; released automatically if the process dies."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "agent.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows only
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise AgentAlreadyRunning(f"another FinLedger agent is using {self.path.parent}") from None
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the lock on both platforms
            self._fd = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


_RECHECK_STATES = (State.TALLY_UNAVAILABLE, State.SEND_NOT_DELIVERED)  # the second: Tally refused the connection
_ERROR_LIMIT = 300  # TickResult.error is for the caller; messages can be long or hold ledger names
_MAX_INTERVAL = 86400.0  # Event.wait overflows on huge timeouts; a day is far beyond any polling need
_NUMERIC_ID = re.compile(r"[0-9]{1,20}")  # P2 jobs.id is a bigint


def _failure(prefix: str, exc: BaseException, **kw: Any) -> "TickResult":
    kind = f"{prefix}{type(exc).__name__}"
    return TickResult(error=f"{kind}: {exc}"[:_ERROR_LIMIT], error_kind=kind, **kw)


def _log_id(value: Any) -> str:
    """An id as it may appear in a log line: a job or document id from the source is shown only when it is a number or
    a canonical UUID, never an arbitrary (possibly long, possibly client-data) value; otherwise only its type."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)[:20]
    if isinstance(value, str) and (_CANONICAL_UUID.fullmatch(value) or _NUMERIC_ID.fullmatch(value)):
        return value
    return f"<{type(value).__name__}>"


_RECHECK_PROBLEMS = ("company_unverified",)


@dataclass
class AgentHost:
    settings: AgentSettings
    source: JobSource
    sink: ResultSink
    connector: TallyConnector | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    startup_report: HealthReport | None = None
    counts: dict[str, int] = field(default_factory=dict)
    last_state: str | None = None
    last_error: str | None = None
    started_at: str | None = None
    _recheck: bool = False
    _health_error: str | None = field(default=None, repr=False)
    _tick_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.settings, AgentSettings):
            raise TypeError("settings must be AgentSettings")
        problems = self.settings.posting_problems()
        if problems:
            raise ValueError("refusing to build a posting agent: " + "; ".join(m for _, m in problems))
        self.connector = self.connector or TallyConnector(self.settings)

    def startup(self) -> HealthReport | None:
        try:
            report = self.connector.health()
        except Exception as exc:  # noqa: BLE001 - a failed health check means "not ready", never "ready"
            log.error("health check failed: %s", type(exc).__name__)
            self.startup_report, self.last_error = None, f"health check failed: {type(exc).__name__}"
            self._health_error = self.last_error
            return None
        if not isinstance(report, HealthReport):
            log.error("health check returned %s, not a HealthReport", type(report).__name__)
            self.startup_report = None
            self.last_error = self._health_error = f"health check failed: {type(report).__name__} returned"
            return None
        self.startup_report, self._health_error = report, None
        self.started_at = self.started_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._recheck = False
        log.info("agent health: tally=%s posting=%s", report.status, report.posting)
        return report

    def status(self) -> dict[str, Any]:
        """Structured status for an operator or a service manager. No company name, no invoice data, no exception
        messages (``last_error`` is a label such as ``claim failed: ConnectionError``). Counts are this process's;
        what survives a restart is the journal, not this dict."""
        r = self.startup_report
        return {"ready_to_post": self.ready_to_post, "not_ready_reason": self.not_ready_reason,
                "stopped": self.stop.is_set(), "started_at": self.started_at,
                "tally": r.status if r else None, "posting": r.posting if r else None,
                "ledger_count": r.ledger_count if r else None, "health_recheck_pending": self._recheck,
                "ticks": sum(self.counts.values()), "by_state": dict(sorted(self.counts.items())),
                "last_state": self.last_state, "last_error": self.last_error,
                "settings": self.settings.redacted()}

    @property
    def not_ready_reason(self) -> str | None:
        """None when ready to post; otherwise one stable code: not_started, health_check_failed, a ``Health`` status
        (tally_unreachable, company_ambiguous, ...), masters_unavailable, posting_disabled or posting_not_ready."""
        r = self.startup_report
        if r is None:
            return "health_check_failed" if self._health_error else "not_started"
        if r.status != Health.COMPANY_VERIFIED:
            return r.status
        if r.posting == "disabled":
            return "posting_disabled"
        if not r.read_only_available:
            return "masters_unavailable"
        return None if r.posting == "enabled" else "posting_not_ready"

    @property
    def ready_to_post(self) -> bool:
        r = self.startup_report
        return r is not None and r.status == Health.COMPANY_VERIFIED and r.posting == "enabled"

    def tick(self) -> TickResult:
        """Claim at most one job and handle it once. Never claims when not ready to post. One tick at a time."""
        with self._tick_lock:
            return self._counted(self._tick())

    def _counted(self, result: TickResult) -> TickResult:
        key = result.outcome.state if result.outcome else ("error" if result.error else
                                                           "claimed" if result.claimed else "idle")
        self.counts[key] = self.counts.get(key, 0) + 1
        self.last_state = key
        if result.error:
            self.last_error = result.error_kind or "error"  # never the message: it can name ledgers
        if result.outcome is not None and (result.outcome.state in _RECHECK_STATES or any(
                code in _RECHECK_PROBLEMS for code, _ in result.outcome.problems)):
            self._recheck = True  # Tally or the company went away: check health before claiming again
        return result

    def _tick(self) -> TickResult:
        if self._recheck:
            self.startup()
            self._recheck = not self.ready_to_post  # keep checking each tick until Tally and the company are back
        if not self.ready_to_post:
            return TickResult(claimed=False, error="not ready to post (run startup(); see health report)",
                              error_kind="not ready to post")
        try:
            claimed = self.source.claim()
        except Exception as exc:  # noqa: BLE001 - the platform being unreachable is not a reason to stop the loop
            log.error("job claim failed: %s", type(exc).__name__)
            return _failure("claim failed: ", exc, claimed=False)
        if claimed is None:
            return TickResult(claimed=False)
        if not isinstance(claimed, ClaimedJob):
            log.error("job source returned %s, not a ClaimedJob; nothing posted", type(claimed).__name__)
            kind = f"invalid claim: {type(claimed).__name__}"
            return TickResult(claimed=True, error=kind, error_kind=kind)
        job_id, doc = _log_id(claimed.job_id), "<unread>"
        try:
            doc = _log_id(claimed.payload.get("document_id") if isinstance(claimed.payload, Mapping) else None)
            outcome = self.connector.post_invoice(claimed.payload, claimed.authorization)
            if not isinstance(outcome, PostOutcome):
                raise TypeError(f"post_invoice returned {type(outcome).__name__}, not a PostOutcome")
            result = outcome.erp_result()
        except Exception as exc:  # noqa: BLE001 - logged and surfaced, never reported as success
            log.error("post failed with an unexpected %s: job=%s document=%s", type(exc).__name__, job_id, doc)
            return _failure("", exc, claimed=True)
        if result is None:
            log.info("not reported: job=%s document=%s state=%s", job_id, doc, outcome.state)
            return TickResult(claimed=True, outcome=outcome)
        try:
            self.sink.report(claimed, result)
        except Exception as exc:  # noqa: BLE001 - the journal already holds the outcome; a later report may follow
            log.error("result report failed (%s): job=%s document=%s state=%s", type(exc).__name__, job_id, doc,
                      outcome.state)
            return _failure("report failed: ", exc, claimed=True, outcome=outcome)
        return TickResult(claimed=True, outcome=outcome, reported=True)

    def run(self, *, interval: float = 30.0, max_ticks: int | None = None) -> list[TickResult]:
        """Lock the state folder, startup, then tick until ``stop`` is set or ``max_ticks`` ticks ran, waiting
        ``interval`` seconds after every tick (an error does not shorten the wait). Returns at once, having claimed
        nothing, when startup finds the agent not ready to post; the caller (a service wrapper) decides when to start
        again.

        Raises ValueError for an interval that is not a finite number of seconds in (0, 86400] or a ``max_ticks`` that
        is not a positive integer, AgentAlreadyRunning when another agent holds the state folder."""
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) \
                or not 0 < interval <= _MAX_INTERVAL:
            raise ValueError("interval must be a finite number of seconds in (0, 86400]")
        if max_ticks is not None and (isinstance(max_ticks, bool) or not isinstance(max_ticks, int) or max_ticks < 1):
            raise ValueError("max_ticks must be None or a positive integer")
        with InstanceLock(self.settings.state_dir):
            return self._run(interval, max_ticks)

    def _run(self, interval: float, max_ticks: int | None) -> list[TickResult]:
        results: list[TickResult] = []
        self.startup()
        if not self.ready_to_post:
            r = self.startup_report
            log.warning("agent not posting: tally=%s posting=%s", r.status if r else None, r.posting if r else None)
            return results
        while not self.stop.is_set() and (max_ticks is None or len(results) < max_ticks):
            results.append(self.tick())
            if self.stop.wait(interval):
                break
        log.info("agent stopped after %d ticks", len(results))
        return results


def stop_on_signals(host: AgentHost, signals: tuple[int, ...] | None = None) -> None:
    """Make Ctrl+C / SIGTERM (or a Windows service stop that raises SIGBREAK) end ``host.run`` after the current
    job: the in-flight post finishes and is journaled; nothing new is claimed. Call from the main thread."""
    import signal as _signal
    if signals is None:
        names = ("SIGINT", "SIGTERM", "SIGBREAK")
        signals = tuple(getattr(_signal, n) for n in names if hasattr(_signal, n))

    def handler(signum, frame):  # noqa: ARG001
        log.info("stop requested by signal %s", signum)
        host.stop.set()

    for sig in signals:
        _signal.signal(sig, handler)


__all__ = ["AgentAlreadyRunning", "AgentHost", "ClaimedJob", "InstanceLock", "JobSource", "ResultSink", "TickResult",
           "stop_on_signals"]
