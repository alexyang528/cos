"""Local-only queue UI.

Bound to 127.0.0.1. Mutations need an X-COS-Token header holding a per-launch secret that is only
embedded in the page we serve; a custom header forces a CORS preflight we never answer, so other
sites in the browser can't post here. The Host check blocks DNS-rebinding reads of that page.
"""

import json
import re
import secrets
from urllib.parse import urlparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, db, store
from .lead import SOURCE_HOSTS
from .leads import QUEUE_OWNERS, SOURCE_TO_LEAD

INDEX = Path(__file__).with_name("static") / "index.html"
# Inter and Reddit Mono (SIL OFL 1.1), copied from console-next; Console's own Regola font is licensed.
FONTS = {"/fonts/inter.woff2": "Inter-Variable-latin.woff2", "/fonts/reddit-mono.woff2": "RedditMono-Variable.woff2"}
# Each service's own favicon, saved locally because the page's CSP allows no remote images. Switchboard's is
# behind SSO, so it was copied from the switchboard repo (web/public/brand/apple-touch-icon-192.png).
# Notion's comes from the Notion desktop app's icon (electron.icns), converted to PNG; Gmail's is Google's product
# icon (gstatic.com/images/branding/product/2x/gmail_2020q4_48dp.png); GitHub's is github.com/fluidicon.png.
LOGOS = {"/logos/slack.png": "slack.png", "/logos/granola.png": "granola.png", "/logos/switchboard.png": "switchboard.png",
         "/logos/notion.png": "notion.png", "/logos/gmail.png": "gmail.png", "/logos/github.png": "github.png"}
APP_ICON = re.compile(r"^/app-icons/([a-z]+)\.png$")
OPEN_STATUSES = (*store.OPEN_STATUSES, "executing")
DECISIONS = {
    "approve": "approved",
    "reject": "rejected",
    "changes": "changes_requested",
    "dismiss": "dismissed",
    "unapprove": "pending",
    "retry": "approved",
    "close": "done",
    "reopen": "pending",
}
ITEM_PATH = re.compile(r"^/api/items/(\d+)/([a-z]+)$")
RULE_PATH = re.compile(r"^/api/rules/(\d+)/(confirm|retire)$")
WS_GET = re.compile(r"^/api/workstreams/(\d+)$")
WS_PATH = re.compile(r"^/api/workstreams/(\d+)/([a-z]+)$")
SOURCE_PATH = re.compile(r"^/api/sources/(\d+)/(accept|reject|remove)$")
PROPOSAL_PATH = re.compile(r"^/api/proposals/(\d+)/(accept|reject)$")
NOTE_PATH = re.compile(r"^/api/notes/(\d+)/dismiss$")
# Workstreams whose lead this server started and hasn't finished; the page polls while one is running.
RUNNING: set[int] = set()


def run_lead_in_background(name: str) -> None:
    import threading

    from .schedule import run_lead_now
    threading.Thread(target=run_lead_now, args=(name,), daemon=True).start()


def run_chief_in_background() -> None:
    import threading

    from .schedule import run_chief_now
    threading.Thread(target=run_chief_now, daemon=True).start()


def run_workstream_in_background(ws_id: int, reasons: list[str] | None = None) -> None:
    import threading

    from .schedule import run_workstream_now

    def work():
        RUNNING.add(ws_id)
        try:
            run_workstream_now(ws_id, reasons)
        except Exception as e:  # shown in the server log; the next tick retries
            print(f"workstream {ws_id} run failed to start: {e}")
        finally:
            RUNNING.discard(ws_id)

    threading.Thread(target=work, daemon=True).start()


def send_in_background(item_id: int) -> None:
    """Run the execute pass for one approved item. Only ever started by an explicit Send click."""
    import threading

    from . import execute

    def work():
        conn = db.open_db()
        try:
            execute.execute_one(conn, item_id)
        except Exception as e:  # surfaced on the item so the UI can show it
            conn.execute("UPDATE items SET exec_error = ? WHERE id = ?", (f"send failed to start: {e}", item_id))
        finally:
            conn.close()

    threading.Thread(target=work, daemon=True).start()


def by_review_order(items: list[dict]) -> list[dict]:
    """Items a lead thinks are finished go after everything that still needs me."""
    return sorted(items, key=lambda i: i["status"] == "likely_done")


def add_expiry(conn, items: list[dict]) -> None:
    """expires_at on each waiting or likely-done item, only once it is within EXPIRE_WARN_DAYS, so the page can
    count down."""
    from datetime import datetime, timedelta, timezone
    warn_from = (datetime.now(timezone.utc) + timedelta(days=config.EXPIRE_WARN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dates = store.expiry_dates(conn, [i["id"] for i in items])
    for i in items:
        e = dates.get(i["id"])
        i["expires_at"] = e if e and e <= warn_from else None


def item_json(row) -> dict:
    d = dict(row)
    d["action"] = json.loads(d["action"]) if d["action"] else None
    d["timeline"] = json.loads(d["timeline"]) if d.get("timeline") else []
    return d


# What the pane's meta line calls an item's source, by the event behind it: (source, event kind) -> label.
SOURCE_LABELS = {
    ("slack", "dm"): "Slack DM", ("slack", "group_dm"): "Slack group DM", ("slack", "mention"): "Slack mention",
    ("slack", "thread_reply"): "Slack thread", ("switchboard", None): "Switchboard plan",
    ("granola", None): "Granola meeting", ("notion", "ticket"): "Inbound Platform", ("notion", "mention"): "Notion page",
    ("email", "email"): "Email", ("email", "zendesk"): "Zendesk",
    ("github", "review_request"): "GitHub review request", ("github", "pr_feedback"): "Review of your PR",
}
OWNER_LABELS = {"slack": "Slack", "switchboard": "Switchboard", "granola": "Granola", "notion": "Notion",
                "email": "Email", "github": "GitHub", "chief": "Chief of staff", "workstream": "Workstream",
                "todo": "To-do"}


def add_source_labels(conn, items: list[dict]) -> None:
    ids = [i["id"] for i in items]
    if not ids:
        return
    kinds = {r["id"]: (r["source"], r["kind"]) for r in conn.execute(
        f"SELECT i.id, e.source, e.kind FROM items i JOIN routes r ON r.id = i.route_id JOIN events e ON e.id = r.event_id "
        f"WHERE i.id IN ({','.join('?' * len(ids))})", ids)}
    for i in items:
        source, kind = kinds.get(i["id"], (None, None))
        if source is None and i["url"]:
            # No source event (a workstream lead raised it): go by where the link points.
            host = urlparse(i["url"]).hostname or ""
            source = next((src for src, h in SOURCE_HOSTS.items() if host.endswith(h.lstrip("."))), None)
        # The surface the item came from; an item filed by a workstream lead keeps its source here.
        i["source_lead"] = (SOURCE_TO_LEAD.get(source) if i["lead"] == "workstream" else None) or i["lead"]
        i["source_label"] = (SOURCE_LABELS.get((source, kind)) or SOURCE_LABELS.get((source, None))
                             or OWNER_LABELS.get(i["lead"], i["lead"]))


def snapshot(conn, include_recent: bool) -> dict:
    from .sessions import links_for
    from . import launch, tell, workstreams
    items = by_review_order([item_json(r) for r in store.queue(conn, OPEN_STATUSES)])
    recent = []
    if include_recent:
        recent = [item_json(r) for r in conn.execute(
            "SELECT * FROM items WHERE status IN ('done','rejected','dismissed','expired') "
            "ORDER BY COALESCE(executed_at, decided_at, updated_at) DESC LIMIT 30"
        )]
    links = links_for(conn, [i["id"] for i in items + recent])
    for i in items + recent:
        i["sessions"] = links.get(i["id"], [])
    add_expiry(conn, items)
    add_source_labels(conn, items + recent)
    today = conn.execute(
        "SELECT ROUND(COALESCE(SUM(cost_usd),0),2) c, COUNT(*) n, SUM(status='error') e FROM runs "
        "WHERE started_at >= strftime('%Y-%m-%dT00:00:00Z','now')"
    ).fetchone()
    rules = [dict(r) for r in conn.execute(
        "SELECT * FROM rules WHERE status IN ('provisional','confirmed') ORDER BY status DESC, created_at DESC"
    )]
    return {
        "items": items,
        "recent": recent,
        "rules": rules,
        "owners": sorted(QUEUE_OWNERS),
        "leads": [*config.ENABLED_LEADS, "workstream"],
        "workstreams": [{**w, "running": w["id"] in RUNNING} for w in workstreams.summaries(conn)],
        "ws_proposals": workstreams.pending_proposals(conn),
        "chief_instructions": tell.recent(conn, 15),
        "chief_notes": tell.notes(conn),
        "workdirs": list(config.SESSION_WORKDIRS),
        "default_workdir": config.DEFAULT_WORKDIR,
        "worktree_by_default": sorted(config.WORKTREE_BY_DEFAULT),
        "launcher": config.LAUNCHER,
        "expire_after_days": config.EXPIRE_AFTER_DAYS,
        "launcher_label": config.LAUNCHERS[config.LAUNCHER],
        "resume_targets": launch.resume_targets(),
        "today": {"cost_usd": today["c"], "runs": today["n"], "errors": today["e"] or 0},
    }


def workstream_snapshot(conn, ws_id: int) -> dict:
    from . import workstreams
    from .sessions import links_for
    d = workstreams.detail(conn, ws_id)
    items = by_review_order([item_json(r) for r in store.queue(conn, OPEN_STATUSES, workstream_id=ws_id)])
    closed = [item_json(r) for r in conn.execute(
        "SELECT * FROM items WHERE workstream_id = ? AND status IN ('done','rejected','dismissed','expired') "
        "ORDER BY COALESCE(executed_at, decided_at, updated_at) DESC LIMIT 20", (ws_id,))]
    links = links_for(conn, [i["id"] for i in items + closed])
    for i in items + closed:
        i["sessions"] = links.get(i["id"], [])
    add_expiry(conn, items)
    add_source_labels(conn, items + closed)
    from .ws_sessions import links_for as ws_links
    return {**d, "items": items, "closed": closed, "running": ws_id in RUNNING, "sessions": ws_links(conn, ws_id)}


def runs_snapshot(conn) -> dict:
    from . import config
    from .schedule import AGENTS_DIR, LABELS, UNTIL_KEY, in_work_hours

    summary = [dict(r) for r in store.run_summary(conn)]
    recent = [dict(r) for r in conn.execute(
        "SELECT id, job, lead, item_id, model, status, started_at, finished_at, cost_usd, num_turns, error, denials "
        "FROM runs ORDER BY id DESC LIMIT 50"
    )]
    days = [dict(r) for r in conn.execute(
        "SELECT substr(started_at, 1, 10) AS day, ROUND(SUM(COALESCE(cost_usd, 0)), 4) AS cost_usd, COUNT(*) AS runs, "
        "SUM(status = 'error') AS errors FROM runs GROUP BY day ORDER BY day DESC LIMIT 14"
    )]
    today = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE started_at >= strftime('%Y-%m-%dT00:00:00Z','now')"
    ).fetchone()[0]
    return {
        "summary": summary,
        "recent": recent,
        "days": days,
        "today_cost_usd": round(today, 4),
        "daily_cap_usd": config.DAILY_BUDGET_USD,
        "schedule": {
            "jobs": {label: (AGENTS_DIR / f"{label}.plist").exists() for label in LABELS},
            "until": store.kv_get(conn, UNTIL_KEY) or None,
            "in_work_hours": in_work_hours(),
            "tick_minute": config.TICK_MINUTE,
            "work_hours": list(config.WORK_HOURS),
            "daily_at": "%02d:%02d" % config.DAILY_AT,
        },
    }


def make_handler(token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "cos"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "font-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _host_ok(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            if self.path == "/":
                html = INDEX.read_text().replace("__COS_TOKEN__", token)
                self._send(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8")
            elif self.path in FONTS:
                self._send(HTTPStatus.OK, (INDEX.parent / "fonts" / FONTS[self.path]).read_bytes(), "font/woff2")
            elif self.path in LOGOS:
                self._send(HTTPStatus.OK, (INDEX.parent / "logos" / LOGOS[self.path]).read_bytes(), "image/png")
            elif APP_ICON.match(self.path):
                from . import launch
                png = launch.app_icon(APP_ICON.match(self.path).group(1))
                if png:
                    self._send(HTTPStatus.OK, png, "image/png")
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no icon"})
            elif self.path.startswith("/api/queue") or self.path == "/api/runs" or WS_GET.match(self.path):
                if not secrets.compare_digest(self.headers.get("X-COS-Token", ""), token):
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
                conn = db.open_db()
                try:
                    if self.path == "/api/runs":
                        self._json(HTTPStatus.OK, runs_snapshot(conn))
                    elif WS_GET.match(self.path):
                        self._json(HTTPStatus.OK, workstream_snapshot(conn, int(WS_GET.match(self.path).group(1))))
                    else:
                        self._json(HTTPStatus.OK, snapshot(conn, "recent=1" in self.path))
                except KeyError as e:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(e).strip("'")})
                finally:
                    conn.close()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _instruct(self, item_id: int):
            """Send my free-text instruction to the item's lead and run that lead now (read-only; it only
            proposes). Whatever action it proposes still waits for my Approve and Send."""
            length = int(self.headers.get("Content-Length") or 0)
            try:
                note = (json.loads(self.rfile.read(length) or b"{}").get("note") or "").strip()
            except json.JSONDecodeError:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
            if not note:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "say what you want the lead to do"})
            conn = db.open_db()
            try:
                item = store.get_item(conn, item_id)
                from . import config
                if item["workstream_id"] is not None:
                    from . import workstreams
                    if workstreams.get(conn, item["workstream_id"])["status"] != "active":
                        return self._json(HTTPStatus.CONFLICT, {"error": "this item's workstream isn't active"})
                elif item["lead"] not in config.ENABLED_LEADS:
                    return self._json(HTTPStatus.CONFLICT, {"error": f"{item['lead']} items can't take instructions"})
                if item["status"] == "approved":
                    store.transition(conn, item_id, "pending")
                store.transition(conn, item_id, "changes_requested", feedback=note)
            except KeyError as e:
                return self._json(HTTPStatus.NOT_FOUND, {"error": str(e)})
            except store.InvalidTransition as e:
                return self._json(HTTPStatus.CONFLICT, {"error": str(e)})
            finally:
                conn.close()
            if item["workstream_id"] is not None:
                run_workstream_in_background(item["workstream_id"])
            else:
                run_lead_in_background(item["lead"])
            return self._json(HTTPStatus.ACCEPTED, {"ok": True, "status": "with lead"})

        def _tell_chief(self):
            """Queue my instruction for the chief of staff and answer it now (or in the running tick)."""
            from . import tell
            try:
                text = str(self._payload().get("text") or "")
                conn = db.open_db()
                try:
                    instr_id = tell.add(conn, text)
                finally:
                    conn.close()
            except ValueError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            run_chief_in_background()
            return self._json(HTTPStatus.ACCEPTED, {"ok": True, "id": instr_id})

        def _proposal(self, proposal_id: str, verb: str):
            """Create or reject a workstream the chief of staff suggested. Creating one starts its lead."""
            from . import workstreams as ws
            try:
                body = self._payload()
            except ValueError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            conn = db.open_db()
            try:
                if verb == "accept":
                    name, desc = body.get("name"), body.get("description")
                    ws_id = ws.accept_proposal(conn, int(proposal_id), name=str(name) if name else None,
                                               description=str(desc) if isinstance(desc, str) else None)
                    result = {"ok": True, "id": ws_id}
                else:
                    ws.reject_proposal(conn, int(proposal_id), str(body.get("note") or ""))
                    result = {"ok": True}
            except KeyError as e:
                return self._json(HTTPStatus.NOT_FOUND, {"error": str(e).strip("'")})
            except ws.Conflict as e:
                return self._json(HTTPStatus.CONFLICT, {"error": str(e)})
            except ValueError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            finally:
                conn.close()
            if verb == "accept":
                run_workstream_in_background(result["id"])
            return self._json(HTTPStatus.OK, result)

        def _close_likely_done(self):
            """Close the likely-done items I was looking at. Only those ids, and only while still likely_done, so an
            item a lead reopened since the page loaded stays open."""
            try:
                ids = self._payload().get("ids")
            except ValueError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "ids must be a list of item ids"})
            conn = db.open_db()
            closed = []
            try:
                for item_id in ids:
                    try:
                        if store.get_item(conn, item_id)["status"] == "likely_done":
                            store.transition(conn, item_id, "done")
                            closed.append(item_id)
                    except (KeyError, store.InvalidTransition):
                        continue
            finally:
                conn.close()
            return self._json(HTTPStatus.OK, {"ok": True, "closed": closed})

        def _payload(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                value = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise ValueError("bad json")
            if not isinstance(value, dict):
                raise ValueError("bad json")
            return value

        def _workstream_post(self) -> bool:
            """Workstream, source and item-filing endpoints. Returns False if the path isn't one of them."""
            from . import workstreams as ws
            path = self.path
            m_ws, m_src, m_item = WS_PATH.match(path), SOURCE_PATH.match(path), ITEM_PATH.match(path)
            if not (path == "/api/workstreams" or m_ws or m_src
                    or (m_item and m_item.group(2) in ("move", "promote"))):
                return False
            run_after, run_reasons = None, None
            conn = db.open_db()
            try:
                body = self._payload()
                if path == "/api/workstreams":
                    links = body.get("links") or []
                    if isinstance(links, str):
                        links = links.splitlines()
                    ws_id, unparsed = ws.create(conn, str(body.get("name") or ""), str(body.get("description") or ""),
                                                [str(x) for x in links])
                    run_after = ws_id
                    result = {"ok": True, "id": ws_id, "unparsed": unparsed}
                elif m_ws:
                    ws_id, verb = int(m_ws.group(1)), m_ws.group(2)
                    ws.get(conn, ws_id)
                    result = {"ok": True}
                    if verb == "instruct":
                        ws.instruct(conn, ws_id, str(body.get("text") or ""))
                        run_after = ws_id
                    elif verb == "run":
                        if ws.get(conn, ws_id)["status"] != "active":
                            raise ws.Conflict("this workstream isn't active")
                        run_after, run_reasons = ws_id, ["requested"]
                    elif verb == "source":
                        parsed = ws.parse_link(str(body.get("link") or ""))
                        if not parsed:
                            raise ValueError("paste a Slack channel or thread link, or a Switchboard plan link")
                        result["id"] = ws.add_source(conn, ws_id, parsed[0], parsed[1], by="user", url=parsed[2],
                                                     label=str(body.get("label") or "") or None)
                    elif verb in ("pause", "resume", "archive"):
                        ws.set_status(conn, ws_id, {"pause": "paused", "resume": "active", "archive": "archived"}[verb])
                    elif verb == "unlink":
                        from .ws_sessions import unlink
                        if not unlink(conn, ws_id, str(body.get("session_id") or "")):
                            raise KeyError("no such link")
                    elif verb == "edit":
                        ws.update_fields(conn, ws_id, name=body.get("name"), description=body.get("description"),
                                         charter=body.get("charter"))
                    else:
                        return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"}) or True
                elif m_src:
                    ws.decide_source(conn, int(m_src.group(1)), m_src.group(2))
                    result = {"ok": True}
                elif m_item.group(2) == "move":
                    target = body.get("workstream_id")
                    ws.move_item(conn, int(m_item.group(1)), int(target) if target is not None else None)
                    result = {"ok": True}
                else:
                    ws_id = ws.promote_item(conn, int(m_item.group(1)), str(body.get("name") or ""))
                    run_after = ws_id
                    result = {"ok": True, "id": ws_id}
            except KeyError as e:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(e).strip("'")})
                return True
            except ws.Conflict as e:
                self._json(HTTPStatus.CONFLICT, {"error": str(e)})
                return True
            except ValueError as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return True
            finally:
                conn.close()
            if run_after is not None:
                run_workstream_in_background(run_after, run_reasons)
            self._json(HTTPStatus.OK, result)
            return True

        def _launch(self, item_id: int):
            """Start a Claude Code session (config.LAUNCHER) from the item's prompt (as I edited it). It only opens a
            session for me to drive; nothing is sent anywhere."""
            from . import launch
            try:
                body = self._payload()
            except ValueError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            conn = db.open_db()
            try:
                wt = body.get("worktree")
                launch.launch(conn, item_id, str(body.get("prompt") or ""),
                              str(body.get("workdir") or config.DEFAULT_WORKDIR),
                              worktree=wt if isinstance(wt, bool) else None)
                self._json(HTTPStatus.OK, item_json(store.get_item(conn, item_id)))
            except KeyError as e:
                return self._json(HTTPStatus.NOT_FOUND, {"error": str(e).strip("'")})
            except store.InvalidTransition as e:
                return self._json(HTTPStatus.CONFLICT, {"error": str(e)})
            except ValueError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except launch.LaunchError as e:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
            finally:
                conn.close()
            launch.link_soon()

        def _resume(self):
            """Open a linked Claude Code session again: where the request says, else where config.LAUNCHER says."""
            from . import launch
            try:
                body = self._payload()
            except ValueError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            conn = db.open_db()
            try:
                where = launch.resume(conn, str(body.get("session_id") or ""), body.get("where") or None)
                return self._json(HTTPStatus.OK, {"ok": True, "opened_in": where})
            except KeyError as e:
                return self._json(HTTPStatus.NOT_FOUND, {"error": str(e).strip("'")})
            except ValueError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except launch.LaunchError as e:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
            finally:
                conn.close()

        def _unlink(self, item_id: int):
            from .sessions import unlink
            length = int(self.headers.get("Content-Length") or 0)
            try:
                session_id = str(json.loads(self.rfile.read(length) or b"{}").get("session_id") or "")
            except json.JSONDecodeError:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
            conn = db.open_db()
            try:
                if not unlink(conn, item_id, session_id):
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such link"})
            finally:
                conn.close()
            return self._json(HTTPStatus.OK, {"ok": True})

        def do_POST(self):
            if not self._host_ok():
                return
            if not secrets.compare_digest(self.headers.get("X-COS-Token", ""), token):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
            rm = RULE_PATH.match(self.path)
            if rm:
                from . import memory
                conn = db.open_db()
                try:
                    rule_id, verb = int(rm.group(1)), rm.group(2)
                    (memory.confirm_rule if verb == "confirm" else memory.retire_rule)(conn, rule_id, by="user")
                    return self._json(HTTPStatus.OK, {"ok": True})
                except ValueError as e:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(e)})
                finally:
                    conn.close()
            if self._workstream_post():
                return
            if self.path == "/api/sessions/resume":
                return self._resume()
            if self.path == "/api/items/close-likely-done":
                return self._close_likely_done()
            if self.path == "/api/chief":
                return self._tell_chief()
            if NOTE_PATH.match(self.path):
                from . import tell
                conn = db.open_db()
                try:
                    tell.dismiss_note(conn, int(NOTE_PATH.match(self.path).group(1)))
                    return self._json(HTTPStatus.OK, {"ok": True})
                except KeyError as e:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(e).strip("'")})
                finally:
                    conn.close()
            if PROPOSAL_PATH.match(self.path):
                return self._proposal(*PROPOSAL_PATH.match(self.path).groups())
            m = ITEM_PATH.match(self.path)
            if m and m.group(2) == "unlink":
                return self._unlink(int(m.group(1)))
            if m and m.group(2) == "launch":
                return self._launch(int(m.group(1)))
            if m and m.group(2) == "instruct":
                return self._instruct(int(m.group(1)))
            if m and m.group(2) == "escalate":
                conn = db.open_db()
                try:
                    store.set_escalated(conn, int(m.group(1)), self._payload().get("escalated"))
                    return self._json(HTTPStatus.OK, item_json(store.get_item(conn, int(m.group(1)))))
                except KeyError as e:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(e).strip("'")})
                except store.InvalidTransition as e:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(e)})
                except ValueError as e:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                finally:
                    conn.close()
            if m and m.group(2) == "send":
                item_id = int(m.group(1))
                conn = db.open_db()
                try:
                    item = store.get_item(conn, item_id)
                except KeyError as e:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(e)})
                finally:
                    conn.close()
                if item["status"] != "approved" or not item["action"]:
                    return self._json(HTTPStatus.CONFLICT, {"error": f"item {item_id} is {item['status']}, not approved"})
                send_in_background(item_id)
                return self._json(HTTPStatus.ACCEPTED, {"ok": True, "status": "sending"})
            if not m or m.group(2) not in DECISIONS:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            item_id, decision = int(m.group(1)), m.group(2)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad json"})
            note = (payload.get("note") or "").strip() or None
            body = payload.get("body")
            if decision == "changes" and not note:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "say what to change"})
            if decision != "approve":
                body = None
            conn = db.open_db()
            try:
                current = store.get_item(conn, item_id)["status"]
                if decision == "unapprove" and current != "approved":
                    raise store.InvalidTransition(f"item {item_id} is {current}, not approved")
                if decision == "retry" and current != "failed":
                    raise store.InvalidTransition(f"item {item_id} is {current}, not failed")
                store.transition(conn, item_id, DECISIONS[decision], feedback=note, new_body=body)
                self._json(HTTPStatus.OK, item_json(store.get_item(conn, item_id)))
            except KeyError as e:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(e)})
            except store.InvalidTransition as e:
                self._json(HTTPStatus.CONFLICT, {"error": str(e)})
            finally:
                conn.close()

    return Handler


def serve(port: int = 8787) -> tuple[ThreadingHTTPServer, str]:
    db.open_db().close()
    token = secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(token, port))
    return httpd, token
