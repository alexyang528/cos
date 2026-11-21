"""Telling the chief of staff something directly: track a conversation, add or retire a rule, rerun an agent.

My instructions are trusted. The chief answers with a reply and a set of actions; Python validates each action,
applies it, and records what it actually did (not what the chief says it did), which is what the page shows.

When I send a link the system missed, Python first looks up what it knows about that conversation (never scanned,
skipped by the intern, dropped by a lead, or already an item), so the chief can say why it was missed and write
the rule that would catch it next time, as well as filing it for the right lead.
"""

import json
import re
import sqlite3
from urllib.parse import urlparse

from . import config, prompts, store
from . import workstreams as ws
from .leads import LEADS, SOURCE_TO_LEAD
from .policy import pass_spec
from .runner import preview, run_agent

NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
URL = re.compile(r"https://[^\s<>()\"'`]+")
# Where each scanner and triage pass is defined, for the chief to read when it explains a miss.
CODE = {"slack scan": "chief/scan_slack.py", "email scan": "chief/scan_email.py",
        "github scan": "chief/scan_github.py", "notion scan": "chief/scan_notion.py",
        "switchboard scan": "chief/scan_switchboard.py", "granola scan": "chief/scan_granola.py",
        "intern (triage)": "chief/intern.py", "lead pass": "chief/lead.py",
        "lead charters": "leads/<lead>.md", "workstream lead": "chief/ws_lead.py"}
MAX_TRACK = 5


def add(conn: sqlite3.Connection, text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("say what you want the chief of staff to do")
    return conn.execute("INSERT INTO chief_instructions(text) VALUES (?)", (text[:4000],)).lastrowid


def pending(conn: sqlite3.Connection):
    """Instructions no chief run has taken yet."""
    return conn.execute("SELECT * FROM chief_instructions WHERE picked_up_at IS NULL ORDER BY id").fetchall()


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM chief_instructions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "actions": json.loads(r["actions"] or "[]")} for r in rows]


def runnable(conn: sqlite3.Connection) -> dict[str, str]:
    """Agents the chief may rerun, with what a run does."""
    out = {"intern": "triage events scanned but not yet judged",
           "chief": "the daily audit: rules, routing, expired items, workstream suggestions",
           "brain": "re-distill the preferences file from recent decisions"}
    for s in config.SCAN_SURFACES:
        out[f"scan:{s}"] = f"scan {s} for new events now"
    for name in config.ENABLED_LEADS:
        out[f"lead:{name}"] = f"the {name} lead works its inbox and revision requests (no call if both are empty)"
        out[f"review:{name}"] = f"the {name} lead re-reads its stalest waiting items and closes out finished ones"
    for w in ws.active(conn):
        out[f"ws:{w['slug']}"] = f"the '{w['name']}' workstream lead runs now"
    return out


# --- what the system knows about a link -----------------------------------------------------------------------

def source_of(url: str) -> str | None:
    from .lead import SOURCE_HOSTS
    host = urlparse(url).hostname or ""
    return next((s for s, h in SOURCE_HOSTS.items() if host.endswith(h.lstrip("."))), None)


def _event_view(conn, e) -> dict:
    r = conn.execute("SELECT r.*, w.name AS ws FROM routes r LEFT JOIN workstreams w ON w.id = r.workstream_id "
                     "WHERE r.event_id = ?", (e["id"],)).fetchone()
    items = conn.execute("SELECT id, status, title, feedback FROM items WHERE route_id = ?",
                         (r["id"],)).fetchall() if r else []
    return {"event_id": e["id"], "kind": e["kind"], "author": e["author"], "at": e["occurred_at"],
            "scanned_at": e["first_seen_at"], "text": (e["text"] or "")[:300],
            "triage": None if r is None else {
                "decision": r["decision"], "lead": r["lead"], "workstream": r["ws"], "summary": r["summary"],
                "reason": r["reason"], "lead_outcome": r["lead_outcome"],
                "lead_picked_up": r["picked_up_at"] is not None},
            "items": [dict(i) for i in items]}


def lookup(conn: sqlite3.Connection, url: str) -> dict:
    """Everything the system recorded about the conversation a link points at."""
    source = source_of(url)
    parsed = ws.parse_link(url)
    if source == "slack" and parsed and parsed[0] in ("slack_thread", "slack_channel"):
        channel, _, ts = parsed[1].partition(":")
        if ts:
            events = conn.execute(
                "SELECT * FROM events WHERE source = 'slack' AND json_extract(payload, '$.channel_id') = ? AND "
                "(json_extract(payload, '$.thread_ts') = ? OR json_extract(payload, '$.ts') = ?) ORDER BY id",
                (channel, ts, ts)).fetchall()
        else:
            events = []
    else:
        key = url.split("?")[0].split("#")[0].rstrip("/")
        events = conn.execute("SELECT * FROM events WHERE url LIKE ? ORDER BY id LIMIT 20",
                              (f"{key}%",)).fetchall() if source else []
    key = url.split("?")[0].rstrip("/")
    items = conn.execute("SELECT id, lead, status, title, feedback FROM items WHERE url LIKE ? ORDER BY id",
                         (f"{key}%",)).fetchall()
    watcher = ws.watcher_of(conn, *parsed[:2]) if parsed else None
    return {"url": url, "source": source, "events": [_event_view(conn, e) for e in events],
            "items_with_this_link": [dict(i) for i in items],
            "watched_by_workstream": watcher["name"] if watcher else None,
            "never_scanned": source is not None and not events}


# --- the pass -------------------------------------------------------------------------------------------------

def build_prompt(conn: sqlite3.Connection, instrs) -> str:
    me = config.ME_NAME
    links = {u for i in instrs for u in URL.findall(i["text"])}
    rules = [dict(r) for r in conn.execute(
        "SELECT id, agent, text, why, status FROM rules WHERE status IN ('provisional','confirmed') ORDER BY id")]
    agents = sorted({"intern", "*", *LEADS, *(f"ws:{w['slug']}" for w in ws.active(conn))})
    return f"""{prompts.SAFETY}
You are the chief of staff in {me}'s agent system: scanners collect events from Slack, Gmail, GitHub, Notion,
Switchboard and Granola; the intern triages each event (route to a lead, or skip); a lead per source reads the
conversation and files queue items for {me}, or drops the route with a reason; workstream leads track
long-running projects. {me} is talking to you directly. These instructions are trusted: do what they ask
within the actions below. Use your read-only tools to find threads and check facts.

What you can do:
- "track": put a conversation in front of its lead with {me}'s note, so the lead reads it and files an item.
  The lead always queues a tracked conversation; it doesn't judge whether it meets the bar. Give the link to
  the conversation itself (a Slack permalink to the thread, a PR, a plan). If {me} describes a thread without
  a link, find it (Slack search) and use its permalink; if you can't find it, say so in the reply.
- "rules": add a rule for an agent ({", ".join(agents)}). Rules are instructions every run of that agent
  reads. They take effect as confirmed rules because {me} asked; write them general enough to catch the next
  case like this one, specific enough not to flood the queue.
- "retire_rules": retire rules by id, when one is wrong or caused the miss.
- "runs": rerun agents now, from: {json.dumps(runnable(conn))}. A tracked conversation's lead runs on its own.
- "workstream_sources": watch a Slack channel or thread, or a Switchboard plan, in an active workstream.
- "workstream_instructions": pass an instruction on to a workstream's lead.
- "reply": always. What you did and found, in 1 to 6 short sentences, to {me}.

When {me} sends a conversation the system didn't pick up, find out why before writing rules. "Links" below has
what the system recorded for each link:
- never_scanned: no event. The scanner didn't collect it. Read the scanner's code to see what it searches for
  and say exactly why this was outside it. Rules don't change scanners; if it needs a code change, say what
  in the reply (you can't change code).
- triage.decision "skip": the intern judged it didn't need {me}; its reason is there. Write an intern rule.
- triage.decision "route" with lead_outcome "dropped: ...": the lead dropped it. Write a rule for that lead.
- An item exists: it was tracked, maybe closed (status, and {me}'s feedback). Say so rather than tracking it
  twice, unless {me} wants it back.
Code locations, to Read: {json.dumps(CODE)}.

Output schema:
{{"reply": "...",
 "track": [{{"url": "https://...", "summary": "one line: what it is and what {me} needs to do",
   "note": "what {me} asked for, for the lead"}}],
 "rules": [{{"agent": "intern", "text": "an instruction to that agent", "why": "the miss it prevents"}}],
 "retire_rules": [{{"rule_id": 3, "why": "..."}}],
 "runs": ["lead:slack"],
 "workstream_sources": [{{"workstream": "slug", "link": "https://..."}}],
 "workstream_instructions": [{{"workstream": "slug", "text": "..."}}]}}

Active rules:
{prompts.untrusted("rules", rules) if rules else "(none)"}

Links in the instructions:
{prompts.untrusted("links", [lookup(conn, u) for u in sorted(links)]) if links else "(none)"}

Instructions from {me}, oldest first:
{chr(10).join(f"- #{i['id']}: {i['text']}" for i in instrs)}
"""


def _track(conn, t: dict, instr_id: int) -> tuple[str, str | None]:
    """File a conversation for its lead as a manual event and route. Returns (what happened, agent to run)."""
    url = str(t.get("url") or "").strip()
    source = source_of(url) if url.startswith("https://") else None
    if source not in SOURCE_TO_LEAD:
        raise ValueError(f"not a link to a source the leads read: {url!r}")
    payload = {"url": url, "permalink": url, "instruction": t.get("note"), "author": config.ME_NAME}
    parsed = ws.parse_link(url)
    if source == "slack" and parsed and parsed[0] == "slack_thread":
        channel, _, ts = parsed[1].partition(":")
        payload.update(channel_id=channel, thread_ts=ts, ts=ts)
    elif source == "switchboard" and parsed and parsed[0] == "switchboard_plan":
        payload["plan_id"] = parsed[1]
    summary = str(t.get("summary") or "").strip()[:200] or f"Tracked at {config.ME_FIRST}'s request"
    source_id = f"manual:{url}:{instr_id}"
    event_id = conn.execute(
        "INSERT INTO events(source, source_id, kind, url, author, text, occurred_at, payload) "
        f"VALUES (?, ?, 'manual', ?, ?, ?, {NOW}, ?)",
        (source, source_id, url, config.ME_NAME, str(t.get("note") or summary)[:2000], json.dumps(payload)),
    ).lastrowid
    from .intern import route_target
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    lead, ws_id, filed_by = route_target(conn, event)
    conn.execute("INSERT INTO routes(event_id, decision, lead, summary, url, reason, workstream_id, filed_by) "
                 "VALUES (?, 'route', ?, ?, ?, ?, ?, ?)",
                 (event_id, lead, summary, url, f"tracked at {config.ME_FIRST}'s request (instruction #{instr_id})",
                  ws_id, filed_by))
    if ws_id:
        w = ws.get(conn, ws_id)
        return f"Sent {url} to the '{w['name']}' workstream lead", f"ws:{w['slug']}"
    return f"Sent {url} to the {lead} lead", f"lead:{lead}"


def apply(conn: sqlite3.Connection, instrs, data: dict, run_id: int) -> tuple[list[str], list[str], list[str]]:
    """Apply the chief's actions. Returns (what was done, agents to run, what was refused)."""
    from .memory import add_rule, confirm_rule, retire_rule
    done, runs, problems = [], [], []
    first = instrs[0]["id"]
    for t in (data.get("track") or [])[:MAX_TRACK]:
        try:
            what, agent = _track(conn, t if isinstance(t, dict) else {}, first)
            done.append(what)
            runs.append(agent)
        except (ValueError, sqlite3.IntegrityError) as e:
            problems.append(f"track: {e}")
    for r in (data.get("rules") or [])[:5]:
        try:
            rid = add_rule(conn, agent=str(r.get("agent") or ""), text=str(r.get("text") or ""),
                           why=str(r.get("why") or ""), source="chief", source_ref=f"instruction:{first}")
            confirm_rule(conn, rid, by="user")
            done.append(f"Added rule #{rid} for {r['agent']}: {r['text']}")
        except (ValueError, AttributeError) as e:
            problems.append(f"rule: {e}")
    for r in (data.get("retire_rules") or [])[:5]:
        try:
            retire_rule(conn, int(r.get("rule_id")), by="user")
            done.append(f"Retired rule #{r['rule_id']}")
        except (ValueError, TypeError, AttributeError) as e:
            problems.append(f"retire rule: {e}")
    for s in (data.get("workstream_sources") or [])[:5]:
        try:
            w = ws.by_slug(conn, str(s.get("workstream") or ""))
            parsed = ws.parse_link(str(s.get("link") or ""))
            if not parsed:
                raise ValueError(f"not a Slack channel/thread or Switchboard plan: {s.get('link')!r}")
            ws.add_source(conn, w["id"], parsed[0], parsed[1], by="user", url=parsed[2])
            done.append(f"'{w['name']}' now watches {s['link']}")
        except (KeyError, ValueError, ws.Conflict, AttributeError) as e:
            problems.append(f"workstream source: {e}")
    for s in (data.get("workstream_instructions") or [])[:5]:
        try:
            w = ws.by_slug(conn, str(s.get("workstream") or ""))
            ws.instruct(conn, w["id"], str(s.get("text") or ""))
            done.append(f"Told the '{w['name']}' lead: {s['text']}")
            runs.append(f"ws:{w['slug']}")
        except (KeyError, ValueError, ws.Conflict, AttributeError) as e:
            problems.append(f"workstream instruction: {e}")
    allowed = runnable(conn)
    for a in data.get("runs") or []:
        if a in allowed:
            runs.append(a)
        else:
            problems.append(f"run {a!r}: not an agent it can run")
    return done, list(dict.fromkeys(runs)), problems


def run_agents(conn: sqlite3.Connection, agents: list[str]) -> list[str]:
    """Run what the chief asked for, in order. Returns one line per failure."""
    from . import chief, intern, lead, memory, scan_email, scan_github, scan_granola, scan_notion, scan_slack
    from . import scan_switchboard, ws_lead
    scanners = {"slack": scan_slack, "switchboard": scan_switchboard, "granola": scan_granola,
                "notion": scan_notion, "email": scan_email, "github": scan_github}
    failed = []
    for a in agents:
        kind, _, name = a.partition(":")
        try:
            if kind == "scan":
                scanners[name].run(conn)
            elif kind == "lead":
                lead.run(conn, name)
            elif kind == "review":
                lead.review_open(conn, name, force=True)
            elif kind == "ws":
                ws_lead.run(conn, ws.by_slug(conn, name), reasons=[f"{config.ME_FIRST} asked the chief of staff"])
            elif a == "intern":
                intern.run(conn)
            elif a == "chief":
                chief.run(conn)
            elif a == "brain":
                memory.distill(conn)
        except Exception as e:  # one failed run must not stop the others; the budget cap stops them all
            failed.append(f"{a}: {type(e).__name__}: {e}")
            from .runner import BudgetExceeded
            if isinstance(e, BudgetExceeded):
                break
    return failed


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    instrs = pending(conn)
    if not instrs:
        print("tell: no instructions for the chief of staff (no model call)")
        return
    spec = pass_spec("chief")
    prompt = build_prompt(conn, instrs)
    if dry_run:
        print(preview(spec, prompt))
        return
    result = run_agent(conn, spec, prompt, job="chief", lead="tell")
    ids = [i["id"] for i in instrs]
    marks = ",".join("?" * len(ids))
    if not result.ok:
        conn.execute(f"UPDATE chief_instructions SET picked_up_at = {NOW}, finished_at = {NOW}, run_id = ?, "
                     f"response = ? WHERE id IN ({marks})",
                     (result.run_id, f"The chief of staff couldn't finish: {result.error}. Send it again.", *ids))
        print(f"tell failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    # Picked up before the reruns, so a crash during them can't apply the same actions twice; finished after,
    # so the page shows the chief still working until the agents it started are done.
    done, runs, problems = apply(conn, instrs, result.data, result.run_id)
    reply = str(result.data.get("reply") or "").strip()[:3000] or "(no reply)"
    actions = done + [f"Couldn't: {p}" for p in problems]
    conn.execute(f"UPDATE chief_instructions SET picked_up_at = {NOW}, run_id = ?, response = ?, actions = ? "
                 f"WHERE id IN ({marks})",
                 (result.run_id, reply, json.dumps(actions + [f"Running {a}…" for a in runs]), *ids))
    print(f"tell: {len(ids)} instructions, did {done}, running {runs} (run {result.run_id}, ${result.cost_usd:.3f})")
    for p in problems:
        print(f"  rejected: {p}")
    failed = dict(f.split(": ", 1) for f in run_agents(conn, runs))
    actions += [f"Ran {a}" if a not in failed else f"Run of {a} failed: {failed[a]}" for a in runs]
    conn.execute(f"UPDATE chief_instructions SET actions = ?, finished_at = {NOW} WHERE id IN ({marks})",
                 (json.dumps(actions), *ids))
    for a, err in failed.items():
        print(f"  run failed: {a}: {err}")
