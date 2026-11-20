"""Claude Code sessions linked to workstreams.

The unified session-linking pass (sessions.link) judges workstreams alongside queue items in the same model
call: this module builds each active workstream's entry, validates the links the model returns, and serves
them to the web app and to the workstream's lead (which reads the digests as context on what I've done).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config

NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_refs(conn: sqlite3.Connection, ws_id: int) -> tuple[set[str], set[str]]:
    """(exact session-ref keys, Slack channel prefixes) for a workstream's watched sources, in the format
    sessions.refs_in produces: slack:<channel>:<ts> and switchboard:<plan>."""
    exact, prefixes = set(), set()
    for s in conn.execute("SELECT kind, ref FROM ws_sources WHERE workstream_id = ? AND status = 'active'", (ws_id,)):
        if s["kind"] == "slack_thread":
            exact.add(f"slack:{s['ref']}")
        elif s["kind"] == "slack_channel":
            prefixes.add(f"slack:{s['ref']}:")
        else:
            exact.add(f"switchboard:{s['ref']}")
    return exact, prefixes


def links_its_sources(session_refs: list[str], exact: set[str], prefixes: set[str]) -> bool:
    return any(r in exact or any(r.startswith(p) for p in prefixes) for r in session_refs)


def window_start(w, now: datetime) -> str:
    return w["sessions_checked_at"] or _iso(now - timedelta(days=config.SESSION_FIRST_CHECK_DAYS))


def work(conn: sqlite3.Connection, sessions, now: datetime) -> tuple[list[dict], dict[int, set[str]], set[str]]:
    """Entries for every active workstream with sessions still to judge: (entries, allowed session ids per
    workstream, session ids that link a workstream's own sources and so must stay in the catalog)."""
    entries, allowed, pinned = [], {}, set()
    for w in conn.execute("SELECT * FROM workstreams WHERE status = 'active' ORDER BY id").fetchall():
        seen = {r[0] for r in conn.execute("SELECT session_id FROM ws_sessions WHERE workstream_id = ?", (w["id"],))}
        exact, prefixes = source_refs(conn, w["id"])
        since = window_start(w, now)
        cands, strong = [], []
        for s in sessions:
            if s["id"] in seen:
                continue
            hits = links_its_sources(json.loads(s["refs"]), exact, prefixes)
            if s["last_at"] > since or (hits and not w["sessions_checked_at"]):
                cands.append(s["id"])
                if hits:
                    strong.append(s["id"])
        if not cands:
            continue
        allowed[w["id"]] = set(cands)
        pinned.update(strong)
        sources = [r["label"] or r["ref"] for r in conn.execute(
            "SELECT label, ref FROM ws_sources WHERE workstream_id = ? AND status = 'active'", (w["id"],))]
        entries.append({
            "workstream_id": w["id"], "name": w["name"],
            "about": (w["description"] or "")[:500], "charter": (w["charter"] or "")[:800],
            "latest": (w["latest"] or "")[:600], "watched_sources": sources[:12],
            "already_linked": [r[0] for r in conn.execute(
                "SELECT s.title FROM ws_sessions l JOIN claude_sessions s ON s.id = l.session_id "
                "WHERE l.workstream_id = ? AND l.removed_at IS NULL ORDER BY s.last_at DESC LIMIT 6", (w["id"],))],
            "consider_sessions_active_after": since,
            "sessions_that_link_its_sources": strong,
        })
    return entries, allowed, pinned


PROMPT_RULES = """\
Workstreams are long-running projects {me} leads. Link a session to a workstream when the session was spent
working on that project: building or reviewing its code, researching it, drafting its docs or messages,
analysing its data. A session about one of the workstream's queue items is usually about the workstream too.
The same precision bar applies: a shared broad topic is not enough. A session may belong to at most one
workstream unless it plainly spans two. "sessions_that_link_its_sources" lists sessions whose prompts
contain one of its watched Slack threads, channels or plans: strong evidence, confirm it from the digest.
Judge each workstream only against sessions active after its consider_sessions_active_after, plus those."""


def apply(conn: sqlite3.Connection, links: list, allowed: dict[int, set[str]], run_id: int) -> tuple[list[str], list[str]]:
    linked, rejected = [], []
    for ln in links or []:
        wid, sid = (ln or {}).get("workstream_id"), (ln or {}).get("session_id")
        if sid not in allowed.get(wid, ()):
            rejected.append(f"ws{wid}->{sid}")
            continue
        if link(conn, wid, sid, str(ln.get("why") or "")[:500], run_id):
            linked.append(f"ws{wid}->{sid[:8]}")
    return linked, rejected


def link(conn: sqlite3.Connection, ws_id: int, session_id: str, why: str, run_id: int | None) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO ws_sessions(workstream_id, session_id, why, run_id) VALUES (?, ?, ?, ?)",
                       (ws_id, session_id, why, run_id))
    return cur.rowcount == 1


def mark_checked(conn: sqlite3.Connection, ws_ids, settled: str) -> None:
    ids = list(ws_ids)
    if ids:
        conn.execute(f"UPDATE workstreams SET sessions_checked_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                     (settled, *ids))


def links_for(conn: sqlite3.Connection, ws_id: int, limit: int = 30) -> list[dict]:
    from .sessions import resume_command
    rows = conn.execute(
        "SELECT l.why, l.created_at, s.id, s.title, s.cwd, s.git_branch, s.last_at, s.prs, s.digest_path, s.prompt_count "
        "FROM ws_sessions l JOIN claude_sessions s ON s.id = l.session_id "
        "WHERE l.workstream_id = ? AND l.removed_at IS NULL ORDER BY s.last_at DESC LIMIT ?", (ws_id, limit)).fetchall()
    return [{"session_id": r["id"], "title": r["title"], "cwd": r["cwd"], "branch": r["git_branch"],
             "last_at": r["last_at"], "prs": json.loads(r["prs"]), "why": r["why"], "digest": r["digest_path"],
             "prompt_count": r["prompt_count"], "resume": resume_command(r["cwd"], r["id"])} for r in rows]


def unlink(conn: sqlite3.Connection, ws_id: int, session_id: str) -> bool:
    cur = conn.execute(f"UPDATE ws_sessions SET removed_at = {NOW} "
                       "WHERE workstream_id = ? AND session_id = ? AND removed_at IS NULL", (ws_id, session_id))
    return cur.rowcount == 1


def lead_context(conn: sqlite3.Connection, ws_id: int, limit: int = 8) -> list[dict]:
    """What the workstream's lead sees: my recent sessions on it, with digests it can Read for detail."""
    return [{"title": s["title"], "last_active": s["last_at"], "project": s["cwd"], "branch": s["branch"],
             "prs": s["prs"], "why_linked": s["why"], "digest": s["digest"]}
            for s in links_for(conn, ws_id, limit)]
