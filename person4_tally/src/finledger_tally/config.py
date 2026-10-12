"""Site-agent settings. The Tally host is always 127.0.0.1; only the port is configurable. Posting is off by default.

Environment (all optional except where a feature needs them):
- ``FINLEDGER_TALLY_PORT`` (default 9000), ``FINLEDGER_TALLY_TIMEOUT`` seconds (default 30),
  ``FINLEDGER_TALLY_MAX_REPLY_BYTES`` (default 64 MiB)
- ``FINLEDGER_TALLY_CLIENT_ID``: the FinLedger client this agent is paired with (UUID)
- ``FINLEDGER_TALLY_COMPANY``: the exact Tally company name (person2 ``erp_links.company_name``)
- ``FINLEDGER_TALLY_COMPANY_GUID``: that company's GUID, read from ``health()`` at pairing. Required for posting:
  a company name alone is not a stable identity. (person2 ``erp_links`` has no column for it yet.)
- ``FINLEDGER_TALLY_STATE_DIR``: journal, raw responses, offline XML, masters cache (default
  ``./finledger_tally_state``, relative to the working directory). The journal is the resend guard, so a posting
  agent must use an ABSOLUTE path: a relative one silently becomes a different, empty journal when a service starts
  in another working directory.
- ``FINLEDGER_TALLY_POSTING``: exactly ``enabled`` turns live posting on. Anything else, or unset, keeps it off.
- ``FINLEDGER_TALLY_OFFLINE_XML``: exactly ``enabled`` lets ``post_approved`` write a manual-import XML when Tally is
  down. Off by default because an issued file blocks live posting for that document until a person clears it.

There is deliberately no host or URL setting. Numbers are plain ASCII digits (``int``/``float`` would also take
``9_000``, other scripts' digits or ``nan``).

``posting_problems()`` is the one rule for a posting deployment (posting enabled => paired client, company name and
GUID, and an absolute state folder). ``AgentHost`` refuses to be built, the CLI exits 1 and ``TallyConnector``'s post
methods refuse (nothing sent) while it reports anything. ``from_env`` itself stays lenient so a value object can be
built and shown (``python -m finledger_tally config``) before it is fixed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .company import check_guid, check_name
from .identity import _CANONICAL_UUID
from .transport import DEFAULT_MAX_REPLY, DEFAULT_PORT, DEFAULT_TIMEOUT, TallyTransport

_INTEGER = re.compile(r"[0-9]{1,12}")
_DECIMAL = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6})?")


@dataclass(frozen=True)
class AgentSettings:
    client_id: str | None = None
    company_name: str | None = None
    company_guid: str | None = None
    port: int = DEFAULT_PORT
    timeout: float = DEFAULT_TIMEOUT
    max_reply_bytes: int = DEFAULT_MAX_REPLY
    state_dir: Path = field(default_factory=lambda: Path("finledger_tally_state"))
    posting_enabled: bool = False
    offline_xml_enabled: bool = False

    def __post_init__(self):
        if self.client_id is not None and not _CANONICAL_UUID.fullmatch(self.client_id):
            raise ValueError("client_id must be a canonical lowercase UUID")
        if self.company_name is not None:
            try:
                check_name(self.company_name, "company name")
            except ValueError:  # check_name quotes the value; the name is client data and must not reach a log
                raise ValueError("company name (FINLEDGER_TALLY_COMPANY) is empty, padded, or holds control "
                                 "characters") from None
        check_guid(self.company_guid)
        for flag in ("posting_enabled", "offline_xml_enabled"):
            if getattr(self, flag) is not True and getattr(self, flag) is not False:
                raise ValueError(f"{flag} must be True or False")
        if not isinstance(self.state_dir, (str, os.PathLike)) or "\x00" in str(self.state_dir):
            raise ValueError("state_dir must be a path")
        self.transport()  # validates port, timeout, reply bound

    def posting_problems(self) -> tuple[tuple[str, str], ...]:
        """Why this configuration must not run a posting agent, as (code, message) pairs; empty when posting is off.

        Pure (no I/O): it does not create or check the folder, only that its path cannot depend on the working
        directory."""
        if not self.posting_enabled:
            return ()
        problems = []
        if not (self.client_id and self.company_name and self.company_guid):
            problems.append(("not_paired", "posting is enabled but FINLEDGER_TALLY_CLIENT_ID, FINLEDGER_TALLY_COMPANY "
                                           "and FINLEDGER_TALLY_COMPANY_GUID are not all set"))
        if not Path(self.state_dir).is_absolute():
            problems.append(("state_dir_not_absolute", "posting is enabled but FINLEDGER_TALLY_STATE_DIR is not an "
                                                       "absolute path; the resend journal would depend on the working "
                                                       "directory"))
        return tuple(problems)

    def transport(self) -> TallyTransport:
        return TallyTransport(port=self.port, timeout=self.timeout, max_reply_bytes=self.max_reply_bytes)

    @property
    def journal_dir(self) -> Path:
        return Path(self.state_dir) / "journal"

    @property
    def offline_dir(self) -> Path:
        return Path(self.state_dir) / "offline_xml"

    @property
    def masters_cache(self) -> Path:
        return Path(self.state_dir) / "masters_cache.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AgentSettings":
        """Settings from ``env`` (default ``os.environ``). ``FINLEDGER_TALLY_STATE_DIR`` is kept exactly as given and
        is NOT resolved against the working directory: resolving it here would turn a relative path into an absolute
        one that still depends on where the process happened to start, hiding the ``state_dir_not_absolute`` problem
        that ``posting_problems()`` and ``check_authorization`` report. ``redacted()`` shows whether it is absolute."""
        env = os.environ if env is None else env

        def num(key, default, kind):
            raw = env.get(key)
            if raw is None or raw == "":
                return default
            if not isinstance(raw, str) or not (_INTEGER if kind is int else _DECIMAL).fullmatch(raw):
                raise ValueError(f"{key} must be a plain number")
            return kind(raw)

        return cls(
            client_id=env.get("FINLEDGER_TALLY_CLIENT_ID") or None,
            company_name=env.get("FINLEDGER_TALLY_COMPANY") or None,
            company_guid=env.get("FINLEDGER_TALLY_COMPANY_GUID") or None,
            port=num("FINLEDGER_TALLY_PORT", DEFAULT_PORT, int),
            timeout=num("FINLEDGER_TALLY_TIMEOUT", DEFAULT_TIMEOUT, float),
            max_reply_bytes=num("FINLEDGER_TALLY_MAX_REPLY_BYTES", DEFAULT_MAX_REPLY, int),
            state_dir=Path(env.get("FINLEDGER_TALLY_STATE_DIR") or "finledger_tally_state"),
            posting_enabled=env.get("FINLEDGER_TALLY_POSTING") == "enabled",
            offline_xml_enabled=env.get("FINLEDGER_TALLY_OFFLINE_XML") == "enabled",
        )

    def redacted(self) -> dict:
        """Settings safe to log: no company name (client data), only whether identity fields are set. The client id is
        kept: it is the platform's identifier of the pairing, not a secret, and support needs it. The state folder is
        reported only as absolute or not (its path can hold a Windows user name)."""
        return {"client_id": self.client_id, "company_name_set": bool(self.company_name),
                "company_guid_set": bool(self.company_guid), "port": self.port, "timeout": self.timeout,
                "max_reply_bytes": self.max_reply_bytes, "state_dir_absolute": Path(self.state_dir).is_absolute(),
                "posting_enabled": self.posting_enabled, "offline_xml_enabled": self.offline_xml_enabled}
