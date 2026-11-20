"""Rules (structured, reviewable) and the brain (preferences distilled from my queue decisions).

Rules from my own decisions start confirmed. Rules an agent or the chief writes for itself are provisional
and expire after 7 days unless I or the chief confirm them. Rules never live in agents' free-text notes.
"""

import json
import sqlite3
from datetime import datetime, timezone

from . import config, paths, prompts, store
from .leads import QUEUE_OWNERS
from .policy import pass_spec
from .runner import preview, run_agent

NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
PROVISIONAL_DAYS = 7
RULE_AGENTS = QUEUE_OWNERS | {"intern", "*"}
BRAIN_CURSOR = "brain.since"


def add_rule(conn: sqlite3.Connection, *, agent: str, text: str, why: str, source: str,
             source_ref: str | None = None) -> int:
    if agent not in RULE_AGENTS and not agent.startswith("ws:"):
        raise ValueError(f"unknown rule agent {agent!r}")
    text, why = text.strip(), why.strip()
    if not text or not why:
        raise ValueError("a rule needs text and why")
    if source == "user":
        cur = conn.execute(
            f"INSERT INTO rules(agent, text, why, source, source_ref, status, confirmed_at, confirmed_by) "
            f"VALUES (?, ?, ?, 'user', ?, 'confirmed', {NOW}, 'user')",
            (agent, text, why, source_ref),
        )
    else:
        cur = conn.execute(
            f"INSERT INTO rules(agent, text, why, source, source_ref, status, expires_at) "
            f"VALUES (?, ?, ?, ?, ?, 'provisional', strftime('%Y-%m-%dT%H:%M:%SZ','now','+{PROVISIONAL_DAYS} days'))",
            (agent, text, why, source, source_ref),
        )
    return cur.lastrowid


def confirm_rule(conn: sqlite3.Connection, rule_id: int, *, by: str) -> None:
    cur = conn.execute(
        f"UPDATE rules SET status = 'confirmed', confirmed_at = {NOW}, confirmed_by = ?, expires_at = NULL "
        f"WHERE id = ? AND status = 'provisional' AND expires_at > {NOW}",
        (by, rule_id),
    )
    if cur.rowcount != 1:
        raise ValueError(f"rule {rule_id} is not an active provisional rule")


def retire_rule(conn: sqlite3.Connection, rule_id: int, *, by: str) -> None:
    cur = conn.execute(
        "UPDATE rules SET status = 'retired', confirmed_by = ? WHERE id = ? AND status IN ('provisional','confirmed')",
        (by, rule_id),
    )
    if cur.rowcount != 1:
        raise ValueError(f"rule {rule_id} is not active")


def expire_rules(conn: sqlite3.Connection) -> int:
    return conn.execute(
        f"UPDATE rules SET status = 'expired' WHERE status = 'provisional' AND expires_at <= {NOW}"
    ).rowcount


def brain_path():
    return paths.home() / "brain.md"


def decisions_since(conn: sqlite3.Connection, since: str):
    # Approvals with edits, rejections, change requests and dismissals all teach something; untouched
    # approvals are kept too (as "this was fine").
    return conn.execute(
        "SELECT * FROM items WHERE decided_at > ? AND status IN "
        "('approved','executing','done','failed','rejected','changes_requested','dismissed','pending') "
        "AND decided_at IS NOT NULL ORDER BY decided_at LIMIT 60",
        (since,),
    ).fetchall()


def build_brain_prompt(current: str, rows) -> str:
    me = config.ME_NAME
    decisions = []
    for r in rows:
        decisions.append({
            "item_id": r["id"], "lead": r["lead"], "kind": r["kind"], "title": r["title"],
            "outcome": r["status"], "my_note": r["feedback"],
            "agent_draft": r["original_body"] if r["original_body"] is not None else r["body"],
            "my_edited_version": r["body"] if r["original_body"] is not None else None,
            "agent_prompt": r["original_prompt"] if r["original_prompt"] is not None else r["prompt"],
            "my_edited_prompt": r["prompt"] if r["original_prompt"] is not None else None,
            "launched_session": r["launched_at"] is not None,
        })
    return f"""{prompts.SAFETY}
You maintain {me}'s preferences file. Every lead and the intern read it before working. Update it from {me}'s
recent queue decisions below. What each outcome means:
- approved/done on a reply (older items; leads no longer draft replies): the draft, or {me}'s edit, was right.
- done on a task, an fyi ("Got it") or a decision ("I'll handle it"): useful to surface; {me} acted on it.
- dismissed ("Not useful" / "Not needed" / "Not mine"): not worth {me}'s attention; surface fewer like it.
- rejected: the recommendation was wrong.
- changes_requested: my_note says what was wrong or what {me} wanted instead.
- launched_session: {me} started a Claude Code session from the item's agent prompt. agent_prompt is what the
  lead drafted; my_edited_prompt is what {me} actually ran, if they changed it. An item with a prompt that
  {me} closed without launching suggests the prompt wasn't needed; one {me} launched after writing their own
  prompt (agent_prompt null) suggests the lead should have drafted one.

Rules for the file:
- Markdown, at most about 60 lines, grouped by topic (what to escalate vs drop, when to draft an agent prompt
  and how to write one, per-lead specifics). Keep what is still true from the current file; merge duplicates; drop nothing without
  evidence.
- Only write preferences the decisions actually show. Compare agent_draft with my_edited_version to see
  what {me} changes, and agent_prompt with my_edited_prompt. A single rejection without a note is weak evidence; say "tentative" if you include it.
- Cite evidence as (items 12, 15) after each preference.

Separately, propose rules: short imperative instructions for one agent ("slack", "switchboard", "intern",
or "*" for all) that follow directly from an explicit note by {me} (my_note) or a repeated edit pattern.
Each needs why and evidence_item_ids. Propose none if the evidence is thin.

Output schema:
{{"brain_md": "the full new file", "rules": [{{"agent": "slack", "text": "...", "why": "...",
  "evidence_item_ids": [12]}}], "summary": "one line on what changed"}}

Current file:
{prompts.untrusted("brain.md", current or "(empty)")}

Recent decisions:
{prompts.untrusted("decisions", decisions)}
"""


def distill(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    since = store.kv_get(conn, BRAIN_CURSOR) or "1970-01-01T00:00:00Z"
    rows = decisions_since(conn, since)
    if not rows:
        print("brain: no new decisions since last distill (no model call)")
        return
    current = brain_path().read_text() if brain_path().exists() else ""
    spec = pass_spec("brain")
    prompt = build_brain_prompt(current, rows)
    if dry_run:
        print(preview(spec, prompt))
        return
    result = run_agent(conn, spec, prompt, job="chief", lead="brain")
    if not result.ok or not result.data.get("brain_md"):
        print(f"brain failed (run {result.run_id}, ${result.cost_usd:.3f}): {result.error or 'no brain_md'}")
        return
    hist = paths.home() / "brain-history"
    hist.mkdir(exist_ok=True)
    if current:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (hist / f"brain-{stamp}.md").write_text(current)
    brain_path().write_text(result.data["brain_md"])
    valid_ids = {r["id"] for r in rows}
    added = []
    for rule in result.data.get("rules") or []:
        evidence = [i for i in rule.get("evidence_item_ids") or [] if i in valid_ids]
        if not evidence:
            continue
        try:
            added.append(add_rule(conn, agent=rule.get("agent", "*"), text=rule.get("text", ""),
                                  why=rule.get("why", ""), source="user",
                                  source_ref="items:" + ",".join(map(str, evidence))))
        except ValueError as e:
            print(f"  rejected rule: {e}")
    store.kv_set(conn, BRAIN_CURSOR, rows[-1]["decided_at"])
    print(f"brain: updated from {len(rows)} decisions, {len(added)} rules added {added} "
          f"(run {result.run_id}, ${result.cost_usd:.3f}): {result.data.get('summary', '')}")


def lead_rules(conn: sqlite3.Connection, lead: str, rules: list, run_id: int) -> list[int]:
    """Rules a lead proposes for itself: always provisional."""
    added = []
    for r in rules[:3]:
        try:
            added.append(add_rule(conn, agent=lead, text=str(r.get("text", ""))[:300],
                                  why=str(r.get("why", ""))[:300], source="agent", source_ref=f"run:{run_id}"))
        except ValueError:
            continue
    return added
