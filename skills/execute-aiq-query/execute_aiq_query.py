"""execute_aiq_query — OpenClaw skill execution module.

Contract: docs/04_skills_contracts.md §5

Calls a locally-hosted NVIDIA A-IQ service (started with `nat serve`) via its
async REST API. Submits a deep_researcher job, polls until completion, and returns
the final report.

Runtime config (env vars override trading_constants defaults):
  AIQ_BASE_URL          — base URL of the A-IQ service  (default: http://localhost:8000)
  AIQ_POLL_INTERVAL_SEC — seconds between status polls  (default: 2.0)
  AIQ_TIMEOUT_SEC       — max seconds to wait for result (default: 120.0)
"""
from __future__ import annotations

import os
import time

import requests
from pydantic import BaseModel, ConfigDict

from config.trading_constants import (
    AIQ_BASE_URL as _DEFAULT_BASE_URL,
    AIQ_POLL_INTERVAL_SEC as _DEFAULT_POLL,
    AIQ_TIMEOUT_SEC as _DEFAULT_TIMEOUT,
)

_AGENT_TYPE = "deep_researcher"
_SUBMIT_PATH = "/v1/jobs/async/submit"
_STATUS_PATH = "/v1/jobs/async/job/{job_id}"
_REPORT_PATH = "/v1/jobs/async/job/{job_id}/report"
_REQUEST_TIMEOUT_SEC = 30

_TERMINAL_STATUSES = {"SUCCESS", "FAILURE", "INTERRUPTED"}


def _resolve_config() -> tuple[str, float, float]:
    base_url = os.environ.get("AIQ_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    try:
        poll = float(os.environ.get("AIQ_POLL_INTERVAL_SEC", _DEFAULT_POLL))
    except (TypeError, ValueError):
        poll = _DEFAULT_POLL
    try:
        timeout = float(os.environ.get("AIQ_TIMEOUT_SEC", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    return base_url, poll, timeout


class ExecuteAiqQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


class ExecuteAiqQueryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_data: str
    error: str | None


def execute_aiq_query(
    query: str,
) -> ExecuteAiqQueryOutput:
    try:
        base_url, poll_interval, timeout_sec = _resolve_config()

        # Step 1 — submit job
        submit_resp = requests.post(
            f"{base_url}{_SUBMIT_PATH}",
            json={"agent_type": _AGENT_TYPE, "input": query},
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        submit_resp.raise_for_status()
        job_id: str = submit_resp.json()["job_id"]

        # Step 2 — poll until terminal state or timeout
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            status_resp = requests.get(
                f"{base_url}{_STATUS_PATH.format(job_id=job_id)}",
                timeout=_REQUEST_TIMEOUT_SEC,
            )
            status_resp.raise_for_status()
            status_body = status_resp.json()
            status: str = status_body.get("status", "")

            if status == "SUCCESS":
                # Step 3 — fetch final report
                report_resp = requests.get(
                    f"{base_url}{_REPORT_PATH.format(job_id=job_id)}",
                    timeout=_REQUEST_TIMEOUT_SEC,
                )
                report_resp.raise_for_status()
                report_body = report_resp.json()
                research_data: str = report_body.get("report", "")
                return ExecuteAiqQueryOutput(research_data=research_data, error=None)

            if status in ("FAILURE", "INTERRUPTED"):
                job_error = status_body.get("error") or f"A-IQ job ended with status {status}"
                return ExecuteAiqQueryOutput(research_data="", error=job_error)

        return ExecuteAiqQueryOutput(
            research_data="",
            error=f"A-IQ job {job_id} did not complete within {timeout_sec}s.",
        )

    except Exception as exc:
        return ExecuteAiqQueryOutput(research_data="", error=str(exc))
