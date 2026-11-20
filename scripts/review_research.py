#!/usr/bin/env python3
"""Build review packets from recent research notes, and record reviewer verdicts.

  python3 scripts/review_research.py packets [VAULT] [-n 20] [--format md|json] [--gamma] [--blind] [-o FILE]
  python3 scripts/review_research.py record  [VAULT] --verdicts verdicts.json

Packets: one per market, from Vault/02_Active_Research/<id>.md (frontmatter), its bundle in
02_Active_Research/research_bundles/<id>.json and the latest research row of 05_Ledger/decisions.jsonl.
A reviewer agent (rubric: docs/07_evaluation.md) scores each packet GOOD / OK / BAD; `record` appends the verdicts
to Vault/00_System/research_reviews.md. Read-only on the vault except for that file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VAULT = "/opt/pmbot/Vault"
GRADES = ("GOOD", "OK", "BAD")
URL_RE = re.compile(r"https?://[^\s|)\]>]+")
REVIEWS_HEADER = "# Research reviews\n\n| reviewed_at | market_id | pipeline | grade | own_p | bot_p | notes |\n|---|---|---|---|---|---|---|\n"


def _vault_root(arg: str | None) -> Path:
    root = Path(arg or DEFAULT_VAULT)
    if not (root / "02_Active_Research").is_dir():
        raise SystemExit(f"not a vault (no 02_Active_Research): {root}")
    return root


def parse_note(path: Path) -> tuple[dict[str, Any], str]:
    import yaml

    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _sort_key(path: Path, fm: dict) -> float:
    raw = str(fm.get("researched_at") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return path.stat().st_mtime


def ledger_rows(vault: Path) -> dict[str, dict]:
    """Latest research-stage ledger row per market."""
    out: dict[str, dict] = {}
    path = vault / "05_Ledger" / "decisions.jsonl"
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("stage", "")).startswith("research") and row.get("market_id"):
            out[row["market_id"]] = row
    return out


def cited_lines(vault: Path, market_id: str) -> list[dict[str, str]]:
    path = vault / "02_Active_Research" / "research_bundles" / f"{market_id}.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    out = []
    for q in data.get("queries", []):
        for line in str(q.get("research_data") or "").splitlines():
            m = URL_RE.search(line)
            if m:
                out.append({"query": str(q.get("query") or ""), "line": line.strip(), "url": m.group(0)})
    return out


def gamma_market(market_id: str) -> dict[str, str]:
    """Question and rules from Gamma (best effort; {} on any failure)."""
    import requests

    base = os.environ.get("GAMMA_BASE_URL", "https://gamma-api.polymarket.com").rstrip("/")
    try:
        r = requests.get(f"{base}/markets", params={"condition_ids": market_id, "closed": "true"}, timeout=15)
        data = r.json()
        if r.status_code == 200 and isinstance(data, list) and data:
            r2 = r
        else:
            r2 = requests.get(f"{base}/markets", params={"condition_ids": market_id}, timeout=15)
            data = r2.json()
        if isinstance(data, list) and data:
            return {"question": data[0].get("question") or "", "rules": data[0].get("description") or ""}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _question_from_note(fm: dict, body: str) -> str:
    for k in ("question", "title"):
        if fm.get(k):
            return str(fm[k])
    m = re.search(r"^# (.+)$", body, re.M)
    return m.group(1).strip() if m else ""


def build_packets(vault_arg: str | None, n: int, *, use_gamma: bool = False, blind: bool = False) -> list[dict]:
    vault = _vault_root(vault_arg)
    notes = []
    for p in (vault / "02_Active_Research").glob("*.md"):
        fm, body = parse_note(p)
        notes.append((_sort_key(p, fm), p, fm, body))
    notes.sort(key=lambda t: t[0], reverse=True)
    ledger = ledger_rows(vault)
    packets = []
    for _, p, fm, body in notes[: max(0, n)]:
        mid = p.stem  # filename is the condition id (YAML may parse an unquoted 0x.. as an int)
        lrow = ledger.get(mid, {})
        g = gamma_market(mid) if use_gamma else {}
        packet = {
            "market_id": mid,
            "question": g.get("question") or _question_from_note(fm, body),
            "rules": g.get("rules") or "",
            "resolution_checklist": fm.get("resolution_checklist") or [],
            "end_date": fm.get("end_date"),
            "as_of": fm.get("as_of"),
            "researched_at": fm.get("researched_at"),
            "pipeline": fm.get("pipeline") or lrow.get("pipeline") or "legacy",
            "evidence_source": fm.get("evidence_source"),
            "bot": {"p": fm.get("estimated_p"), "p_low": fm.get("p_low"), "p_high": fm.get("p_high"),
                    "samples": fm.get("samples"), "confidence": fm.get("confidence"),
                    "confidence_level": fm.get("confidence_level"),
                    "confidence_reasons": fm.get("confidence_reasons"),
                    "judge_confidence_score": fm.get("judge_confidence_score"),
                    "judge_confidence_reason": fm.get("judge_confidence_reason")},
            "cited_lines": cited_lines(vault, mid),
            "note_body": body.strip(),
        }
        if not blind:
            packet["bot"]["q"] = fm.get("q_at_research", lrow.get("q"))
        packets.append(packet)
    return packets


def render_packet_md(pk: dict) -> str:
    b = pk["bot"]
    out = [f"## {pk['question'] or pk['market_id']}", "", f"- market_id: {pk['market_id']}", f"- pipeline: {pk['pipeline']}",
           f"- as_of: {pk['as_of']}  end_date: {pk['end_date']}  evidence: {pk['evidence_source']}", "",
           "### Rules", pk["rules"] or "(not fetched; use --gamma or the checklist below)", ""]
    if pk["resolution_checklist"]:
        out += ["Resolution checklist (bot's reading):"] + [f"- {c}" for c in pk["resolution_checklist"]] + [""]
    out += ["### Cited evidence lines", ""]
    out += [f"- {c['line']}" for c in pk["cited_lines"]] or ["(none)"]
    out += ["", "### Bot note body", "", pk["note_body"] or "(empty)", "",
            "### Bot estimate (read AFTER forming your own p)", "",
            f"- p: {b['p']}  range: [{b['p_low']}, {b['p_high']}]  samples: {b['samples']}  confidence: {b['confidence']}"]
    if b.get("confidence_level") or b.get("judge_confidence_score") is not None:
        out.append(
            f"- code confidence: {b.get('confidence_level')} {b.get('confidence_reasons') or []}  "
            f"judge score: {b.get('judge_confidence_score')} ({b.get('judge_confidence_reason') or 'no reason'})"
        )
    if "q" in b:
        out.append(f"- market q at research: {b['q']}")
    return "\n".join(out) + "\n"


def append_reviews(vault_arg: str | None, verdicts: list[dict]) -> Path:
    vault = _vault_root(vault_arg)
    path = vault / "00_System" / "research_reviews.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    for v in verdicts:
        grade = str(v.get("grade", "")).upper()
        if grade not in GRADES:
            raise ValueError(f"grade must be one of {GRADES}, got {v.get('grade')!r}")
        if not v.get("market_id"):
            raise ValueError("verdict without market_id")
        note = str(v.get("notes", "")).replace("|", "/").replace("\n", " ")
        rows.append(f"| {now} | {v['market_id']} | {v.get('pipeline', '')} | {grade} | {v.get('own_p', '')} | {v.get('bot_p', '')} | {note} |")
    text = path.read_text(encoding="utf-8") if path.is_file() else REVIEWS_HEADER
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("packets")
    pk.add_argument("vault", nargs="?", default=DEFAULT_VAULT)
    pk.add_argument("-n", type=int, default=20)
    pk.add_argument("--format", choices=("md", "json"), default="md")
    pk.add_argument("--gamma", action="store_true", help="fetch question and rules from Gamma")
    pk.add_argument("--blind", action="store_true", help="omit the market price q from packets")
    pk.add_argument("-o", "--output")
    rc = sub.add_parser("record")
    rc.add_argument("vault", nargs="?", default=DEFAULT_VAULT)
    rc.add_argument("--verdicts", required=True, help="JSON list of {market_id, pipeline, grade, own_p, bot_p, notes}")
    args = ap.parse_args(argv)
    if args.cmd == "packets":
        packets = build_packets(args.vault, args.n, use_gamma=args.gamma, blind=args.blind)
        text = json.dumps(packets, indent=2, default=str) if args.format == "json" else "\n---\n\n".join(render_packet_md(p) for p in packets)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {len(packets)} packets to {args.output}")
        else:
            print(text)
        return 0
    verdicts = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))
    print(f"appended {len(verdicts)} reviews to {append_reviews(args.vault, verdicts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
