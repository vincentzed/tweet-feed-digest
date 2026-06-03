"""Sync X-timeline digests/learnings into this repo and regenerate the README index.

The summarizer pipeline writes `digest_<ts>.md` / `learnings_<ts>.md` files to its
output dir. This script copies the newest run per UTC date into `digests/` and
`learnings/` here (named `YYYY-MM-DD.md`), then rebuilds `README.md` as the index
GitHub renders. Run: `uv run sync.py` (optionally `--source <dir>`).
"""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.table import Table

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = (
    Path.home()
    / "Documents/Github/open_source/mine/company-scraper"
    / "classification/pipeline/src/pipeline/defs/output"
)

# digest_20260603_064826.md  ->  kind="digest", ts=2026-06-03 06:48:26 UTC
FILENAME_RE = re.compile(r"^(?P<kind>digest|learnings)_(?P<ts>\d{8}_\d{6})\.md$")

console = Console()


class DayEntry(BaseModel):
    """The canonical (newest) digest + learnings markdown for a single UTC date."""

    model_config = ConfigDict(frozen=True)

    day: date
    digest_src: Path | None = None
    digest_ts: datetime | None = None
    learnings_src: Path | None = None
    learnings_ts: datetime | None = None


def _parse_ts(stem_ts: str) -> datetime:
    return datetime.strptime(stem_ts, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)


def collect_entries(source: Path) -> list[DayEntry]:
    """Group source markdown by UTC date, keeping the latest run per kind per day."""
    latest: dict[date, dict[str, tuple[datetime, Path]]] = defaultdict(dict)
    for path in source.glob("*.md"):
        m = FILENAME_RE.match(path.name)
        if not m:
            continue
        ts = _parse_ts(m["ts"])
        kind = m["kind"]
        prev = latest[ts.date()].get(kind)
        if prev is None or ts > prev[0]:
            latest[ts.date()][kind] = (ts, path)

    entries: list[DayEntry] = []
    for day in sorted(latest, reverse=True):
        kinds = latest[day]
        digest = kinds.get("digest")
        learnings = kinds.get("learnings")
        entries.append(
            DayEntry(
                day=day,
                digest_src=digest[1] if digest else None,
                digest_ts=digest[0] if digest else None,
                learnings_src=learnings[1] if learnings else None,
                learnings_ts=learnings[0] if learnings else None,
            )
        )
    return entries


def write_outputs(entries: list[DayEntry], repo_root: Path) -> int:
    """Copy each entry's source files into digests/ and learnings/. Returns files written."""
    digests_dir = repo_root / "digests"
    learnings_dir = repo_root / "learnings"
    digests_dir.mkdir(exist_ok=True)
    learnings_dir.mkdir(exist_ok=True)

    written = 0
    for e in entries:
        name = f"{e.day.isoformat()}.md"
        if e.digest_src is not None:
            shutil.copyfile(e.digest_src, digests_dir / name)
            written += 1
        if e.learnings_src is not None:
            shutil.copyfile(e.learnings_src, learnings_dir / name)
            written += 1
    return written


def render_readme(entries: list[DayEntry]) -> str:
    """Build the index README grouped by month, newest first."""
    lines: list[str] = [
        "# Tweet Feed Digest",
        "",
        "Daily AI/tech digests distilled from an X/Twitter timeline by an LLM summarizer.",
        "Each day's feed is scraped, deduplicated, scored, and summarized into a technical "
        "digest plus an extracted-learnings companion. This repo is the published archive — "
        "GitHub renders the markdown directly.",
        "",
    ]

    if not entries:
        lines += ["_No digests yet._", ""]
        return "\n".join(lines)

    latest = entries[0]
    if latest.digest_src is not None:
        lines += [
            f"**Latest:** [{latest.day.isoformat()}](digests/{latest.day.isoformat()}.md)"
            + (
                f" · [learnings](learnings/{latest.day.isoformat()}.md)"
                if latest.learnings_src is not None
                else ""
            ),
            "",
        ]

    lines += [f"**{len(entries)} days archived.**", "", "---", ""]

    by_month: dict[str, list[DayEntry]] = defaultdict(list)
    for e in entries:
        by_month[e.day.strftime("%Y-%m")].append(e)

    for month in sorted(by_month, reverse=True):
        heading = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
        lines += [f"## {heading}", "", "| Date | Digest | Learnings |", "| --- | --- | --- |"]
        for e in by_month[month]:
            iso = e.day.isoformat()
            digest_cell = f"[digest](digests/{iso}.md)" if e.digest_src else "—"
            learn_cell = f"[learnings](learnings/{iso}.md)" if e.learnings_src else "—"
            lines.append(f"| {iso} | {digest_cell} | {learn_cell} |")
        lines.append("")

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines += ["---", "", f"_Index regenerated {stamp} by `sync.py`._", ""]
    return "\n".join(lines)


def main(
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            "-s",
            help="Directory holding digest_*.md / learnings_*.md from the pipeline.",
            exists=True,
            file_okay=False,
            dir_okay=True,
        ),
    ] = DEFAULT_SOURCE,
    repo_root: Annotated[
        Path,
        typer.Option("--repo-root", help="Repo root to write into.", file_okay=False),
    ] = REPO_ROOT,
) -> None:
    """Copy the newest digest/learnings per day from SOURCE and rebuild README.md."""
    entries = collect_entries(source)
    if not entries:
        console.print(f"[yellow]No digest/learnings markdown found in[/] {source}")
        raise typer.Exit(1)

    written = write_outputs(entries, repo_root)
    readme = render_readme(entries)
    (repo_root / "README.md").write_text(readme, encoding="utf-8")

    table = Table(title="Synced digests", show_edge=False)
    table.add_column("Date", style="cyan")
    table.add_column("Digest", justify="center")
    table.add_column("Learnings", justify="center")
    for e in entries[:10]:
        table.add_row(
            e.day.isoformat(),
            "✓" if e.digest_src else "—",
            "✓" if e.learnings_src else "—",
        )
    console.print(table)
    if len(entries) > 10:
        console.print(f"[dim]… and {len(entries) - 10} earlier days.[/]")
    console.print(f"[green]Wrote {written} files across {len(entries)} days; README.md rebuilt.[/]")


if __name__ == "__main__":
    typer.run(main)
