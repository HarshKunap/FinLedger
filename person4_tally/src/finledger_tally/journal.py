"""Durable, append-only posting journal on the agent machine, one folder per document.

Why it exists: person2's ``posting_attempts`` row has only ``pending | posted | failed`` and no raw-response column, so
it cannot record "an Import may have reached Tally". This journal is that record. It is written and fsync'ed BEFORE an
Import is sent, so a crash, a timeout or a re-delivered job can never lead to a blind resend. It is local state of the
site agent, not a platform persistence system; docs/CONTRACT_GAPS.md lists the P2 fields that would replace it.

Layout: ``<root>/<document_id>/journal.jsonl`` (one JSON event per line) and ``<root>/<document_id>/<seq>_<kind>.xml``
(exact request/response bytes); ``<root>/.locks/<document_id>.lock`` is the per-document OS lock. Files are never
rewritten. The journal fails closed: reading it raises ``JournalCorrupt`` (and posting for that document blocks) when
the file or its folder cannot be read, a line is torn, is not exactly what ``append`` writes (fields, types, canonical
JSON, consecutive ``seq``), names an unknown phase, records an unblocking event (posted / reconciled / human_cleared)
without its evidence, or when a saved request/response file is newer than the journal (lines were lost). An edit that
keeps a line well-formed cannot be detected: the journal is not tamper-proof against someone who rewrites it on purpose.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .identity import _CANONICAL_UUID

_SAVED_FILE = re.compile(r"([0-9]{1,9})_(?:request|response)\.xml")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class JournalCorrupt(RuntimeError):
    """The journal for a document cannot be read completely. Posting for it is blocked until a person checks it."""


class DocumentBusy(RuntimeError):
    """Another post, reconciliation or clearance of this document holds its lock. Nothing was read, sent or written."""


class Phase(str, Enum):
    """Journal event kinds. Names follow the conceptual post lifecycle; P3/P2 only know approved/posted/exception."""

    VALIDATION_REJECTED = "validation_rejected"  # never sent
    PREVIEW_READY = "preview_ready"
    OFFLINE_ARTIFACT_ISSUED = "offline_artifact_issued"  # manual-import XML handed out: live send blocked
    SEND_STARTED = "send_started"  # durable BEFORE the Import leaves
    SEND_NOT_DELIVERED = "send_not_delivered"  # connection never opened / refused before sending: nothing reached Tally
    RESPONSE_RECEIVED = "response_received"
    TALLY_REJECTED = "tally_rejected"  # reply says not created AND a lookup did not see it
    POSTED = "posted"  # reply says created AND exactly one matching voucher found
    OUTCOME_UNCERTAIN = "outcome_uncertain"  # anything else after a send: never resend
    RECONCILIATION_REQUIRED = "reconciliation_required"  # a voucher with this marker exists without a confirmed post
    RECONCILED = "reconciled"  # a later read-only lookup identified exactly one matching voucher
    HUMAN_CLEARED = "human_cleared"  # a person attested the uncertain/offline attempt left nothing in Tally


@dataclass(frozen=True)
class JournalEvent:
    seq: int
    at: str
    phase: str
    revision: int
    idempotency_key: str
    job_hash: str | None = None  # P3's posting_attempts.request_hash for this job
    request_sha256: str | None = None  # sha256 of the Import XML
    response_sha256: str | None = None
    erp_id: str | None = None
    master_id: int | None = None
    alter_id: int | None = None
    reference: str | None = None
    party_amount: str | None = None
    detail: str = ""
    actor: str | None = None


_FIELDS = frozenset(f.name for f in fields(JournalEvent))
_TEXT = ("at", "phase", "idempotency_key", "detail")
_OPTIONAL_TEXT = ("job_hash", "erp_id", "reference", "party_amount", "actor")
_OPTIONAL_DIGEST = ("request_sha256", "response_sha256")
_OPTIONAL_NUMBER = ("master_id", "alter_id")
# Events that unblock or close a document must carry what justifies them (written by posting.py, never without it).
_EVIDENCE = {Phase.POSTED: "erp_id", Phase.RECONCILED: "erp_id", Phase.HUMAN_CLEARED: "actor"}


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _line_event(line: str, n: int) -> JournalEvent:
    """Line ``n`` as an event, or ValueError: it must be exactly what ``append`` writes."""
    raw = json.loads(line)
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        raise ValueError(f"line {n} does not hold exactly the journal fields")
    if json.dumps(raw, sort_keys=True, separators=(",", ":")) != line:
        raise ValueError(f"line {n} is not in the form this journal writes (edited or reformatted)")
    if not _whole(raw["seq"]) or raw["seq"] != n:
        raise ValueError(f"line {n} has seq {raw['seq']!r}")
    if not _whole(raw["revision"]) or raw["revision"] < 1:
        raise ValueError(f"line {n} has revision {raw['revision']!r}")
    bad = [k for k in _TEXT if not isinstance(raw[k], str)]
    bad += [k for k in _OPTIONAL_TEXT if raw[k] is not None and not isinstance(raw[k], str)]
    bad += [k for k in _OPTIONAL_DIGEST if raw[k] is not None
            and not (isinstance(raw[k], str) and _SHA256.fullmatch(raw[k]))]
    bad += [k for k in _OPTIONAL_NUMBER if raw[k] is not None and not _whole(raw[k])]
    if bad:
        raise ValueError(f"line {n} has fields of the wrong type: {sorted(bad)}")
    phase = Phase(raw["phase"])
    needed = _EVIDENCE.get(phase)
    if needed and not (raw[needed] or "").strip():
        raise ValueError(f"line {n} records {phase.value} without {needed}")
    return JournalEvent(**raw)


class FileJournal:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _dir(self, document_id: str) -> Path:
        if not isinstance(document_id, str) or not _CANONICAL_UUID.fullmatch(document_id):
            raise ValueError("document_id must be a canonical lowercase UUID")
        return self.root / document_id

    def events(self, document_id: str) -> list[JournalEvent]:
        """Every event, in order. Only a document with no folder or no journal file and no saved files is ``[]``;
        anything that cannot be read completely raises ``JournalCorrupt`` (never an empty or partial list)."""
        folder = self._dir(document_id)
        try:
            data = (folder / "journal.jsonl").read_bytes()
        except FileNotFoundError:
            data = b""
        except OSError as exc:  # unreadable file or folder (permissions, a file where the folder should be, ...)
            raise JournalCorrupt(f"journal for {document_id} cannot be read: {type(exc).__name__}") from None
        try:
            if data and not data.endswith(b"\n"):
                raise ValueError("last line is incomplete (torn write)")
            lines = data.decode("utf-8").split("\n")[:-1]
            events = [_line_event(line, n) for n, line in enumerate(lines, 1)]
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError) as exc:
            raise JournalCorrupt(f"journal for {document_id} is unreadable: {exc}") from None
        newest = self._newest_saved(folder, document_id)
        if newest > len(events) + 1:  # each file is saved when the journal holds seq-1 lines, and lines only grow
            raise JournalCorrupt(f"journal for {document_id} has {len(events)} lines but a request/response file "
                                 f"was saved at seq {newest}: journal lines are missing")
        return events

    @staticmethod
    def _newest_saved(folder: Path, document_id: str) -> int:
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise JournalCorrupt(f"journal folder for {document_id} cannot be listed: {type(exc).__name__}") from None
        return max((int(m.group(1)) for m in map(_SAVED_FILE.fullmatch, names) if m), default=0)

    def append(self, document_id: str, phase: Phase, *, revision: int, idempotency_key: str,
               **fields: Any) -> JournalEvent:
        folder = self._dir(document_id)
        new_folder = not folder.exists()
        private_dir(folder)
        if new_folder:
            _fsync_dir(folder.parent)
        seq = len(self.events(document_id)) + 1
        event = JournalEvent(seq=seq, at=datetime.now(timezone.utc).isoformat(timespec="seconds"), phase=phase.value,
                             revision=revision, idempotency_key=idempotency_key, **fields)
        line = (json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(folder / "journal.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            _write_all(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        if new_folder:
            _fsync_dir(folder)
        return event

    def save_bytes(self, document_id: str, name: str, data: bytes) -> tuple[Path, str]:
        """Write exact bytes once (exclusive create, fsync). Returns the path and the sha256 of ``data``.

        A write that fails part-way (disk full) may leave a partial file; it is evidence only if a journal event
        records its sha256, which never happens for a failed save."""
        folder = self._dir(document_id)
        private_dir(folder)
        path = folder / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.read_bytes() != data:  # a crash after saving but before the journal line: same bytes are fine
                raise
            return path, hashlib.sha256(data).hexdigest()
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(folder)
        return path, hashlib.sha256(data).hexdigest()

    def next_seq(self, document_id: str) -> int:
        return len(self.events(document_id)) + 1

    def lock(self, document_id: str) -> "DocumentLock":
        """Take the document's exclusive OS lock (released by the OS if the process dies). Raises ``DocumentBusy``
        when another holder has it, ``OSError`` when the lock file cannot be created."""
        self._dir(document_id)
        folder = self.root / ".locks"
        private_dir(folder)
        fd = os.open(folder / f"{document_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            raise DocumentBusy(f"another post, reconciliation or clearance of document {document_id} is running; "
                               "nothing was done") from None
        return DocumentLock(fd)


class DocumentLock:
    """A held document lock; release() (or leaving a ``with`` block) frees it."""

    def __init__(self, fd: int):
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the lock on both platforms
            self._fd = None

    def __enter__(self) -> "DocumentLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def private_dir(path: Path | str) -> Path:
    """Create ``path`` and every missing parent with mode 0o700 (owner only, before the umask). Folders that already
    exist keep their mode: a state folder placed inside an existing shared folder never changes that folder."""
    path = Path(path)
    missing = []
    for folder in (path, *path.parents):
        if folder.is_dir():
            break
        missing.append(folder)
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)
    return path


def _lock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("write made no progress")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return  # directories cannot be opened for fsync on Windows; NTFS journals metadata itself
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Gate:
    """What the journal allows next for one document."""

    status: str  # clear | posted | rejected | blocked
    reason: str
    posted: JournalEvent | None = None
    rejected: JournalEvent | None = None
    unresolved: tuple[JournalEvent, ...] = ()  # sends / offline files whose outcome is not settled, oldest first
    # sends / offline files that only a person's HUMAN_CLEARED settled, oldest first. A clearance is an attestation,
    # not proof: reconcile may still match one of these to a voucher that did reach Tally.
    cleared: tuple[JournalEvent, ...] = ()


def gate(events: list[JournalEvent]) -> Gate:
    """Replay the journal. Only ``clear`` (and ``rejected`` for a newer revision) may lead to a send.

    Every SEND_STARTED and OFFLINE_ARTIFACT_ISSUED stays unresolved until exactly one of these settles it:
    - SEND_NOT_DELIVERED as the very next event after that SEND_STARTED (nothing left this machine);
    - TALLY_REJECTED right after that SEND_STARTED's own RESPONSE_RECEIVED, for the same revision and request;
    - POSTED / RECONCILED (final: nothing after it reopens the document);
    - HUMAN_CLEARED (a person attests nothing reached Tally; it also lifts every other block). Those sends are kept
      in ``Gate.cleared`` so a voucher that did reach Tally can still be reconciled to them.
    A TALLY_REJECTED that answers no send in flight is a doubt AND keeps its revision refused, even after a clearance:
    a person may clear the doubt (the sends it hides may then be reconciled or a NEWER revision sent), but a clearance
    never lets a revision that a rejection names be sent again (``Gate.rejected`` survives it).
    OUTCOME_UNCERTAIN, RECONCILIATION_REQUIRED, and a reply or rejection that answers no send in flight block until
    a person clears them. SEND_NOT_DELIVERED anywhere else proves nothing and changes nothing. While anything is
    unresolved or in doubt the status is ``blocked``, whatever came later.
    """
    posted = rejected = None
    unresolved: list[tuple[JournalEvent, str]] = []
    doubts: list[tuple[JournalEvent, str]] = []
    settled_by_person: list[JournalEvent] = []
    cleared = "no send recorded"
    in_flight: JournalEvent | None = None  # the SEND_STARTED that every later event so far merely answers
    replied = False
    for ev in events:
        phase = Phase(ev.phase)
        if posted is not None:
            continue  # nothing un-posts a voucher; a later event never reopens the document
        flight, was_replied = in_flight, replied
        in_flight, replied = None, False
        if phase is Phase.SEND_STARTED:
            unresolved.append((ev, f"r{ev.revision} send started with no recorded outcome; reconcile first"))
            in_flight = ev
        elif phase is Phase.OFFLINE_ARTIFACT_ISSUED:
            unresolved.append((ev, f"r{ev.revision} offline XML was issued for manual import; a person must "
                                   "confirm it was not imported before any live send"))
        elif phase is Phase.SEND_NOT_DELIVERED:
            if flight is not None and not was_replied:
                unresolved = [u for u in unresolved if u[0] is not flight]
                cleared = "previous send never reached Tally"
        elif phase is Phase.RESPONSE_RECEIVED:
            if flight is not None and not was_replied:
                in_flight, replied = flight, True
            else:
                doubts.append((ev, f"r{ev.revision} a reply is recorded with no send in flight; a person must "
                                   "check the journal"))
        elif phase is Phase.TALLY_REJECTED:
            if flight is not None and was_replied and (ev.revision, ev.request_sha256) == (flight.revision,
                                                                                           flight.request_sha256):
                unresolved = [u for u in unresolved if u[0] is not flight]
            else:
                doubts.append((ev, f"r{ev.revision} a rejection is recorded that answers no send in flight; a person "
                                   "must check the journal"))
            if rejected is None or ev.revision >= rejected.revision:  # the highest rejected revision stays refused
                rejected = ev
        elif phase in (Phase.OUTCOME_UNCERTAIN, Phase.RECONCILIATION_REQUIRED):
            doubts.append((ev, f"r{ev.revision} {phase.value}: {ev.detail}"))
        elif phase in (Phase.POSTED, Phase.RECONCILED):
            posted = ev
        elif phase is Phase.HUMAN_CLEARED and (unresolved or doubts):
            settled_by_person.extend(e for e, _ in unresolved)
            unresolved, doubts = [], []
            cleared = f"cleared by {ev.actor}: {ev.detail}"
        # PREVIEW_READY, VALIDATION_REJECTED and a HUMAN_CLEARED with nothing to clear change nothing
    if posted is not None:
        return Gate("posted", f"posted as {posted.erp_id} (r{posted.revision})", posted, rejected)
    pending = tuple(e for e, _ in unresolved)
    if unresolved or doubts:
        _, reason = max(unresolved + doubts, key=lambda item: item[0].seq)
        return Gate("blocked", reason, None, rejected, pending, tuple(settled_by_person))
    if rejected is not None:
        return Gate("rejected", f"r{rejected.revision} rejected by Tally: {rejected.detail}", None, rejected,
                    cleared=tuple(settled_by_person))
    return Gate("clear", cleared, cleared=tuple(settled_by_person))
