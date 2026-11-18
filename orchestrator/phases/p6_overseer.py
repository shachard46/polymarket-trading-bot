"""Phase 6 - Macro-learning loop (Overseer): auto-revert, bounded directive proposals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from obsidian_utils import ObsidianManager, VaultWriteError
from config.trading_constants import (
    AUTO_REVERT_BRIER_WORSE_BY,
    AUTO_REVERT_MIN_SCORED,
    BOUNDS,
    FILTERS,
    KNOB_COOLDOWN_VERSIONS,
)
import json
import re

from orchestrator import failure_tags, gate, ledger, notify, stats
from orchestrator.config import OVERSEER_INTERVAL_SEC, overseer_min_new_resolved
from orchestrator.directives import (
    DirectiveBoundsError,
    changed_knobs,
    effective_knobs,
    validate_directive_change,
)
from orchestrator.phases.agent_call import run_structured_agent
from orchestrator.phases.common import log
from orchestrator.runner import AgentRunner, spawn_agent

PHASE6_MAX_POST_MORTEMS = 15
PHASE6_MAX_STATS_CHARS = 12 * 1024
PHASE6_MAX_POST_MORTEM_CHARS = 5 * 1024
PHASE6_MAX_TOTAL_CHARS = 90 * 1024



def _bounded_post_mortems(vault: ObsidianManager) -> list[dict[str, Any]]:
    """Most recent post-mortems by mtime, each truncated, within a total size cap."""
    paths = sorted(vault.iter_post_mortems(), key=lambda p: p.stat().st_mtime, reverse=True)
    batch: list[dict[str, Any]] = []
    total = 0
    for path in paths[:PHASE6_MAX_POST_MORTEMS]:
        content = path.read_text(encoding="utf-8")[:PHASE6_MAX_POST_MORTEM_CHARS]
        if total + len(content) > PHASE6_MAX_TOTAL_CHARS:
            break
        total += len(content)
        batch.append({"market_id": path.stem, "content": content})
    dropped = len(paths) - len(batch)
    if dropped:
        log.info(
            "[PHASE 6] sending %d of %d post-mortems (%d dropped by size/count caps)",
            len(batch),
            len(paths),
            dropped,
        )
    return batch


def _directives_frontmatter(vault: ObsidianManager) -> dict[str, Any]:
    from orchestrator.research import split_yaml_frontmatter_markdown

    text = vault.read_directives()
    try:
        return split_yaml_frontmatter_markdown(text)[0] if text.strip() else {}
    except ValueError:
        return {}


def _bounds_payload(vault: ObsidianManager, current_md: str) -> list[dict[str, Any]]:
    """The Overseer-facing bounds table: per knob current/default/min/max/step/cooldown."""
    knobs = effective_knobs(current_md)
    fm = _directives_frontmatter(vault)
    klc = fm.get("knob_last_changed") if isinstance(fm.get("knob_last_changed"), dict) else {}
    next_version = vault.latest_directives_version() + 1
    rows = []
    for key, b in BOUNDS.items():
        try:
            at = int(klc.get(key))
        except (TypeError, ValueError):
            at = None
        rows.append({
            "knob": key,
            "current": knobs[key],
            "default": FILTERS[key],
            "min": b["min"],
            "max": b["max"],
            "max_step_frac": b["max_step_frac"],
            "last_changed_version": at,
            "locked": at is not None and next_version - at < KNOB_COOLDOWN_VERSIONS,
        })
    return rows


def _auto_revert_if_worse(vault: ObsidianManager, rows: list[dict[str, Any]]) -> bool:
    """Restore the previous directives version when the current one scores clearly worse.

    Compares model Brier (stats helpers grouped by ledger ``directive_version``) once the current
    version has ``AUTO_REVERT_MIN_SCORED`` resolved scored markets (and so does the previous one).
    A version that is itself a restore is never reverted (no flip-flopping).
    """
    current = ledger.current_directive_version(vault)
    previous = current - 1
    fm = _directives_frontmatter(vault)
    if previous < 1 or fm.get("restored_from") is not None:
        return False
    if vault.read_directives_version(previous) is None:
        return False
    by_version = stats.brier_by_directive_version(rows)
    n_cur, b_cur = by_version.get(current, (0, 0.0))
    n_prev, b_prev = by_version.get(previous, (0, 0.0))
    if n_cur < AUTO_REVERT_MIN_SCORED or n_prev < AUTO_REVERT_MIN_SCORED:
        return False
    if b_cur - b_prev <= AUTO_REVERT_BRIER_WORSE_BY:
        return False
    reason = (
        f"auto-revert: v{current} Brier {b_cur:.4f} (n={n_cur}) is worse than "
        f"v{previous} {b_prev:.4f} (n={n_prev}) by more than {AUTO_REVERT_BRIER_WORSE_BY}"
    )
    new_version = vault.restore_directives(previous, reason)
    vault.patch_directives_frontmatter({"last_overseer_at": datetime.now(timezone.utc).isoformat()})
    log.error("[PHASE 6] !!! DIRECTIVES AUTO-REVERTED: %s -> restored as v%d; Overseer skipped",
              reason, new_version)
    return True


def phase6_macro_learning_loop(
    vault: ObsidianManager,
    runner: AgentRunner = spawn_agent,
) -> None:
    """Stats + failure classes + post-mortems -> Overseer -> bounded, versioned directives.

    Order: (1) auto-revert a version that scores clearly worse than its predecessor (and skip the
    Overseer); (2) run only if >= ``OVERSEER_MIN_NEW_RESOLVED`` new resolution rows exist since the
    last run and ``OVERSEER_INTERVAL_SEC`` has passed; (3) a proposal that violates the bounds is
    rejected whole and the old directives stay.
    """
    log.info("[PHASE 6] Macro-learning loop (Overseer)")
    rows = list(ledger.iter_rows(vault))
    if _auto_revert_if_worse(vault, rows):
        return

    fm = _directives_frontmatter(vault)
    now = datetime.now(timezone.utc)
    last_at = gate._parse_ts(fm.get("last_overseer_at"))
    if last_at is not None and (now - last_at).total_seconds() < OVERSEER_INTERVAL_SEC:
        log.info("[PHASE 6] skipped: last Overseer run %s is under the minimum interval", last_at)
        return
    n_resolved = sum(1 for r in rows if r.get("stage") == ledger.RESOLUTION_STAGE)
    try:
        last_count = int(fm.get("last_overseer_ledger_count") or 0)
    except (TypeError, ValueError):
        last_count = 0
    need = overseer_min_new_resolved()
    if n_resolved - last_count < need:
        log.info(
            "[PHASE 6] skipped: %d new resolutions since the last Overseer run (need %d)",
            n_resolved - last_count, need,
        )
        return

    try:
        stats.write_stats(vault)
    except Exception:  # noqa: BLE001 - fall back to computing in memory
        log.exception("[PHASE 6] write_stats failed")
    stats_path = vault._dirs["system"] / "stats.md"
    stats_md = stats_path.read_text(encoding="utf-8") if stats_path.exists() else ""
    current = vault.read_directives()
    class_counts, candidate_tags = failure_tags.overseer_failure_views(
        stats.failure_class_counts(rows), failure_tags.load_registry(vault)
    )
    payload = {
        "post_mortems": _bounded_post_mortems(vault),
        "current_directives": current,
        "stats_md": stats_md[:PHASE6_MAX_STATS_CHARS],
        "failure_class_counts": class_counts,
        "candidate_failure_tags": candidate_tags,
        "bounds": _bounds_payload(vault, current),
    }
    parsed, reason = run_structured_agent(runner, "overseer", payload)
    if reason:
        log.error("Overseer failed: %s — keeping prior directives", reason)
        return

    from orchestrator.research import split_yaml_frontmatter_markdown

    proposal = parsed.get("new_directives_markdown")
    unchanged = False
    try:
        unchanged = (
            not changed_knobs(current, proposal)
            and split_yaml_frontmatter_markdown(current)[1]
            == split_yaml_frontmatter_markdown(proposal)[1]
        )
    except (ValueError, TypeError, AttributeError):
        pass  # malformed: let write_directives reject it
    if not unchanged and notify.approve_directives_enabled():
        # Same guards as write_directives, but nothing is applied: the proposal waits for a vote.
        if _write_directive_proposal(vault, parsed, current, stats_md, n_resolved, now) is None:
            return
        log.info("[PHASE 6] directive proposal written to the concierge outbox; awaiting vote")
        return
    if not unchanged:
        try:
            vault.write_directives(parsed)
        except VaultWriteError as exc:
            log.error(
                "Directives rejected: %s — keeping prior directives (v%d)",
                exc.cause, ledger.current_directive_version(vault),
            )
            return
    else:
        log.info("[PHASE 6] Overseer proposed no change; keeping v%d", ledger.current_directive_version(vault))
    vault.patch_directives_frontmatter(
        {"last_overseer_ledger_count": n_resolved, "last_overseer_at": now.isoformat()}
    )


# ---------------------------------------------------------------- directive approval (concierge)

_PROPOSAL_STATS_CHARS = 1500


def _pending_dir(vault: ObsidianManager):
    path = notify.outbox_dir(vault) / "_pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_directive_proposal(
    vault: ObsidianManager,
    parsed: dict[str, Any],
    current_md: str,
    stats_md: str,
    n_resolved: int,
    now: datetime,
):
    """Validate like write_directives would, then write outbox/directive-v<N>.md (+ pending payload)."""
    from datetime import timedelta

    from obsidian_utils import DirectivesPayload

    try:
        new_md = DirectivesPayload.model_validate(parsed).new_directives_markdown
        base_version = vault.latest_directives_version()
        new_version = base_version + 1
        validate_directive_change(current_md, new_md, new_version)
    except (DirectiveBoundsError, ValueError) as exc:
        log.error("Directives rejected: %s - keeping prior directives (v%d)", exc,
                  ledger.current_directive_version(vault))
        return None
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError etc.
        log.error("Directives rejected: %s - keeping prior directives (v%d)", exc,
                  ledger.current_directive_version(vault))
        return None
    old_k, new_k = effective_knobs(current_md), effective_knobs(new_md)
    diff = [f"- {k}: {old_k[k]} -> {new_k[k]}" for k in changed_knobs(current_md, new_md)]
    proposal_id = f"{notify.DIRECTIVE_ID_PREFIX}v{new_version}"
    expires_at = now + timedelta(seconds=notify.directive_ttl_sec())
    rationale = str(parsed.get("rationale") or "").strip()
    body = (
        f"---\nproposal_id: {proposal_id}\nbase_version: {base_version}\nnew_version: {new_version}\n"
        f"created_at: {now.isoformat()}\nexpires_at: {expires_at.isoformat()}\n---\n\n"
        f"# Overseer directive proposal {proposal_id}\n\n"
        f"## Knob changes\n\n" + ("\n".join(diff) or "- (no knob change; text only)") + "\n\n"
        f"## Reason\n\n{rationale or '(none given)'}\n\n"
        f"## Stats excerpt\n\n{stats_md[:_PROPOSAL_STATS_CHARS]}\n\n"
        f"Reply yes or no. Vote file: inbox/vote-{proposal_id}.json\n"
    )
    payload = {
        "proposal_id": proposal_id, "base_version": base_version, "new_version": new_version,
        "expires_at": expires_at.isoformat(), "parsed": parsed,
        "meta": {"last_overseer_ledger_count": n_resolved, "last_overseer_at": now.isoformat()},
    }
    notify._write_json(_pending_dir(vault) / f"{proposal_id}.json", payload)
    path = notify.outbox_dir(vault) / f"{proposal_id}.md"
    path.write_text(body, encoding="utf-8")
    vault.patch_directives_frontmatter(payload["meta"])  # throttle the Overseer like an applied run
    return path


def process_directive_votes(vault: ObsidianManager, now: datetime | None = None) -> dict[str, int]:
    """Handle ``inbox/vote-directive-v<N>.json`` and expire unvoted proposals. Never raises per file.

    yes before expiry: re-validate against the CURRENT directives (rejected if the version moved) and
    apply through ``vault.write_directives``. no or expired: dropped. Offer votes (other id prefix)
    are left for ``p4_execution.process_inbox``.
    """
    now = now or datetime.now(timezone.utc)
    summary = {"applied": 0, "rejected": 0, "declined": 0, "expired": 0, "ignored": 0}
    inbox, outbox, pending = notify.inbox_dir(vault), notify.outbox_dir(vault), _pending_dir(vault)

    def finish(pid: str, outcome: str, vote_path=None) -> None:
        log.info("[PHASE 6] directive proposal %s: %s", pid, outcome)
        for p in (pending / f"{pid}.json", outbox / f"{pid}.md", vote_path):
            if p is not None and p.exists():
                notify.move_to_done(p)

    def load(pid: str) -> dict[str, Any] | None:
        return notify.read_json(pending / f"{pid}.json")

    def is_expired(prop: dict[str, Any]) -> bool:
        exp = notify.parse_ts(prop.get("expires_at"))
        return exp is not None and now > exp

    for vote_path in sorted(inbox.glob(f"vote-{notify.DIRECTIVE_ID_PREFIX}*.json")):
        try:
            vote = notify.read_json(vote_path) or {}
            pid = vote_path.stem[len("vote-"):]
            if not re.fullmatch(r"directive-v\d+", pid):
                summary["ignored"] += 1
                notify.move_to_done(vote_path)
                continue
            choice = str(vote.get("vote") or "").strip().lower()
            prop = load(pid)
            if prop is None or choice not in ("yes", "no"):
                log.warning("[PHASE 6] ignoring directive vote %s (unknown proposal or bad vote)", vote_path.name)
                summary["ignored"] += 1
                notify.move_to_done(vote_path)
                continue
            if is_expired(prop):
                finish(pid, "expired before vote - dropped", vote_path)
                summary["expired"] += 1
            elif choice == "no":
                finish(pid, "declined - dropped", vote_path)
                summary["declined"] += 1
            elif vault.latest_directives_version() != prop.get("base_version"):
                finish(pid, f"rejected: directives moved from v{prop.get('base_version')} "
                            f"to v{vault.latest_directives_version()}", vote_path)
                summary["rejected"] += 1
            else:
                try:
                    vault.write_directives(prop["parsed"])  # re-validates against CURRENT directives
                except VaultWriteError as exc:
                    finish(pid, f"rejected on re-validation: {exc.cause}", vote_path)
                    summary["rejected"] += 1
                    continue
                vault.patch_directives_frontmatter(prop.get("meta") or {})
                finish(pid, f"approved - applied as v{vault.latest_directives_version()}", vote_path)
                summary["applied"] += 1
        except Exception:  # noqa: BLE001 - one bad vote must not block the rest
            log.exception("[PHASE 6] failed processing %s", vote_path.name)

    for pend in sorted(pending.glob(f"{notify.DIRECTIVE_ID_PREFIX}v*.json")):  # unvoted, past expiry
        try:
            prop = notify.read_json(pend) or {}
            if is_expired(prop):
                finish(pend.stem, "expired with no vote - dropped")
                summary["expired"] += 1
        except Exception:  # noqa: BLE001
            log.exception("[PHASE 6] failed expiring %s", pend.name)
    return summary
