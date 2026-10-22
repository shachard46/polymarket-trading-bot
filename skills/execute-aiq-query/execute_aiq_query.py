"""execute_aiq_query — OpenClaw skill execution module.

Contract: docs/04_skills_contracts.md §5

Calls a locally-hosted NVIDIA A-IQ service (started with `nat serve`) via its
async REST API. Submits a shallow_researcher job, polls until completion, and returns
the final report.

Runtime config (env vars override trading_constants defaults):
  AIQ_BASE_URL          — base URL of the A-IQ service  (default: http://localhost:8000)
  AIQ_POLL_INTERVAL_SEC — seconds between status polls  (default: 2.0)
  AIQ_TIMEOUT_SEC       — max seconds to wait for result (default: trading_constants.AIQ_TIMEOUT_SEC)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

_OPENCLAW_ROOT = "/home/boldplane/.openclaw"
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
for _path in (_REPO_ROOT, _OPENCLAW_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import requests
from pydantic import BaseModel, ConfigDict

from config.trading_constants import (
    AIQ_BASE_URL as _DEFAULT_BASE_URL,
    AIQ_POLL_INTERVAL_SEC as _DEFAULT_POLL,
    AIQ_TIMEOUT_SEC as _DEFAULT_TIMEOUT,
)

_AGENT_TYPE = "shallow_researcher"
_SUBMIT_PATH = "/v1/jobs/async/submit"
_STATUS_PATH = "/v1/jobs/async/job/{job_id}"
_REPORT_PATH = "/v1/jobs/async/job/{job_id}/report"
_REQUEST_TIMEOUT_SEC = 30

_SUCCESS_STATUSES = {"success", "completed"}
_FAILURE_STATUSES = {
    "failure",
    "failed",
    "error",
    "interrupted",
    "cancelled",
    "canceled",
    "not_found",
}

logger = logging.getLogger(__name__)


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

        # Step 2 — poll until terminal state or deadline
        deadline = time.monotonic() + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                status_resp = requests.get(
                    f"{base_url}{_STATUS_PATH.format(job_id=job_id)}",
                    timeout=min(_REQUEST_TIMEOUT_SEC, remaining),
                )
                if status_resp.status_code >= 500:
                    raise requests.HTTPError(
                        f"HTTP {status_resp.status_code}", response=status_resp
                    )
                status_resp.raise_for_status()
                status_body = status_resp.json()
            except (requests.ConnectionError, requests.Timeout) as exc:
                logger.warning("A-IQ poll transient error for job %s: %s", job_id, exc)
                continue
            except requests.HTTPError as exc:
                code = getattr(exc.response, "status_code", None)
                if code is not None and code >= 500:
                    logger.warning("A-IQ poll HTTP %s for job %s; retrying", code, job_id)
                    continue
                raise
            if not isinstance(status_body, dict):
                continue
            raw_status = status_body.get("status")
            status = str(raw_status or "").lower()

            if status in _SUCCESS_STATUSES:
                # Step 3 — fetch final report
                remaining = max(deadline - time.monotonic(), 1.0)
                report_resp = requests.get(
                    f"{base_url}{_REPORT_PATH.format(job_id=job_id)}",
                    timeout=min(_REQUEST_TIMEOUT_SEC, remaining),
                )
                report_resp.raise_for_status()
                report_body = report_resp.json()
                research_data = (
                    report_body.get("report") if isinstance(report_body, dict) else None
                )
                if not isinstance(research_data, str) or not research_data.strip():
                    return ExecuteAiqQueryOutput(
                        research_data="",
                        error=f"A-IQ job {job_id} succeeded but returned an empty or missing report.",
                    )
                return ExecuteAiqQueryOutput(research_data=research_data, error=None)

            if status in _FAILURE_STATUSES:
                job_error = status_body.get("error") or f"A-IQ job ended with status {raw_status}"
                return ExecuteAiqQueryOutput(research_data="", error=str(job_error))

        return ExecuteAiqQueryOutput(
            research_data="",
            error=f"A-IQ job {job_id} did not complete within {timeout_sec}s.",
        )

    except Exception as exc:
        return ExecuteAiqQueryOutput(research_data="", error=str(exc))
