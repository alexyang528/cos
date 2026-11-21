"""Workstreams: long-running projects I lead, each with its own lead agent.

A workstream exists on its own: a charter, the sources it watches, a timeline of updates, and a short overview of
where it stands (the `latest` column). Items are touchpoints that need me; an item in a workstream shows on its
page and in the queue. Routing into a workstream is by source (a watched channel, thread or plan), by the intern's
judgment, by the workstream's lead, or by me moving an item.
"""

import json
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlparse

from . import config
from .db import transaction
from .leads import SOURCE_TO_LEAD
from .policy import ACTION_SURFACE

NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
OPEN_ITEM_STATUSES = ("pending", "likely_done", "changes_requested", "approved", "failed", "executing")
# Items a move or adoption may carry; executing is mid-send and changes_requested is mid-revision by a lead.
MOVABLE_STATUSES = ("pending", "likely_done", "approved", "failed")
SOURCE_KINDS = ("slack_channel", "slack_thread", "switchboard_plan")

CHANNEL = re.compile(r"^[CDG][A-Z0-9]{6,}$")
TS = re.compile(r"^\d{9,11}\.\d{1,6}$")
PLAN_ID = re.compile(r"^pl_[0-9a-f]{32}$")
SLACK_PATH = re.compile(r"^/archives/([CDG][A-Z0-9]{6,})(?:/p(\d{10})(\d{6}))?/?$")
SB_PATH = re.compile(r"^/plans/(pl_[0-9a-f]{32})")
URL_HOSTS = (".slack.com", "switchboard.checkhq.com", "notes.granola.ai")


class Conflict(Exception):
    pass


def safe_url(url) -> str | None:
    """Only https links to the surfaces we read, so agent output can't put an arbitrary link in front of me."""
    try:
        p = urlparse(str(url or ""))
    except ValueError:
        return None
    host = p.hostname or ""
    if p.scheme == "https" and any(host == h.lstrip(".") or host.endswith(h) for h in URL_HOSTS):
        return str(url)
    return None


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workstream"


# --- sources ------------------------------------------------------------------------------------------------

def parse_link(link: str) -> tuple[str, str, str | None] | None:
    """A Slack permalink, Slack channel link, Switchboard plan link, bare channel id or bare plan id ->
    (kind, ref, url). A Slack message link means its thread."""
    link = link.strip()
    if CHANNEL.match(link):
        return "slack_channel", link, None
    if PLAN_ID.match(link):
        return "switchboard_plan", link, f"https://switchboard.checkhq.com/plans/{link}"
    try:
        p = urlparse(link)
    except ValueError:
        return None
    host = p.hostname or ""
    if p.scheme == "https" and host.endswith(".slack.com"):
        m = SLACK_PATH.match(p.path)
        if not m:
            return None
        channel, secs, micros = m.groups()
        thread_ts = (parse_qs(p.query).get("thread_ts") or [None])[0]
        if thread_ts and TS.match(thread_ts):
            return "slack_thread", f"{channel}:{thread_ts}", link
        if secs:
            return "slack_thread", f"{channel}:{secs}.{micros}", link
        return "slack_channel", channel, link
    if p.scheme == "https" and host == "switchboard.checkhq.com":
        m = SB_PATH.match(p.path)
        if m:
            return "switchboard_plan", m.group(1), f"https://switchboard.checkhq.com/plans/{m.group(1)}"
    return None


def valid_source(kind: str, ref: str) -> bool:
    if kind == "slack_channel":
        return bool(CHANNEL.match(ref))
    if kind == "slack_thread":
        channel, _, ts = ref.partition(":")
        return bool(CHANNEL.match(channel) and TS.match(ts))
    if kind == "switchboard_plan":
        return bool(PLAN_ID.match(ref))
    return False


def _initial_cursor(kind: str) -> str | None:
    if kind.startswith("slack_"):
        return f"{time.time() - config.WS_FIRST_SCAN_LOOKBACK_H * 3600:.6f}"
    return None


def watcher_of(conn, kind: str, ref: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT w.* FROM ws_sources s JOIN workstreams w ON w.id = s.workstream_id "
        "WHERE s.kind = ? AND s.ref = ? AND s.status = 'active'", (kind, ref)).fetchone()


def add_source(conn, ws_id: int, kind: str, ref: str, *, by: str, label=None, url=None, why=None) -> int:
    """I add sources active; the lead's start proposed. Re-adding a removed or rejected source revives it."""
    if kind not in SOURCE_KINDS or not valid_source(kind, ref):
        raise ValueError(f"bad source {kind} {ref!r}")
    status = "active" if by == "user" else "proposed"
    if status == "active":
        other = watcher_of(conn, kind, ref)
        if other and other["id"] != ws_id:
            raise Conflict(f"already watched by workstream '{other['name']}'")
    existing = conn.execute("SELECT * FROM ws_sources WHERE workstream_id = ? AND kind = ? AND ref = ?",
                            (ws_id, kind, ref)).fetchone()
    url = safe_url(url)
    if existing:
        if by == "lead" and existing["status"] in ("proposed", "active", "removed", "rejected"):
            return existing["id"]  # the lead never revives what I removed or rejected
        conn.execute(
            f"UPDATE ws_sources SET status = ?, label = COALESCE(?, label), url = COALESCE(?, url), "
            f"cursor = COALESCE(cursor, ?), decided_at = {NOW} WHERE id = ?",
            (status, label, url, _initial_cursor(kind), existing["id"]))
        source_id = existing["id"]
    else:
        source_id = conn.execute(
            "INSERT INTO ws_sources(workstream_id, kind, ref, label, url, status, added_by, why, cursor, decided_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'active' THEN {NOW} END)",
            (ws_id, kind, ref, label, url, status, by, why, _initial_cursor(kind), status)).lastrowid
    if status == "active":
        adopt_open_items(conn, ws_id)
    return source_id


def decide_source(conn, source_id: int, verb: str) -> None:
    src = conn.execute("SELECT * FROM ws_sources WHERE id = ?", (source_id,)).fetchone()
    if src is None:
        raise KeyError(f"no source {source_id}")
    if verb == "accept":
        if src["status"] != "proposed":
            raise Conflict(f"source {source_id} is {src['status']}, not proposed")
        other = watcher_of(conn, src["kind"], src["ref"])
        if other:
            raise Conflict(f"already watched by workstream '{other['name']}'")
        conn.execute(f"UPDATE ws_sources SET status = 'active', decided_at = {NOW}, "
                     "cursor = COALESCE(cursor, ?) WHERE id = ?", (_initial_cursor(src["kind"]), source_id))
        adopt_open_items(conn, src["workstream_id"])
    elif verb in ("reject", "remove"):
        new = "rejected" if verb == "reject" else "removed"
        conn.execute(f"UPDATE ws_sources SET status = ?, decided_at = {NOW} WHERE id = ?", (new, source_id))
    else:
        raise ValueError(f"unknown source action {verb!r}")


def event_refs(source: str, payload: dict) -> list[tuple[str, str]]:
    """The watched-source keys an event belongs to, most specific first."""
    if source == "slack":
        channel = str(payload.get("channel_id") or "")
        thread = payload.get("thread_ts") or payload.get("ts")
        refs = [("slack_thread", f"{channel}:{thread}")] if thread else []
        return refs + [("slack_channel", channel)]
    if source == "switchboard" and payload.get("plan_id"):
        return [("switchboard_plan", str(payload["plan_id"]))]
    return []


def owner_for_refs(conn, refs: list[tuple[str, str]]) -> int | None:
    for kind, ref in refs:
        row = conn.execute(
            "SELECT s.workstream_id FROM ws_sources s JOIN workstreams w ON w.id = s.workstream_id "
            "WHERE s.kind = ? AND s.ref = ? AND s.status = 'active' AND w.status = 'active'", (kind, ref)).fetchone()
        if row:
            return row["workstream_id"]
    return None


def owner_for_event(conn, event) -> int | None:
    return owner_for_refs(conn, event_refs(event["source"], json.loads(event["payload"] or "{}")))


def adopt_open_items(conn, ws_id: int) -> list[int]:
    """Queue items whose conversation a workstream now watches move into it."""
    rows = conn.execute(
        f"SELECT i.id, e.source, e.payload FROM items i JOIN routes r ON r.id = i.route_id "
        f"JOIN events e ON e.id = r.event_id WHERE i.workstream_id IS NULL "
        f"AND i.status IN ({','.join('?' * len(MOVABLE_STATUSES))})", MOVABLE_STATUSES).fetchall()
    moved = []
    for r in rows:
        if owner_for_refs(conn, event_refs(r["source"], json.loads(r["payload"] or "{}"))) == ws_id:
            conn.execute("UPDATE items SET workstream_id = ?, lead = 'workstream', filed_by = 'source' WHERE id = ?",
                         (ws_id, r["id"]))
            moved.append(r["id"])
    # Routes waiting for a surface lead follow too, so the workstream's lead drafts them.
    for r in conn.execute("SELECT r.id, e.source, e.payload FROM routes r JOIN events e ON e.id = r.event_id "
                          "WHERE r.decision = 'route' AND r.picked_up_at IS NULL AND r.workstream_id IS NULL"):
        if owner_for_refs(conn, event_refs(r["source"], json.loads(r["payload"] or "{}"))) == ws_id:
            conn.execute("UPDATE routes SET lead = 'workstream', workstream_id = ?, filed_by = 'source' WHERE id = ?",
                         (ws_id, r["id"]))
    return moved


# --- workstreams --------------------------------------------------------------------------------------------

def get(conn, ws_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM workstreams WHERE id = ?", (ws_id,)).fetchone()
    if row is None:
        raise KeyError(f"no workstream {ws_id}")
    return row


def by_slug(conn, slug_or_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM workstreams WHERE slug = ? OR CAST(id AS TEXT) = ?",
                       (slug_or_id, slug_or_id)).fetchone()
    if row is None:
        raise KeyError(f"no workstream {slug_or_id!r}")
    return row


def active(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM workstreams WHERE status = 'active' ORDER BY id").fetchall()


def create(conn, name: str, description: str = "", links: list[str] | None = None) -> tuple[int, list[str]]:
    """Returns the new id and any links that weren't a watchable source (they stay in the description for the
    lead to follow up on its first run)."""
    name = name.strip()
    if not name:
        raise ValueError("a workstream needs a name")
    base = slugify(name)
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM workstreams WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    unparsed = []
    with transaction(conn):
        ws_id = conn.execute("INSERT INTO workstreams(slug, name, description) VALUES (?, ?, ?)",
                             (slug, name, description.strip())).lastrowid
        for link in links or []:
            if not link.strip():
                continue
            parsed = parse_link(link)
            if not parsed:
                unparsed.append(link.strip())
                continue
            try:
                add_source(conn, ws_id, parsed[0], parsed[1], by="user", url=parsed[2])
            except Conflict as e:
                unparsed.append(f"{link.strip()} ({e})")
        if unparsed:
            conn.execute("UPDATE workstreams SET description = description || ? WHERE id = ?",
                         ("\n\nStarting links:\n" + "\n".join(f"- {u}" for u in unparsed), ws_id))
    return ws_id, unparsed


def update_fields(conn, ws_id: int, *, name=None, description=None, charter=None) -> None:
    sets, params = [f"updated_at = {NOW}"], []
    if name is not None and name.strip():
        sets.append("name = ?"); params.append(name.strip())
    if description is not None:
        sets.append("description = ?"); params.append(description.strip())
    if charter is not None:
        sets.append("charter = ?"); params.append(charter.strip())
        sets.append("charter_by = 'user'")
    conn.execute(f"UPDATE workstreams SET {', '.join(sets)} WHERE id = ?", (*params, ws_id))


def home_lead(conn, item) -> str:
    """The surface lead an item goes back to when it leaves a workstream."""
    if item["route_id"]:
        ev = conn.execute("SELECT e.source FROM routes r JOIN events e ON e.id = r.event_id WHERE r.id = ?",
                          (item["route_id"],)).fetchone()
        if ev and ev["source"] in SOURCE_TO_LEAD:
            return SOURCE_TO_LEAD[ev["source"]]
    if item["action"]:
        surface = ACTION_SURFACE.get(json.loads(item["action"]).get("type"))
        if surface in SOURCE_TO_LEAD:
            return SOURCE_TO_LEAD[surface]
    return "chief"


def move_item(conn, item_id: int, ws_id: int | None) -> None:
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise KeyError(f"no item {item_id}")
    if item["status"] not in MOVABLE_STATUSES:
        raise Conflict(f"item {item_id} is {item['status']}; move it once that finishes")
    if ws_id is None:
        conn.execute("UPDATE items SET workstream_id = NULL, goal_id = NULL, filed_by = NULL, lead = ? WHERE id = ?",
                     (home_lead(conn, item), item_id))
        return
    ws = get(conn, ws_id)
    if ws["status"] == "archived":
        raise Conflict(f"workstream '{ws['name']}' is archived")
    conn.execute("UPDATE items SET workstream_id = ?, goal_id = NULL, filed_by = 'user', lead = 'workstream' "
                 "WHERE id = ?", (ws_id, item_id))


def promote_item(conn, item_id: int, name: str) -> int:
    """Make a new workstream from a queue item: its conversation becomes the first watched source."""
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise KeyError(f"no item {item_id}")
    desc = f"Started from queue item #{item_id}: {item['title']}"
    if item["summary"]:
        desc += "\n" + item["summary"]
    ws_id, _ = create(conn, name or item["title"], desc)
    refs = []
    if item["route_id"]:
        ev = conn.execute("SELECT e.source, e.payload FROM routes r JOIN events e ON e.id = r.event_id "
                          "WHERE r.id = ?", (item["route_id"],)).fetchone()
        if ev:
            refs = event_refs(ev["source"], json.loads(ev["payload"] or "{}"))
    for kind, ref in refs[:1]:
        try:
            add_source(conn, ws_id, kind, ref, by="user", url=item["url"], label=item["title"][:80])
        except (Conflict, ValueError):
            pass
    if conn.execute("SELECT workstream_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] != ws_id:
        move_item(conn, item_id, ws_id)
    return ws_id


def set_status(conn, ws_id: int, status: str) -> None:
    ws = get(conn, ws_id)
    if status == "archived":
        archive(conn, ws_id)
        return
    if ws["status"] == "archived":
        raise Conflict("an archived workstream can't be resumed; create a new one")
    conn.execute(f"UPDATE workstreams SET status = ?, updated_at = {NOW} WHERE id = ?", (status, ws_id))


def archive(conn, ws_id: int) -> int:
    """Open items from conversations and undrafted routes go back to the queue's leads; next steps the lead
    raised itself only made sense inside the workstream, so they are dismissed. Watched sources are released."""
    with transaction(conn):
        returned = 0
        for item in conn.execute("SELECT * FROM items WHERE workstream_id = ? AND status IN "
                                 "('pending','likely_done','changes_requested','approved','failed')",
                                 (ws_id,)).fetchall():
            if item["route_id"] is None and item["filed_by"] == "lead":
                conn.execute(f"UPDATE items SET status = 'dismissed', decided_at = {NOW}, updated_at = {NOW}, "
                             "feedback = 'workstream archived' WHERE id = ?", (item["id"],))
                continue
            conn.execute("UPDATE items SET workstream_id = NULL, goal_id = NULL, filed_by = NULL, lead = ? "
                         "WHERE id = ?", (home_lead(conn, item), item["id"]))
            returned += 1
        for r in conn.execute("SELECT r.id, e.source FROM routes r JOIN events e ON e.id = r.event_id "
                              "WHERE r.workstream_id = ? AND r.picked_up_at IS NULL", (ws_id,)).fetchall():
            conn.execute("UPDATE routes SET workstream_id = NULL, filed_by = NULL, lead = ? WHERE id = ?",
                         (SOURCE_TO_LEAD.get(r["source"], "chief"), r["id"]))
        conn.execute(f"UPDATE ws_sources SET status = 'removed', decided_at = {NOW} "
                     "WHERE workstream_id = ? AND status IN ('active','proposed')", (ws_id,))
        conn.execute(f"UPDATE workstreams SET status = 'archived', archived_at = {NOW}, updated_at = {NOW} "
                     "WHERE id = ?", (ws_id,))
    return returned


def instruct(conn, ws_id: int, text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("say what you want the lead to do")
    if get(conn, ws_id)["status"] != "active":
        raise Conflict("this workstream isn't active; resume it first")
    return conn.execute("INSERT INTO ws_instructions(workstream_id, text) VALUES (?, ?)", (ws_id, text)).lastrowid


# --- read models for the UI ---------------------------------------------------------------------------------

def summaries(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT w.id, w.slug, w.name, w.status, w.latest_at, w.last_run_at, w.created_at, "
        "SUM(i.status IN ('pending','changes_requested')) AS needs_review, "
        "SUM(i.status IN ('pending','changes_requested') AND i.priority = 0) AS urgent, "
        "COUNT(i.id) AS open_items, "
        "(SELECT COUNT(*) FROM ws_sources s WHERE s.workstream_id = w.id AND s.status = 'proposed') AS proposed_sources "
        "FROM workstreams w LEFT JOIN items i ON i.workstream_id = w.id AND i.status IN "
        "('pending','likely_done','changes_requested','approved','failed','executing') "
        "GROUP BY w.id ORDER BY CASE w.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, w.name"
    ).fetchall()
    return [{**dict(r), "needs_review": r["needs_review"] or 0, "urgent": r["urgent"] or 0} for r in rows]


def detail(conn, ws_id: int) -> dict:
    ws = dict(get(conn, ws_id))
    sources = [dict(r) for r in conn.execute(
        "SELECT * FROM ws_sources WHERE workstream_id = ? AND status IN ('active','proposed') "
        "ORDER BY status DESC, id", (ws_id,))]
    return {
        "workstream": ws,
        "sources": sources,
        "updates": [dict(r) for r in conn.execute(
            "SELECT * FROM ws_updates WHERE workstream_id = ? ORDER BY COALESCE(occurred_at, created_at) DESC, id DESC "
            "LIMIT 5", (ws_id,))],
        "instructions": [dict(r) for r in conn.execute(
            "SELECT * FROM ws_instructions WHERE workstream_id = ? ORDER BY id DESC LIMIT 8", (ws_id,))],
        "runs": [dict(r) for r in conn.execute(
            "SELECT id, status, started_at, finished_at, cost_usd, error FROM runs WHERE job = 'lead' AND lead = ? "
            "ORDER BY id DESC LIMIT 10", (f"ws:{ws['slug']}",))],
        "pending_activity": conn.execute(
            "SELECT COUNT(*) FROM ws_activity a JOIN ws_sources s ON s.id = a.source_id "
            "WHERE s.workstream_id = ? AND a.processed_run_id IS NULL", (ws_id,)).fetchone()[0],
    }
