"""Slack scan via a read-only Haiku agent using the claude.ai Slack connector (no Slack API token).

The agent only collects facts; Python validates them, dedupes on (source, source_id), and moves the cursor.
"""

import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from . import config, paths, prompts, store
from .policy import pass_spec
from .runner import AgentResult, preview, run_agent

CURSOR_KEY = "scan.slack.since"
KINDS = {"dm", "group_dm", "mention", "thread_reply"}
TS = re.compile(r"^\d{9,11}\.\d{1,6}$")
CHANNEL = re.compile(r"^[CDG][A-Z0-9]{6,}$")


def build_prompt(since: int) -> str:
    me = config.SLACK_USER_ID
    since_iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""{prompts.SAFETY}
Task: collect new Slack messages that might need {config.ME_NAME} (Slack user id {me}) since unix time
{since} ({since_iso}). Collect facts only; do not judge importance and do not summarize.

Use mcp__claude_ai_Slack__slack_search_public_and_private with sort="timestamp", sort_dir="asc",
response_format="detailed", include_context=false, limit=20, and after="{since}". Follow the cursor until
the results end, up to 10 pages per search; if you stop at the cap, say so in notes. Run all four searches.

1. DMs and group DMs to {me}: filters="to:<@{me}>", channel_types="im,mpim". Kind "dm" or "group_dm".
2. {me}'s own DM replies: filters="from:<@{me}>", channel_types="im,mpim". Evidence only, never events.
3. Mentions: keywords=["<@{me}>"], channel_types="public_channel,private_channel". Keep a result only if
   its text really contains <@{me}>. Kind "mention".
4. Threads {config.ME_NAME} is in: filters="with:<@{me}> is:thread",
   channel_types="public_channel,private_channel". Kind "thread_reply". The parent thread_ts is the
   thread_ts query parameter in the permalink. This returns {me}'s own thread messages too: evidence only.

Use search results only; do not read whole threads or channels, except: if a thread_reply from someone
else has no earlier {me} message in the results and you need my_last_message, you may call
mcp__claude_ai_Slack__slack_read_thread for that thread, at most 5 times in total.

Drop bot and app messages. If one message qualifies more than once, report it once, preferring
dm/group_dm, then mention, then thread_reply.

channel_name: "#channel" for channels, "DM with <name>" for a DM, and "Group DM with <names>" for a group DM,
listing the other members only. Never include {config.ME_NAME} or add notes in parentheses.

For each message also fill in:
- my_last_message: the most recent message by {me} in the same thread (or in the DM, if not threaded)
  that comes BEFORE this message, verbatim, or null if you did not see one.
- i_replied_after: true if a later message by {me} in the same thread (or same DM conversation, if not
  threaded) appears in the results; otherwise false. For a channel mention outside any thread, null.

Output schema:
{{"events": [{{
   "source_id": "<channel_id>:<message ts>",
   "kind": "dm" | "group_dm" | "mention" | "thread_reply",
   "channel_id": "C.../D.../G...", "channel_name": "#name or 'DM with X'",
   "ts": "1731000000.000100", "thread_ts": "parent ts or null",
   "author": "display name", "author_id": "U...",
   "text": "full message text",
   "permalink": "https://...slack.com/archives/...",
   "my_last_message": "string or null",
   "i_replied_after": true | false | null
 }}],
 "notes": "anything that failed or was truncated"}}
"""


def _iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(ev: dict) -> str | None:
    if ev.get("kind") not in KINDS:
        return f"bad kind {ev.get('kind')!r}"
    if not CHANNEL.match(str(ev.get("channel_id", ""))):
        return f"bad channel {ev.get('channel_id')!r}"
    if not TS.match(str(ev.get("ts", ""))):
        return f"bad ts {ev.get('ts')!r}"
    if ev.get("source_id") != f"{ev['channel_id']}:{ev['ts']}":
        return "source_id must be channel_id:ts"
    if ev.get("thread_ts") is not None and not TS.match(str(ev["thread_ts"])):
        return f"bad thread_ts {ev.get('thread_ts')!r}"
    if ev.get("author_id") == config.SLACK_USER_ID:
        return "authored by me"
    return None


def _without_me(channel_name) -> str | None:
    """DM titles name the other members; the scanner sometimes adds me, which reads as if someone else were me."""
    if not channel_name:
        return channel_name
    name = re.sub(rf"\s*\({re.escape(config.ME_NAME)}\)", "", str(channel_name))
    m = re.match(r"^(.*?(?:with|:))\s*(.*)$", name)
    if not m:
        return name
    others = [n.strip() for n in m.group(2).split(",") if n.strip() and n.strip() != config.ME_NAME]
    return f"{m.group(1)} {', '.join(others)}"


def seen_elsewhere(conn: sqlite3.Connection, ev: dict) -> bool:
    """The scanner sometimes garbles a channel id (and the permalink with it), which gives the same message a new
    source_id. A ts is unique per channel, so the same ts from the same author is the same message."""
    return conn.execute(
        "SELECT 1 FROM events WHERE source = 'slack' AND json_extract(payload, '$.ts') = ? "
        "AND json_extract(payload, '$.author_id') IS ? LIMIT 1",
        (ev["ts"], ev.get("author_id")),
    ).fetchone() is not None


def ingest(conn: sqlite3.Connection, events: list[dict]) -> dict[str, int]:
    counts = {"new": 0, "duplicate": 0, "rejected": 0}
    for ev in events:
        if not isinstance(ev, dict) or validate(ev):
            counts["rejected"] += 1
            continue
        if seen_elsewhere(conn, ev):
            counts["duplicate"] += 1
            continue
        if ev["kind"] in ("dm", "group_dm"):
            ev = {**ev, "channel_name": _without_me(ev.get("channel_name"))}
        link = ev.get("permalink") if str(ev.get("permalink", "")).startswith("https://") else None
        text = str(ev.get("text") or "")[:4000]
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(source, source_id, kind, url, author, text, occurred_at, payload) "
            "VALUES ('slack', ?, ?, ?, ?, ?, ?, ?)",
            (ev["source_id"], ev["kind"], link, ev.get("author"), text, _iso(ev["ts"]),
             json.dumps(ev, ensure_ascii=False)),
        )
        counts["new" if cur.rowcount else "duplicate"] += 1
    return counts


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    started = int(time.time())
    stored = store.kv_get(conn, CURSOR_KEY)
    since = int(stored) if stored else started - config.SLACK_FIRST_SCAN_LOOKBACK_H * 3600
    spec = pass_spec("scan", lead="slack")
    prompt = build_prompt(since)
    if dry_run:
        print(preview(spec, prompt))
        return
    result: AgentResult = run_agent(conn, spec, prompt, job="scan", lead="slack")
    if not result.ok:
        print(f"scan slack failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    events = result.data.get("events") or []
    snap = paths.snapshots_dir() / f"slack-{started}.json"
    snap.write_text(json.dumps(result.data, ensure_ascii=False, indent=1))
    counts = ingest(conn, events)
    # Only advance after a successful ingest; the overlap re-reads a little and dedupe drops repeats.
    store.kv_set(conn, CURSOR_KEY, str(started - config.SCAN_OVERLAP_S))
    print(f"scan slack: {counts['new']} new, {counts['duplicate']} already seen, {counts['rejected']} rejected "
          f"(run {result.run_id}, ${result.cost_usd:.3f}, snapshot {snap.name})")
    if result.data.get("notes"):
        print(f"  notes: {result.data['notes']}")
