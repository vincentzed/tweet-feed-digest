"""Sync X-timeline digests/learnings from Neon Postgres and regenerate the README index.

The summarizer pipeline saves each run into two Neon tables (shared with the NextJS UI):
`"nextjs-ui_summary"` (the digest) and `"nextjs-ui_learning"`. This script reads the newest
row per UTC date from each, writes it into `digests/` and `learnings/` here (named
`YYYY-MM-DD.md`, with the same heading the pipeline prepends), then rebuilds `README.md` as
the index GitHub renders.

Run: `uv run sync.py`. Needs `DATABASE_URL` — export it, drop a `.env` in the repo root, or
pass `--database-url` / `--env-file`.
"""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import psycopg
import typer
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.table import Table

REPO_ROOT = Path(__file__).resolve().parent

# Neon tables the pipeline writes (quoted — the names contain a hyphen).
SUMMARY_TABLE = '"nextjs-ui_summary"'
LEARNING_TABLE = '"nextjs-ui_learning"'

# `.env` files searched (highest priority first) when DATABASE_URL isn't already exported.
# Sibling repos under open_source/mine: the binutils pipeline (the live summarizer that
# writes these tables) and nextjs-ui both carry the shared
# op://Private/binutils-dev/DATABASE_URL field materialized by fnox.
ENV_CANDIDATES = (
    REPO_ROOT / ".env",
    REPO_ROOT.parent / "binutils" / "classification" / "pipeline" / ".env",
    REPO_ROOT.parent / "nextjs-ui" / ".env",
)

# Last-resort source when no .env is found: the canonical 1Password field itself.
OP_DATABASE_URL_REF = "op://Private/binutils-dev/DATABASE_URL"

console = Console()


class DayEntry(BaseModel):
    """The canonical (newest) digest + learnings markdown for a single UTC date."""

    model_config = ConfigDict(frozen=True)

    day: date
    digest_output: str | None = None
    digest_ts: datetime | None = None
    learnings_output: str | None = None
    learnings_ts: datetime | None = None

    @property
    def has_digest(self) -> bool:
        return self.digest_output is not None

    @property
    def has_learnings(self) -> bool:
        return self.learnings_output is not None

    def digest_body(self) -> str:
        ts = self.digest_ts.astimezone(UTC) if self.digest_ts else None
        stamp = ts.strftime("%Y-%m-%d %H:%M") if ts else self.day.isoformat()
        return f"# X Timeline Digest - {stamp}\n\n{self.digest_output}"

    def learnings_body(self) -> str:
        ts = self.learnings_ts.astimezone(UTC) if self.learnings_ts else None
        stamp = ts.strftime("%Y-%m-%d %H:%M") if ts else self.day.isoformat()
        return f"# Learnings - {stamp}\n\n{self.learnings_output}"


def _load_env(env_file: Path | None) -> None:
    """Populate DATABASE_URL from .env files. Already-exported env vars win (no override)."""
    candidates = ([env_file] if env_file else []) + list(ENV_CANDIDATES)
    for path in candidates:
        if path is not None and path.exists():
            load_dotenv(path)


def _op_read_database_url() -> str | None:
    """Resolve DATABASE_URL straight from 1Password when no .env carries it."""
    try:
        result = subprocess.run(
            ["op", "read", OP_DATABASE_URL_REF],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _latest_per_day(cur: psycopg.Cursor, table: str) -> dict[date, tuple[datetime, str]]:
    """Newest (created_at, output) per UTC date from a summary/learning table."""
    cur.execute(
        f"""
        SELECT DISTINCT ON ((created_at AT TIME ZONE 'UTC')::date)
               (created_at AT TIME ZONE 'UTC')::date AS day,
               created_at,
               output
        FROM {table}
        ORDER BY (created_at AT TIME ZONE 'UTC')::date DESC, created_at DESC
        """
    )
    return {day: (created_at, output) for day, created_at, output in cur.fetchall()}


def collect_entries(database_url: str) -> list[DayEntry]:
    """Read the newest digest + learnings per UTC date from Neon, newest day first."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        digests = _latest_per_day(cur, SUMMARY_TABLE)
        learnings = _latest_per_day(cur, LEARNING_TABLE)

    entries: list[DayEntry] = []
    for day in sorted(set(digests) | set(learnings), reverse=True):
        digest = digests.get(day)
        learning = learnings.get(day)
        entries.append(
            DayEntry(
                day=day,
                digest_output=digest[1] if digest else None,
                digest_ts=digest[0] if digest else None,
                learnings_output=learning[1] if learning else None,
                learnings_ts=learning[0] if learning else None,
            )
        )
    return entries


def write_outputs(entries: list[DayEntry], repo_root: Path) -> int:
    """Write each entry's digest/learnings markdown into digests/ and learnings/. Returns count."""
    digests_dir = repo_root / "digests"
    learnings_dir = repo_root / "learnings"
    digests_dir.mkdir(exist_ok=True)
    learnings_dir.mkdir(exist_ok=True)

    written = 0
    for e in entries:
        name = f"{e.day.isoformat()}.md"
        if e.has_digest:
            (digests_dir / name).write_text(e.digest_body(), encoding="utf-8")
            written += 1
        if e.has_learnings:
            (learnings_dir / name).write_text(e.learnings_body(), encoding="utf-8")
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
    if latest.has_digest:
        lines += [
            f"**Latest:** [{latest.day.isoformat()}](digests/{latest.day.isoformat()}.md)"
            + (
                f" · [learnings](learnings/{latest.day.isoformat()}.md)"
                if latest.has_learnings
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
            digest_cell = f"[digest](digests/{iso}.md)" if e.has_digest else "—"
            learn_cell = f"[learnings](learnings/{iso}.md)" if e.has_learnings else "—"
            lines.append(f"| {iso} | {digest_cell} | {learn_cell} |")
        lines.append("")

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines += ["---", "", f"_Index regenerated {stamp} by `sync.py`._", ""]
    return "\n".join(lines)


def main(
    database_url: Annotated[
        str | None,
        typer.Option(
            "--database-url",
            "-d",
            help="Neon Postgres connection string. Defaults to $DATABASE_URL / a discovered .env.",
        ),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="Explicit .env to load DATABASE_URL from (takes priority over discovery).",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    repo_root: Annotated[
        Path,
        typer.Option("--repo-root", help="Repo root to write into.", file_okay=False),
    ] = REPO_ROOT,
) -> None:
    """Pull the newest digest/learnings per day from Neon Postgres and rebuild README.md."""
    _load_env(env_file)
    url = database_url or os.environ.get("DATABASE_URL") or _op_read_database_url()
    if not url:
        console.print(
            "[red]No DATABASE_URL.[/] Export it, add a .env, pass --database-url / --env-file,"
            " or sign in to 1Password (op)."
        )
        raise typer.Exit(1)

    try:
        entries = collect_entries(url)
    except psycopg.Error as exc:
        console.print(f"[red]Database error:[/] {exc}")
        raise typer.Exit(1) from exc

    if not entries:
        console.print("[yellow]No digest/learnings rows found in Neon.[/]")
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
            "✓" if e.has_digest else "—",
            "✓" if e.has_learnings else "—",
        )
    console.print(table)
    if len(entries) > 10:
        console.print(f"[dim]… and {len(entries) - 10} earlier days.[/]")
    console.print(f"[green]Wrote {written} files across {len(entries)} days; README.md rebuilt.[/]")


if __name__ == "__main__":
    typer.run(main)
