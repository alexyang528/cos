"""Granola scan via a read-only Haiku agent: list the meetings I attended that have ended, one event each.

The scan only lists meetings; the granola lead reads the notes. Each scan looks back GRANOLA_LOOKBACK_DAYS
and events dedupe on the meeting id, so a meeting skipped as unsettled is simply picked up next time.
"""

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import config, paths, prompts
from .policy import pass_spec
from .runner import preview, run_agent

MEETING_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NOTE_URL = "https://notes.granola.ai/"


def build_prompt(since: str) -> str:
    me = config.ME_NAME
    return f"""{prompts.SAFETY}
Task: list the Granola meetings {me} ({config.ME_EMAIL}) attended that have ended, since {since}. Collect
facts only.

1. Call mcp__claude_ai_Granola__search_meetings with attended_only=true, from_date="{since}", limit=50,
   max_participants=15. While the result has a next_cursor, call it again with only cursor set.
2. Report every meeting in meetings_data. Ignore upcoming_meetings: they have not ended and have no summary.
3. Do not read notes or transcripts, and call no other tool.

Output schema:
{{"meetings": [{{"meeting_id": "uuid from the id attribute", "title": "...", "meeting_at": "as given",
   "updated_at": "as given", "url": "https://notes.granola.ai/d/...", "captured_by_me": true,
   "participants": ["Name <email>"], "note_headers": ["..."]}}],
 "notes": "anything that failed, or null"}}
"""


def _utc(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(timezone.utc)
    except ValueError:
        return None


def ingest(conn: sqlite3.Connection, data: dict) -> dict[str, int]:
    counts = {"new": 0, "duplicate": 0, "unsettled": 0, "rejected": 0}
    settled = datetime.now(timezone.utc) - timedelta(minutes=config.GRANOLA_SETTLE_MIN)
    for m in data.get("meetings") or []:
        if not isinstance(m, dict) or not MEETING_ID.match(str(m.get("meeting_id", ""))):
            counts["rejected"] += 1
            continue
        at, updated = _utc(m.get("meeting_at")), _utc(m.get("updated_at"))
        if at is None:
            counts["rejected"] += 1
            continue
        if updated and updated > settled:
            counts["unsettled"] += 1
            continue
        url = m.get("url") if str(m.get("url", "")).startswith(NOTE_URL) else f"{NOTE_URL}d/{m['meeting_id']}"
        people = ", ".join(str(p) for p in (m.get("participants") or [])[:15])
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(source, source_id, kind, url, author, text, occurred_at, payload) "
            "VALUES ('granola', ?, 'meeting', ?, 'Granola', ?, ?, ?)",
            (m["meeting_id"], url, f"{m.get('title')} with {people}"[:2000], at.strftime("%Y-%m-%dT%H:%M:%SZ"),
             json.dumps(m, ensure_ascii=False)),
        )
        counts["new" if cur.rowcount else "duplicate"] += 1
    return counts


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    since = (datetime.now().astimezone() - timedelta(days=config.GRANOLA_LOOKBACK_DAYS)).date().isoformat()
    spec = pass_spec("scan", lead="granola")
    prompt = build_prompt(since)
    if dry_run:
        print(preview(spec, prompt))
        return
    result = run_agent(conn, spec, prompt, job="scan", lead="granola")
    if not result.ok:
        print(f"scan granola failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    snap = paths.snapshots_dir() / f"granola-{int(time.time())}.json"
    snap.write_text(json.dumps(result.data, ensure_ascii=False, indent=1))
    counts = ingest(conn, result.data)
    print(f"scan granola: {counts['new']} new, {counts['duplicate']} seen, {counts['unsettled']} still being written, "
          f"{counts['rejected']} rejected (run {result.run_id}, ${result.cost_usd:.3f}, snapshot {snap.name})")
    if result.data.get("notes"):
        print(f"  notes: {result.data['notes']}")
