"""Activity on the sources workstreams watch, via a read-only Haiku agent. Facts only.

This is the cheap check that decides whether a workstream's lead needs to run. Activity goes to ws_activity,
never to events, so channel chatter never reaches the intern or the surface leads. Each source's cursor (the
last Slack ts, or the Switchboard head revision) is moved by Python, only for sources the agent read cleanly.
"""

import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from . import config, paths, prompts
from .policy import pass_spec
from .runner import preview, run_agent

TS = re.compile(r"^\d{9,11}\.\d{1,6}$")
REV_ID = re.compile(r"^rev_[0-9a-f]{32}$")
NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def due_sources(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT s.* FROM ws_sources s JOIN workstreams w ON w.id = s.workstream_id "
        "WHERE s.status = 'active' AND w.status = 'active' ORDER BY s.last_scanned_at IS NOT NULL, s.last_scanned_at "
        "LIMIT ?", (config.WS_SCAN_BATCH,)).fetchall()


def _since(src) -> float:
    try:
        return float(src["cursor"])
    except (TypeError, ValueError):
        return time.time() - config.WS_FIRST_SCAN_LOOKBACK_H * 3600


def build_prompt(sources) -> str:
    batch = []
    for s in sources:
        entry = {"source_id": s["id"], "kind": s["kind"]}
        if s["kind"] == "slack_channel":
            entry.update(channel_id=s["ref"], since=f"{_since(s):.6f}")
        elif s["kind"] == "slack_thread":
            channel, _, ts = s["ref"].partition(":")
            entry.update(channel_id=channel, thread_ts=ts, since=f"{_since(s):.6f}")
        else:
            entry.update(plan_id=s["ref"], last_revision=s["cursor"])
        batch.append(entry)
    n = config.WS_SCAN_MESSAGES_PER_SOURCE
    return f"""{prompts.SAFETY}
Task: collect new activity on the Slack channels, Slack threads and Switchboard plans below. Collect facts
only: do not judge, filter for importance, or summarize.

For each source:
- slack_channel: mcp__claude_ai_Slack__slack_read_channel(channel_id, oldest=since). Report every message
  after since. For a message that has thread replies newer than since (its latest reply is after since,
  even if the parent itself is older), call mcp__claude_ai_Slack__slack_read_thread(channel_id, thread_ts)
  and report the replies after since. At most 8 thread reads per channel; say so in notes if you stop.
- slack_thread: mcp__claude_ai_Slack__slack_read_thread(channel_id, thread_ts). Report messages after since.
- switchboard_plan: mcp__claude_ai_Switchboard__get_plan(plan_id). Report the plan state once. If its
  head_revision_id equals last_revision, report it anyway; it is dropped as unchanged.
Skip channel join/leave notices. Keep bot messages (e.g. deploy or PR notifications) with the bot's name as
author. At most {n} messages per source: keep the newest and say so in notes. If a source fails, report it
with ok false and the error; do not retry more than once.

Output schema:
{{"sources": [{{"source_id": 1, "ok": true, "error": null,
   "messages": [{{"ts": "1731000000.000100", "thread_ts": "parent ts or null", "author": "display name",
     "author_id": "U...", "text": "full text", "permalink": "https://...slack.com/archives/..."}}],
   "plan": null | {{"head_revision_id": "rev_...", "title": "...", "phase": "...", "turn": "human" | "switchboard",
     "url": "https://switchboard.checkhq.com/plans/pl_...",
     "open_threads": [{{"id": "...", "latest": "latest message, at most 400 chars"}}],
     "switchboard_message": "switchboard_changes.message, at most 800 chars, or null"}}}}],
 "notes": "anything that failed or was truncated, or null"}}

Sources:
{prompts.untrusted("sources", batch)}
"""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest(conn: sqlite3.Connection, sources, data: dict) -> dict[str, int]:
    by_id = {s["id"]: s for s in sources}
    counts = {"new": 0, "seen": 0, "failed": 0, "rejected": 0}
    for rep in data.get("sources") or []:
        src = by_id.get((rep or {}).get("source_id"))
        if src is None:
            counts["rejected"] += 1
            continue
        if not rep.get("ok"):
            counts["failed"] += 1
            continue
        if src["kind"] == "switchboard_plan":
            plan = rep.get("plan") or {}
            rev = str(plan.get("head_revision_id") or "")
            if REV_ID.match(rev) and rev != src["cursor"]:
                threads = "; ".join(f"{t.get('latest', '')[:200]}" for t in (plan.get("open_threads") or [])[:5]
                                    if isinstance(t, dict))
                text = (f"Plan '{plan.get('title')}' is at {plan.get('phase')}, turn: {plan.get('turn')}."
                        + (f" Switchboard says: {str(plan.get('switchboard_message'))[:800]}"
                           if plan.get("switchboard_message") else "")
                        + (f" Open threads: {threads}" if threads else ""))
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO ws_activity(source_id, ext_id, author, text, url, occurred_at, payload) "
                    f"VALUES (?, ?, 'Switchboard', ?, ?, {NOW}, ?)",
                    (src["id"], rev, text[:3000], src["url"], json.dumps(plan, ensure_ascii=False)[:8000]))
                counts["new" if cur.rowcount else "seen"] += 1
                conn.execute("UPDATE ws_sources SET cursor = ? WHERE id = ?", (rev, src["id"]))
        else:
            since = _since(src)
            newest = since
            msgs = [m for m in rep.get("messages") or [] if isinstance(m, dict) and TS.match(str(m.get("ts", "")))]
            msgs = sorted(msgs, key=lambda m: float(m["ts"]))[-config.WS_SCAN_MESSAGES_PER_SOURCE:]
            for m in msgs:
                ts = float(m["ts"])
                if ts <= since:
                    continue
                newest = max(newest, ts)
                link = m.get("permalink") if str(m.get("permalink", "")).startswith("https://") \
                    and ".slack.com/" in str(m.get("permalink")) else None
                payload = {k: m.get(k) for k in ("ts", "thread_ts", "author_id")}
                payload["is_me"] = m.get("author_id") == config.SLACK_USER_ID
                cur = conn.execute(
                    "INSERT OR IGNORE INTO ws_activity(source_id, ext_id, author, text, url, occurred_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (src["id"], m["ts"], str(m.get("author") or "")[:100], str(m.get("text") or "")[:3000], link,
                     _iso(ts), json.dumps(payload)))
                counts["new" if cur.rowcount else "seen"] += 1
            conn.execute("UPDATE ws_sources SET cursor = ? WHERE id = ?", (f"{newest:.6f}", src["id"]))
        conn.execute(f"UPDATE ws_sources SET last_scanned_at = {NOW} WHERE id = ?", (src["id"],))
    return counts


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    sources = due_sources(conn)
    if not sources:
        print("scan workstreams: no watched sources (no model call)")
        return
    spec = pass_spec("scan", lead="workstreams")
    prompt = build_prompt(sources)
    if dry_run:
        print(preview(spec, prompt))
        return
    from .ws_lead import ws_spend_today
    if ws_spend_today(conn) + spec.budget_usd > config.WS_DAILY_BUDGET_USD:
        print(f"scan workstreams: skipped, workstreams are at their ${config.WS_DAILY_BUDGET_USD:.2f} daily budget")
        return
    result = run_agent(conn, spec, prompt, job="scan", lead="workstreams")
    if not result.ok:
        print(f"scan workstreams failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    snap = paths.snapshots_dir() / f"workstreams-{int(time.time())}.json"
    snap.write_text(json.dumps(result.data, ensure_ascii=False, indent=1))
    counts = ingest(conn, sources, result.data)
    print(f"scan workstreams: {len(sources)} sources, {counts['new']} new, {counts['seen']} seen, "
          f"{counts['failed']} failed, {counts['rejected']} rejected (run {result.run_id}, ${result.cost_usd:.3f})")
    if result.data.get("notes"):
        print(f"  notes: {result.data['notes']}")
