"""Phase 3 FULL stage, question-led (``crux_v1``): scout -> architect -> per-crux search -> gap round -> audit -> judges.

1. ``scout``: one broad evidence pull summarised into a landscape (no estimate, no price).
2. architect (role ``briefer``): 5-8 role-assigned cruxes (mechanism | comparator | evidence | horizon | skeptic) with queries.
   Code enforces the required roles and fills a missing one from a template.
3. Each crux is searched in parallel through the evidence backend (Brave / Gemini / A-IQ); rows carry ``crux_id``/``role``.
4. ``gap_analyzer``: one round (``MAX_GAP_ROUNDS``), marks cruxes answered / unanswered / contradicted and may add
   up to ``GAP_MAX_QUERIES`` reworded follow-up queries; unresolved cruxes go to the judge as known unknowns.
5. audit: each line gets a source-quality tag; ``auditor`` checks that the lines behind answered cruxes (at most
   ``AUDIT_MAX_LINES``) are really stated in their page text. Failed lines are dropped; lines without page text
   (Gemini / cache / beyond the cap) stay but are labelled ``unverified``.
6. Judges (``deep_researcher``, mode ``judge``), A/B on cost: arm ``two`` runs J1 (asks P(YES)) and J2 (asks P(NO)) in
   parallel; arm ``one`` runs a single judge whose framing (YES or NO) is picked per market by hash. Code flips a
   NO-framed answer and combines them in :func:`orchestrator.phases.p3_samples.aggregate_samples`. The judges see only
   the lines behind answered/contradicted cruxes (top ``JUDGE_LINES_PER_CRUX`` per crux). A compound market (AND / OR
   of components) is checked in code (:mod:`orchestrator.compound`).

No stage sees the market price. Bundle and ledger writes happen in the calling thread only; worker threads only
fetch rows and call agents.
"""

from __future__ import annotations

import contextvars
import re
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from pydantic import BaseModel

import agents_blueprint  # module import avoids a cycle when agents_blueprint is imported first
from config.trading_constants import (
    AUDIT_MAX_LINES,
    BRAVE_PAGE_TEXT_CHARS,
    CRUX_COUNT,
    CRUX_MAX_QUERIES,
    CRUX_SEARCH_WORKERS,
    DEPTH_FULL_MIN_USD,
    EVIDENCE_QUALITY_TIERS,
    GAP_MAX_QUERIES,
    JUDGE_LINES_PER_CRUX,
    LOCAL_QUERY_ROLES,
    RESEARCH_LOCAL_EXTRA_WEB_CALLS,
    SOURCE_CLASSIFIER_MAX_DOMAINS,
    LIQ_FRAC,
    JUDGE_PARTIAL_LINES,
    MAX_GAP_ROUNDS,
    MAX_ORDER_USD,
    RESEARCH_MAX_WEB_CALLS,
    RESEARCH_PIPELINE,
)
from obsidian_utils import ObsidianManager
from orchestrator import compound as compound_mod
from orchestrator import brave_search, evidence, source_quality
from orchestrator.agent_outputs import (
    REQUIRED_CRUX_ROLES,
    AuditOutput,
    BrieferOutput,
    DeepResearcherComplete,
    DeepResearcherNeedsMore,
    GapOutput,
    ScoutOutput,
    SourceClassifierOutput,
)
from orchestrator import judge_model
from orchestrator.config import FORCED_SYNTHESIS_OVERRIDE, JUDGE_ARM_ONE
from orchestrator.parse import agent_error_reason, normalize_structured_output, run_agent_with_format_retries
from orchestrator.phases.agent_call import check_complete, invoke_deep_researcher
from orchestrator.phases.common import _as_float, _dr_payload, _text_fields, log
from orchestrator.phases.p3_evidence import _ensure_as_of, fetch_rows, resolve_source
from orchestrator.playbooks import playbook_for
from orchestrator.runner import AgentRunner
from orchestrator.state import InfraUnavailable, is_infra_error

Sample = tuple[DeepResearcherComplete, Any, str]  # (judge output, parsed markdown, framing "yes" | "no")

_UNVERIFIED_NOTE = "all supporting lines failed the citation audit"


# ---- depth, budget, small helpers ---------------------------------------------------------------------


def potential_max_stake_usd(md: dict[str, Any]) -> float:
    """Largest order this market could take: the per-order ceiling, or the liquidity cap when that is lower."""
    liq = _as_float(md.get("liquidity"))
    return MAX_ORDER_USD if liq is None else min(MAX_ORDER_USD, LIQ_FRAC * liq)


def research_depth(md: dict[str, Any]) -> str:
    """``lite`` (fewer cruxes, no audit) when the potential max order is below ``DEPTH_FULL_MIN_USD``, else ``full``."""
    return "full" if potential_max_stake_usd(md) >= DEPTH_FULL_MIN_USD else "lite"


class WebBudget:
    """Per-market cap on search calls (thread-safe)."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.left = total
        self._lock = threading.Lock()

    def take(self, n: int = 1, *, reserve: int = 0) -> bool:
        """Spend ``n`` calls if at least ``n + reserve`` remain."""
        with self._lock:
            if self.left - reserve >= n:
                self.left -= n
                return True
            return False

    def extend(self, n: int) -> None:
        """Raise the cap by ``n`` calls (non-English locale: local-language queries)."""
        with self._lock:
            self.total += n
            self.left += n

    @property
    def used(self) -> int:
        return self.total - self.left


# ---- market locale ----------------------------------------------------------------------------------------

_ENGLISH_LANGS = frozenset({"en", "en-gb", "en-us", "eng", "english"})


def normalize_locale(raw: Any) -> dict[str, Any] | None:
    """``{"country": ISO-2 or None, "language": lowercase code}`` from a scout / architect locale, else ``None``.

    The country must be a value Brave supports (else None); the language is kept as the model wrote it (lower
    case) and validated against Brave only when a search is sent. A missing language means no locale.
    """
    if not isinstance(raw, dict):
        return None
    lang = str(raw.get("language") or "").strip().lower().replace("_", "-")
    if not lang:
        return None
    return {"country": brave_search.valid_country(raw.get("country")), "language": lang}


def is_non_english(locale: dict[str, Any] | None) -> bool:
    return bool(locale) and str(locale["language"]) not in _ENGLISH_LANGS


def _pmap(fn: Callable[[Any], Any], items: list[Any], workers: int) -> list[Any]:
    """``fn`` over ``items`` in a thread pool (order kept, caller's context copied); a failure comes back as the
    exception object, except :class:`InfraUnavailable`, which is re-raised."""
    def run(item: Any) -> Any:
        try:
            return fn(item)
        except Exception as exc:  # noqa: BLE001 - returned to the caller, in order
            if isinstance(exc, InfraUnavailable) or is_infra_error(exc):
                raise  # an outage aborts the tick; it never degrades a stage
            return exc

    if len(items) <= 1:
        return [run(i) for i in items]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, run, i) for i in items]
        return [f.result() for f in futures]


def _call_agent(
    runner: AgentRunner, role: str, payload: dict[str, Any], model: type[BaseModel], flags: list[str], label: str
) -> Any | None:
    """Run a structured agent with format retries; ``None`` (and a flag) on any failure."""
    spec = agents_blueprint.AGENTS[role]

    def validate(parsed: dict[str, Any]) -> Any:
        return model.model_validate(
            normalize_structured_output(role, payload, parsed, output_schema=spec.get("output_schema"))
        )

    try:
        out = run_agent_with_format_retries(runner, role, payload, validate_fn=validate)
    except Exception as exc:  # noqa: BLE001 - every helper role degrades; the market is never flagged for it
        if isinstance(exc, InfraUnavailable) or is_infra_error(exc):
            raise
        log.warning("[PHASE 3] %s failed for %s: %s", role, payload.get("market_id"), exc)
        flags.append(f"{label}_failed")
        return None
    err = agent_error_reason(out.model_dump())
    if err:
        log.warning("[PHASE 3] %s error for %s: %s", role, payload.get("market_id"), err)
        flags.append(f"{label}_failed")
        return None
    return out


def _strip_pages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != evidence.PAGES_KEY} for r in rows]


# ---- scout ----------------------------------------------------------------------------------------------


def _scout(
    runner: AgentRunner, row: dict[str, Any], today: str, fetch: Callable[[list[str]], list[dict[str, Any]]],
    budget: WebBudget, flags: list[str],
) -> dict[str, Any] | None:
    """One broad evidence pull, summarised; ``None`` (flagged) when the pull or the agent fails."""
    if not budget.take(1):
        flags.append("scout_skipped")
        return None
    md = row.get("market_data") or {}
    title = str(row.get("market_title") or "").strip()
    end = md.get("end_date")
    query = f"{title} latest news and status as of {today}" + (f", before {str(end)[:10]}" if end else "")
    rows = _strip_pages(fetch([query]))
    if not evidence.rows_usable(rows):
        flags.append("scout_failed")
        return None
    payload = {**_text_fields(row, today), "evidence": evidence.label_bundle(rows, today)}
    out = _call_agent(runner, "scout", payload, ScoutOutput, flags, "scout")
    if out is None:
        return None
    return out.model_dump(exclude={"market_id", "error"})


# ---- architect ------------------------------------------------------------------------------------------

_LEGACY_SLOT_ROLES = ("mechanism", "comparator", "evidence", "skeptic")


def _templates(title: str, end: str) -> dict[str, dict[str, Any]]:
    horizon_q = f"{title} scheduled decisions deadlines upcoming events" + (f" before {end}" if end else "")
    return {
        "mechanism": {
            "question": "Under the market rules, what exactly resolves YES, who or what decides it, and by which official source?",
            "queries": [f"{title} resolution criteria official source how it is decided"],
            "moves_p_if": "A strict or narrow resolution clause pushes p down; a loose one pushes it up.",
        },
        "comparator": {
            "question": "How often have comparable events happened (base rate over a comparable window)?",
            "queries": [f"{title} base rate historical frequency comparable past cases"],
            "moves_p_if": "A high base rate pushes p up; a low one pushes it down.",
        },
        "skeptic": {
            "question": "What is the strongest evidence that the obvious outcome will not happen?",
            "queries": [f"{title} strongest evidence against reasons it will not happen"],
            "moves_p_if": "Credible disconfirming facts push p down; their absence leaves it unchanged.",
        },
        "horizon": {
            "question": "Which scheduled events or deadlines before the end date could decide the outcome?",
            "queries": [horizon_q],
            "moves_p_if": "A decisive event inside the window raises the variance of p; none pushes p toward the status quo.",
        },
        "evidence": {
            "question": "What is the latest verified state of the matter?",
            "queries": [f"{title} latest status news"],
            "moves_p_if": "Fresh confirming facts push p up; fresh disconfirming facts push it down.",
        },
    }


_RULES_Q_RE = re.compile(
    r"^\s*(?:do|does|did|is|are|what do|what does|how do|how does)\b.{0,60}?\b(?:market|resolution)\s+"
    r"(?:rules?|criteria|description|text|terms)\b",
    re.IGNORECASE,
)


def _is_rules_question(question: Any) -> bool:
    """Safety net: the question asks what the market rules / resolution text say (the judge reads those, no search)."""
    return bool(_RULES_Q_RE.search(str(question or "")))


def _normalize_cruxes(
    proposed: list[dict[str, Any]], row: dict[str, Any], today: str, depth: str,
    spec: dict[str, Any] | None = None, locale: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Enforce the required roles (template fill), the crux count for ``depth`` and unique, dated queries; assign ids.

    For a compound market (``spec``) every component gets at least one crux (an ``evidence`` template is added for a
    component the architect left out) and the first crux of each component survives the count cap.
    """
    lo, hi = CRUX_COUNT[depth]
    title = str(row.get("market_title") or "").strip()
    end = str((row.get("market_data") or {}).get("end_date") or "")[:10]
    tpl = _templates(title, end)
    comp_ids = [c["id"] for c in spec["components"]] if spec else []
    cruxes = [dict(c) for c in proposed]
    for c in cruxes:
        if c.get("component") not in comp_ids:
            c["component"] = None
    for c in cruxes:
        c["source"] = "rules" if (c.get("source") == "rules" or _is_rules_question(c.get("question"))) else "web"
    present = {c["role"] for c in cruxes}
    for role in REQUIRED_CRUX_ROLES:
        if role not in present:
            cruxes.append({"role": role, **tpl[role], "template": True})
    for comp in spec["components"] if spec else []:
        if not any(c.get("component") == comp["id"] for c in cruxes):
            label = comp["label"]
            cruxes.append({
                "role": "evidence", "component": comp["id"], "template": True,
                "question": f"What is the latest verified state of the component: {label}?",
                "queries": [f"{title} {label} latest status news"],
                "moves_p_if": "Fresh confirming facts push this component's probability up; disconfirming facts push it down.",
            })
    while len(cruxes) < lo:
        cruxes.append({"role": "evidence", **tpl["evidence"], "template": True})
    if len(cruxes) > hi:  # drop from the end, never the first crux of a required role
        protected: set[int] = set()
        seen: set[str] = set()
        for i, c in enumerate(cruxes):
            if c["role"] in REQUIRED_CRUX_ROLES and c["role"] not in seen:
                seen.add(c["role"])
                protected.add(i)
        seen_comp: set[str] = set()
        for i, c in enumerate(cruxes):
            if c.get("component") and c["component"] not in seen_comp:
                seen_comp.add(c["component"])
                protected.add(i)
        keep = [i for i in range(len(cruxes)) if i in protected]
        for i in range(len(cruxes)):
            if len(keep) >= hi:
                break
            if i not in protected:
                keep.append(i)
        cruxes = [cruxes[i] for i in sorted(keep)]
    seen_q: set[str] = set()
    out = []
    for n, c in enumerate(cruxes, start=1):
        queries = []
        prov = str(c.get("provenance_question") or "").strip()
        for q in c["queries"][: CRUX_MAX_QUERIES - 1 if prov else CRUX_MAX_QUERIES]:
            q = _ensure_as_of(str(q).strip(), today)
            if q.lower() not in seen_q:
                seen_q.add(q.lower())
                queries.append(q)
        if prov:  # source reliability: who produced the forecast / estimate, their stake and track record
            pq = _ensure_as_of(prov, today)
            if pq.lower() not in seen_q:
                seen_q.add(pq.lower())
                queries.append(pq)
        if not queries:  # every query duplicated an earlier crux's: ask the question itself
            q = _ensure_as_of(c["question"], today)
            seen_q.add(q.lower())
            queries = [q]
        local: list[str] = []
        if is_non_english(locale) and c["role"] in LOCAL_QUERY_ROLES and c.get("source") != "rules":
            for lq in c.get("queries_local") or []:  # one local-language query per crux (no "as of" suffix)
                lq = str(lq).strip()
                if lq and lq.lower() not in seen_q:
                    seen_q.add(lq.lower())
                    local.append(lq)
                    break
        out.append({
            "crux_id": f"c{n}", "role": c["role"], "question": c["question"],
            "moves_p_if": c.get("moves_p_if", ""), "queries": queries, "queries_local": local,
            "component": c.get("component"), "provenance_question": prov, "source": c.get("source", "web"),
            "status": "rules" if c.get("source") == "rules" else "unanswered", "missing": "", "followed_up": False,
        })
    return out


def _architect(
    runner: AgentRunner, row: dict[str, Any], today: str, depth: str, scout: dict[str, Any] | None, flags: list[str],
    locale: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
    """Returns ``(cruxes, compound spec or None, locale)``; the scout's ``locale`` wins over the architect's own."""
    payload = {**_text_fields(row, today), "depth": depth, "scout": scout}
    playbook = playbook_for((row.get("market_data") or {}).get("category"))
    if playbook:
        payload["playbook"] = playbook  # optional key: only present when the category has a playbook
    out = _call_agent(runner, "briefer", payload, BrieferOutput, flags, "architect")
    proposed: list[dict[str, Any]] = []
    spec = compound_mod.parse_spec(out.compound) if out is not None else None
    if out is not None:
        if out.cruxes:
            proposed = [c.model_dump() for c in out.cruxes]
        else:  # legacy four fixed slots
            proposed = [
                {"role": role, "question": q, "queries": [q], "moves_p_if": ""}
                for role, q in zip(_LEGACY_SLOT_ROLES, out.research_queries)
            ]
    if locale is None and out is not None:
        locale = normalize_locale(getattr(out, "locale", None))
    # a failed architect leaves ``proposed`` empty: the templates (the old fixed slots plus horizon) fill in
    return _normalize_cruxes(proposed, row, today, depth, spec, locale), spec, locale


# ---- search ---------------------------------------------------------------------------------------------


def _search_jobs(
    jobs: list[tuple[dict[str, Any], str]],
    fetch: Callable[..., list[dict[str, Any]]],
    gap: bool,
    locale: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Search each ``(crux, query)`` in a worker thread; rows come back tagged with the crux (writes stay with the caller).

    A crux's ``queries_local`` entries are searched with the market ``locale`` (Brave country / search_lang).
    """
    def one(job: tuple[dict[str, Any], str]) -> list[dict[str, Any]]:
        if locale and job[1] in (job[0].get("queries_local") or []):
            return fetch([job[1]], locale=locale)
        return fetch([job[1]])

    results = _pmap(one, jobs, CRUX_SEARCH_WORKERS)
    rows: list[dict[str, Any]] = []
    for (crux, query), res in zip(jobs, results):
        if isinstance(res, Exception) or not res:
            res = [{"query": query, "research_data": "", "error": f"search failed: {res}"}]
        for r in res:
            rows.append({**r, "crux_id": crux["crux_id"], "role": crux["role"], **({"gap": True} if gap else {})})
    return rows


def _initial_jobs(cruxes: list[dict[str, Any]], budget: WebBudget) -> list[tuple[dict[str, Any], str]]:
    """First query of every crux, then second queries while the gap round's reserve stays free."""
    jobs = []
    for c in cruxes:
        if c.get("source") != "rules" and budget.take(1):
            jobs.append((c, c["queries"][0]))
    for c in cruxes:  # local-language queries next: they have their own extra budget
        for q in c.get("queries_local") or []:
            if budget.take(1, reserve=GAP_MAX_QUERIES):
                jobs.append((c, q))
    for c in cruxes:
        if c.get("source") == "rules":
            continue
        for q in c["queries"][1:]:
            if budget.take(1, reserve=GAP_MAX_QUERIES):
                jobs.append((c, q))
    return jobs


# ---- gap round ------------------------------------------------------------------------------------------


def _labelled_lines_by_crux(rows: list[dict[str, Any]], today: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for lab, src in zip(evidence.label_bundle(rows, today, quality=True), rows):
        for line in str(lab.get("research_data") or "").splitlines():
            if line.startswith("[E"):
                out.setdefault(str(src.get("crux_id")), []).append(line)
    return out


def _comparator_followup(crux: dict[str, Any], today: str) -> str:
    """Reworded history query for an unanswered comparator crux ("historical results / how often ...")."""
    question = str(crux.get("question") or "").strip().rstrip("?. ")
    return _ensure_as_of(f"historical results, how often: {question}", today)


def _gap_round(
    runner: AgentRunner, row: dict[str, Any], today: str, cruxes: list[dict[str, Any]],
    rows: list[dict[str, Any]], budget: WebBudget, fetch: Callable[[list[str]], list[dict[str, Any]]],
    flags: list[str],
) -> tuple[list[dict[str, Any]], int]:
    """Assess every crux, run at most one round of follow-up searches; returns (new rows, follow-up query count)."""
    lines = _labelled_lines_by_crux(rows, today)
    searched = [c for c in cruxes if c.get("source") != "rules"]  # rules cruxes are read by the judge, not searched
    for c in searched:  # default by evidence presence (used when the analyzer fails or skips a crux)
        c["status"] = "answered" if lines.get(c["crux_id"]) else "unanswered"
    md = row.get("market_data") or {}
    payload = {
        "market_id": row["market_id"], "title": row.get("market_title", ""), "end_date": md.get("end_date"),
        "today": today, "assess_only": False,
        "cruxes": [
            {"crux_id": c["crux_id"], "role": c["role"], "question": c["question"], "moves_p_if": c["moves_p_if"],
             "evidence": lines.get(c["crux_id"], [])}
            for c in searched
        ],
    }
    new_rows: list[dict[str, Any]] = []
    queries_run = 0
    for _ in range(MAX_GAP_ROUNDS):
        out = _call_agent(runner, "gap_analyzer", payload, GapOutput, flags, "gap")
        if out is None:
            return new_rows, queries_run
        by_id = {c["crux_id"]: c for c in searched}
        for a in out.assessments:
            if a.crux_id in by_id:
                by_id[a.crux_id]["status"] = a.status
                by_id[a.crux_id]["missing"] = a.missing.strip()
        known = {q.lower() for c in searched for q in c["queries"]}
        wanted: list[tuple[dict[str, Any], str]] = []
        proposed_for: set[str] = set()
        for f in out.follow_up_queries:
            c = by_id.get(f.crux_id)
            q = _ensure_as_of(f.query.strip(), today) if f.query.strip() else ""
            if c is None or c["status"] != "unanswered" or not q or q.lower() in known:
                continue
            known.add(q.lower())
            wanted.append((c, q))
            proposed_for.add(c["crux_id"])
        for c in searched:
            if c["role"] == "comparator" and c["status"] == "unanswered" and c["crux_id"] in proposed_for:
                log.info("[PHASE 3] gap: analyzer proposed comparator follow-up for %s (%s)", row["market_id"], c["crux_id"])
        for c in searched:  # history first: an unanswered comparator crux always gets a follow-up
            if c["role"] == "comparator" and c["status"] == "unanswered" and c["crux_id"] not in proposed_for:
                q = _comparator_followup(c, today)
                if q.lower() not in known:
                    known.add(q.lower())
                    wanted.insert(0, (c, q))
                    log.info("[PHASE 3] gap: added comparator follow-up for %s: %s", row["market_id"], q)
                    break
        jobs: list[tuple[dict[str, Any], str]] = []
        for c, q in wanted[:GAP_MAX_QUERIES]:  # lowest-priority (last) analyzer queries fall off the cap
            if not budget.take(1):
                break
            jobs.append((c, q))
        if jobs:
            new_rows = _search_jobs(jobs, fetch, gap=True)
            queries_run = len(jobs)
            for c, q in jobs:
                c["followed_up"] = True
                c["queries"].append(q)
            _reassess(runner, row, today, searched, rows + new_rows, payload, flags)
        break  # one round only (MAX_GAP_ROUNDS is capped in code)
    return new_rows, queries_run


def _reassess(
    runner: AgentRunner, row: dict[str, Any], today: str, searched: list[dict[str, Any]],
    all_rows: list[dict[str, Any]], base_payload: dict[str, Any], flags: list[str],
) -> None:
    """Assess-only gap call for the followed-up cruxes (all their lines, follow-up included); no web calls.

    Updates status and ``missing``; any returned follow-up queries are ignored. On failure the earlier statuses stay
    and ``gap_reassess_failed`` is flagged.
    """
    followed = [c for c in searched if c["followed_up"]]
    if not followed:
        return
    lines = _labelled_lines_by_crux(all_rows, today)
    payload = {
        **{k: v for k, v in base_payload.items() if k != "cruxes"},
        "assess_only": True,
        "cruxes": [
            {"crux_id": c["crux_id"], "role": c["role"], "question": c["question"], "moves_p_if": c["moves_p_if"],
             "evidence": lines.get(c["crux_id"], [])}
            for c in followed
        ],
    }
    before = sum(1 for c in followed if c["status"] == "unanswered")
    local_flags: list[str] = []
    out = _call_agent(runner, "gap_analyzer", payload, GapOutput, local_flags, "gap_reassess")
    if out is None:
        flags.extend(local_flags)
        return
    by_id = {c["crux_id"]: c for c in followed}
    for a in out.assessments:
        if a.crux_id in by_id:
            by_id[a.crux_id]["status"] = a.status
            by_id[a.crux_id]["missing"] = a.missing.strip()
    after = sum(1 for c in followed if c["status"] == "unanswered")
    log.info(
        "[PHASE 3] gap reassess for %s: %d cruxes, %d→%d unanswered", row["market_id"], len(followed), before, after
    )


# ---- audit ----------------------------------------------------------------------------------------------


def _page_for(row: dict[str, Any], url: str | None) -> str | None:
    if not url:
        return None
    for p in row.get(evidence.PAGES_KEY) or []:
        if evidence.normalize_url(str(p.get("url") or "")) == url:
            return str(p.get("text") or "") or None
    return None


def _audit(
    runner: AgentRunner, row: dict[str, Any], today: str, cruxes: list[dict[str, Any]],
    rows: list[dict[str, Any]], flags: list[str],
) -> tuple[list[dict[str, Any]], int]:
    """Check the lines behind answered cruxes against their page text; returns (rows without dropped lines, dropped).

    Surviving checked lines are recorded per row in ``verified`` (line indexes); everything else is ``unverified``.
    """
    scope = {c["crux_id"] for c in cruxes if c["status"] == "answered" or c["followed_up"]}
    items: list[dict[str, Any]] = []
    pos: dict[str, tuple[int, int]] = {}
    for rec in evidence.label_lines(rows, today):
        if len(items) >= AUDIT_MAX_LINES:
            break
        if rec["crux_id"] not in scope:
            continue
        page = _page_for(rows[rec["row"]], rec["url"])
        if page is None:
            continue
        items.append({
            "id": rec["id"], "claim": rec["text"], "url": rec["url"],
            "page_text": evidence.sanitize_text(page)[:BRAVE_PAGE_TEXT_CHARS],
        })
        pos[rec["id"]] = (rec["row"], rec["line"])
    if not items:
        return rows, 0
    payload = {"market_id": row["market_id"], "today": today, "items": items}
    out = _call_agent(runner, "auditor", payload, AuditOutput, flags, "audit")
    if out is None:
        return rows, 0
    supported = {v.id for v in out.verdicts if v.supported}
    judged = {v.id for v in out.verdicts}
    dropped_at = {pos[i] for i in pos if i in judged and i not in supported}  # an item with no verdict stays unverified
    verified_at = {pos[i] for i in pos if i in supported}
    new_rows = []
    for ri, r in enumerate(rows):
        src = str(r.get("research_data") or "").splitlines()
        if not src or not any(p[0] == ri for p in dropped_at | verified_at):
            new_rows.append(r)
            continue
        kept, verified = [], []
        for li, line in enumerate(src):
            if (ri, li) in dropped_at:
                continue
            if (ri, li) in verified_at:
                verified.append(len(kept))
            kept.append(line)
        new_rows.append({**r, "research_data": "\n".join(kept), "verified": verified})
    return new_rows, len(dropped_at)


# ---- orchestration of the evidence stage ------------------------------------------------------------------


def _crux_view(c: dict[str, Any]) -> dict[str, Any]:
    view = {k: c[k] for k in ("crux_id", "role", "question", "moves_p_if", "status", "missing", "followed_up")}
    if c.get("source") == "rules":
        view["source"] = "rules"
    if c.get("component"):
        view["component"] = c["component"]
    if c.get("provenance_question"):
        view["provenance_question"] = c["provenance_question"]
    return view


# ---- source classifier ------------------------------------------------------------------------------------


def _classify_sources(
    vault: ObsidianManager, runner: AgentRunner, market_id: str, rows: list[dict[str, Any]], flags: list[str]
) -> int:
    """One batched cheap ``source_classifier`` call for the domains no YAML entry, cache or heuristic resolves.

    Answers are persisted in the vault's classifier cache, so a domain is never asked twice; a failed call is flagged
    and retried by a later research. Returns the number of domains stored.
    """
    source_quality.bind_cache(source_quality.cache_path_for(vault))
    urls = []
    for r in rows:
        if evidence.row_usable(r):
            urls += [m.group(0) for m in evidence._URL_RE.finditer(str(r.get("research_data") or ""))]
    domains = source_quality.unresolved_domains(urls)[:SOURCE_CLASSIFIER_MAX_DOMAINS]
    if not domains:
        return 0
    out = _call_agent(
        runner, source_quality.CLASSIFIER_ROLE, {"market_id": market_id, "domains": domains},
        SourceClassifierOutput, flags, "source_classifier",
    )
    if out is None:
        return 0
    asked = set(domains)
    tiers: dict[str, str] = {}
    for d in out.domains:
        key = source_quality.domain_key(d.domain.strip().lower())
        if key in asked:
            tiers[key] = d.tier.strip().lower() if d.tier.strip().lower() in EVIDENCE_QUALITY_TIERS else "unknown"
    n = source_quality.store(tiers)
    log.info("[PHASE 3] source_classifier for %s: asked=%d stored=%d", market_id, len(domains), n)
    return n


def build_evidence(
    vault: ObsidianManager, runner: AgentRunner, row: dict[str, Any], today: str, depth: str,
    *, persist: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any], str, bool]:
    """Scout -> architect -> crux search -> gap round -> audit. Returns ``(rows, crux_meta, source, fallback)``.

    With ``persist`` the bundle (cleared first) and its metadata are written, from this (the calling) thread.
    Helper-role failures degrade (flags in ``crux_meta['flags']``); only :class:`InfraUnavailable`` propagates.
    """
    market_id = row["market_id"]
    flags: list[str] = []
    budget = WebBudget(RESEARCH_MAX_WEB_CALLS)
    state = {"source": resolve_source(vault, market_id)[0], "fallback": False}
    state_lock = threading.Lock()

    def fetch(queries: list[str], locale: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with state_lock:
            source = state["source"]
        if locale:
            rows, used, fell = fetch_rows(vault, market_id, queries, runner, today, source, locale)
        else:
            rows, used, fell = fetch_rows(vault, market_id, queries, runner, today, source)
        if fell:
            with state_lock:
                state["source"], state["fallback"] = used, True
        return rows

    scout = _scout(runner, row, today, fetch, budget, flags)
    cruxes, compound_spec, locale = _architect(
        runner, row, today, depth, scout, flags, normalize_locale((scout or {}).get("locale"))
    )
    if is_non_english(locale):
        budget.extend(RESEARCH_LOCAL_EXTRA_WEB_CALLS)
    jobs = _initial_jobs(cruxes, budget)
    local_queries = sum(1 for c, q in jobs if q in (c.get("queries_local") or []))
    rows = _search_jobs(jobs, fetch, gap=False, locale=locale)
    log.info(
        "[LOCALE] %s: country=%s lang=%s local_queries=%d", market_id,
        (locale or {}).get("country"), (locale or {}).get("language"), local_queries,
    )
    gap_rows, gap_queries = _gap_round(runner, row, today, cruxes, rows, budget, fetch, flags)
    rows = rows + gap_rows
    _classify_sources(vault, runner, market_id, rows, flags)

    dropped = 0
    audit_ran = False
    if depth == "full":
        before = len(flags)
        rows, dropped = _audit(runner, row, today, cruxes, rows, flags)
        audit_ran = len(flags) == before
    else:
        flags.append("audit_skipped_lite")

    by_crux = _labelled_lines_by_crux(rows, today)
    for c in cruxes:
        if c["status"] == "answered" and not by_crux.get(c["crux_id"]):
            c["status"], c["missing"] = "unanswered", c["missing"] or _UNVERIFIED_NOTE
    known_unknowns = [
        (c["question"] + (f" ({c['missing']})" if c["missing"] else ""))
        for c in cruxes
        if c["status"] in ("unanswered", "contradicted")
    ]
    lines = evidence.label_lines(rows, today)
    verified = {(ri, li) for ri, r in enumerate(rows) for li in r.get("verified") or []}
    meta = {
        "pipeline": RESEARCH_PIPELINE,
        "research_depth": depth,
        "cruxes": [_crux_view(c) for c in cruxes],
        "compound": compound_spec,
        "known_unknowns": known_unknowns,
        "gap_queries": gap_queries,
        "audit_ran": audit_ran,
        "audit_dropped": dropped,
        "audit_unverified": sum(1 for rec in lines if (rec["row"], rec["line"]) not in verified),
        "source_mix": dict(Counter(rec["quality"] for rec in lines)),
        "flags": flags,
        "web_calls": budget.used,
        "locale_country": (locale or {}).get("country"),
        "locale_language": (locale or {}).get("language"),
        "local_queries": local_queries,
    }
    stored = _strip_pages(rows)
    if persist:
        vault.clear_research_bundle(market_id)
        vault.write_research_bundle(
            market_id, stored,
            meta={"evidence_source": state["source"], "evidence_fallback": state["fallback"], "crux_meta": meta},
        )
    log.info(
        "[PHASE 3] crux_v1 evidence for %s: depth=%s cruxes=%d web_calls=%d gap_queries=%d dropped=%d flags=%s",
        market_id, depth, len(cruxes), budget.used, gap_queries, dropped, flags,
    )
    return stored, meta, state["source"], state["fallback"]


# ---- judges ---------------------------------------------------------------------------------------------


_TIER_RANK = {"official": 0, "major": 1, "aggregator": 2, "unknown": 3, "blog": 4}


def crux_table(
    bundle: list[dict[str, Any]], crux_meta: dict[str, Any] | None, today: str, only_ids: set[str] | None = None
) -> list[dict[str, Any]] | None:
    """The judge's crux table: each crux with the E-ids of its evidence lines (ids as in the rendered bundle).

    With ``only_ids`` just the ids the judge is actually sent are listed.
    """
    if not crux_meta or not crux_meta.get("cruxes"):
        return None
    ids: dict[str, list[str]] = {}
    for rec in evidence.label_lines(bundle, today):
        if only_ids is None or rec["id"] in only_ids:
            ids.setdefault(str(rec["crux_id"]), []).append(rec["id"])
    out = []
    for c in crux_meta["cruxes"]:
        ev = ids.get(c["crux_id"], [])
        out.append({**c, "evidence_ids": ev, **({"evidence_partial": True} if ev and c.get("status") == "unanswered" else {})})
    return out


def select_judge_ids(
    bundle: list[dict[str, Any]], crux_meta: dict[str, Any] | None, today: str, k: int = JUDGE_LINES_PER_CRUX
) -> set[str] | None:
    """E-ids of the lines the judge is sent, or ``None`` for no trimming (no crux metadata: legacy bundle).

    Per crux the top ``k`` lines by source-quality tier, then recency (undated last) behind answered / contradicted
    cruxes; an unanswered crux sends at most ``JUDGE_PARTIAL_LINES`` (2) lines, labelled partial in the crux table
    (``evidence_partial``), next to its ``known_unknowns`` entry. Lines of a crux the metadata does not know are kept.
    """
    if not crux_meta or not crux_meta.get("cruxes"):
        return None
    by_id = {c["crux_id"]: c for c in crux_meta["cruxes"]}
    per: dict[str, list[dict[str, Any]]] = {}
    keep: set[str] = set()
    for rec in evidence.label_lines(bundle, today):
        c = by_id.get(str(rec["crux_id"]))
        if c is None:
            keep.add(rec["id"])
        elif c["status"] in ("answered", "contradicted", "unanswered"):
            per.setdefault(c["crux_id"], []).append(rec)
    for cid, recs in per.items():
        recs.sort(key=lambda r: (
            _TIER_RANK.get(r["quality"], 3), r["age_days"] if r["age_days"] is not None else 10**9, int(r["id"][1:]),
        ))
        keep.update(r["id"] for r in recs[: JUDGE_PARTIAL_LINES if by_id[cid]["status"] == "unanswered" else k])
    return keep


def _judge_one(
    vault: ObsidianManager, runner: AgentRunner, row: dict[str, Any], dr_in: dict[str, Any], framing: str
) -> Sample | None:
    market_id = row["market_id"]
    try:
        out = invoke_deep_researcher(runner, dr_in)
    except Exception as exc:  # noqa: BLE001 - one failed judge is tolerated (the other carries on)
        if isinstance(exc, InfraUnavailable) or is_infra_error(exc):
            raise
        log.warning("[PHASE 3] judge %s for %s failed: %s", dr_in.get("session_tag"), market_id, exc)
        return None
    if isinstance(out, DeepResearcherNeedsMore):
        log.warning("[PHASE 3] judge %s for %s ignored the forced-synthesis override", dr_in.get("session_tag"), market_id)
        return None
    research = check_complete(vault, market_id, out, flag=False)
    return (out, research, framing) if research is not None else None


def judge_plan(market_id: str) -> tuple[str, list[tuple[str, str]]]:
    """``(arm, [(tag, framing), ...])``: two judges (J1 YES, J2 NO) or one judge with a hash-picked framing."""
    arm = evidence.judge_arm(market_id)
    if arm == JUDGE_ARM_ONE:
        return arm, [("j1", evidence.judge_framing(market_id))]
    return arm, [("j1", "yes"), ("j2", "no")]


def failed_judges(market_id: str, samples: list[Any]) -> list[str]:
    """Tags of the planned judges that returned no usable sample."""
    have = {(s[2] if len(s) > 2 else "yes") for s in samples}
    return [tag for tag, framing in judge_plan(market_id)[1] if framing not in have]


def run_judges(
    vault: ObsidianManager, runner: AgentRunner, row: dict[str, Any], directives: str, today: str,
    crux_meta: dict[str, Any] | None,
) -> tuple[list[Sample], list[str]]:
    """The judges of the market's arm in parallel on the same trimmed bundle; returns ``(samples, failed judge tags)``."""
    from orchestrator.llm import usage  # noqa: PLC0415

    market_id = row["market_id"]
    judge_model.reset(market_id)
    judge_model.log_arm(market_id)
    source_quality.bind_cache(source_quality.cache_path_for(vault))
    bundle = vault.read_research_bundle(market_id) or []
    only = select_judge_ids(bundle, crux_meta, today)
    total = len(evidence.label_lines(bundle, today))
    log.info(
        "[PHASE 3] judge input lines for %s: %d -> %d", market_id, total, total if only is None else len(only)
    )
    table = crux_table(bundle, crux_meta, today, only)
    unknowns = list(crux_meta.get("known_unknowns") or []) if crux_meta else None
    compound_spec = (crux_meta or {}).get("compound")
    playbook = playbook_for((row.get("market_data") or {}).get("category"))
    arm, plan = judge_plan(market_id)
    jobs = []
    for tag, framing in plan:
        dr_in = _dr_payload(
            row, directives, bundle, mode="judge", today=today, system_override=FORCED_SYNTHESIS_OVERRIDE,
            session_tag=tag, ask=framing, crux_table=table, known_unknowns=unknowns,
            compound=compound_spec, playbook=playbook, only_ids=only,
        )
        jobs.append((tag, framing, dr_in))
    with usage.judge_arm_scope(arm):
        results = _pmap(lambda j: _judge_one(vault, runner, row, j[2], j[1]), jobs, 2)
    samples: list[Sample] = []
    failed: list[str] = []
    for (tag, _, _), res in zip(jobs, results):
        if isinstance(res, tuple):
            samples.append(res)
            nf = normalise_prior(res[0])
            if nf["prior"] is not None:  # how far the judge moved from its prior (asked outcome)
                log.info(
                    "[PHASE 3] judge %s for %s: prior=%.3f (%s) base_rate=%s estimated_p=%.3f (moved %+.3f)",
                    tag, market_id, nf["prior"], nf["prior_kind"],
                    "null" if nf["base_rate"] is None else f"{nf['base_rate']:.3f}",
                    res[0].estimated_p, res[0].estimated_p - nf["prior"],
                )
            if nf["flags"]:
                log.warning("[PHASE 3] judge %s for %s prior flags: %s", tag, market_id, nf["flags"])
        else:
            failed.append(tag)
    return samples, failed


# ---- offer evidence ---------------------------------------------------------------------------------------


def _bare(line: str) -> str:
    line = line.strip()
    return line[2:].strip() if line.startswith("- ") else line


def offer_evidence(
    bundle: list[dict[str, Any]], today: str, samples: list[Sample], limit: int = 3
) -> dict[str, list[str]]:
    """Verbatim evidence lines for the offer, split by the direction a judge cited them for.

    Audit-verified cited lines are used; a side with none verified falls back to its cited lines marked
    ``(unverified)`` (e.g. Gemini rows, lines beyond the audit cap, or an audit that failed), so the lists are only
    empty when the judges cited nothing for that side. A line cited in both directions is ambiguous and skipped.
    """
    recs = {r["id"]: r for r in evidence.label_lines(bundle, today)}
    directions: dict[str, set[str]] = {}
    for complete, _, _ in samples:
        for v in complete.crux_verdicts:
            if v.direction == "neutral":
                continue
            for eid in v.cited:
                directions.setdefault(eid, set()).add(v.direction)
    sides: dict[str, tuple[list[str], list[str]]] = {"yes": ([], []), "no": ([], [])}
    for eid, dirs in directions.items():
        rec = recs.get(eid)
        if rec is None or len(dirs) != 1:
            continue
        side = next(iter(dirs))
        ok = rec["line"] in (bundle[rec["row"]].get("verified") or [])
        sides[side][0 if ok else 1].append(_bare(rec["text"]))
    def pick(side: str) -> list[str]:
        verified, rest = sides[side]
        return verified[:limit] if verified else [f"{t} (unverified)" for t in rest][:limit]
    return {"evidence_bull": pick("yes"), "evidence_bear": pick("no")}


# ---- prior / base rate --------------------------------------------------------------------------------------

PRIOR_KINDS = ("reference_class", "structural", "judgment")


def normalise_prior(c: DeepResearcherComplete) -> dict[str, Any]:
    """Validated prior fields of one judge output, in the judge's own framing (asked outcome).

    ``base_rate`` survives only with a positive ``base_rate_n`` and a ``base_rate_source`` (real reference-class counts);
    otherwise it is dropped. A bad ``prior_kind`` becomes ``judgment``; ``reference_class`` without a base rate is
    downgraded to ``judgment``; ``reference_class`` forces ``prior == base_rate``. Problems come back as ``flags``.
    """
    flags: list[str] = []
    base, n, src = c.base_rate, c.base_rate_n, (c.base_rate_source or "").strip() or None
    if base is not None and not (n and n > 0 and src):
        flags.append("base_rate_unsupported")
        base = n = src = None
    if base is None:
        n = src = None
    kind = (c.prior_kind or "").strip().lower()
    if kind not in PRIOR_KINDS:
        flags.append("prior_kind_invalid" if c.prior_kind else "prior_kind_missing")
        kind = "judgment"
    prior = c.prior
    if kind == "reference_class":
        if base is None:
            flags.append("prior_downgraded")
            kind = "judgment"
        else:
            prior = base
    if prior is None:
        flags.append("prior_missing")
        if kind == "judgment" and base is not None:
            prior = base
    return {"prior": prior, "prior_kind": kind, "base_rate": base, "base_rate_n": n, "base_rate_source": src,
            "flags": flags}


def prior_fields(samples: list[Sample]) -> dict[str, Any]:
    """Prior / base-rate ledger fields (P(YES) framing) from the judges: J1's (else the lone NO judge's, flipped).

    Returns ``prior``, ``prior_kind``, ``base_rate``, ``base_rate_n`` plus ``prior_flags`` (normalisation problems of every
    judge) and ``prior_j2``/``prior_kind_j2`` when both judges ran. Empty when there are no samples.
    """
    if not samples:
        return {}
    normed = [(normalise_prior(s[0]), s[2] if len(s) > 2 else "yes") for s in samples]

    def yes_view(f: dict[str, Any], framing: str) -> dict[str, Any]:
        if framing != "no":
            return f
        flip = lambda v: None if v is None else round(1 - v, 6)  # noqa: E731
        return {**f, "prior": flip(f["prior"]), "base_rate": flip(f["base_rate"])}

    primary = next((x for x in normed if x[1] == "yes"), normed[0])
    main = yes_view(*primary)
    out: dict[str, Any] = {k: main[k] for k in ("prior", "prior_kind", "base_rate", "base_rate_n")}
    flags = [f"{f}:{'j1' if i == 0 else 'j2'}" for i, (n, _) in enumerate(normed) for f in n["flags"]]
    if flags:
        out["prior_flags"] = flags
    if len(normed) == 2:
        other = yes_view(*next(x for x in normed if x is not primary))
        out["prior_j2"] = other["prior"]
        out["prior_kind_j2"] = other["prior_kind"]
    return out


# ---- ledger / frontmatter fields --------------------------------------------------------------------------


def crux_fields(
    crux_meta: dict[str, Any] | None, framing_gap: float | None, failed: list[str],
    *, judge_arm: str | None = None, judge_framing: str | None = None, compound: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flat, JSON-safe summary of one crux run for the ledger, ``research_state`` and the report frontmatter.

    ``judge_framing`` is set only in the one-judge arm (null with two judges); ``compound`` is the result of
    :func:`orchestrator.compound.check` (its ``compound_*`` keys are copied).
    """
    meta = crux_meta or {}
    statuses = Counter(c.get("status") for c in meta.get("cruxes") or [])
    flags = list(meta.get("flags") or []) + [f"judge_failed:{t}" for t in failed]
    out = {
        "pipeline": RESEARCH_PIPELINE,
        "research_depth": meta.get("research_depth"),
        "cruxes_total": len(meta.get("cruxes") or []),
        "crux_answered": statuses.get("answered", 0),
        "crux_unanswered": statuses.get("unanswered", 0),
        "crux_contradicted": statuses.get("contradicted", 0),
        "crux_rules": statuses.get("rules", 0),
        "gap_queries": meta.get("gap_queries", 0),
        "audit_dropped": meta.get("audit_dropped", 0),
        "audit_unverified": meta.get("audit_unverified", 0),
        "framing_gap": framing_gap,
        "source_mix": meta.get("source_mix") or {},
        "pipeline_flags": flags,
        "web_calls": meta.get("web_calls", 0),
        "locale_country": meta.get("locale_country"),
        "locale_language": meta.get("locale_language"),
        "local_queries": meta.get("local_queries", 0),
        "judge_arm": judge_arm,
        "judge_framing": judge_framing,
    }
    if compound:
        out.update(compound)
        if compound.get("compound_gap") is None:
            out["pipeline_flags"] = flags + ["compound_components_missing"]
    return out


# ---- backtest entry ---------------------------------------------------------------------------------------


def _row_from_gamma(g: dict[str, Any]) -> dict[str, Any]:
    """Pipeline row from a Gamma-shaped market dict (an already-shaped pipeline row passes through)."""
    if g.get("market_id") and g.get("market_title") is not None:
        return g
    mid = g.get("conditionId") or g.get("condition_id") or g.get("market_id") or g.get("id")
    return {
        "market_id": str(mid),
        "market_title": g.get("question") or g.get("title") or "",
        "market_description": g.get("description") or "",
        "market_data": {
            "resolution_source": g.get("resolutionSource") or g.get("resolution_source"),
            "end_date": g.get("endDate") or g.get("end_date"),
            "category": g.get("category"),
            "liquidity": _as_float(g.get("liquidityNum") or g.get("liquidity")),
        },
    }


def backtest_research(row: dict[str, Any], today: str, search_freshness: str | None = None) -> dict[str, Any]:
    """Run the crux pipeline once for a Gamma-shaped ``row`` as of ``today`` (never the clock).

    No decision-ledger, filter or trade side effects: everything lives in a throwaway vault. Searches are limited
    by ``search_freshness`` (a Brave freshness value) through :func:`brave_search.freshness_override`.
    Returns ``{p, p_low, p_high, confidence, pipeline, ...}`` (or ``{"error": ...}`` when both judges fail).
    """
    from orchestrator import brave_search  # noqa: PLC0415
    from orchestrator.phases.p3_samples import aggregate_samples  # noqa: PLC0415
    from orchestrator.runner import spawn_agent  # noqa: PLC0415

    prow = _row_from_gamma(row)
    with tempfile.TemporaryDirectory(prefix="pmbot-backtest-") as tmp:
        vault = ObsidianManager(vault_base=tmp)
        vault.cold_start_protocol()
        directives = vault.read_directives()
        depth = "full"
        with brave_search.freshness_override(search_freshness):
            _, meta, _, _ = build_evidence(vault, spawn_agent, prow, today, depth)
            samples, failed = run_judges(vault, spawn_agent, prow, directives, today, meta)
        bundle = vault.read_research_bundle(prow["market_id"]) or []
        if not samples:
            return {"error": "both judges failed", "pipeline": RESEARCH_PIPELINE,
                    **crux_fields(meta, None, failed)}
        agg = aggregate_samples(samples)
        return {
            "p": agg["p"], "p_low": agg["p_low"], "p_high": agg["p_high"], "confidence": agg["confidence"],
            "samples": agg["samples"], "spread": agg["spread"], "pipeline": RESEARCH_PIPELINE,
            **crux_fields(
                meta, agg["spread"] if len(samples) == 2 else None, failed,
                judge_arm=judge_plan(prow["market_id"])[0],
                judge_framing=samples[0][2] if len(samples) == 1 else None,
                compound=compound_mod.check(samples, meta["compound"]) if meta.get("compound") else None,
            ),
            **{k: v for k, v in prior_fields(samples).items() if k != "prior_flags"},
            **offer_evidence(bundle, today, samples),
        }


__all__ = [
    "WebBudget",
    "backtest_research",
    "build_evidence",
    "crux_fields",
    "crux_table",
    "failed_judges",
    "judge_plan",
    "normalise_prior",
    "offer_evidence",
    "prior_fields",
    "select_judge_ids",
    "potential_max_stake_usd",
    "research_depth",
    "run_judges",
]
