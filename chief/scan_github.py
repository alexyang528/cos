"""GitHub scan with the gh CLI's GraphQL API. No model call: search already says exactly what is waiting on me.

Two kinds of event:
- review_request: an open, non-draft PR that asks me (not just one of my teams) for review. One event per
  request, keyed by the ReviewRequestedEvent, so a re-request after I review raises it again.
- pr_feedback: an open PR of mine where someone else reviewed or commented after my last commit, review, reply
  or comment. One event per PR and newest piece of feedback; if I already answered it, there is no event.

Both look back GITHUB_LOOKBACK_DAYS, so a review request or review older than that never reaches the queue.
"""

import json
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone

from . import config, paths

QUERY = """
query($requested: String!, $mine: String!) {
  viewer { login }
  requested: search(query: $requested, type: ISSUE, first: 50) { nodes { ...pr
    ... on PullRequest {
      reviewRequests(first: 30) { nodes { requestedReviewer { __typename ... on User { login } } } }
      timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], last: 30) { nodes { ... on ReviewRequestedEvent {
        id createdAt actor { login } requestedReviewer { __typename ... on User { login } } } } }
    } } }
  mine: search(query: $mine, type: ISSUE, first: 50) { nodes { ...pr
    ... on PullRequest {
      commits(last: 1) { nodes { commit { committedDate } } }
      reviews(last: 40) { nodes { id url state submittedAt body author { __typename login }
        comments(first: 8) { totalCount nodes { path line body } } } }
      comments(last: 40) { nodes { id url createdAt body author { __typename login } } }
    } } }
}
fragment pr on PullRequest {
  number title url isDraft createdAt updatedAt additions deletions changedFiles reviewDecision
  repository { nameWithOwner } author { __typename login } headRefName baseRefName
  statusCheckRollup: commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
}
"""

BODY_MAX = 600
FEEDBACK_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
VERB = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes on", "COMMENTED": "reviewed",
        "COMMENT": "commented on"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(since_day: str) -> dict:
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("the gh CLI is not on PATH; install it and run `gh auth login`")
    window = f"is:pr is:open archived:false updated:>={since_day}"
    out = subprocess.run(
        [gh, "api", "graphql", "-f", f"query={QUERY}",
         "-f", f"requested={window} draft:false review-requested:@me",
         "-f", f"mine={window} author:@me"],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api graphql failed: {(out.stderr or out.stdout).strip()[:500]}")
    data = json.loads(out.stdout)
    if data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {json.dumps(data['errors'])[:500]}")
    return data["data"]


def _is_bot(author: dict | None) -> bool:
    author = author or {}
    return author.get("__typename") == "Bot" or str(author.get("login", "")).endswith("[bot]")


def _ci(pr: dict) -> str | None:
    nodes = (pr.get("statusCheckRollup") or {}).get("nodes") or []
    rollup = (nodes[0]["commit"].get("statusCheckRollup") if nodes else None) or {}
    return rollup.get("state")


def _base(pr: dict) -> dict:
    repo = pr["repository"]["nameWithOwner"]
    return {
        "repo": repo, "number": pr["number"], "title": pr["title"], "url": pr["url"],
        "author": (pr.get("author") or {}).get("login"), "head": pr.get("headRefName"), "base": pr.get("baseRefName"),
        "created_at": pr.get("createdAt"), "additions": pr.get("additions"), "deletions": pr.get("deletions"),
        "changed_files": pr.get("changedFiles"), "review_decision": pr.get("reviewDecision"), "ci": _ci(pr),
        "is_draft": pr.get("isDraft"), "channel_name": f"{repo}#{pr['number']}",
    }


def review_requests(data: dict, me: str, cutoff: str) -> list[dict]:
    events = []
    for pr in data["requested"]["nodes"] or []:
        if not pr or pr.get("isDraft"):
            continue
        # Search also matches requests made to my teams; only a request to me personally counts.
        requested = {(n.get("requestedReviewer") or {}).get("login") for n in pr["reviewRequests"]["nodes"]}
        if me not in requested:
            continue
        asks = [n for n in pr["timelineItems"]["nodes"]
                if n and (n.get("requestedReviewer") or {}).get("login") == me]
        ask = asks[-1] if asks else None
        at = ask["createdAt"] if ask else pr["updatedAt"]
        if at < cutoff:
            continue
        base = _base(pr)
        by = ((ask or {}).get("actor") or {}).get("login") or base["author"]
        who = "You requested your own" if by == me else f"{by} requested your"
        events.append({
            "source_id": f"review_request:{base['repo']}#{base['number']}:{ask['id'] if ask else 'unknown'}",
            "kind": "review_request", "url": base["url"], "author": by, "occurred_at": at,
            "text": f"{who} review on {base['channel_name']}: {base['title']} "
                    f"(+{base['additions']}/-{base['deletions']}, {base['changed_files']} files)",
            "payload": {**base, "requested_by": by, "requested_at": at,
                        "other_reviewers": sorted(r for r in requested if r and r != me)},
        })
    return events


def pr_feedback(data: dict, me: str, cutoff: str) -> list[dict]:
    events = []
    for pr in data["mine"]["nodes"] or []:
        if not pr:
            continue
        commits = pr["commits"]["nodes"]
        mine = [c["commit"]["committedDate"] for c in commits if c]
        mine += [r["submittedAt"] for r in pr["reviews"]["nodes"]
                 if r and (r.get("author") or {}).get("login") == me and r.get("submittedAt")]
        mine += [c["createdAt"] for c in pr["comments"]["nodes"] if c and (c.get("author") or {}).get("login") == me]
        answered = max([cutoff, *mine])
        feedback = []
        for r in pr["reviews"]["nodes"]:
            if not r or r.get("state") not in FEEDBACK_STATES or not r.get("submittedAt"):
                continue
            if _is_bot(r.get("author")) or (r.get("author") or {}).get("login") == me or r["submittedAt"] <= answered:
                continue
            feedback.append({
                "id": r["id"], "type": "review", "state": r["state"], "by": r["author"]["login"],
                "at": r["submittedAt"], "url": r.get("url"), "body": (r.get("body") or "")[:BODY_MAX],
                "inline_comments": r["comments"]["totalCount"],
                "inline": [{"path": c.get("path"), "line": c.get("line"), "body": (c.get("body") or "")[:300]}
                           for c in r["comments"]["nodes"]],
            })
        for c in pr["comments"]["nodes"]:
            if not c or _is_bot(c.get("author")) or (c.get("author") or {}).get("login") == me \
                    or c["createdAt"] <= answered:
                continue
            feedback.append({"id": c["id"], "type": "comment", "state": "COMMENT", "by": c["author"]["login"],
                             "at": c["createdAt"], "url": c.get("url"), "body": (c.get("body") or "")[:BODY_MAX]})
        if not feedback:
            continue
        feedback.sort(key=lambda f: f["at"])
        newest = feedback[-1]
        base = _base(pr)
        who = sorted({f["by"] for f in feedback})
        verdicts = [f"{f['by']} {VERB[f['state']]}" for f in feedback if f["state"] != "COMMENTED"] or \
                   [f"{', '.join(who)} {'reviewed' if any(f['type'] == 'review' for f in feedback) else 'commented on'}"]
        events.append({
            "source_id": f"pr_feedback:{base['repo']}#{base['number']}:{newest['id']}",
            "kind": "pr_feedback", "url": newest.get("url") or base["url"], "author": newest["by"],
            "occurred_at": newest["at"],
            "text": f"{'; '.join(dict.fromkeys(verdicts))} your PR {base['channel_name']}: {base['title']}",
            "payload": {**base, "feedback": feedback, "my_last_activity": max(mine) if mine else None},
        })
    return events


def ingest(conn: sqlite3.Connection, events: list[dict]) -> dict[str, int]:
    counts = {"new": 0, "duplicate": 0}
    for e in events:
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(source, source_id, kind, url, author, text, occurred_at, payload) "
            "VALUES ('github', ?, ?, ?, ?, ?, ?, ?)",
            (e["source_id"], e["kind"], e["url"], e["author"], e["text"][:2000], e["occurred_at"],
             json.dumps(e["payload"], ensure_ascii=False)),
        )
        counts["new" if cur.rowcount else "duplicate"] += 1
    return counts


def run(conn: sqlite3.Connection, *, dry_run: bool = False) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=config.GITHUB_LOOKBACK_DAYS)
    try:
        data = fetch(since.date().isoformat())
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"scan github failed: {e}")
        return
    me = data["viewer"]["login"]
    cutoff = _iso(since)
    requests, feedback = review_requests(data, me, cutoff), pr_feedback(data, me, cutoff)
    if dry_run:
        for e in requests + feedback:
            print(f"[dry-run] {e['kind']} {e['source_id']}: {e['text']}")
        print(f"[dry-run] {len(requests)} review requests, {len(feedback)} PRs with new feedback (nothing stored)")
        return
    snap = paths.snapshots_dir() / f"github-{int(time.time())}.json"
    snap.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    counts = ingest(conn, requests + feedback)
    print(f"scan github: {counts['new']} new, {counts['duplicate']} seen ({len(requests)} review requests, "
          f"{len(feedback)} PRs of yours with unanswered feedback; snapshot {snap.name})")
