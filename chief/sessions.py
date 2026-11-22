"""My Claude Code sessions: an incremental index, and the pass where a lead links them to its open items.

The CLI, the desktop app and Orca all write transcripts to ~/.claude/projects/<project>/<session>.jsonl.
Indexing reads only my prompts, titles and PR links (assistant turns are most of the bytes and say little
about what the session was for), and re-reads a file only when its size or mtime changed.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, paths, prompts
from .policy import pass_spec
from .runner import preview, run_agent
from .store import OPEN_STATUSES

# A session still in use is left alone until it has been quiet this long, so one burst of work is judged
# once instead of on every tick.
SETTLE_MIN = 20

NOISE_PREFIXES = (
    "<task-notification", "<command-", "<local-command", "[Request interrupted", "This session is being continued",
    "<system-reminder", "Caveat:", "<bash-",
)
SLACK_LINK = re.compile(r"https://[\w.-]*slack\.com/archives/([CDG][A-Z0-9]+)/p(\d{10})(\d{6})[^\s)>\]|]*")
THREAD_TS = re.compile(r"thread_ts=(\d{9,11}\.\d{1,6})")
PLAN = re.compile(r"\bpl_[0-9a-f]{32}\b")
PR_LINK = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
URL = re.compile(r"https?://[^\s)>\]|\"'`]+")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refs_in(text: str) -> set[str]:
    """Slack threads/messages, Switchboard plans and GitHub PRs a text points at, as comparable keys."""
    refs = set()
    for m in SLACK_LINK.finditer(text):
        refs.add(f"slack:{m.group(1)}:{m.group(2)}.{m.group(3)}")
        t = THREAD_TS.search(m.group(0))
        if t:
            refs.add(f"slack:{m.group(1)}:{t.group(1)}")
    refs.update(f"switchboard:{p}" for p in PLAN.findall(text))
    refs.update(f"github:{repo.lower()}#{n}" for repo, n in PR_LINK.findall(text))
    return refs


def item_refs(url: str | None, payload: dict | None) -> set[str]:
    refs = refs_in(url or "")
    payload = payload or {}
    channel = payload.get("channel_id")
    for key in ("ts", "thread_ts"):
        if channel and payload.get(key):
            refs.add(f"slack:{channel}:{payload[key]}")
    if payload.get("plan_id"):
        refs.add(f"switchboard:{payload['plan_id']}")
    return refs


def _prompt_text(content) -> str:
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = (content or "").strip() if isinstance(content, str) else ""
    return "" if text.startswith(NOISE_PREFIXES) else text


def parse(path: Path) -> dict:
    info = {"cwd": None, "entrypoint": None, "git_branch": None, "started_at": None, "custom_title": None,
            "ai_title": None, "prs": [], "prompts": []}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"type":"assistant"' in line or '"type":"api-request' in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "custom-title" and d.get("customTitle"):
                info["custom_title"] = d["customTitle"]
            elif t == "ai-title" and d.get("aiTitle"):
                info["ai_title"] = d["aiTitle"]
            elif t == "pr-link" and d.get("prUrl") and d["prUrl"] not in info["prs"]:
                info["prs"].append(d["prUrl"])
            elif t == "user" and not d.get("isSidechain"):
                info["cwd"] = info["cwd"] or d.get("cwd")
                info["entrypoint"] = info["entrypoint"] or d.get("entrypoint")
                info["started_at"] = info["started_at"] or d.get("timestamp")
                info["git_branch"] = d.get("gitBranch") or info["git_branch"]
                if d.get("isMeta") or d.get("isCompactSummary"):
                    continue
                text = _prompt_text((d.get("message") or {}).get("content"))
                if text and (not info["prompts"] or info["prompts"][-1] != text):
                    info["prompts"].append(text)
    return info


def resume_command(cwd: str | None, session_id: str) -> str:
    import shlex
    return (f"cd {shlex.quote(cwd)} && " if cwd else "") + f"claude --resume {session_id}"


def _digest(sid: str, info: dict, title: str, last_at: str) -> str:
    me = config.ME_NAME
    ps = info["prompts"]
    shown = ps if len(ps) <= 60 else ps[:20] + [f"… {len(ps) - 60} prompts omitted …"] + ps[-40:]
    lines = [
        f"# {title}",
        f"session {sid} · {info['entrypoint']} · {info['cwd']} · branch {info['git_branch']}",
        f"started {info['started_at']} · last active {last_at}",
        "PRs: " + (", ".join(info["prs"]) or "none"),
        f"Resume: {resume_command(info['cwd'], sid)}",
        "",
        f"## {me}'s prompts ({len(ps)})",
        *(f"{i}. {p[:800]}" for i, p in enumerate(shown, 1)),
    ]
    return "\n".join(lines) + "\n"


def index(conn: sqlite3.Connection) -> tuple[int, int]:
    """Bring the index up to date. Returns (files re-read, sessions now linkable)."""
    paths.sessions_dir().mkdir(parents=True, exist_ok=True)
    known = {r["id"]: (r["size"], r["mtime"]) for r in conn.execute("SELECT id, size, mtime FROM claude_sessions")}
    own = {r[0] for r in conn.execute("SELECT session_id FROM runs WHERE session_id IS NOT NULL")}
    # A session relocated between project dirs leaves a copy behind; the newest file is the real one.
    files: dict[str, Path] = {}
    for p in paths.claude_projects_dir().glob("*/*.jsonl"):
        if p.stem not in files or p.stat().st_mtime > files[p.stem].stat().st_mtime:
            files[p.stem] = p
    changed = 0
    for sid, path in files.items():
        st = path.stat()
        if known.get(sid) == (st.st_size, st.st_mtime):
            continue
        info = parse(path)
        last_at = _iso(datetime.fromtimestamp(st.st_mtime, timezone.utc))
        # sdk-cli is headless `claude -p`, i.e. this system's own lead and intern runs; sessions in this repo
        # are about building the system, never about a queue item.
        in_repo = bool(info["cwd"]) and Path(info["cwd"]).is_relative_to(paths.ROOT)
        skipped = int(sid in own or info["entrypoint"] == "sdk-cli" or in_repo or not info["prompts"])
        title = info["custom_title"] or info["ai_title"] or (info["prompts"][0][:80] if info["prompts"] else sid)
        refs, urls = set(), []
        for text in info["prompts"] + info["prs"]:
            refs |= refs_in(text)
            urls += [u for u in URL.findall(text) if u not in urls]
        digest_path = None
        if not skipped:
            digest_path = paths.sessions_dir() / f"{sid}.md"
            digest_path.write_text(_digest(sid, info, title, last_at))
        conn.execute(
            "INSERT INTO claude_sessions(id, path, size, mtime, skipped, cwd, entrypoint, git_branch, title, "
            "started_at, last_at, prompt_count, opening_prompts, prs, refs, urls, digest_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "path = excluded.path, size = excluded.size, mtime = excluded.mtime, skipped = excluded.skipped, "
            "cwd = excluded.cwd, entrypoint = excluded.entrypoint, git_branch = excluded.git_branch, "
            "title = excluded.title, started_at = excluded.started_at, last_at = excluded.last_at, "
            "prompt_count = excluded.prompt_count, opening_prompts = excluded.opening_prompts, prs = excluded.prs, "
            "refs = excluded.refs, urls = excluded.urls, digest_path = excluded.digest_path, "
            "indexed_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')",
            (sid, str(path), st.st_size, st.st_mtime, skipped, info["cwd"], info["entrypoint"], info["git_branch"],
             title[:200], info["started_at"], last_at, len(info["prompts"]),
             json.dumps([p[:300] for p in info["prompts"][:3]]), json.dumps(info["prs"]),
             json.dumps(sorted(refs)), json.dumps(urls[:30]), str(digest_path) if digest_path else None),
        )
        _link_launched(conn, sid, info["prompts"][:3])
        changed += 1
    live = conn.execute("SELECT COUNT(*) FROM claude_sessions WHERE skipped = 0").fetchone()[0]
    return changed, live


LAUNCHED_WHY = "Started from this item"


def _link_launched(conn: sqlite3.Connection, sid: str, opening_prompts: list[str]) -> None:
    """A session started from an item carries its tag in the first prompt: link it for certain. A link I removed
    stays removed (the insert is ignored)."""
    from . import ws_sessions
    from .launch import tagged_items
    for item_id in tagged_items(opening_prompts):
        item = conn.execute("SELECT workstream_id FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            continue
        conn.execute("INSERT OR IGNORE INTO item_sessions(item_id, session_id, why) VALUES (?, ?, ?)",
                     (item_id, sid, LAUNCHED_WHY))
        if item["workstream_id"]:
            ws_sessions.link(conn, item["workstream_id"], sid, LAUNCHED_WHY, None)


def _catalog_entry(s: sqlite3.Row) -> dict:
    return {
        "session_id": s["id"], "title": s["title"], "project": s["cwd"], "branch": s["git_branch"],
        "started_at": s["started_at"], "last_active": s["last_at"], "prompt_count": s["prompt_count"],
        "prs": json.loads(s["prs"]), "links_in_prompts": json.loads(s["urls"])[:8],
        "opening_prompts": [p[:200] for p in json.loads(s["opening_prompts"])[:2]], "digest": s["digest_path"],
    }


def _candidates(items, sessions, seen, now: datetime) -> dict[int, list[sqlite3.Row]]:
    """Sessions each item still needs judging against.

    An item already checked only needs the sessions with activity since. A new item is checked against the
    last SESSION_FIRST_CHECK_DAYS of sessions plus any older one whose prompts link its Slack thread or plan:
    work on an item almost always happens around when it arrives, and the full 30-day catalog per new item
    was the bulk of the linking cost.
    """
    out = {}
    for it in items:
        refs = item_refs(it["url"], json.loads(it["payload"]) if it["payload"] else None)
        since = _window_start(it, now)
        cands = [s for s in sessions if (it["id"], s["id"]) not in seen and (
            s["last_at"] > since or (not it["sessions_checked_at"] and refs & set(json.loads(s["refs"]))))]
        if cands:
            out[it["id"]] = cands
    return out


def _window_start(item, now: datetime) -> str:
    return item["sessions_checked_at"] or _iso(now - timedelta(days=config.SESSION_FIRST_CHECK_DAYS))


def link(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    """One pass for every lead's open items and every active workstream: decide which recently active sessions
    belong to which item, and which to which workstream."""
    from . import ws_sessions
    leads = tuple(config.ENABLED_LEADS)
    items = conn.execute(
        f"SELECT i.id, i.lead, i.kind, i.title, i.summary, i.url, i.sessions_checked_at, i.workstream_id, "
        f"w.name AS workstream, e.payload FROM items i "
        f"LEFT JOIN routes r ON r.id = i.route_id LEFT JOIN events e ON e.id = r.event_id "
        f"LEFT JOIN workstreams w ON w.id = i.workstream_id "
        f"WHERE (i.lead IN ({','.join('?' * len(leads))}) OR i.workstream_id IS NOT NULL) "
        f"AND i.status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY i.id",
        (*leads, *OPEN_STATUSES),
    ).fetchall()
    has_ws = conn.execute("SELECT 1 FROM workstreams WHERE status = 'active' LIMIT 1").fetchone()
    if not items and not has_ws:
        print("sessions: no open items or workstreams (no model call)")
        return
    now = datetime.now(timezone.utc)
    # Each item is checked against the sessions active in (its last check, settled]; the next pass picks up
    # from settled, so a session that is still busy now is judged on a later pass, not skipped.
    settled = _iso(now - timedelta(minutes=SETTLE_MIN))
    oldest = _iso(now - timedelta(days=config.SESSION_LOOKBACK_DAYS))
    sessions = conn.execute(
        "SELECT * FROM claude_sessions WHERE skipped = 0 AND last_at > ? AND last_at <= ? ORDER BY last_at DESC",
        (oldest, settled),
    ).fetchall()
    ids = [i["id"] for i in items]
    seen = {(r[0], r[1]) for r in conn.execute(
        f"SELECT item_id, session_id FROM item_sessions WHERE item_id IN ({','.join('?' * len(ids))})", ids)} if ids else set()

    cands = _candidates(items, sessions, seen, now)
    ws_work, ws_allowed, ws_pinned = ws_sessions.work(conn, sessions, now)
    if not cands and not ws_work:
        print(f"sessions: no new sessions to check against {len(items)} open items or the workstreams (no model call)")
        return
    # Newest first, but a session that links an item's own source is never cut by the cap.
    by_id = {s["id"]: s for c in cands.values() for s in c}
    by_id.update({s["id"]: s for s in sessions if any(s["id"] in a for a in ws_allowed.values())})
    work, allowed = [], {}
    for it in items:
        if it["id"] not in cands:
            continue
        refs = item_refs(it["url"], json.loads(it["payload"]) if it["payload"] else None)
        allowed[it["id"]] = {s["id"] for s in cands[it["id"]]}
        work.append({
            "item_id": it["id"], "lead": it["lead"], "workstream": it["workstream"], "kind": it["kind"], "title": it["title"],
            "summary": (it["summary"] or "")[:300], "url": it["url"],
            "consider_sessions_active_after": _window_start(it, now),
            "sessions_that_link_this_source": [s["id"] for s in cands[it["id"]] if refs & set(json.loads(s["refs"]))],
        })
    pinned = {sid for w in work for sid in w["sessions_that_link_this_source"]} | ws_pinned
    ranked = sorted(by_id.values(), key=lambda s: s["last_at"], reverse=True)
    keep = [s for s in ranked if s["id"] in pinned]
    keep += [s for s in ranked if s["id"] not in pinned][:max(0, config.SESSION_LINK_BATCH - len(keep))]
    catalog = {s["id"]: s for s in sorted(keep, key=lambda s: s["last_at"], reverse=True)}
    for w in work:
        allowed[w["item_id"]] &= set(catalog)
    for wid in ws_allowed:
        ws_allowed[wid] &= set(catalog)
    item_ws = {it["id"]: it["workstream_id"] for it in items if it["workstream_id"]}

    me = config.ME_NAME
    prompt = f"""{prompts.SAFETY}
You work for {me}'s leads (slack: Slack threads waiting on {me}; switchboard: Switchboard plans waiting on
{me}; granola: action items from {me}'s meetings; github: PRs waiting on {me}'s review, and reviews of {me}'s
PRs; workstream: the lead of one of {me}'s long-running projects, named in the item's "workstream").
{me} sometimes works on one of their items in a Claude Code session: researching it, drafting the answer, building the fix, or talking it through. Link each open item below to
the sessions that were about the same piece of work, so {me} can jump back into them. Then link sessions to
the workstreams below.

How to judge:
- Same work means the session was about this conversation, plan, PR, bug, customer or launch. A shared broad
  topic (both are "about taxes", both touch the same repo) is not enough.
- "sessions_that_link_this_source" lists sessions whose prompts contain this item's Slack thread,
  Switchboard plan or PR link, or that opened its PR. That is strong evidence; confirm it from the digest.
- Judge each item only against sessions with last_active after its consider_sessions_active_after, plus
  its sessions_that_link_this_source.
- Shortlist from titles, PRs, links and opening prompts, then Read the digest file of each plausible session
  before linking. Most items have no session; don't read digests of sessions that are plainly unrelated.
- Precision over recall: a wrong link costs {me} more than a missing one. When unsure, don't link.

{ws_sessions.PROMPT_RULES.format(me=me)}

Output schema:
{{"links": [{{"item_id": 1, "session_id": "uuid", "why": "one line: what ties them, e.g. both are the MD PFML setup-param rollout"}}],
 "workstream_links": [{{"workstream_id": 1, "session_id": "uuid", "why": "one line: what the session did for this project"}}]}}
Empty lists are a fine answer.

Open items:
{prompts.untrusted("queue-items", work) if work else "(none with new sessions to judge)"}

Workstreams:
{prompts.untrusted("workstreams", ws_work) if ws_work else "(none with new sessions to judge)"}

Claude Code sessions (newest first):
{prompts.untrusted("claude-sessions", [_catalog_entry(s) for s in catalog.values()])}
"""
    spec = pass_spec("link")
    if dry_run:
        print(preview(spec, prompt))
        return
    result = run_agent(conn, spec, prompt, job="lead", lead="sessions")
    if not result.ok:
        print(f"sessions failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    linked, rejected = [], []
    for ln in result.data.get("links") or []:
        iid, sid = ln.get("item_id"), ln.get("session_id")
        if sid not in allowed.get(iid, ()):
            rejected.append(f"{iid}->{sid}")
            continue
        cur = conn.execute("INSERT OR IGNORE INTO item_sessions(item_id, session_id, why, run_id) VALUES (?, ?, ?, ?)",
                           (iid, sid, str(ln.get("why") or "")[:500], result.run_id))
        if cur.rowcount:
            linked.append(f"#{iid}->{catalog[sid]['title'][:40]!r}")
        # Work on a workstream's item is work on the workstream.
        if iid in item_ws:
            ws_sessions.link(conn, item_ws[iid], sid, str(ln.get("why") or "")[:500], result.run_id)
    ws_linked, ws_rejected = ws_sessions.apply(conn, result.data.get("workstream_links"), ws_allowed, result.run_id)
    linked += ws_linked
    rejected += ws_rejected
    if allowed:
        conn.execute(f"UPDATE items SET sessions_checked_at = ? WHERE id IN ({','.join('?' * len(allowed))})",
                     (settled, *allowed))
    ws_sessions.mark_checked(conn, ws_allowed, settled)
    print(f"sessions: checked {len(work)} items and {len(ws_work)} workstreams against {len(catalog)} sessions, linked {linked or 'none'}"
          + (f", rejected {rejected}" if rejected else "") + f" (run {result.run_id}, ${result.cost_usd:.3f})")


def links_for(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, list[dict]]:
    if not item_ids:
        return {}
    rows = conn.execute(
        f"SELECT l.item_id, l.why, l.created_at, s.id, s.title, s.cwd, s.git_branch, s.last_at, s.prs, "
        f"s.digest_path FROM item_sessions l JOIN claude_sessions s ON s.id = l.session_id "
        f"WHERE l.removed_at IS NULL AND l.item_id IN ({','.join('?' * len(item_ids))}) ORDER BY s.last_at DESC",
        item_ids,
    ).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["item_id"], []).append({
            "session_id": r["id"], "title": r["title"], "cwd": r["cwd"], "branch": r["git_branch"],
            "last_at": r["last_at"], "prs": json.loads(r["prs"]), "why": r["why"], "digest": r["digest_path"],
            "resume": resume_command(r["cwd"], r["id"]),
        })
    return out


def all_sessions(conn: sqlite3.Connection, limit: int = 300) -> list[dict]:
    """Every Claude Code session I ran, newest first, with the items and workstreams it is linked to. Unlike
    linking, this keeps sessions in this repo; it leaves out only this system's own headless runs. Those are
    known by the preamble every agent prompt opens with: run from an Orca or other terminal they don't carry the
    sdk-cli entrypoint, and runs against a scratch database (COS_HOME) aren't in this runs table."""
    agent_opening = "[" + json.dumps(prompts.SAFETY.split("\n", 1)[0])[:-1] + "%"
    rows = conn.execute(
        "SELECT * FROM claude_sessions WHERE prompt_count > 0 AND COALESCE(entrypoint, '') != 'sdk-cli' "
        "AND opening_prompts NOT LIKE ? AND id NOT IN (SELECT session_id FROM runs WHERE session_id IS NOT NULL) "
        "ORDER BY last_at DESC LIMIT ?", (agent_opening, limit)).fetchall()
    ids = [r["id"] for r in rows]
    items: dict[str, list[dict]] = {}
    streams: dict[str, list[dict]] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(
                f"SELECT l.session_id, i.id, COALESCE(i.ask, i.title) AS name, i.status FROM item_sessions l "
                f"JOIN items i ON i.id = l.item_id WHERE l.removed_at IS NULL AND l.session_id IN ({marks}) "
                f"ORDER BY i.id", ids):
            items.setdefault(r["session_id"], []).append({"id": r["id"], "name": r["name"], "status": r["status"]})
        for r in conn.execute(
                f"SELECT l.session_id, w.id, w.name FROM ws_sessions l JOIN workstreams w ON w.id = l.workstream_id "
                f"WHERE l.removed_at IS NULL AND w.status != 'archived' AND l.session_id IN ({marks})", ids):
            streams.setdefault(r["session_id"], []).append({"id": r["id"], "name": r["name"]})
    return [{"session_id": r["id"], "title": r["title"], "cwd": r["cwd"], "branch": r["git_branch"],
             "entrypoint": r["entrypoint"], "started_at": r["started_at"], "last_at": r["last_at"],
             "prompts": r["prompt_count"], "prs": json.loads(r["prs"] or "[]"),
             "resume": resume_command(r["cwd"], r["id"]),
             "items": items.get(r["id"], []), "workstreams": streams.get(r["id"], [])} for r in rows]


def unlink(conn: sqlite3.Connection, item_id: int, session_id: str) -> bool:
    cur = conn.execute(
        "UPDATE item_sessions SET removed_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE item_id = ? AND session_id = ? AND removed_at IS NULL",
        (item_id, session_id),
    )
    return cur.rowcount == 1
