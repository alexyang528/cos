"""Notion scan via a read-only Haiku agent using the Notion connector.

Notion has no inbox for agents, so the scan looks in two places:
- Inbound Platform tickets where I'm the DRI, still open, and edited since the last scan. One event per ticket edit,
  noting which fields changed since the previous edit. The intern routes them straight to the notion lead, which
  reads the ticket and its comments, except edits that only changed the DRIs.
- Pages that name me, from a workspace search sorted by last edit. Search matches my name, not true @-mentions,
  so these go through the intern, which keeps only the ones asking for my input. One event per page and snippet,
  so a busy page that keeps naming me in the same place isn't raised again on every edit.
"""

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import config, paths, prompts, store
from .policy import pass_spec
from .runner import preview, run_agent

CURSOR_KEY = "scan.notion.since"
PAGE_ID = re.compile(r"([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket_query() -> tuple[str, list]:
    closed = ", ".join("?" for _ in config.NOTION_TICKET_CLOSED)
    ds = config.NOTION_INBOUND_DS
    sql = (f'SELECT url, "Name", "userDefined:ID", "Status", "Priority", "Type", "Last edited time", "Created time", '
           f'"AI Summary", "DRI" FROM "{ds}" WHERE "DRI" LIKE ? AND "Status" NOT IN ({closed}) '
           f'AND datetime("Last edited time") > datetime(?) ORDER BY "Last edited time" LIMIT 50')
    return sql, [f"%{config.NOTION_USER_ID}%", *config.NOTION_TICKET_CLOSED]


def build_prompt(since: str) -> str:
    if config.NOTION_INBOUND_DS:
        sql, params = ticket_query()
        query = {"data": {"mode": "sql", "data_source_urls": [config.NOTION_INBOUND_DS], "query": sql,
                          "params": [*params, since]}}
        tickets_step = f"""1. Call mcp__claude_ai_Notion__notion-query-data-sources with exactly these arguments:
{json.dumps(query)}
   Report every row as a ticket."""
    else:
        tickets_step = '1. There is no ticket database to query; report "tickets": [].'
    search = {"query": config.ME_NAME, "filters": {"last_edited_date_range": {"start_date": since[:10]}},
              "sort": "last_edited", "page_size": 50, "max_highlight_length": 300}
    return f"""{prompts.SAFETY}
Task: collect Notion activity that might need {config.ME_NAME}. Collect facts only; do not judge importance.
Load the tools with ToolSearch first ("select:mcp__claude_ai_Notion__notion-query-data-sources,mcp__claude_ai_Notion__notion-search").

{tickets_step}
2. Call mcp__claude_ai_Notion__notion-search with exactly these arguments:
{json.dumps(search)}
   Report every result whose highlight mentions {config.ME_NAME.split()[0]}, with the highlight verbatim.
3. Call no other tool and read no pages.

Output schema:
{{"tickets": [{{"url": "...", "name": "...", "ticket_id": 1234, "status": "...", "priority": "...", "type": "...",
   "last_edited": "as given", "created": "as given", "ai_summary": "as given, or null", "dri": "DRI as given"}}],
 "mentions": [{{"id": "page id", "url": "...", "title": "...", "path": "as given, or null", "highlight": "verbatim",
   "timestamp": "as given"}}],
 "notes": "anything that failed, or null"}}
"""


def _page_id(*values) -> str | None:
    for v in values:
        m = PAGE_ID.search(str(v or "").lower())
        if m:
            return m.group(1).replace("-", "")
    return None


def page_url(page_id: str) -> str:
    """See links.py for why Notion links are rewritten to www.notion.so."""
    return f"https://www.notion.so/{page_id}"


def canonical_url(value) -> str | None:
    pid = _page_id(value)
    return page_url(pid) if pid else None


def _utc(value) -> str:
    try:
        return _iso(datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00")))
    except ValueError:
        return _iso(datetime.now(timezone.utc))


TRACKED = ("name", "status", "priority", "dri")


def ticket_changes(conn: sqlite3.Connection, page_id: str, ticket: dict) -> list[str] | None:
    """Tracked fields that differ from the previous edit of this ticket, or None if it's the first one seen. An empty
    list means the edit touched something not tracked here (the body, a comment)."""
    prev = conn.execute(
        "SELECT payload FROM events WHERE source = 'notion' AND kind = 'ticket' "
        "AND json_extract(payload, '$.page_id') = ? ORDER BY occurred_at DESC LIMIT 1", (page_id,)).fetchone()
    if not prev:
        return None
    before = json.loads(prev["payload"])
    # Events from before DRIs were collected have no "dri"; don't count that as a change.
    return [f for f in TRACKED if f in before and f in ticket and before[f] != ticket[f]]


def ingest(conn: sqlite3.Connection, data: dict) -> dict[str, int]:
    counts = {"tickets": 0, "mentions": 0, "duplicate": 0, "rejected": 0}
    for t in data.get("tickets") or []:
        pid = _page_id((t or {}).get("url"))
        if not pid or (t.get("status") in config.NOTION_TICKET_CLOSED):
            counts["rejected"] += 1
            continue
        edited = _utc(t.get("last_edited"))
        name = str(t.get("name") or "Untitled ticket")
        label = f"IP-{t['ticket_id']}" if t.get("ticket_id") else "Inbound Platform"
        payload = {**t, "page_id": pid, "channel_name": f"Inbound Platform {label}", "assigned_to_me": True}
        payload["changed"] = ticket_changes(conn, pid, t)
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(source, source_id, kind, url, author, text, occurred_at, payload) "
            "VALUES ('notion', ?, 'ticket', ?, 'Inbound Platform', ?, ?, ?)",
            (f"ticket:{pid}:{edited}", page_url(pid),
             f"{label} {name} ({t.get('status')}, {t.get('priority')}); you are the DRI"[:2000], edited,
             json.dumps(payload, ensure_ascii=False)),
        )
        counts["tickets" if cur.rowcount else "duplicate"] += 1
    first = config.ME_NAME.split()[0].lower()
    for m in data.get("mentions") or []:
        pid = _page_id((m or {}).get("id"), (m or {}).get("url"))
        highlight = str((m or {}).get("highlight") or "")
        if not pid or first not in highlight.lower():
            counts["rejected"] += 1
            continue
        digest = hashlib.sha1(re.sub(r"\W+", " ", highlight.lower()).strip().encode()).hexdigest()[:12]
        payload = {**m, "page_id": pid, "channel_name": f"Notion page: {m.get('title')}"}
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(source, source_id, kind, url, author, text, occurred_at, payload) "
            "VALUES ('notion', ?, 'mention', ?, 'Notion', ?, ?, ?)",
            (f"mention:{pid}:{digest}", page_url(pid), f"{m.get('title')}: {highlight}"[:2000],
             _utc(m.get("timestamp")), json.dumps(payload, ensure_ascii=False)),
        )
        counts["mentions" if cur.rowcount else "duplicate"] += 1
    return counts


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    started = datetime.now(timezone.utc)
    since = store.kv_get(conn, CURSOR_KEY) or _iso(started - timedelta(days=config.NOTION_FIRST_SCAN_LOOKBACK_D))
    spec = pass_spec("scan", lead="notion")
    prompt = build_prompt(since)
    if dry_run:
        print(preview(spec, prompt))
        return
    result = run_agent(conn, spec, prompt, job="scan", lead="notion")
    if not result.ok:
        print(f"scan notion failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    snap = paths.snapshots_dir() / f"notion-{int(time.time())}.json"
    snap.write_text(json.dumps(result.data, ensure_ascii=False, indent=1))
    counts = ingest(conn, result.data)
    # The search filters by day, so overlap is large anyway; dedupe on source_id drops what was already seen.
    store.kv_set(conn, CURSOR_KEY, _iso(started - timedelta(seconds=config.SCAN_OVERLAP_S)))
    print(f"scan notion: {counts['tickets']} ticket updates, {counts['mentions']} new mentions, "
          f"{counts['duplicate']} seen, {counts['rejected']} rejected (run {result.run_id}, ${result.cost_usd:.3f}, "
          f"snapshot {snap.name})")
    if result.data.get("notes"):
        print(f"  notes: {result.data['notes']}")
