"""The ONLY interface OpenClaw (or any LLM agent) may use. A fixed allowlist of job names; no arguments,
no free text, no shell. Anything else is rejected and logged. Agents cannot place orders or resume trading;
they may only trigger the emergency HALT (safe direction)."""
from __future__ import annotations

import re
from enum import Enum

from .ledger import HALTED, Ledger


class Job(str, Enum):
    RUN_DAILY = "run_daily"
    GENERATE_REPORT = "generate_report"
    STATUS = "status"
    EMERGENCY_HALT = "emergency_halt"


_NAME = re.compile(r"^[a-z_]{1,32}$")


class JobRejected(Exception):
    pass


def parse_job(raw: str, ledger: Ledger | None = None) -> Job:
    text = raw if isinstance(raw, str) else repr(raw)
    if not _NAME.fullmatch(text) or text not in {j.value for j in Job}:
        if ledger is not None:
            ledger.event("WARN", "JOB_REJECTED", f"Rejected agent job request: {text[:80]!r}")
        raise JobRejected(f"Job not in allowlist: {text[:80]!r}")
    return Job(text)


def emergency_halt(ledger: Ledger, who: str) -> None:
    ledger.set_state(HALTED, f"EMERGENCY STOP by {who}", "CRITICAL")
