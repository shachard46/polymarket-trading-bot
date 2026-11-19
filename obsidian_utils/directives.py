"""Directives versioning, seeding and migration: a mixin for ObsidianManager."""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from config.trading_constants import (
    F_MAX,
    BOUNDS,
    FILTERS,
    RESEARCH_PIPELINE,
)

from .schemas import DirectivesPayload, VaultWriteError, _atomic_write_text, _dump_frontmatter

log = logging.getLogger(__name__)

_DIRECTIVES_FILENAME = "active_directives.md"


class DirectivesMixin:
    """Live directives file plus its ``directives_history/`` archive."""

    _dirs: dict[str, Path]

    # -- directives versioning ----------------------------------------------

    def directives_history_dir(self) -> Path:
        return self._dirs["system"] / "directives_history"

    def latest_directives_version(self) -> int:
        """Highest version number seen in the live file or the history archive."""
        from orchestrator.directives import directive_version

        best = directive_version(self.read_directives())
        hist = self.directives_history_dir()
        if hist.is_dir():
            for path in hist.glob("v*.md"):
                m = re.fullmatch(r"v(\d+)\.md", path.name)
                if m:
                    best = max(best, int(m.group(1)))
        return best

    def read_directives_version(self, version: int) -> str | None:
        path = self.directives_history_dir() / f"v{int(version)}.md"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def _install_directives(self, text: str, version: int) -> Path:
        """Atomically replace the live file (``.bak`` of the old one) and archive ``v{version}.md``."""
        dest = self._dirs["system"] / _DIRECTIVES_FILENAME
        if dest.exists():
            shutil.copy2(dest, dest.with_name(dest.name + ".bak"))
        hist = self.directives_history_dir()
        hist.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(hist / f"v{version}.md", text)
        _atomic_write_text(dest, text)
        return dest

    def _next_directives_version(self) -> int:
        return self.latest_directives_version() + 1

    def write_directives(
        self,
        payload: dict,
        *,
        enforce_bounds: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> Path:
        """Validate an Overseer payload and install it as the next directives version.

        The Hub owns the bookkeeping frontmatter: ``version`` (monotonic int, previous + 1),
        ``knob_last_changed`` (knob -> version), ``last_overseer_*`` (carried over) and any
        ``meta`` fields; whatever the Overseer put in those keys is discarded. The previous file is
        copied to ``.bak`` and the new one archived to ``directives_history/v{N}.md``.

        Raises
        ------
        VaultWriteError
            If the payload fails ``DirectivesPayload`` validation, or (``enforce_bounds``) the
            ``filters:`` change violates the guard-rails in ``config.trading_constants.BOUNDS``.
            In both cases nothing is written.
        """
        from orchestrator.directives import (
            DirectiveBoundsError,
            changed_knobs,
            validate_directive_change,
        )
        from orchestrator.research import split_yaml_frontmatter_markdown

        try:
            validated = DirectivesPayload.model_validate(payload)
        except ValidationError as exc:
            raise VaultWriteError("__system__", exc) from exc

        old = self.read_directives()
        new_version = self._next_directives_version()
        new_md = validated.new_directives_markdown
        try:
            if enforce_bounds:
                changed = validate_directive_change(old, new_md, new_version)
            else:
                changed = changed_knobs(old, new_md)
        except DirectiveBoundsError as exc:
            raise VaultWriteError("__system__", exc) from exc  # type: ignore[arg-type]

        fm, body = split_yaml_frontmatter_markdown(new_md)
        try:
            old_fm = split_yaml_frontmatter_markdown(old)[0] if old.strip() else {}
        except ValueError:
            old_fm = {}
        for key in ("restored_from", "restore_note", "migrated_from",
                    "last_overseer_ledger_count", "last_overseer_at"):
            fm.pop(key, None)
        for key in ("last_overseer_ledger_count", "last_overseer_at"):
            if key in old_fm:
                fm[key] = old_fm[key]
        klc = old_fm.get("knob_last_changed")
        klc = dict(klc) if isinstance(klc, dict) else {}
        for key in changed:
            klc[key] = new_version
        fm["version"] = new_version
        fm["knob_last_changed"] = klc
        fm.update(meta or {})
        return self._install_directives(_dump_frontmatter(fm, body) + "\n", new_version)

    def patch_directives_frontmatter(self, updates: dict[str, Any]) -> None:
        """Merge ``updates`` into the live directives frontmatter (no version bump)."""
        from orchestrator.research import split_yaml_frontmatter_markdown

        dest = self._dirs["system"] / _DIRECTIVES_FILENAME
        fm, body = split_yaml_frontmatter_markdown(dest.read_text(encoding="utf-8"))
        fm.update(updates)
        _atomic_write_text(dest, _dump_frontmatter(fm, body) + "\n")

    def restore_directives(self, version: int, reason: str) -> int:
        """Reinstall archived ``v{version}`` as a NEW version; return the new version number.

        Bounds are not enforced (it is a known-good earlier document). The new frontmatter
        records ``restored_from`` and ``restore_note``; knobs that differ from the replaced
        document restart their cooldown.
        """
        text = self.read_directives_version(version)
        if text is None:
            raise FileNotFoundError(f"no archived directives v{version}")
        self.write_directives(
            {"new_directives_markdown": text, "rationale": reason},
            enforce_bounds=False,
            meta={"restored_from": int(version), "restore_note": reason},
        )
        return self.latest_directives_version()

    def read_directives(self) -> str:
        """Return the raw contents of ``active_directives.md``.

        Returns
        -------
        str
            Full file contents, or an empty string if the file does not exist.
        """
        directives_path = self._dirs["system"] / _DIRECTIVES_FILENAME
        if not directives_path.exists():
            return ""
        return directives_path.read_text(encoding="utf-8")

    def cold_start_protocol(self) -> bool:
        """Write a seed ``active_directives.md`` if the file is missing or empty.

        The seed file embeds the neutral defaults from
        ``config/trading_constants.py`` so the Deep Researcher has a
        functional baseline before the Overseer completes its first learning
        loop.

        Returns
        -------
        bool
            ``True`` if the seed was written, ``False`` if a non-empty
            directives file already exists.
        """
        directives_path = self._dirs["system"] / _DIRECTIVES_FILENAME
        if directives_path.exists() and directives_path.stat().st_size > 0:
            self._migrate_directives_version()
            self._migrate_research_pipeline()
            return False

        seed_content = _build_seed_directives()
        directives_path.parent.mkdir(parents=True, exist_ok=True)
        self._install_directives(seed_content, 1)
        return True

    def _migrate_research_pipeline(self) -> None:
        """Bump the directives version once when the research pipeline changes (a new Brier cohort).

        ``research_max_sample_spread`` changed meaning with ``crux_v1`` (the gap between the J1 and flipped
        J2 judges, not the spread of 3 samples), so scores before and after must not share a directives version.
        The knobs are untouched; the frontmatter records the pipeline and a note, and the old file is archived.
        """
        from orchestrator.research import split_yaml_frontmatter_markdown  # noqa: PLC0415

        text = self.read_directives()
        try:
            fm, _ = split_yaml_frontmatter_markdown(text)
        except ValueError:
            return
        if fm.get("research_pipeline") == RESEARCH_PIPELINE:
            return
        try:
            self.write_directives(
                {
                    "new_directives_markdown": text,
                    "rationale": f"Hub migration: research pipeline {RESEARCH_PIPELINE} (new scoring cohort; knobs unchanged).",
                },
                enforce_bounds=False,
                meta={
                    "research_pipeline": RESEARCH_PIPELINE,
                    "pipeline_note": (
                        f"{RESEARCH_PIPELINE}: research_max_sample_spread now bounds the gap between the "
                        "J1 (P(YES)) and flipped J2 (P(NO)) judges; version bumped, knob values unchanged."
                    ),
                },
            )
        except Exception:  # noqa: BLE001 - a migration must never stop the daemon
            log.exception("could not stamp directives with research_pipeline=%s", RESEARCH_PIPELINE)

    def _migrate_directives_version(self) -> None:
        """Rewrite a legacy non-integer ``version`` (e.g. ``"0.1-seed"``) as integer 1."""
        from orchestrator.research import split_yaml_frontmatter_markdown

        text = self.read_directives()
        try:
            fm, body = split_yaml_frontmatter_markdown(text)
        except ValueError:
            return
        raw = fm.get("version")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return
        fm["migrated_from"] = str(raw)
        fm["version"] = 1
        fm.setdefault("knob_last_changed", {})
        self._install_directives(_dump_frontmatter(fm, body) + "\n", 1)


# ---------------------------------------------------------------------------
# Cold Start seed builder (module-level helper, not part of public API)
# ---------------------------------------------------------------------------

def _build_seed_directives() -> str:
    """Return the full text of a seed ``active_directives.md``.

    Embeds live values from ``trading_constants`` so the document is always
    consistent with the rest of the configuration.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    # Build a YAML-safe representation of the FILTERS dict
    filters_yaml_lines = "\n".join(
        f"  {key}: {FILTERS[key]}" for key in BOUNDS
    )

    frontmatter_block = f"""\
---
version: 1
knob_last_changed: {{}}
seeded_at: "{now_iso}"
overseer_updated: false
research_pipeline: {RESEARCH_PIPELINE}
---"""

    body = f"""\
# Active Directives for the Deep Researcher

> **NOTE — COLD START SEED**
> This file was auto-generated by the Cold Start Protocol because no prior
> directives existed.  It will be overwritten the first time the Overseer
> completes a learning loop.

---

## Research Protocol

Apply this methodology to every market before forming `estimated_p`:

1. **Resolution first** — Work out exactly what resolves YES, which source
   decides, and whether YES is the event in the title or its inverse.
2. **Base Rate Anchoring** — Start from a calibrated base rate for the event
   class (e.g., incumbent re-election rates, historical merger completion
   rates) over the remaining window before adjusting for specific evidence.
3. **Verified Present Facts** — Adjust only for facts in the `research_bundle`
   that are dated and sourced.  Treat failed fetches as missing, not as evidence.
4. **Upcoming Catalysts** — Identify scheduled events (earnings, elections,
   regulatory decisions, economic releases) within the resolution window that
   could sharply shift the probability.
5. **Disconfirm** — Look for the strongest evidence against your leading view
   before you commit.  Inconclusive evidence means a wide range and
   `confidence: "low"`.

---

## Filter Weightings (Neutral Defaults)

The `filters:` block below is applied by Hub code and tuned only by the
Overseer within bounds.  It is not an input to your estimate.

```yaml
filters:
{filters_yaml_lines}
```

---

## Risk Constraints

Sizing is deterministic Hub code (`F_MAX` = `{F_MAX}`).  Do not factor position
size, liquidity or trading mode into `estimated_p`.

---

## Output Requirements

Follow the output format of your system prompt exactly.  The document
frontmatter carries `market_id`, `estimated_p`, `p_low`, `p_high` and `error`:

```
---
market_id: "<id>"
estimated_p: <float between 0.0 and 1.0>
p_low: <float, <= estimated_p>
p_high: <float, >= estimated_p>
error: null
---

## Bull Thesis

<2-3 fact-backed bullets>

## Bear Thesis

<2-3 fact-backed bullets>

## Post-Mortem
```

Leave the `## Post-Mortem` section empty.  The Orchestrator will populate
it after the market resolves.

---

*This seed is active until the Overseer completes its first learning loop.*
"""

    return frontmatter_block + "\n\n" + body
