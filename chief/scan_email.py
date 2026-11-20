"""Gmail scan via a read-only Haiku agent using the Gmail connector.

The agent only lists inbox threads (the newest message of each); Python decides what becomes an event:
- mail from a person, sent to me or cc'ing me, where the newest message isn't mine: kind "email", for the intern;
- anything from a Zendesk (a partner's support ticket): kind "zendesk". My own ticket comments are skipped without
  a model; the intern reads the thread for the rest and routes only the ones that ask something of me.
Calendar traffic, automated senders (Google Docs, Linear, Ashby, GitHub, digests) and the Promotions, Social,
Updates and Forums tabs are dropped here, so they never cost a model call.
"""

import html
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import config, paths, prompts, store
from .policy import pass_spec
from .runner import preview, run_agent

CURSOR_KEY = "scan.email.since"
MAX_PAGES = 4
CALENDAR = re.compile(r"^(accepted|declined|tentatively accepted|invitation|updated invitation|canceled event|"
                      r"cancelled event|proposed new time|new event|event canceled)\b", re.I)
AUTOMATED_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|notify|alerts?|mailer-daemon|digest|news|"
                             r"newsletters?|info|marketing|hello|team|updates?|support|comments-noreply|calendar-"
                             r"notification|drive-shares)", re.I)
AUTOMATED_DOMAINS = ("docs.google.com", "linear.app", "ashbyhq.com", "github.com", "dtdg.co",
                     "calendar.google.com", "slack.com", "notion.so", "mail.notion.so", "atlassian.com", "figma.com")
SKIP_CATEGORIES = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}
# Zendesk update mail: "Ticket comments: <author> Nov 18, 2026, 7:41 AM MST <comment>".
ZENDESK_COMMENT_AUTHOR = re.compile(r"Ticket comments:\s*(.+?)\s+[A-Z][a-z]{2,8} \d{1,2}, \d{4}")


def build_prompt(since_day: str) -> str:
    query = f"in:inbox after:{since_day} -category:promotions -category:social -category:forums"
    return f"""{prompts.SAFETY}
Task: list {config.ME_NAME}'s recent Gmail inbox threads. Collect facts only; do not judge importance and do not
read any email bodies. Load the tool with ToolSearch first ("select:mcp__claude_ai_Gmail__search_threads").

Call mcp__claude_ai_Gmail__search_threads with query={json.dumps(query)}, view="THREAD_VIEW_SUMMARY",
pageSize=50. While the result has a nextPageToken, call it again with the same query and that pageToken, up to
{MAX_PAGES} calls in total; if you stop at the cap, say so in notes. Call no other tool.

For every thread, report its newest message (the one in "messages") exactly as given.

Output schema:
{{"threads": [{{"thread_id": "...", "message_count": 1, "thread_url": "viewUrl of the thread",
   "message_id": "...", "message_url": "viewUrl of the message", "sender": "email address",
   "sender_name": "display name for the sender from addressDisplayNames, or null", "to": ["..."], "cc": ["..."],
   "subject": "...", "snippet": "...", "date": "as given", "labels": ["labelIds as given"]}}],
 "notes": "anything that failed, or null"}}
"""


def classify(t: dict) -> str | None:
    """'email', 'zendesk', or None for mail that never needs a model's judgment."""
    sender = str(t.get("sender") or "").lower()
    local, _, domain = sender.partition("@")
    if not sender or sender == config.ME_EMAIL:
        return None  # malformed, or my own reply is the newest message
    if domain.endswith("zendesk.com"):
        return "zendesk"
    if CALENDAR.match(str(t.get("subject") or "").strip()):
        return None
    if set(t.get("labels") or []) & SKIP_CATEGORIES:
        return None
    if AUTOMATED_LOCAL.match(local) or domain.endswith(AUTOMATED_DOMAINS):
        return None
    recipients = [str(r).lower() for r in [*(t.get("to") or []), *(t.get("cc") or [])]]
    if config.ME_EMAIL not in " ".join(recipients):
        return None  # a list or alias I'm on, not mail to me
    return "email"


def _utc(value) -> str:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest(conn: sqlite3.Connection, data: dict) -> dict[str, int]:
    counts = {"email": 0, "zendesk": 0, "filtered": 0, "duplicate": 0, "rejected": 0}
    for t in data.get("threads") or []:
        if not isinstance(t, dict) or not t.get("message_id") or not t.get("thread_id"):
            counts["rejected"] += 1
            continue
        kind = classify(t)
        if kind is None:
            counts["filtered"] += 1
            continue
        url = next((u for u in (t.get("message_url"), t.get("thread_url"))
                    if str(u or "").startswith("https://mail.google.com/")), None)
        name = t.get("sender_name") or t.get("sender")
        subject, snippet = html.unescape(str(t.get("subject") or "")), html.unescape(str(t.get("snippet") or ""))
        where = f"Zendesk ({t['sender'].split('@')[1].split('.')[0]})" if kind == "zendesk" else f"Email from {name}"
        payload = {**t, "subject": subject, "snippet": snippet, "channel_name": where}
        if kind == "zendesk":
            m = ZENDESK_COMMENT_AUTHOR.search(snippet)
            payload["comment_author"] = m.group(1) if m else None
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(source, source_id, kind, url, author, text, occurred_at, payload) "
            "VALUES ('email', ?, ?, ?, ?, ?, ?, ?)",
            (t["message_id"], kind, url, str(name)[:200], f"{subject}\n{snippet}"[:2000], _utc(t.get("date")),
             json.dumps(payload, ensure_ascii=False)),
        )
        counts[kind if cur.rowcount else "duplicate"] += 1
    return counts


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    started = datetime.now(timezone.utc)
    stored = store.kv_get(conn, CURSOR_KEY)
    since = datetime.fromisoformat(stored) if stored else started - timedelta(days=config.GMAIL_FIRST_SCAN_LOOKBACK_D)
    # Gmail's after: is a whole local day, so each scan re-reads today; dedupe on message id drops what was seen.
    since_day = since.astimezone().strftime("%Y/%m/%d")
    spec = pass_spec("scan", lead="email")
    prompt = build_prompt(since_day)
    if dry_run:
        print(preview(spec, prompt))
        return
    result = run_agent(conn, spec, prompt, job="scan", lead="email")
    if not result.ok:
        print(f"scan email failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    snap = paths.snapshots_dir() / f"email-{int(time.time())}.json"
    snap.write_text(json.dumps(result.data, ensure_ascii=False, indent=1))
    counts = ingest(conn, result.data)
    store.kv_set(conn, CURSOR_KEY, started.isoformat())
    print(f"scan email: {counts['email']} from people, {counts['zendesk']} zendesk, {counts['filtered']} filtered out, "
          f"{counts['duplicate']} seen, {counts['rejected']} rejected (run {result.run_id}, ${result.cost_usd:.3f}, "
          f"snapshot {snap.name})")
    if result.data.get("notes"):
        print(f"  notes: {result.data['notes']}")
