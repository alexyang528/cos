"""Switchboard scan via a read-only Haiku agent using the Switchboard connector.

Most scans list only the plans involving me (list_plans involving=me: problem owner, solution lead, or an open
thread). Every SWITCHBOARD_SWEEP_H a scan lists every open plan instead, to find plans where I'm only the approver or
own a lane; ownership learned by get_plan is cached, and a plan known not to be mine is skipped until its phase
changes. A plan of mine waiting on a human is fetched again only when its turn or phase changed, or when it was last
read more than SWITCHBOARD_RECHECK_H ago (that catches a new revision whose turn went to Switchboard and back between
scans, and new thread messages). Each new head revision becomes one event.
"""

import json
import re
import sqlite3
import time

from . import config, paths, prompts, store
from .policy import pass_spec
from .runner import preview, run_agent

CACHE_KEY = "scan.switchboard.plans"
SWEEP_KEY = "scan.switchboard.swept_at"
PLAN_ID = re.compile(r"^pl_[0-9a-f]{32}$")
REV_ID = re.compile(r"^rev_[0-9a-f]{32}$")


def load_cache(conn) -> dict[str, dict]:
    raw = store.kv_get(conn, CACHE_KEY)
    return json.loads(raw) if raw else {}


def skip_list(cache: dict[str, dict], now: float) -> dict[str, str]:
    """Plans not to fetch while their phase is unchanged: ones known not to be mine, and mine read recently."""
    fresh = now - config.SWITCHBOARD_RECHECK_H * 3600
    return {pid: c["phase"] for pid, c in cache.items()
            if c.get("mine") is False or (c.get("mine") and c.get("checked", 0) > fresh)}


def build_prompt(cache: dict[str, dict], *, sweep: bool, now: float | None = None) -> str:
    me = config.ME_EMAIL
    skip = skip_list(cache, now or time.time())
    listing = ("Call mcp__claude_ai_Switchboard__list_plans once, with no arguments (every open plan)." if sweep else
               'Call mcp__claude_ai_Switchboard__list_plans once, with involving set to "me".')
    return f"""{prompts.SAFETY}
Task: find Switchboard plans that are waiting on {config.ME_NAME} ({me}). Collect facts only.

1. {listing}
2. For every plan whose turn is "human", call mcp__claude_ai_Switchboard__get_plan, EXCEPT plans listed in
   SKIP below whose phase still equals the phase shown there. A SKIP entry is either a plan known not to
   involve {me} or one of {me}'s plans that was read recently and hasn't changed phase; either way, skipping
   it is expected and not worth a note.
3. Never fetch plans whose turn is "switchboard".

SKIP (plan_id -> phase when last read):
{json.dumps(skip)}

For each plan you fetched, report plan_id, title, url, phase, turn, head_revision_id, problem_owner_email,
solution_lead_email, approver_email, can_approve (as returned), and my_lanes: lanes whose owner or assignee is
{me} (lane id, title, status), or [] if none.
Only when the plan involves {me} (one of those three emails is {me}, or my_lanes isn't empty), also report:
- open_threads: each open thread's id and its latest message text, verbatim (cut at 600 chars)
- unanswered_agent_questions: titles of "### " questions under "## Agent Questions" whose "**Answer:**"
  line is empty
- switchboard_message: switchboard_changes.message, verbatim (cut at 1500 chars), or null
For a plan that doesn't involve {me}, leave those three out.

Output schema:
{{"human_turn_plan_ids": ["every plan_id from list_plans whose turn is human"],
 "plans": [{{"plan_id": "pl_...", "title": "...", "url": "https://switchboard.checkhq.com/plans/pl_...",
   "phase": "...", "turn": "human", "head_revision_id": "rev_...", "problem_owner_email": "...",
   "solution_lead_email": "...", "approver_email": "...", "can_approve": false, "my_lanes": [],
   "open_threads": [{{"id": "...", "latest": "..."}}], "unanswered_agent_questions": ["..."],
   "switchboard_message": "..."}}],
 "notes": "anything that failed"}}
"""


def involves_me(p: dict) -> bool:
    me = config.ME_EMAIL
    return me in (p.get("problem_owner_email"), p.get("solution_lead_email"), p.get("approver_email")) \
        or bool(p.get("my_lanes"))


def ingest(conn: sqlite3.Connection, data: dict, cache: dict[str, dict], *, sweep: bool = True,
           now: float | None = None) -> dict[str, int]:
    counts = {"new": 0, "duplicate": 0, "not_mine": 0, "rejected": 0}
    for p in data.get("plans") or []:
        if not isinstance(p, dict) or not PLAN_ID.match(str(p.get("plan_id", ""))) \
                or not REV_ID.match(str(p.get("head_revision_id", ""))):
            counts["rejected"] += 1
            continue
        mine = involves_me(p)
        cache[p["plan_id"]] = {"phase": p.get("phase"), "mine": mine, "rev": p["head_revision_id"],
                               "checked": now or time.time()}
        if not mine or p.get("turn") != "human":
            counts["not_mine"] += 1
            continue
        url = p.get("url") if str(p.get("url", "")).startswith("https://switchboard.checkhq.com/") else None
        roles = [r for r, k in (("problem owner", "problem_owner_email"), ("solution lead", "solution_lead_email"),
                                ("approver", "approver_email")) if p.get(k) == config.ME_EMAIL]
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(source, source_id, kind, url, author, text, occurred_at, payload) "
            "VALUES ('switchboard', ?, 'plan_turn', ?, 'Switchboard', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?)",
            (f"{p['plan_id']}:{p['head_revision_id']}", url,
             f"{p.get('title')} ({p.get('phase')}); you are {', '.join(roles) or 'a lane owner'}",
             json.dumps(p, ensure_ascii=False)),
        )
        counts["new" if cur.rowcount else "duplicate"] += 1
    # Plans that left the human turn no longer need a cache entry; ownership is relearned if they return, and a
    # plan of mine coming back from Switchboard's turn is read again. A scan listing only plans involving me says
    # nothing about the rest, so it prunes only plans of mine.
    still_human = set(data.get("human_turn_plan_ids") or [])
    if still_human or not sweep:
        for pid in list(cache):
            if pid not in still_human and (sweep or cache[pid].get("mine")):
                del cache[pid]
    return counts


def sweep_due(conn: sqlite3.Connection, now: float) -> bool:
    last = store.kv_get(conn, SWEEP_KEY)
    return not last or now - float(last) >= config.SWITCHBOARD_SWEEP_H * 3600


def run(conn: sqlite3.Connection, *, dry_run: bool = False, sweep: bool | None = None) -> None:
    cache = load_cache(conn)
    now = time.time()
    sweep = sweep_due(conn, now) if sweep is None else sweep
    spec = pass_spec("scan", lead="switchboard")
    prompt = build_prompt(cache, sweep=sweep, now=now)
    if dry_run:
        print(preview(spec, prompt))
        return
    result = run_agent(conn, spec, prompt, job="scan", lead="switchboard")
    if not result.ok:
        print(f"scan switchboard failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error}")
        return
    snap = paths.snapshots_dir() / f"switchboard-{int(time.time())}.json"
    snap.write_text(json.dumps(result.data, ensure_ascii=False, indent=1))
    counts = ingest(conn, result.data, cache, sweep=sweep, now=now)
    store.kv_set(conn, CACHE_KEY, json.dumps(cache))
    if sweep:
        store.kv_set(conn, SWEEP_KEY, str(now))
    print(f"scan switchboard ({'every open plan' if sweep else 'plans involving you'}): {counts['new']} new, "
          f"{counts['duplicate']} unchanged, {counts['not_mine']} not yours, {counts['rejected']} rejected "
          f"(run {result.run_id}, ${result.cost_usd:.3f}, snapshot {snap.name})")
    if result.data.get("notes"):
        print(f"  notes: {result.data['notes']}")
