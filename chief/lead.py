"""Run one surface lead: work its inbox and revision requests, push proposals to the queue."""

import json
import re
import sqlite3

from . import config, paths, prompts, store
from .links import fix_notion_links
from .leads import LEADS
from .policy import LEAD_ACTIONS, pass_spec
from .runner import preview, run_agent

TS = re.compile(r"^\d{9,11}\.\d{1,6}$")
CHANNEL = re.compile(r"^[CDG][A-Z0-9]{6,}$")
PLAN_ID = re.compile(r"^pl_[0-9a-f]{32}$")

ACTION_FORMATS = {
    "switchboard_approve_phase": '{"type": "switchboard_approve_phase", "plan_id": "pl_...", "phase": '
                                 '"problem_definition" | "technical_design"}. Only when can_approve is true.',
    "switchboard_close_plan": '{"type": "switchboard_close_plan", "plan_id": "pl_..."}. Closes the plan for good. '
                              f'ONLY when {config.ME_FIRST} explicitly instructs you to close it; never propose it '
                              'yourself.',
}
ACTION_FIELDS = {
    "switchboard_close_plan": {"type", "plan_id"},
    "slack_reply": {"type", "channel_id", "thread_ts"},
    "switchboard_reply": {"type", "plan_id", "thread_id"},
    "switchboard_approve_phase": {"type", "plan_id", "phase"},
}
PROMPT_MAX = 6000


def prompt_rules() -> str:
    """When and how a lead drafts an agent prompt. Shared by the surface leads and the workstream leads."""
    me = config.ME_NAME
    dirs = ", ".join(f'"{k}"' for k in config.SESSION_WORKDIRS)
    # The default reads as the catch-all ("everything else"), so it goes last.
    order = sorted(config.WORKDIR_USE, key=lambda k: k == config.DEFAULT_WORKDIR)
    uses = "; ".join(f'"{k}" for {config.WORKDIR_USE[k] or "anything"}' for k in order)
    return f"""Agent prompts: {me} hands open-ended work to a Claude Code agent, in a new session started from the
item. For each item, decide whether a prompt is worth drafting:
- Draft one when the work is open-ended: it needs investigation (why is X happening, does Y hold across
  companies), building or changing code, analysing data, reviewing a design or PR in depth, or writing a doc,
  plan or spec. Answering a question counts when the answer needs that kind of digging.
- Leave "prompt" null when the work is closed: a choice only {me} can make, an approval, an fyi, a fact you
  already found and put in the body, an answer {me} can type from what is in the body, or a chore {me} does by
  hand (send a file, book a meeting).
- Write the prompt to the agent in {me}'s voice ("I need...", "Help me..."). It must stand alone: the agent has
  none of your context. Give the goal and why it matters now; the links (Slack permalink, Switchboard plan URL
  and id, PR, ticket, meeting note) and the key facts and IDs you found, so it doesn't redo your research; the
  open questions; and what done looks like (a finding with evidence, a PR, a draft doc). If the outcome is a
  message or comment for someone, ask the agent to draft it for {me} to review and post: the agent never posts
  or sends anything itself. Under about 300 words, plain text.
- "workdir": where the session starts, one of {dirs}. Use {uses}."""


def clean_prompt(value) -> str | None:
    text = str(value or "").strip()
    return text[:PROMPT_MAX] or None


def clean_workdir(value, prompt: str | None) -> str | None:
    if not prompt:
        return None
    return value if value in config.SESSION_WORKDIRS else config.DEFAULT_WORKDIR


def framing_rules() -> str:
    """The fields the item pane leads with. Shared by the surface leads and the workstream leads."""
    me = config.ME_NAME
    return f"""What {me} sees first on an item (the pane leads with these, so make them exact):
- "ask": the request in one line, at most 90 characters, phrased as the question when it is one ("Does the
  export support CSV?"), else as what to do ("Send Priya the onboarding page outline"). Not
  "<who> in <where>: ...": who and where are shown separately.
- "requester": the person asking or waiting on {me}, by name ("Priya Shah"), or null.
- "reference": one external reference worth showing beside the requester ("Zendesk 123456", "IP-1234",
  "PR #1234", "INC-123"), or null.
- "timeline": what happened in the conversation that bears on what is pending, oldest first, 1 to 5 rows:
  {{"at": "ISO 8601 time with its offset, of the row's first event", "until": "ISO time of its last event, only
  when the row groups several", "text": "one line, at most 90 characters"}}. Group consecutive events of the same
  kind into one row ("Sent 4 screenshots and started 2 huddles"). The requester is the implied subject ("Asked for
  a quick take", "Nudged: ..."); name anyone else. Quote a short nudge or question verbatim when it carries the
  tone. Facts only, no recommendation. Take times from the source (Slack ts, comment and email dates)."""


def done_rules() -> str:
    """When a lead may say an open item is probably finished. Shared by the surface leads and the workstream leads."""
    me = config.ME_NAME
    return f"""Closing out items: an open item can be finished without {me} clicking anything in this queue. It is
"likely_done" when the source shows the ask was met: {me} answered or acted in the thread, someone else answered
or did it, the PR merged or closed or {me} already submitted the review, the plan moved past the phase that
needed {me}, the ticket or issue closed, the meeting follow-up shows up as done elsewhere, or the asker said it
isn't needed any more. A Claude Code session {me} ran on it counts when its digest shows the work finished (a
PR opened, a doc written), not just started.
- Mark it only on concrete evidence in the source that is newer than the last ask. Silence, time passing or a
  promise to do it later are not evidence. When unsure, keep it open: a wrong close hides work from {me}.
- "evidence": one or two sentences: who did what, when, and where ("{config.ME_FIRST} replied in the thread at
  14:02 with the CSV; Priya thanked them"). {me} reads this to confirm the close.
- An item {me} reopened (reopened_at set) stays open unless something happened after reopened_at.
- An item already likely_done goes back to "open" when something new needs {me} (a follow-up question, the
  PR got new review comments, the fix didn't work)."""


def status_changes(conn: sqlite3.Connection, changes, allowed: set[int]) -> tuple[list[int], list[int], list[str]]:
    """Apply a lead's likely_done / open verdicts to its own open items. Returns (closed, reopened, problems)."""
    closed, reopened, problems = [], [], []
    for c in changes or []:
        item_id, state = (c or {}).get("item_id"), (c or {}).get("state")
        if item_id not in allowed:
            problems.append(f"status change for #{item_id}: not one of this lead's open items")
            continue
        if state == "likely_done":
            evidence = str(c.get("evidence") or "").strip()
            if not evidence:
                problems.append(f"#{item_id}: likely_done needs evidence")
            elif store.mark_likely_done(conn, item_id, evidence):
                closed.append(item_id)
        elif state == "open":
            if store.lead_reopen(conn, item_id):
                reopened.append(item_id)
        else:
            problems.append(f"#{item_id}: bad state {state!r}")
    return closed, reopened, problems


def _clean_time(value) -> str | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat() if value else None
    except ValueError:
        return None


def framing(out: dict) -> dict:
    """The pane's lead fields from a lead's item or revision, validated. Missing ones are None, so a revision keeps
    the stored value."""
    rows = []
    for r in (out.get("timeline") or [])[:6] if isinstance(out.get("timeline"), list) else []:
        text = str((r or {}).get("text") or "").strip() if isinstance(r, dict) else ""
        if text:
            rows.append({"at": _clean_time(r.get("at")), "until": _clean_time(r.get("until")), "text": text[:200]})
    short = lambda k, n: (str(out.get(k)).strip()[:n] or None) if out.get(k) else None
    return {"ask": short("ask", 160), "requester": short("requester", 80), "reference": short("reference", 80),
            "timeline": json.dumps(rows, ensure_ascii=False) if rows else None}


def charter(name: str) -> str:
    path = paths.ROOT / "leads" / f"{name}.md"
    if not path.exists():
        raise ValueError(f"no charter for lead {name!r} at {path}")
    text = path.read_text()
    # Charters are shared; the personal parts come from me.toml. Plain replacement, so other braces are left alone.
    for key, value in charter_vars().items():
        text = text.replace("{" + key + "}", value)
    return text


def charter_vars() -> dict[str, str]:
    return {"me": config.ME_FIRST, "email": config.ME_EMAIL, "about": config.ME_ABOUT,
            "notion_user_id": config.NOTION_USER_ID, "github_repo": config.GITHUB_REPO or "(none set)",
            "default_workdir": config.DEFAULT_WORKDIR}


def memory_block(conn: sqlite3.Connection, *agents: str) -> str:
    parts = []
    brain = paths.home() / "brain.md"
    if brain.exists():
        parts.append("Preferences distilled from past approvals and edits:\n" + brain.read_text())
    marks = ",".join("?" * len(agents))
    rules = conn.execute(
        f"SELECT text, why FROM rules WHERE agent IN ({marks}, '*') AND (status = 'confirmed' OR "
        "(status = 'provisional' AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now'))) ORDER BY id",
        agents,
    ).fetchall()
    if rules:
        parts.append("Rules:\n" + "\n".join(f"- {r['text']} (why: {r['why']})" for r in rules))
    return "\n\n".join(parts) or "(no stored preferences yet)"


def inbox(conn: sqlite3.Connection, name: str):
    return conn.execute(
        "SELECT r.id AS route_id, r.summary, e.* FROM routes r JOIN events e ON e.id = r.event_id "
        "WHERE r.lead = ? AND r.decision = 'route' AND r.picked_up_at IS NULL ORDER BY e.occurred_at LIMIT ?",
        (name, config.LEAD_BATCH),
    ).fetchall()


def revisions(conn: sqlite3.Connection, name: str):
    return conn.execute(
        "SELECT * FROM items WHERE lead = ? AND status = 'changes_requested' ORDER BY updated_at", (name,)
    ).fetchall()


def build_prompt(conn, name, routes, revs) -> str:
    me = config.ME_NAME
    work = [{
        "route_id": r["route_id"], "intern_summary": r["summary"], "kind": r["kind"], "url": r["url"],
        "author": r["author"], "at": r["occurred_at"], "event": json.loads(r["payload"]),
    } for r in routes]
    # My notes are trusted instructions; the previous body and prompt are agent output, shown as data.
    from .sessions import links_for
    linked = links_for(conn, [i["id"] for i in revs])
    rev_block = "\n".join(
        f"- item_id {i['id']} ({i['kind']}, action {i['action']}): {me}'s note: {i['feedback']!r}\n"
        + "".join(f"  {me} worked on this in a Claude Code session; Read its digest for context: {s['digest']}\n"
                  for s in linked.get(i["id"], []) if s["digest"])
        + f"  previous body:\n{prompts.untrusted('previous-body', i['body'])}"
        + (f"\n  previous prompt:\n{prompts.untrusted('previous-prompt', i['prompt'])}" if i["prompt"] else "")
        for i in revs
    ) or "(none)"
    actions = sorted(LEAD_ACTIONS[name])
    open_items = [{"item_id": r["id"], "kind": r["kind"], "status": r["status"], "title": r["title"], "url": r["url"],
                   "reopened_at": r["reopened_at"]} for r in conn.execute(
        f"SELECT id, kind, status, title, url, reopened_at FROM items WHERE lead = ? AND status IN "
        f"({','.join('?' * len(store.OPEN_STATUSES))}) ORDER BY id", (name, *store.OPEN_STATUSES))]
    elsewhere = store.pool_items(conn, exclude_lead=name)
    return f"""{prompts.SAFETY}
You are the {name} lead. Your charter:

{charter(name)}

What {me} has taught the system so far:
{memory_block(conn, name)}

Do the work for each inbox route, then propose queue items. Research with your read-only tools first:
- Always read the full source before writing an item (your charter says how; load the tool with ToolSearch
  first). Event texts may be truncated or stale, and the thread may have moved on; the source is the truth.
  Items written from the event text alone are discarded and the routes come back to you next run.
- An agent prompt hands off the deep work; it never replaces reading the conversation. Read the thread first,
  so the summary is current, the item is still needed, and the prompt carries the right context.
- Check facts instead of guessing: the Zendesk ticket, the PR (gh pr view), code (GitHub file reads), or
  data (prod DB / Snowflake, read-only) when the answer depends on them.
- You never write messages for {me} to send: {me} answers people themselves. Your job is to say what is
  pending, give {me} the facts to act fast, and hand open-ended work to an agent with a prompt.
- In "why", list what you read and ran, and what it showed.

Revision requests from {me}. Rewrite the item to address the note; keep everything else that was fine.
When the note says {me} doesn't need the item ("not needed", "not mine", "I don't need to track this",
"already handled"), don't rewrite it: return {{"item_id": ..., "close": true, "why": "..."}} and it closes.
A note may ask for a prompt ("have an agent dig into this"), a different prompt, or an action you can take
("close this plan", "approve it"). switchboard_close_plan or switchboard_approve_phase → kind "decision" with
the action and a one-line body saying what will happen and why. If the note needs something you can't do,
say so in the body and offer the closest thing you can. An item of kind "reply" is from before leads stopped
drafting replies: revise it into a task or decision, with a prompt if the work is open-ended. {me} still
reviews and launches or approves everything; you never act:
{rev_block}

Item rules:
- kind "task": something {me} needs to do: answer someone, review something, follow up on a commitment.
  body: what to do first, then what {me} needs to do it: the answer you found and its evidence, the
  numbers, the links. Facts and findings, not a message to send. No action.
- kind "decision": body states the question, the options, and your recommendation. No action, except
  switchboard_approve_phase, which only a decision item may carry.
- kind "fyi": something {me} should know but need not answer. No action.
- Allowed action types for you: {actions or "none"}.{(" Formats:" + chr(10) + chr(10).join(f"  {ACTION_FORMATS[a]}" for a in actions)) if actions else ""}
- priority (lower is more urgent): 0 = urgent, someone is blocked on {me} today or it is an incident;
  1 = needs {me} (a question, decision or task); 2 = fyi. Every fyi item is 2.
- title: "<who> in <where>: <gist>" (a meeting task: "<meeting>: <what to do>"), at most 100 characters. It
  labels the item in lists; the pane shows "ask" instead.
- summary: 1 to 4 short lines, each starting with "- ": where the discussion stands now and exactly what is
  pending on {me}. Facts only, newest state first, no recommendation (that goes in body).
- url: the link that opens the conversation: the Slack permalink of the thread (or DM message), the
  Switchboard plan URL, the Granola note URL, or the GitHub PR URL.
- A route of kind "manual" is one {me} sent through the chief of staff to be tracked; its text is {me}'s note.
  Always queue it (never drop it as below the bar), read the conversation, and frame the item by the note. Only
  if it repeats one of the open items below, drop it and name that item.
- One item may cover several routes about the same conversation, and one route (a meeting) may give several
  task items. If a route needs nothing after all, put it in "dropped" with a reason. Every route must appear
  in at least one item or in dropped.
- Your open items are listed below, and so are open items filed by other leads and workstreams. Don't queue
  a duplicate of either; if a route only repeats one, drop it and name the item in the reason. One meeting or
  thread can still give several distinct items, so match on what needs doing, not on the link.
- When an inbox route shows that one of your open items (status pending) is now finished, or that one marked
  likely_done needs {me} again, say so in "status_changes" (rules below). Only for your own items.

{done_rules()}

{framing_rules()}

{prompt_rules()}

Output schema:
{{"items": [{{"route_ids": [1], "kind": "task" | "decision" | "fyi", "title": "...", "summary": "- ...", "body": "...",
   "ask": "...", "requester": "... or null", "reference": "... or null",
   "timeline": [{{"at": "2026-11-16T10:54:00-05:00", "until": null, "text": "..."}}],
   "priority": 1, "url": "https://...", "action": {{...}} | null, "prompt": "... or null", "workdir": "... or null",
   "why": "evidence and reasoning, brief"}}],
 "revisions": [{{"item_id": 7, "body": "...", "title": "... or null", "summary": "updated summary or null",
   "ask": "... or null to keep", "requester": "... or null to keep", "reference": "... or null to keep",
   "timeline": "the full new timeline, or null to keep",
   "kind": "null to keep, or task / decision", "action": "null to keep, or the new action",
   "prompt": "null to keep, \"\" to remove, or the new prompt", "workdir": "null to keep, or a workdir",
   "close": "true only to close the item because the note says it isn't needed", "why": "what changed"}}],
 "dropped": [{{"route_id": 2, "reason": "..."}}],
 "status_changes": [{{"item_id": 5, "state": "likely_done" | "open", "evidence": "..."}}],
 "rules": [{{"text": "an instruction to your future self", "why": "the note or evidence it comes from"}}]}}

"status_changes" is usually empty. "rules" is optional and usually empty: add one only when a note from {me} or
a source taught you something durable (at most 3). They start provisional and expire in 7 days unless confirmed.

Your open items:
{prompts.untrusted("open-items", open_items) if open_items else "(none)"}

Open items from other leads and workstreams (for duplicate checks only; you can't change them):
{prompts.untrusted("other-open-items", elsewhere) if elsewhere else "(none)"}

Inbox:
{prompts.untrusted(name, work)}
"""


# Leads whose surface only reaches me when my input is required; an fyi from them is below the bar.
NO_FYI_LEADS = {"slack", "notion", "email", "github"}

# Irreversible actions a lead may only propose as a revision answering my note, never on its own.
INSTRUCTION_ONLY_ACTIONS = {"switchboard_close_plan"}

SOURCE_HOSTS = {"slack": ".slack.com", "switchboard": "switchboard.checkhq.com", "granola": "notes.granola.ai",
                "notion": ".notion.com", "email": "mail.google.com", "github": "github.com"}


def pick_url(lead: str, proposed, scanned):
    """Prefer the lead's link (it may point at the thread parent), but only if it goes to the right place."""
    from urllib.parse import urlparse
    if lead == "notion":
        from .scan_notion import canonical_url
        return canonical_url(proposed) or canonical_url(scanned) or scanned
    host = SOURCE_HOSTS.get(lead)
    try:
        parsed = urlparse(str(proposed or ""))
    except ValueError:
        parsed = None
    if parsed and parsed.scheme == "https" and host and (parsed.hostname or "").endswith(host.lstrip(".")):
        return proposed
    return scanned


def due_for_review(conn: sqlite3.Connection, name: str, *, force: bool = False):
    """The lead's waiting items it hasn't re-read in a while, stalest first. Items I'm acting on (approved,
    failed, with a lead) are left alone."""
    gate = "" if force else (f"AND COALESCE(i.checked_at, i.created_at) <= "
                             f"strftime('%Y-%m-%dT%H:%M:%SZ','now','-{config.REVIEW_EVERY_H} hours') ")
    return conn.execute(
        "SELECT i.*, e.payload FROM items i LEFT JOIN routes r ON r.id = i.route_id "
        "LEFT JOIN events e ON e.id = r.event_id "
        f"WHERE i.lead = ? AND i.status IN ('pending','likely_done') {gate}"
        "ORDER BY COALESCE(i.checked_at, i.created_at), i.id LIMIT ?",
        (name, config.REVIEW_BATCH),
    ).fetchall()


def review_open(conn: sqlite3.Connection, name: str, *, force: bool = False, dry_run: bool = False) -> None:
    """Re-read the source of the lead's open items: mark the ones already finished likely_done (or reopen
    likely_done ones something new came in on), and rewrite what the pane leads with: summary, ask, requester,
    reference and timeline."""
    items = due_for_review(conn, name, force=force)
    if not items:
        print(f"review {name}: no items due (no model call)")
        return
    me = config.ME_NAME
    from .sessions import links_for
    linked = links_for(conn, [i["id"] for i in items])
    work = [{"item_id": i["id"], "status": i["status"], "kind": i["kind"], "title": i["title"], "ask": i["ask"],
             "url": i["url"], "created_at": i["created_at"], "summary": i["summary"], "body": (i["body"] or "")[:1500],
             "likely_done_because": i["done_reason"], "reopened_at": i["reopened_at"],
             "claude_sessions": [{"title": x["title"], "digest": x["digest"]} for x in linked.get(i["id"], [])
                                 if x["digest"]],
             "source_event": json.loads(i["payload"]) if i["payload"] else None} for i in items]
    prompt = f"""{prompts.SAFETY}
You are the {name} lead. These are open items you queued for {me} earlier. For each one, re-read its source now
(your charter says how; load the tool with ToolSearch first) and decide whether it is still open or likely done.
Read the whole conversation as it stands today, not the source event: that is what the item was filed from,
and the thread may have moved on. Where an item links Claude Code sessions, Read the digest when its title
suggests it covers the work.

{done_rules()}

Then rewrite what {me} sees first. summary: 1 to 4 short lines, each starting with "- ": where the discussion
stands now and exactly what is pending on {me} (for a likely_done item: what finished it). Facts only, newest
state first, no recommendation.

{framing_rules()}

Your charter:
{charter(name)}

Output schema: {{"items": [{{"item_id": 1, "state": "open" | "likely_done", "evidence": "required for likely_done",
  "summary": "- ...", "ask": "...", "requester": "... or null", "reference": "... or null",
  "timeline": [{{"at": "...", "until": null, "text": "..."}}]}}]}}
Every item below must appear once.

Items:
{prompts.untrusted(name, work)}
"""
    spec = pass_spec("lead", lead=name)
    if dry_run:
        print(preview(spec, prompt))
        return
    result = run_agent(conn, spec, prompt, job="lead", lead=f"review:{name}")
    if not result.ok:
        print(f"review {name} failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    ids = {i["id"] for i in items}
    out = [s for s in result.data.get("items") or [] if isinstance(s, dict) and s.get("item_id") in ids]
    refreshed = []
    for s in out:
        if s.get("summary"):
            f = framing(s)
            conn.execute("UPDATE items SET summary = ?, ask = COALESCE(?, ask), requester = COALESCE(?, requester), "
                         "reference = COALESCE(?, reference), timeline = COALESCE(?, timeline) WHERE id = ?",
                         (fix_notion_links(str(s["summary"])[:1200]), fix_notion_links(f["ask"]), f["requester"],
                          f["reference"], fix_notion_links(f["timeline"]), s["item_id"]))
            refreshed.append(s["item_id"])
    if result.turns < 2:
        # A verdict without re-reading the source is a guess; keep the items as they are.
        closed, reopened, problems = [], [], ["answered without calling any tool; status changes ignored"]
    else:
        closed, reopened, problems = status_changes(conn, out, ids)
    # Every item in the batch counts as checked, so one the lead skipped doesn't hold the next batch back.
    conn.execute(f"UPDATE items SET checked_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                 f"WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
    print(f"review {name}: {len(items)} items re-read, likely done {closed}, reopened {reopened}, "
          f"refreshed {len(refreshed)} (run {result.run_id}, ${result.cost_usd:.3f})")
    for p in problems:
        print(f"  rejected: {p}")


def check_action(name: str, action) -> str | None:
    if action is None:
        return None
    if not isinstance(action, dict) or action.get("type") not in LEAD_ACTIONS[name]:
        return f"action {action!r} not allowed for {name}"
    extra = set(action) - ACTION_FIELDS[action["type"]]
    if extra:
        return f"unexpected action fields {sorted(extra)}"
    if action["type"] == "slack_reply":
        if not CHANNEL.match(str(action.get("channel_id", ""))):
            return f"bad channel_id {action.get('channel_id')!r}"
        if action.get("thread_ts") is not None and not TS.match(str(action["thread_ts"])):
            return f"bad thread_ts {action.get('thread_ts')!r}"
    if action["type"].startswith("switchboard_"):
        if not PLAN_ID.match(str(action.get("plan_id", ""))):
            return f"bad plan_id {action.get('plan_id')!r}"
        if action["type"] == "switchboard_approve_phase" and action.get("phase") not in (
                "problem_definition", "technical_design"):
            return f"bad phase {action.get('phase')!r}"
        if action["type"] == "switchboard_reply" and action.get("thread_id") is not None \
                and not isinstance(action["thread_id"], str):
            return f"bad thread_id {action.get('thread_id')!r}"
    return None


def apply_revision(conn: sqlite3.Connection, lead: str, item, rv: dict, check=None) -> str | None:
    """Apply one revision from a lead's output. Returns what was wrong with it, or None."""
    if rv.get("close") is True:
        # My note said I don't need it. Closing it is what I asked for, and it reads to the brain as a
        # dismissal with my note, the same as clicking Not needed.
        try:
            store.transition(conn, item["id"], "dismissed")
        except store.InvalidTransition as e:
            return str(e)
        return None
    if not rv.get("body"):
        return "a revision needs a body unless it closes the item"
    err = check_action(lead, rv.get("action")) or (check(rv.get("action")) if check else None)
    if err:
        return err
    kind = rv.get("kind")
    if kind not in (None, "task", "decision", "fyi"):
        return f"bad kind {kind!r}"
    if kind is None and item["kind"] == "reply":
        kind = "task"  # an old reply item; leads no longer keep drafts
    prompt = rv.get("prompt")
    prompt = None if prompt is None else (clean_prompt(prompt) or "")
    workdir = rv.get("workdir") if rv.get("workdir") in config.SESSION_WORKDIRS else None
    if prompt and not workdir and not item["workdir"]:
        workdir = config.DEFAULT_WORKDIR
    try:
        store.revise_item(conn, item["id"], body=rv["body"], title=rv.get("title"), rationale=rv.get("why"),
                          summary=(str(rv["summary"])[:1200] if rv.get("summary") else None),
                          kind=kind, action=rv.get("action"), prompt=prompt, workdir=workdir, **framing(rv))
    except (store.InvalidTransition, ValueError) as e:
        return str(e)
    return None


def run(conn: sqlite3.Connection, name: str, *, dry_run: bool = False) -> None:
    if name not in LEADS:
        raise ValueError(f"unknown lead {name!r}")
    routes, revs = inbox(conn, name), revisions(conn, name)
    if not routes and not revs:
        print(f"lead {name}: inbox empty, no revisions (no model call)")
        return
    spec = pass_spec("lead", lead=name)
    prompt = build_prompt(conn, name, routes, revs)
    if dry_run:
        print(preview(spec, prompt))
        return

    result = run_agent(conn, spec, prompt, job="lead", lead=name)
    if not result.ok:
        print(f"lead {name} failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    # Writing items without reading the source is the failure this guards against; dropping every route
    # from the event alone (e.g. a bare "thanks") is fine without a tool call. Revisions answer my notes and
    # don't depend on the source, so they still apply; only the new items and drops are thrown away.
    if routes and result.turns < 2 and result.data.get("items"):
        err = "lead answered without calling any tool; new items discarded, routes left in the inbox"
        conn.execute("UPDATE runs SET status = 'error', error = ? WHERE id = ?", (err, result.run_id))
        print(f"lead {name}: {err} (run {result.run_id}, ${result.cost_usd:.3f})")
        result.data["items"], result.data["dropped"], result.data["rules"] = [], [], []
        result.data["status_changes"] = []

    route_ids = {r["route_id"]: r for r in routes}
    covered: dict[int, str] = {}
    created, problems = [], []
    for it in result.data.get("items") or []:
        # A route may back several items (a meeting with several action items), but never one already dropped.
        rids = [r for r in it.get("route_ids") or [] if r in route_ids
                and not covered.get(r, "").startswith("dropped")]
        if name in NO_FYI_LEADS and it.get("kind") == "fyi":
            for r in rids:
                covered.setdefault(r, f"dropped: fyi only, no input needed from {config.ME_NAME} ({it.get('title')})")
            continue
        err = check_action(name, it.get("action"))
        if not err and (it.get("action") or {}).get("type") in INSTRUCTION_ONLY_ACTIONS:
            err = f"{it['action']['type']} may only be proposed in answer to an instruction from {config.ME_NAME}"
        if err or not rids:
            problems.append(err or f"item {it.get('title')!r} has no valid route_ids")
            continue
        prompt = clean_prompt(it.get("prompt"))
        try:
            item_id = store.add_item(
                conn, lead=name, kind=it.get("kind"), title=str(it.get("title") or "")[:200],
                body=str(it.get("body") or ""), action=it.get("action"),
                prompt=prompt, workdir=clean_workdir(it.get("workdir"), prompt), **framing(it),
                priority=it.get("priority", 1), url=pick_url(name, it.get("url"), route_ids[rids[0]]["url"]),
                route_id=rids[0], run_id=result.run_id, rationale=it.get("why"),
                summary=str(it.get("summary") or "")[:1200] or route_ids[rids[0]]["summary"],
            )
        except (ValueError, sqlite3.IntegrityError) as e:
            problems.append(f"item {it.get('title')!r}: {e}")
            continue
        created.append(item_id)
        for r in rids:
            covered[r] = f"{covered[r]}, #{item_id}" if r in covered else f"item #{item_id}"
    for d in result.data.get("dropped") or []:
        if d.get("route_id") in route_ids and d["route_id"] not in covered:
            covered[d["route_id"]] = f"dropped: {d.get('reason')}"
    for rid, outcome in covered.items():
        conn.execute(
            "UPDATE routes SET picked_up_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'), picked_up_run_id = ?, "
            "lead_outcome = ? WHERE id = ?",
            (result.run_id, outcome, rid),
        )

    revised = 0
    revs_by_id = {i["id"]: i for i in revs}
    for rv in result.data.get("revisions") or []:
        if rv.get("item_id") in revs_by_id:
            err = apply_revision(conn, name, revs_by_id[rv["item_id"]], rv)
            if err:
                problems.append(f"revision of #{rv['item_id']}: {err}")
            else:
                revised += 1

    own = {r[0] for r in conn.execute("SELECT id FROM items WHERE lead = ? AND status IN ('pending','likely_done')",
                                      (name,))} - set(created)
    closed, reopened, bad = status_changes(conn, result.data.get("status_changes"), own)
    problems += bad

    from .memory import lead_rules
    new_rules = lead_rules(conn, name, result.data.get("rules") or [], result.run_id)

    left = len(route_ids) - len(covered)
    print(f"lead {name}: {len(created)} items queued {created}, {revised} revised, "
          f"{sum(v.startswith('dropped') for v in covered.values())} dropped"
          + (f", {left} routes left for next run" if left else "")
          + (f", likely done {closed}" if closed else "") + (f", reopened {reopened}" if reopened else "")
          + (f", provisional rules {new_rules}" if new_rules else "")
          + f" (run {result.run_id}, ${result.cost_usd:.3f})")
    for p in problems:
        print(f"  rejected: {p}")
