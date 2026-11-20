import json
import sqlite3
from typing import Any

from .db import transaction
from .links import fix_notion_links
from .leads import QUEUE_OWNERS
from .policy import ACTION_WRITE_TOOLS, DECISION_ACTIONS

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

# The queue state machine. Only `execute` moves items out of approved/executing.
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"approved", "rejected", "changes_requested", "dismissed"},
    "changes_requested": {"pending", "approved", "rejected", "dismissed"},
    "approved": {"pending", "executing"},
    "executing": {"done", "failed"},
    "failed": {"approved", "dismissed"},
    "rejected": set(),
    "done": set(),
    "dismissed": set(),
}


class InvalidTransition(Exception):
    pass


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        f"INSERT INTO kv(key, value) VALUES (?, ?) "
        f"ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = {NOW_SQL}",
        (key, value),
    )


def validate_action(action: dict[str, Any] | None) -> None:
    if action is None:
        return
    kind = action.get("type")
    if kind not in ACTION_WRITE_TOOLS:
        raise ValueError(f"unknown action type {kind!r}; expected one of {sorted(ACTION_WRITE_TOOLS)}")


# P0 = urgent (someone blocked today, incident), P1 = needs me, P2 = FYI. Lower sorts first.
P_URGENT, P_NEEDS_ME, P_FYI = 0, 1, 2


def normalize_priority(kind: str, priority) -> int:
    if kind == "fyi":
        return P_FYI
    try:
        p = int(priority)
    except (TypeError, ValueError):
        return P_NEEDS_ME
    # Anything that isn't an FYI needs me, so it is at most P1.
    return P_URGENT if p <= P_URGENT else P_NEEDS_ME


def add_item(
    conn: sqlite3.Connection,
    *,
    lead: str,
    kind: str,
    title: str,
    body: str = "",
    action: dict[str, Any] | None = None,
    priority: int = 1,
    url: str | None = None,
    route_id: int | None = None,
    run_id: int | None = None,
    rationale: str | None = None,
    summary: str | None = None,
    workstream_id: int | None = None,
    goal_id: int | None = None,
    filed_by: str | None = None,
    prompt: str | None = None,
    workdir: str | None = None,
    ask: str | None = None,
    requester: str | None = None,
    reference: str | None = None,
    timeline: str | None = None,
) -> int:
    if lead not in QUEUE_OWNERS:
        raise ValueError(f"unknown queue owner {lead!r}")
    if kind == "reply":
        raise ValueError("leads no longer draft replies; queue a task or decision, with an agent prompt if useful")
    validate_action(action)
    if kind in ("fyi", "task") and action is not None:
        raise ValueError(f"{kind} items cannot carry an action")
    if kind == "decision" and action is not None and action.get("type") not in DECISION_ACTIONS:
        raise ValueError(f"decision items can only carry {sorted(DECISION_ACTIONS)}")
    if kind == "reply" and action is None:
        raise ValueError("reply items need an action saying where the reply goes")
    priority = normalize_priority(kind, priority)
    title, body, rationale, summary, prompt, ask, timeline = (
        fix_notion_links(v) for v in (title, body, rationale, summary, prompt, ask, timeline))
    cur = conn.execute(
        "INSERT INTO items(lead, kind, title, body, action, priority, url, route_id, created_by_run_id, rationale, "
        "summary, workstream_id, goal_id, filed_by, prompt, workdir, ask, requester, reference, timeline) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (lead, kind, title, body, json.dumps(action) if action else None, priority, url, route_id, run_id,
         rationale, summary, workstream_id, goal_id, filed_by, prompt or None, workdir, ask, requester, reference,
         timeline),
    )
    return cur.lastrowid


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(f"no item {item_id}")
    return row


def queue(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("pending", "changes_requested", "approved", "failed"),
          *, workstream_id: int | None = None):
    """Items in these statuses: all of them, or one workstream's."""
    marks = ",".join("?" * len(statuses))
    where, params = f"status IN ({marks})", list(statuses)
    if workstream_id is not None:
        where += " AND workstream_id = ?"
        params.append(workstream_id)
    return conn.execute(f"SELECT * FROM items WHERE {where} ORDER BY priority, created_at, id", params).fetchall()


def pool_items(conn: sqlite3.Connection, *, exclude_lead: str | None = None, exclude_workstream: int | None = None,
               limit: int = 80) -> list[dict]:
    """Open items filed by other leads or in other workstreams, so a lead can avoid duplicating them."""
    where, params = "i.status IN ('pending','changes_requested','approved','failed')", []
    if exclude_lead:
        where += " AND i.lead != ?"
        params.append(exclude_lead)
    if exclude_workstream is not None:
        where += " AND (i.workstream_id IS NULL OR i.workstream_id != ?)"
        params.append(exclude_workstream)
    rows = conn.execute(
        f"SELECT i.id, i.lead, i.kind, i.title, i.url, i.status, i.workstream_id, w.name AS ws FROM items i "
        f"LEFT JOIN workstreams w ON w.id = i.workstream_id WHERE {where} ORDER BY i.id DESC LIMIT ?", (*params, limit))
    return [{"item_id": r["id"], "lead": r["lead"], "kind": r["kind"], "title": r["title"], "url": r["url"],
             "workstream": r["ws"]} for r in rows]


def transition(
    conn: sqlite3.Connection,
    item_id: int,
    new_status: str,
    *,
    feedback: str | None = None,
    new_body: str | None = None,
) -> None:
    with transaction(conn):
        item = get_item(conn, item_id)
        if new_status not in TRANSITIONS[item["status"]]:
            raise InvalidTransition(f"item {item_id}: {item['status']} -> {new_status} not allowed")
        # Approving something with no action (fyi/decision) just acknowledges it; nothing executes.
        if new_status == "approved" and item["action"] is None:
            new_status = "done"
        sets = ["status = ?", f"updated_at = {NOW_SQL}"]
        params: list[Any] = [new_status]
        if new_status in ("approved", "rejected", "changes_requested", "dismissed", "done"):
            sets.append(f"decided_at = {NOW_SQL}")
        if feedback is not None:
            sets.append("feedback = ?")
            params.append(feedback)
        if new_body is not None and new_body != item["body"]:
            # Keep the agent's first draft so the brain can learn from my edits.
            sets.append("original_body = COALESCE(original_body, body)")
            sets.append("body = ?")
            params.append(new_body)
        conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", (*params, item_id))


def start_run(conn: sqlite3.Connection, job: str, *, lead: str | None = None, item_id: int | None = None,
              model: str | None = None, status: str = "running") -> int:
    cur = conn.execute(
        "INSERT INTO runs(job, lead, item_id, model, status) VALUES (?, ?, ?, ?, ?)",
        (job, lead, item_id, model, status),
    )
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str, cost_usd: float | None = None,
               input_tokens: int | None = None, output_tokens: int | None = None, num_turns: int | None = None,
               session_id: str | None = None, error: str | None = None, log_path: str | None = None,
               denials: str | None = None) -> None:
    conn.execute(
        f"UPDATE runs SET status = ?, finished_at = {NOW_SQL}, cost_usd = ?, input_tokens = ?, "
        "output_tokens = ?, num_turns = ?, session_id = ?, error = ?, log_path = ?, denials = ? WHERE id = ?",
        (status, cost_usd, input_tokens, output_tokens, num_turns, session_id, error, log_path, denials, run_id),
    )


def revise_item(conn: sqlite3.Connection, item_id: int, *, body: str, title: str | None = None,
                rationale: str | None = None, summary: str | None = None,
                kind: str | None = None, action: dict[str, Any] | None = None,
                prompt: str | None = None, workdir: str | None = None, ask: str | None = None,
                requester: str | None = None, reference: str | None = None, timeline: str | None = None) -> None:
    """A lead's revision. It may change the kind (a decision into a task, an old reply into a task) and the action.

    The body and prompt are replaced without touching original_body / original_prompt, which track only my edits.
    An empty prompt string clears the prompt; None keeps it.
    """
    item = get_item(conn, item_id)
    new_kind = kind or item["kind"]
    if new_kind == "reply" and item["kind"] != "reply":
        raise ValueError("leads no longer draft replies")
    if new_kind in ("fyi", "task"):
        new_action = None  # neither kind carries an action, so changing to one drops it
    else:
        new_action = action if action is not None else (json.loads(item["action"]) if item["action"] else None)
    validate_action(new_action)
    if new_kind == "reply" and new_action is None:
        raise ValueError("reply items need an action saying where the reply goes")
    if new_kind == "decision" and new_action is not None and new_action.get("type") not in DECISION_ACTIONS:
        raise ValueError(f"decision items can only carry {sorted(DECISION_ACTIONS)}")
    new_prompt = item["prompt"] if prompt is None else (fix_notion_links(prompt) or None)
    body, title, rationale, summary, ask, timeline = (
        fix_notion_links(v) for v in (body, title, rationale, summary, ask, timeline))
    cur = conn.execute(
        f"UPDATE items SET status = 'pending', body = ?, title = COALESCE(?, title), "
        f"rationale = COALESCE(?, rationale), summary = COALESCE(?, summary), kind = ?, action = ?, "
        f"priority = ?, prompt = ?, workdir = COALESCE(?, workdir), ask = COALESCE(?, ask), "
        f"requester = COALESCE(?, requester), reference = COALESCE(?, reference), timeline = COALESCE(?, timeline), "
        f"updated_at = {NOW_SQL} WHERE id = ? AND status = 'changes_requested'",
        (body, title, rationale, summary, new_kind, json.dumps(new_action) if new_action else None,
         normalize_priority(new_kind, item["priority"]), new_prompt, workdir, ask, requester, reference, timeline,
         item_id),
    )
    if cur.rowcount != 1:
        raise InvalidTransition(f"item {item_id} is not awaiting changes")


LAUNCHABLE = ("pending", "approved", "failed")


def record_launch(conn: sqlite3.Connection, item_id: int, *, prompt: str, workdir: str,
                  error: str | None = None, warning: str | None = None,
                  worktree_path: str | None = None, worktree_branch: str | None = None) -> None:
    """Save the prompt I launched (keeping the lead's draft if I edited it) and the outcome of the launch.

    error: the launch failed. warning: it started, but something needs my attention (shown like an error).
    A launch counts as a decision for the brain (decided_at), but the item stays open: the work happens in the
    session, and I close the item when it's done.
    """
    with transaction(conn):
        item = get_item(conn, item_id)
        if item["status"] not in LAUNCHABLE:
            raise InvalidTransition(f"item {item_id} is {item['status']}; only open items can start a session")
        sets = ["workdir = ?", f"updated_at = {NOW_SQL}"]
        params: list[Any] = [workdir]
        if prompt != (item["prompt"] or ""):
            if item["prompt"]:
                sets.append("original_prompt = COALESCE(original_prompt, prompt)")
            sets.append("prompt = ?")
            params.append(prompt)
        if error:
            sets.append("launch_error = ?")
            params.append(error)
        else:
            # A later launch in the main checkout keeps the record of an earlier worktree.
            sets += [f"launched_at = {NOW_SQL}", f"decided_at = {NOW_SQL}", "launch_error = ?",
                     "worktree_path = COALESCE(?, worktree_path)", "worktree_branch = COALESCE(?, worktree_branch)"]
            params += [warning, worktree_path, worktree_branch]
        conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", (*params, item_id))


def claim_for_execute(conn: sqlite3.Connection, item_id: int) -> bool:
    # Atomic approved -> executing, so two execute passes can never both act on one item.
    cur = conn.execute(
        f"UPDATE items SET status = 'executing', updated_at = {NOW_SQL} WHERE id = ? AND status = 'approved'",
        (item_id,),
    )
    return cur.rowcount == 1


def finish_execute(conn: sqlite3.Connection, item_id: int, *, ok: bool, run_id: int | None,
                   error: str | None = None) -> None:
    conn.execute(
        f"UPDATE items SET status = ?, exec_run_id = ?, exec_error = ?, executed_at = {NOW_SQL}, "
        f"updated_at = {NOW_SQL} WHERE id = ? AND status = 'executing'",
        ("done" if ok else "failed", run_id, error, item_id),
    )


def run_summary(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT job, COALESCE(lead, '') AS lead,
               COUNT(*) AS runs,
               SUM(status = 'error') AS errors,
               ROUND(COALESCE(SUM(cost_usd), 0), 4) AS cost_usd,
               ROUND(COALESCE(AVG(cost_usd), 0), 4) AS avg_cost_usd,
               ROUND(COALESCE(MAX(cost_usd), 0), 4) AS max_cost_usd,
               MAX(started_at) AS last_started,
               (SELECT r2.status FROM runs r2 WHERE r2.job = r.job AND COALESCE(r2.lead,'') = COALESCE(r.lead,'')
                ORDER BY r2.started_at DESC, r2.id DESC LIMIT 1) AS last_status,
               (SELECT r2.error FROM runs r2 WHERE r2.job = r.job AND COALESCE(r2.lead,'') = COALESCE(r.lead,'')
                AND r2.error IS NOT NULL ORDER BY r2.started_at DESC, r2.id DESC LIMIT 1) AS last_error
        FROM runs r GROUP BY job, COALESCE(lead, '') ORDER BY job, lead
        """
    ).fetchall()
