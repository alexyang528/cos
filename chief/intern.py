"""Triage: decide route vs skip for each new event. A routed event goes to the workstream watching its source,
else to a workstream the intern judges it belongs to, else to its surface's lead."""

import json
import re
import sqlite3

from . import config, prompts
from . import workstreams as ws
from .lead import memory_block
from .leads import SOURCE_TO_LEAD
from .policy import pass_spec
from .runner import preview, run_agent


def untriaged(conn: sqlite3.Connection, limit: int):
    return conn.execute(
        "SELECT e.* FROM events e LEFT JOIN routes r ON r.event_id = e.id "
        "WHERE r.id IS NULL ORDER BY e.occurred_at, e.id LIMIT ?",
        (limit,),
    ).fetchall()


def _addressed_to_bot(p: dict, text: str) -> bool:
    """My last message in the thread @-mentioned a bot, and this reply doesn't name me: it's talking to the bot."""
    last = str(p.get("my_last_message") or "").lstrip()
    if not any(last.startswith(f"<@{bot}") for bot in config.SLACK_BOT_IDS):
        return False
    first = config.ME_NAME.split()[0]
    return f"<@{config.SLACK_USER_ID}" not in text and not re.search(rf"\b{re.escape(first)}\b", text, re.I)


def pre_skip_reason(event) -> str | None:
    """Deterministic skips that don't need a model."""
    p = json.loads(event["payload"])
    if event["source"] == "slack" and p.get("i_replied_after") is True:
        return "you already replied after this"
    if event["source"] == "slack" and event["kind"] == "thread_reply" and _addressed_to_bot(p, event["text"] or ""):
        return "reply to the bot you invoked in this thread"
    if event["source"] == "email" and event["kind"] == "zendesk" and p.get("comment_author") == config.ME_NAME:
        return "your own ticket comment"
    if event["source"] == "notion" and event["kind"] == "ticket" and p.get("changed") == ["dri"]:
        return "only the ticket's DRIs changed"
    return None


def pre_route_summary(event) -> str | None:
    """Deterministic routes: a Switchboard plan of mine waiting on a human always needs me, and so does a PR
    that asks for my review or a review of my PR I haven't answered. Every meeting I attended goes to the granola
    lead, which drops it if nothing in it is mine to do, and so does a change to an open Inbound Platform ticket
    I'm the DRI of (DRI-only edits are skipped first)."""
    p = json.loads(event["payload"])
    if event["source"] == "github":
        return event["text"][:200]
    if event["source"] == "notion" and event["kind"] == "ticket":
        changed = p.get("changed")
        what = ("new to you" if changed is None else f"{', '.join(changed)} changed" if changed
                else "edited (body or comments)")
        return f"{p.get('channel_name')}: {p.get('name')} {what} ({p.get('status')}); you are the DRI"[:200]
    if event["source"] == "switchboard":
        return f"Switchboard: '{p.get('title')}' is waiting on you ({p.get('phase')})"[:200]
    if event["source"] == "granola":
        return f"Meeting: '{p.get('title')}' ({(p.get('meeting_at') or '')[:10]}); find your action items"[:200]
    return None


def route_target(conn: sqlite3.Connection, event, judged_ws: int | None = None) -> tuple[str, int | None, str | None]:
    """(lead, workstream_id, filed_by) for a routed event. A watched source always wins over judgment."""
    owner = ws.owner_for_event(conn, event)
    if owner:
        return "workstream", owner, "source"
    if judged_ws:
        return "workstream", judged_ws, "intern"
    return SOURCE_TO_LEAD[event["source"]], None, None


def record(conn: sqlite3.Connection, event, decision: str, *, summary=None, reason=None, run_id=None,
           judged_ws: int | None = None) -> bool:
    lead = ws_id = filed_by = None
    if decision == "route":
        lead, ws_id, filed_by = route_target(conn, event, judged_ws)
    cur = conn.execute(
        "INSERT OR IGNORE INTO routes(event_id, decision, lead, summary, url, reason, run_id, workstream_id, filed_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event["id"], decision, lead, summary, event["url"], reason, run_id, ws_id, filed_by),
    )
    return cur.rowcount == 1


def workstream_catalog(conn: sqlite3.Connection) -> list[dict]:
    return [{"slug": w["slug"], "name": w["name"], "about": (w["description"] or "")[:300]} for w in ws.active(conn)]


def build_prompt(events, catalog: list[dict] | None = None, memory: str = "(no stored preferences yet)") -> str:
    batch = []
    for e in events:
        p = json.loads(e["payload"])
        batch.append({
            "event_id": e["id"], "source": e["source"], "kind": e["kind"], "where": p.get("channel_name"),
            "author": e["author"], "at": e["occurred_at"], "text": e["text"],
            "my_last_message_before_it": p.get("my_last_message"),
            "i_replied_after": p.get("i_replied_after"),
            **({"thread_id": p.get("thread_id")} if e["source"] == "email" else {}),
        })
    me = config.ME_NAME
    return f"""{prompts.SAFETY}
You are the intern. For each event below decide whether it needs {me}: "route" or "skip". You never
research or draft anything. Use your tools only if an event is unintelligible without its thread.

The bar: route only when input from {me} is still required. Ask: if {me} never responds, will the sender
(or someone in the thread) follow up and ask {me} again? Route only if yes.

Route when:
- someone asks {me} a question, asks {me} for something, or requests {me}'s review, approval or decision,
  and is waiting on the answer;
- someone is blocked until {me} acts;
- it replies to something {me} asked or proposed AND asks something back or needs {me}'s decision to move on.
- you can't tell from the message alone whether {me} is expected to answer: route it, and the lead will
  read the thread and apply the same bar.

Skip when:
- it answers {me}'s own question and asks nothing back ("yes", "+1", "sounds good", "Supportive!", an
  emoji): {me} has what they asked for and nobody is waiting on {me};
- it informs, reports status, or flags a problem without asking {me} anything, including an incident or
  deadline in {me}'s area that someone else is handling;
- {me} is only cc'd or tagged for visibility ("cc @{me}", "looping in", "FYI");
- it is addressed to a group or someone else, and nothing shows it needs {me} in particular;
- it is an acknowledgment ("thanks!", "got it"), chatter, an automated notification, or a broadcast.

Who wrote what: no event below was written by {me}; their own messages are removed before you see them, so an
event's author is never {me}, whatever names appear. A "dm" or "group_dm" event comes from a conversation {me} is a
member of, even if its name doesn't list {me}. In a group DM, a message that names another member ("Sam, let
me know thoughts") is addressed to that member, not to {me}.

Bots: when {me}'s last message in a thread invoked a bot or app (an @-mention of Claude or another app), replies
that follow are usually to the bot. Skip them unless they name {me} or ask {me} something directly.

An "email" event is the newest message of an inbox thread sent to {me} by a person (automated mail and
calendar traffic are already filtered out); the text is the subject and a snippet. Skip cold sales, vendor or
recruiting outreach, newsletters, and threads where {me} is only cc'd for visibility. Route when the sender asks
{me} something or is waiting on {me}. Read the thread (get_thread) only if the snippet is unintelligible.

A "zendesk" event is a partner support desk's mail about a ticket {me} filed or is on. The snippet is mostly
boilerplate, so read the thread (get_thread with its thread_id) before deciding. Route when the partner's newest
comment asks {me} a question, needs {me}'s answer or decision, or nudges {me} to reply ("friendly reminder to get
back to us"). Skip receipts ("thanks for contacting"), "ticket solved" notices, and updates that only report what
the partner is doing or will follow up on.

A Notion "mention" is a page whose text names {me}, found by search: the text is a snippet around the name.
Route it only when the snippet asks {me} for input, a decision or a review, or says work is waiting on {me}
("waiting on {config.ME_FIRST}", "{config.ME_FIRST} to decide", "need {config.ME_FIRST}'s call"). Skip it when {me} is only listed (attendees, owner
fields, credits), already did the thing, or is named in a status update.

Every event must get exactly one decision. summary: one line, at most 140 characters, saying who wants
what ("Jane (#payments) asks whether the retry fix can ship Friday"). reason: a few words.

{me}'s preferences and rules, learned from their past decisions. Where one clearly applies to an event, it
overrides the defaults above; name it in the reason.
{memory}
{_workstream_rules(catalog)}
Output schema:
{{"decisions": [{{"event_id": 123, "decision": "route" | "skip", "summary": "...", "reason": "...",
   "workstream": "slug or null"}}]}}

Events:
{prompts.untrusted("events", batch)}
"""


def _workstream_rules(catalog: list[dict] | None) -> str:
    if not catalog:
        return '\nThere are no workstreams; always set "workstream" to null.\n'
    me = config.ME_NAME
    return f"""
{me}'s workstreams (long-running projects, each with its own lead) are listed below. For a routed event
that is clearly about one of them (it names the project, its people are discussing its work), set
"workstream" to its slug; that workstream's lead will handle it instead of the queue. If you are unsure, or
it could be about several, use null. Skipped events always get null.
{prompts.untrusted("workstreams", catalog)}
"""


MAX_BATCHES_PER_RUN = 5


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    # Keep going until everything is triaged, so a big scan doesn't strand events until the next tick.
    for _ in range(1 if dry_run else MAX_BATCHES_PER_RUN):
        if not untriaged(conn, 1):
            return
        before = len(untriaged(conn, config.INTERN_BATCH))
        _run_batch(conn, dry_run=dry_run)
        if len(untriaged(conn, config.INTERN_BATCH)) >= before:
            return  # no progress (e.g. the model failed); leave the rest for the next tick


def _run_batch(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    events = untriaged(conn, config.INTERN_BATCH)
    pre, auto, rest = [], [], []
    for e in events:
        if pre_skip_reason(e):
            pre.append(e)
        elif pre_route_summary(e):
            auto.append(e)
        else:
            rest.append(e)
    if dry_run:
        for e in pre:
            print(f"[dry-run] would skip event {e['id']} without a model: {pre_skip_reason(e)}")
        for e in auto:
            print(f"[dry-run] would route event {e['id']} without a model: {pre_route_summary(e)}")
        if rest:
            print(preview(pass_spec("intern"), build_prompt(rest, workstream_catalog(conn), memory_block(conn, "intern"))))
        else:
            print("[dry-run] nothing for the model to triage")
        return

    for e in pre:
        record(conn, e, "skip", reason=pre_skip_reason(e))
    for e in auto:
        record(conn, e, "route", summary=pre_route_summary(e),
               reason={"granola": "rule: meeting you attended", "notion": "rule: ticket you are DRI of",
                       "github": "rule: PR waiting on you"}.get(
                   e["source"], "rule: plan waiting on you"))
    if not rest:
        print(f"intern: {len(auto)} auto-routed, {len(pre)} auto-skipped, nothing else to triage (no model call)")
        return

    catalog = workstream_catalog(conn)
    result = run_agent(conn, pass_spec("intern"), build_prompt(rest, catalog, memory_block(conn, "intern")),
                       job="intern")
    if not result.ok:
        print(f"intern failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    by_id = {e["id"]: e for e in rest}
    slugs = {w["slug"]: w["id"] for w in ws.active(conn)}
    routed = skipped = 0
    seen = set()
    for d in result.data.get("decisions") or []:
        eid = d.get("event_id")
        if eid not in by_id or eid in seen or d.get("decision") not in ("route", "skip"):
            continue
        seen.add(eid)
        summary = str(d.get("summary") or "")[:200] or None
        judged = slugs.get(d.get("workstream")) if d["decision"] == "route" else None
        if record(conn, by_id[eid], d["decision"], summary=summary, reason=d.get("reason"), run_id=result.run_id,
                  judged_ws=judged):
            routed += d["decision"] == "route"
            skipped += d["decision"] == "skip"
    missing = len(rest) - len(seen)
    print(f"intern: {routed} routed, {skipped + len(pre)} skipped ({len(pre)} without a model)"
          + (f", {missing} left for next run" if missing else "")
          + f" (run {result.run_id}, ${result.cost_usd:.3f})")
