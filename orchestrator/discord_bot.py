"""Built-in Discord front-end for the concierge outbox (separate process, never imported by the Hub).

Run: ``python3 -m orchestrator.discord_bot``. It relays ``06_Concierge/outbox`` offers, digests and
directive proposals to one channel, turns owner button clicks into ``inbox/vote-<id>.json`` files and
answers owner questions read-only. It never trades and never touches knobs; the Hub validates votes.

Env: ``DISCORD_BOT_TOKEN``, ``DISCORD_CHANNEL_ID``, ``DISCORD_OWNER_ID`` (all required),
``CONCIERGE_TZ`` (default UTC), ``CONCIERGE_QUIET_HOURS`` (e.g. ``23-07``), ``CONCIERGE_POLL_SEC``.
The outbox marker ``outbox/_posted.json`` is shared with the OpenClaw concierge (list of file names).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obsidian_utils import write_json_atomic
from obsidian_utils import read_json as _read_json
from orchestrator import notify

log = logging.getLogger(__name__)

POSTED_FILE = "_posted.json"
MAX_MSG = 1900  # Discord hard limit is 2000
QA_MAX_CONTEXT_CHARS = 24000
QA_LEDGER_ROWS = 40
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
_VOTE_ID_PREFIX = "vote:"  # button custom_id = vote:<yes|no>:<id>

_FALLBACK_QA_PROMPT = (
    "You are the read-only Q&A front-end of a Polymarket paper-trading research bot. Answer ONLY from the "
    "context provided (stats.md, ledger rows, research notes). Quote only numbers that appear in the context, "
    "naming the source. If the context does not contain the answer, say so. Never invent, estimate or "
    "extrapolate. Never claim a trade happened unless the context says so. You cannot trade, vote or change "
    "settings."
)


# ---------------------------------------------------------------- pure helpers


def _s(value: Any) -> str:
    return "n/a" if value is None or value == "" else str(value)


def _clip(text: str, limit: int = MAX_MSG) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def is_owner(user_id: Any, owner_id: Any) -> bool:
    try:
        return int(user_id) == int(owner_id)
    except (TypeError, ValueError):
        return False


def is_expired(expires_at: Any, now: datetime | None = None) -> bool:
    exp = notify.parse_ts(expires_at)
    if exp is None:
        return False
    return (now or datetime.now(tz=timezone.utc)) > exp


def in_quiet_hours(spec: str | None, now: datetime | None = None, tz: str | None = None) -> bool:
    """``spec`` is ``HH-HH`` (start inclusive, end exclusive, may wrap midnight) in timezone ``tz``."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", spec or "")
    if not m:
        return False
    start, end = int(m.group(1)) % 24, int(m.group(2)) % 24
    now = now or datetime.now(tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        now = now.astimezone(ZoneInfo(tz)) if tz else now.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001 - bad tz name: fall back to UTC
        now = now.astimezone(timezone.utc)
    h = now.hour
    if start == end:
        return False
    return start <= h < end if start < end else (h >= start or h < end)


def render_offer(offer: dict[str, Any]) -> str:
    approval = offer.get("mode") == "approval"
    lines = [
        f"BUY OFFER {_s(offer.get('offer_id'))}",
        f"Market: {_s(offer.get('question'))} ({_s(offer.get('url'))})",
        f"Side: {_s(offer.get('side'))} | Size: ${_s(offer.get('allocation_usd'))} ({_s(offer.get('shares'))} shares) @ ask {_s(offer.get('ask'))}",
        f"p: {_s(offer.get('p_low'))}-{_s(offer.get('p_high'))} (point {_s(offer.get('p'))}) vs q: {_s(offer.get('q_entry'))} | edge: {_s(offer.get('edge'))}",
        f"Integrity: manipulation_risk={_s(offer.get('manipulation_risk'))}, integ_score={_s(offer.get('integ_score'))}, flags={_s(offer.get('integ_flags'))}",
        f"Evidence: {_s(offer.get('evidence_summary'))}",
        f"Expires: {_s(offer.get('expires_at'))}",
        f"Mode: {_s(offer.get('mode'))}   (approval = needs your vote; informational = already placed)",
    ]
    if approval:
        lines.append("Vote with the buttons below.")
    return _clip("\n".join(lines))


def render_digest(d: dict[str, Any]) -> str:
    rejects = d.get("integrity_rejects") or {}
    rej = ", ".join(f"{k}={v}" for k, v in sorted(rejects.items())) or "none"
    return _clip("\n".join([
        f"DAILY DIGEST {_s(d.get('date'))}",
        f"Realised P&L (after tax): {_s(d.get('realised_pnl_after_tax'))}",
        f"Counterfactual P&L: {_s(d.get('counterfactual_pnl_after_tax'))}",
        f"Brier p: {_s(d.get('brier_p'))} vs q: {_s(d.get('brier_q'))}",
        f"Resolved: {_s(d.get('n_resolved'))} (this week {_s(d.get('resolved_this_week'))})",
        f"New trades: {_s(d.get('new_trades'))} | New offers: {_s(d.get('new_offers'))}",
        f"Integrity rejects: {rej}",
        f"Pending directive proposal: {_s(d.get('pending_directive_proposal'))}",
    ]))


def parse_directive(text: str) -> tuple[dict[str, str], str]:
    """Split ``directive-vN.md`` into (flat frontmatter, body)."""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = parts[2]
    return meta, body.strip()


def render_directive(text: str) -> str:
    meta, body = parse_directive(text)
    head = (
        f"DIRECTIVE PROPOSAL {_s(meta.get('proposal_id'))}\n"
        f"Expires: {_s(meta.get('expires_at'))}\n"
    )
    return _clip(head + body + "\nVote with the buttons below.")


def load_posted(outbox: Path) -> set[str]:
    data = _read_json(outbox / POSTED_FILE)
    return {str(x) for x in data} if isinstance(data, list) else set()


def save_posted(outbox: Path, posted: set[str]) -> None:
    write_json_atomic(outbox / POSTED_FILE, sorted(posted))


def pending_posts(outbox: Path, posted: set[str]) -> list[Path]:
    """Outbox files not yet posted: offers, digests, directive proposals (top level only), oldest name first."""
    found: list[Path] = []
    for pattern in ("offer-*.json", "digest-*.json", "directive-*.md"):
        found += [p for p in outbox.glob(pattern) if p.is_file() and p.name not in posted]
    return sorted(found, key=lambda p: p.name)


def post_kind(path: Path) -> str:
    return path.name.split("-", 1)[0]  # offer | digest | directive


def item_id(path: Path, data: dict[str, Any] | None = None) -> str:
    """Vote id: offer_id for offers, ``directive-v<N>`` for proposals."""
    if post_kind(path) == "offer":
        return str((data or {}).get("offer_id") or path.stem[len("offer-"):])
    return path.stem


def needs_vote(path: Path, data: dict[str, Any] | None = None) -> bool:
    kind = post_kind(path)
    return kind == "directive" or (kind == "offer" and (data or {}).get("mode") == "approval")


def write_vote(inbox: Path, vote_id: str, vote: str, user_id: Any, owner_id: Any) -> Path | None:
    """Write ``vote-<id>.json`` (never overwrites). ``None`` if not the owner, bad input or already voted."""
    vote = str(vote).strip().lower()
    if not is_owner(user_id, owner_id) or vote not in ("yes", "no") or not _ID_RE.fullmatch(str(vote_id)):
        return None
    path = inbox / f"vote-{vote_id}.json"
    if path.exists() or (inbox / "_done" / path.name).exists():
        return None
    key = "proposal_id" if vote_id.startswith(notify.DIRECTIVE_ID_PREFIX) else "offer_id"
    write_json_atomic(path, {key: vote_id, "vote": vote})
    return path


def qa_system_prompt(agents_md: Path | None = None) -> str:
    """The ``## Q&A mode`` section of the concierge AGENTS.md, else the built-in prompt."""
    try:
        path = agents_md or Path(__file__).resolve().parent.parent / "agents_blueprint" / "concierge" / "AGENTS.md"
        text = path.read_text(encoding="utf-8")
        m = re.search(r"^## Q&A mode\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
        if m and m.group(1).strip():
            return m.group(1).strip()
    except OSError:
        pass
    return _FALLBACK_QA_PROMPT


def build_qa_context(vault: Any, question: str, max_chars: int = QA_MAX_CONTEXT_CHARS) -> str:
    """stats.md + last ledger rows + research notes for market ids named in ``question``; size-capped."""
    from orchestrator import ledger

    parts: list[str] = []
    try:
        stats_path = vault._dirs["system"] / "stats.md"
        if stats_path.exists():
            parts.append("## stats.md\n" + stats_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.warning("qa: stats.md unreadable")
    try:
        rows = list(ledger.iter_rows(vault))[-QA_LEDGER_ROWS:]
        parts.append("## ledger (last %d rows)\n" % len(rows) + "\n".join(json.dumps(r, default=str) for r in rows))
    except Exception:  # noqa: BLE001
        log.warning("qa: ledger unreadable")
    seen: set[str] = set()
    for mid in re.findall(r"0x[0-9a-fA-F]{6,}|\b\d{5,}\b", question):
        if mid in seen:
            continue
        seen.add(mid)
        try:
            path = vault.market_file(mid, "active")
            if path is not None:
                parts.append(f"## research note {mid}\n" + path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - bad id or unreadable note
            continue
    ctx = "\n\n".join(parts)
    return ctx if len(ctx) <= max_chars else ctx[:max_chars] + "\n[context truncated]"


def answer_question(vault: Any, question: str) -> str:
    """One read-only direct-backend call (blocking). ``complete`` is imported lazily."""
    from orchestrator.direct_runner import complete

    user_text = f"Context:\n{build_qa_context(vault, question)}\n\nQuestion: {question}"
    return _clip(str(complete(role="concierge", system_prompt=qa_system_prompt(), user_text=user_text)) or "(no answer)")


# ---------------------------------------------------------------- discord shell (untested)


def _env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def build_vote_view(discord: Any, vote_id: str, inbox: Path, owner_id: int, disabled: bool = False) -> Any:
    """Persistent Yes/No view (timeout=None, stable custom_ids ``vote:<yes|no>:<id>``)."""

    class VoteButton(discord.ui.Button):
        def __init__(self, choice: str):
            super().__init__(
                label=choice.capitalize(),
                style=discord.ButtonStyle.success if choice == "yes" else discord.ButtonStyle.danger,
                custom_id=vote_custom_id(choice, vote_id),
                disabled=disabled,
            )
            self.choice = choice

        async def callback(self, interaction: Any) -> None:
            if not is_owner(interaction.user.id, owner_id):
                await interaction.response.defer()  # ignore non-owner silently
                return
            path = await asyncio.to_thread(write_vote, inbox, vote_id, self.choice, interaction.user.id, owner_id)
            note = f"Voted {self.choice.upper()}" if path else "Vote not recorded (already voted or invalid)"
            content = (interaction.message.content if interaction.message else "") or ""
            done = build_vote_view(discord, vote_id, inbox, owner_id, disabled=True)
            await interaction.response.edit_message(content=_clip(content + f"\n\n{note}"), view=done)

    view = discord.ui.View(timeout=None)
    for choice in ("yes", "no"):
        view.add_item(VoteButton(choice))
    return view


def vote_custom_id(choice: str, vote_id: str) -> str:
    return f"{_VOTE_ID_PREFIX}{choice}:{vote_id}"[:100]


def main() -> None:  # pragma: no cover - thin discord.py shell
    import discord
    from discord.ext import tasks

    from obsidian_utils import ObsidianManager

    token = _env_required("DISCORD_BOT_TOKEN")  # never logged
    channel_id = int(_env_required("DISCORD_CHANNEL_ID"))
    owner_id = int(_env_required("DISCORD_OWNER_ID"))
    tz = os.environ.get("CONCIERGE_TZ") or None
    quiet = os.environ.get("CONCIERGE_QUIET_HOURS") or None
    logging.basicConfig(level=logging.INFO)

    vault = ObsidianManager()
    outbox, inbox = notify.outbox_dir(vault), notify.inbox_dir(vault)

    def vote_view(vote_id: str) -> Any:
        return build_vote_view(discord, vote_id, inbox, owner_id)

    intents = discord.Intents.default()
    intents.message_content = True

    class Bot(discord.Client):
        async def setup_hook(self) -> None:
            # persistent views survive restarts: re-register buttons for still-open posted items
            for path in list(outbox.glob("offer-*.json")) + list(outbox.glob("directive-*.md")):
                data = notify.read_json(path) if path.suffix == ".json" else parse_directive(path.read_text("utf-8"))[0]
                if needs_vote(path, data if path.suffix == ".json" else None) and not is_expired((data or {}).get("expires_at")):
                    self.add_view(vote_view(item_id(path, data if path.suffix == ".json" else None)))
            relay.start()

        async def on_message(self, msg: discord.Message) -> None:
            if msg.author.id != owner_id or self.user is None or msg.author.id == self.user.id:
                return  # only the owner may ask
            if not (isinstance(msg.channel, discord.DMChannel) or msg.channel.id == channel_id):
                return
            question = msg.content.strip()
            if not question:
                return
            async with msg.channel.typing():
                try:
                    reply = await asyncio.to_thread(answer_question, vault, question)
                except Exception:  # noqa: BLE001
                    log.exception("qa failed")
                    reply = "Q&A unavailable right now."
            await msg.reply(reply, mention_author=False)

    client = Bot(intents=intents)

    @tasks.loop(seconds=notify.poll_sec())
    async def relay() -> None:
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            posted = load_posted(outbox)
            for path in pending_posts(outbox, posted):
                kind = post_kind(path)
                if kind == "digest" and in_quiet_hours(quiet, tz=tz):
                    continue  # digests wait; offers/proposals expire so they go out
                if kind == "directive":
                    text = path.read_text(encoding="utf-8")
                    meta = parse_directive(text)[0]
                    data, content = None, render_directive(text)
                    expiry = meta.get("expires_at")
                else:
                    data = notify.read_json(path)
                    if data is None:
                        continue
                    content = render_offer(data) if kind == "offer" else render_digest(data)
                    expiry = data.get("expires_at")
                view = vote_view(item_id(path, data)) if needs_vote(path, data) and not is_expired(expiry) else None
                await channel.send(content, view=view) if view else await channel.send(content)
                posted.add(path.name)
                save_posted(outbox, posted)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("relay tick failed")

    @relay.before_loop
    async def _before_relay() -> None:
        await client.wait_until_ready()

    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
