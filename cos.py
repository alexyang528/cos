import argparse
import json
import shlex
import sys
import textwrap

from chief import db, paths, policy, runner, store
from chief.leads import LEADS, QUEUE_OWNERS

STAGE_FOR_JOB = {"todo": 4}


def cmd_chief(args):
    from chief import chief
    chief.run(db.open_db(), dry_run=args.dry_run)


def cmd_tell(args):
    from chief import tell
    conn = db.open_db()
    if args.text:
        tell.add(conn, args.text)
    tell.run(conn, dry_run=args.dry_run)
    for i in tell.recent(conn, 1):
        print(f"\n{i['response'] or ''}\n" + "".join(f"- {a}\n" for a in i["actions"]))


def cmd_notes(args):
    from chief import tell
    conn = db.open_db()
    if args.dismiss:
        tell.dismiss_note(conn, args.dismiss)
        print(f"note #{args.dismiss} dismissed")
        return
    for n in tell.notes(conn):
        print(f"#{n['id']:<4} {'NEEDS YOU' if n['needs_me'] else 'fixed':<9} {n['created_at'][:10]}  {n['title']}")
        for r in n["rules"]:
            print(f"      rule #{r['id']} ({r['status']}, {r['agent']}): {r['text']}")


def cmd_brain(args):
    from chief import memory
    memory.distill(db.open_db(), dry_run=args.dry_run)


def cmd_rules(args):
    from chief import memory
    conn = db.open_db()
    if args.rcmd == "list":
        memory.expire_rules(conn)
        where = "" if args.all else "WHERE status IN ('provisional','confirmed')"
        for r in conn.execute(f"SELECT * FROM rules {where} ORDER BY status, id"):
            exp = f" expires {r['expires_at']}" if r["status"] == "provisional" else ""
            print(f"#{r['id']:<4} {r['status']:<11} {r['agent']:<12} {r['text']}\n      why: {r['why']} "
                  f"(source {r['source']} {r['source_ref'] or ''}{exp})")
    elif args.rcmd == "add":
        print(f"added confirmed rule #{memory.add_rule(conn, agent=args.agent, text=args.text, why=args.why, source='user')}")
    elif args.rcmd == "confirm":
        memory.confirm_rule(conn, args.id, by="user")
        print(f"rule #{args.id} confirmed")
    elif args.rcmd == "retire":
        memory.retire_rule(conn, args.id, by="user")
        print(f"rule #{args.id} retired")


def cmd_cleanup(args):
    from chief import cleanup
    cleanup.run(dry_run=not args.apply)


def cmd_tick(args):
    from chief import schedule
    schedule.tick(db.open_db(), force=args.force)


def cmd_daily(args):
    from chief import schedule
    schedule.daily(db.open_db())


def cmd_schedule(args):
    from chief import schedule
    if args.scmd == "install":
        schedule.install(db.open_db(), until=args.until, dry_run=args.dry_run)
    elif args.scmd == "uninstall":
        schedule.uninstall()
    else:
        schedule.status(db.open_db())


def cmd_scan(args):
    from chief import (scan_email, scan_github, scan_granola, scan_notion, scan_slack, scan_switchboard,
                       scan_workstreams)
    scanners = {"slack": scan_slack, "switchboard": scan_switchboard, "granola": scan_granola, "notion": scan_notion,
                "email": scan_email, "github": scan_github, "workstreams": scan_workstreams}
    scanners[args.surface].run(db.open_db(), dry_run=args.dry_run)


def cmd_intern(args):
    from chief import intern
    intern.run(db.open_db(), dry_run=args.dry_run)


def cmd_lead(args):
    from chief import lead
    if args.review:
        lead.review_open(db.open_db(), args.name, force=True, dry_run=args.dry_run)
    else:
        lead.run(db.open_db(), args.name, dry_run=args.dry_run)


def cmd_sessions(args):
    from chief import sessions
    conn = db.open_db()
    if args.scmd == "index":
        changed, live = sessions.index(conn)
        print(f"sessions: re-read {changed} transcripts, {live} linkable")
    elif args.scmd == "link":
        sessions.index(conn)
        sessions.link(conn, dry_run=args.dry_run)
    elif args.scmd == "links":
        for item_id, links in sorted(sessions.links_for(conn, [args.id]).items()):
            for s in links:
                print(f"#{item_id} {s['title']}\n      {s['why']}\n      {s['resume']}")
    elif args.scmd == "unlink":
        print("unlinked" if sessions.unlink(conn, args.id, args.session_id) else "no such link")


def cmd_ws(args):
    from chief import workstreams as ws
    from chief import ws_lead
    conn = db.open_db()
    c = args.wcmd
    if c == "list":
        for w in ws.summaries(conn):
            print(f"#{w['id']:<3} {w['slug']:<28} {w['status']:<9} {w['needs_review']} need review, "
                  f"{w['open_items']} open items, last run {w['last_run_at'] or 'never'}")
        return
    if c == "new":
        ws_id, unparsed = ws.create(conn, args.name, args.description or "", args.link or [])
        w = ws.get(conn, ws_id)
        print(f"created workstream #{ws_id} {w['slug']}; its lead sets it up on the next tick "
              f"(or now: cos.py ws run {w['slug']})")
        for u in unparsed:
            print(f"  not a watchable source, kept in the description: {u}")
        return
    if c == "suggested":
        for p in ws.pending_proposals(conn):
            print(f"#{p['id']:<3} {p['name']}  items {p['item_ids']}\n      {p['why']}")
        return
    if c == "accept":
        ws_id = ws.accept_proposal(conn, args.id, name=args.name)
        print(f"created workstream #{ws_id} {ws.get(conn, ws_id)['slug']}; its lead sets it up on the next tick")
        return
    if c == "decline":
        ws.reject_proposal(conn, args.id, args.note)
        print(f"suggestion #{args.id} rejected")
        return
    if c == "move":
        target = None if args.workstream == "queue" else ws.by_slug(conn, args.workstream)["id"]
        ws.move_item(conn, args.item, target)
        print(f"item #{args.item} moved to {args.workstream}")
        return
    w = ws.by_slug(conn, args.workstream)
    if c == "show":
        d = ws.detail(conn, w["id"])
        print(f"{w['name']} ({w['slug']}, {w['status']})\n\n{w['description']}\n")
        print("overview:\n" + (w["latest"] or "(not written yet)"))
        print("\nsources:")
        for s_ in d["sources"]:
            print(f"  [{s_['id']}] {s_['status']:<8} {s_['kind']:<16} {s_['label'] or s_['ref']}  {s_['why'] or ''}")
        print("\nopen items:")
        for row in store.queue(conn, workstream_id=w["id"]):
            print("  " + _fmt_item(row))
        from chief.ws_sessions import links_for
        print("\nClaude chats:")
        for s_ in links_for(conn, w["id"], 10):
            print(f"  {s_['last_at'][:16]}  {s_['title']}\n      {s_['why']}\n      {s_['resume']}")
        print("\nrecent updates:")
        for u in d["updates"]:
            print(f"  {(u['occurred_at'] or u['created_at'])[:16]}  {u['text']}")
    elif c == "run":
        ws_lead.run(conn, w, reasons=["requested"], dry_run=args.dry_run)
    elif c == "source":
        parsed = ws.parse_link(args.link)
        if not parsed:
            sys.exit("not a Slack channel/thread link, Switchboard plan link, channel id or plan id")
        print(f"watching source #{ws.add_source(conn, w['id'], parsed[0], parsed[1], by='user', url=parsed[2])}"
              f" ({parsed[0]} {parsed[1]})")
    elif c == "instruct":
        print(f"instruction #{ws.instruct(conn, w['id'], args.text)} queued for the {w['slug']} lead")
    elif c in ("pause", "resume", "archive"):
        ws.set_status(conn, w["id"], {"pause": "paused", "resume": "active", "archive": "archived"}[c])
        print(f"{w['slug']}: {c}d")


def cmd_execute(args):
    from chief import execute
    execute.execute_one(db.open_db(), args.id, dry_run=args.dry_run)


def cmd_serve(args):
    import webbrowser

    from chief import web

    httpd, _token = web.serve(args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"queue: {url}  (127.0.0.1 only, Ctrl-C to stop)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_migrate(args):
    paths.ensure_dirs()
    conn = db.connect()
    before = db.schema_version(conn)
    applied, backup = db.migrate(conn)
    if not applied:
        print(f"schema already at v{before} ({paths.db_path()})")
        return
    print(f"migrated {paths.db_path()}: v{before} -> v{applied[-1]} (applied {applied})")
    print(f"backup: {backup}" if backup else "backup: none (fresh database)")


def _fmt_item(row) -> str:
    action = json.loads(row["action"])["type"] if row["action"] else "-"
    return f"#{row['id']:<4} P{row['priority']}  {row['status']:<18} {row['lead']:<12} {row['kind']:<9} {action:<26} {row['title']}"


def cmd_status(args):
    conn = db.open_db()
    print(f"db: {paths.db_path()}  schema v{db.schema_version(conn)}")
    counts = conn.execute("SELECT status, COUNT(*) n FROM items GROUP BY status ORDER BY status").fetchall()
    print("items: " + (", ".join(f"{r['status']}={r['n']}" for r in counts) or "none"))
    ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    routed = conn.execute("SELECT COUNT(*) FROM routes WHERE decision='route' AND picked_up_at IS NULL").fetchone()[0]
    print(f"events: {ev}   unread lead inbox routes: {routed}")
    rules = conn.execute("SELECT status, COUNT(*) n FROM rules GROUP BY status").fetchall()
    print("rules: " + (", ".join(f"{r['status']}={r['n']}" for r in rules) or "none"))
    cmd_runs(args, conn=conn)


def cmd_queue(args):
    conn = db.open_db()
    statuses = tuple(store.TRANSITIONS) if args.all else store.OPEN_STATUSES
    rows = store.queue(conn, statuses)
    if not rows:
        print("queue is empty")
    for row in rows:
        print(_fmt_item(row))


def cmd_expire(args):
    from chief import config
    rows = store.expire_stale(db.open_db(), dry_run=args.dry_run)
    print(f"{len(rows)} items untouched for {config.EXPIRE_AFTER_DAYS} days" + (" (dry run)" if args.dry_run else ""))
    for r in rows:
        what = "expire" if r["status"] == "pending" else "close as done"
        print(f"  #{r['id']:<4} {what:<13} {r['lead']:<12} {r['kind']:<9} {r['title']}")


def cmd_show(args):
    conn = db.open_db()
    row = store.get_item(conn, args.id)
    for key in row.keys():
        if key in ("body", "original_body") or row[key] is None:
            continue
        print(f"{key:>18}: {row[key]}")
    print("\n--- body ---\n" + row["body"])
    if row["original_body"] is not None:
        print("\n--- original draft ---\n" + row["original_body"])


def cmd_add(args):
    conn = db.open_db()
    action = json.loads(args.action) if args.action else None
    item_id = store.add_item(conn, lead=args.lead, kind=args.kind, title=args.title, body=args.body or "",
                             action=action, priority=args.priority, url=args.url, prompt=args.prompt)
    print(f"added item #{item_id}")


def _decide(new_status):
    def run(args):
        conn = db.open_db()
        body = None
        if getattr(args, "body_file", None):
            body = open(args.body_file).read()
        elif getattr(args, "body", None) is not None:
            body = args.body
        try:
            store.transition(conn, args.id, new_status, feedback=getattr(args, "note", None), new_body=body)
        except store.InvalidTransition as e:
            sys.exit(str(e))
        print(_fmt_item(store.get_item(conn, args.id)))
    return run


def cmd_runs(args, conn=None):
    conn = conn or db.open_db()
    rows = store.run_summary(conn)
    if not rows:
        print("runs: none yet")
        return
    print(f"{'job':<9} {'lead':<12} {'runs':>5} {'errors':>6} {'cost $':>9}  {'last started':<21} last status / last error")
    for r in rows:
        err = f"  ({r['last_error'][:60]})" if r["last_error"] else ""
        print(f"{r['job']:<9} {r['lead']:<12} {r['runs']:>5} {r['errors'] or 0:>6} {r['cost_usd']:>9.4f}  "
              f"{r['last_started']:<21} {r['last_status']}{err}")


def cmd_policy_show(args):
    spec = policy.pass_spec(args.pass_name, lead=args.lead, action=args.action)
    print(f"pass={spec.name} model={spec.model} budget=${spec.budget_usd:.2f}")
    print(f"\nallowed ({len(spec.allowed)}):")
    for t in spec.allowed:
        print(f"  + {t}")
    print(f"\ndenied ({len(spec.denied)}):")
    for t in spec.denied:
        print(f"  - {t}")
    if args.command:
        print("\n" + shlex.join(policy.build_command(spec)))


def cmd_policy_check(args):
    problems = policy.check_all()
    for p in problems:
        print(f"FAIL {p}")
    if problems:
        sys.exit(1)
    print(f"policy OK: {9 + len(LEADS)} work passes read-only, {len(policy.ACTION_WRITE_TOOLS)} execute actions scoped")


def _job_stub(job):
    def run(args):
        stage = STAGE_FOR_JOB[job]
        if not args.dry_run:
            sys.exit(f"`{job}` is not built yet (stage {stage}). Use --dry-run to see the command it will run.")
        if job == "lead":
            spec = policy.pass_spec("lead", lead=args.name)
        elif job == "execute":
            conn = db.open_db()
            item = store.get_item(conn, args.id)
            if item["status"] != "approved" or not item["action"]:
                sys.exit(f"item #{args.id} is {item['status']} with action={item['action']}; only approved items with an action execute")
            spec = policy.pass_spec("execute", action=json.loads(item["action"])["type"])
        elif job in ("scan", "cleanup"):
            print(f"[dry-run] {job}: plain Python, no LLM, no tools (stage {stage})")
            return
        else:
            spec = policy.pass_spec(job)
        print(f"[dry-run] {job} (implemented in stage {stage}) would run:\n")
        print(textwrap.indent(shlex.join(policy.build_command(spec)), "  "))
    return run


def ws_errors():
    from chief.workstreams import Conflict
    return Conflict


def main(argv=None):
    p = argparse.ArgumentParser(prog="cos.py", description="Personal chief-of-staff agents")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="create/upgrade the database (backs up first)").set_defaults(fn=cmd_migrate)
    sub.add_parser("status", help="queue counts, rules, and per-job run health").set_defaults(fn=cmd_status)
    sub.add_parser("runs", help="last run, cost and errors per job/lead").set_defaults(fn=cmd_runs)

    q = sub.add_parser("queue", help="list the queue")
    q.add_argument("--all", action="store_true")
    q.set_defaults(fn=cmd_queue)

    s = sub.add_parser("show", help="show one item")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)

    a = sub.add_parser("add", help="add a queue item by hand (testing)")
    a.add_argument("--lead", required=True, choices=sorted(QUEUE_OWNERS))
    a.add_argument("--kind", required=True, choices=["pr", "decision", "fyi", "task"])
    a.add_argument("--title", required=True)
    a.add_argument("--body")
    a.add_argument("--action", help='JSON, e.g. {"type":"switchboard_approve_phase","plan_id":"pl_...","phase":"technical_design"}')
    a.add_argument("--prompt", help="an agent prompt to launch as a Claude Code session")
    a.add_argument("--priority", type=int, default=1, choices=range(3),
                   help="0 urgent, 1 needs you, 2 fyi (fyi items are always 2)")
    a.add_argument("--url")
    a.set_defaults(fn=cmd_add)

    for name, status, help_ in (
        ("approve", "approved", "approve an item (fyi/decision items become done)"),
        ("reject", "rejected", "reject an item"),
        ("changes", "changes_requested", "send an item back to its lead with a note"),
        ("dismiss", "dismissed", "drop an item without judging it"),
        ("close", "done", "confirm a likely-done item is finished"),
        ("reopen", "pending", "say a likely-done item isn't finished; its lead keeps it open"),
    ):
        d = sub.add_parser(name, help=help_)
        d.add_argument("id", type=int)
        d.add_argument("--note")
        if name == "approve":
            d.add_argument("--body", help="edit the body before approving")
            d.add_argument("--body-file")
        d.set_defaults(fn=_decide(status))

    ex = sub.add_parser("expire", help="expire waiting items and close likely-done ones you haven't touched in a "
                                       "while (the tick does this)")
    ex.add_argument("--dry-run", action="store_true", help="list what would expire; change nothing")
    ex.set_defaults(fn=cmd_expire)

    sv = sub.add_parser("serve", help="run the local queue web app")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--open", action="store_true", help="open it in the browser")
    sv.set_defaults(fn=cmd_serve)

    pol = sub.add_parser("policy", help="inspect tool permissions")
    psub = pol.add_subparsers(dest="pcmd", required=True)
    ps = psub.add_parser("show")
    ps.add_argument("pass_name", choices=["scan", "intern", "todo", "chief", "lead", "link", "workstream", "execute"])
    ps.add_argument("--lead", help="the lead, the surface for scan (or 'workstreams'), or a workstream's slug")
    ps.add_argument("--action", choices=sorted(policy.ACTION_WRITE_TOOLS))
    ps.add_argument("--command", action="store_true", help="also print the full claude command")
    ps.set_defaults(fn=cmd_policy_show)
    psub.add_parser("check", help="assert work passes are read-only").set_defaults(fn=cmd_policy_check)

    for job in ("todo",):
        j = sub.add_parser(job, help=f"run the {job} job (not built yet)")
        j.add_argument("--dry-run", action="store_true")
        j.set_defaults(fn=_job_stub(job))

    dry = "print the prompt and command; call no model"
    ch = sub.add_parser("chief", help="chief of staff: review rules, audit routing (read-only)")
    ch.add_argument("--dry-run", action="store_true", help=dry)
    ch.set_defaults(fn=cmd_chief)
    te = sub.add_parser("tell", help="tell the chief of staff something: track a thread, add a rule, rerun an agent")
    te.add_argument("text", nargs="?", help="the instruction (omit to answer ones already waiting)")
    te.add_argument("--dry-run", action="store_true", help=dry)
    te.set_defaults(fn=cmd_tell)
    no = sub.add_parser("notes", help="the chief of staff's notes about the system, and the rules it wrote")
    no.add_argument("--dismiss", type=int, metavar="ID")
    no.set_defaults(fn=cmd_notes)
    br = sub.add_parser("brain", help="distill preferences from your queue decisions into brain.md")
    br.add_argument("--dry-run", action="store_true", help=dry)
    br.set_defaults(fn=cmd_brain)

    ru = sub.add_parser("rules", help="list, add, confirm or retire rules")
    rsub = ru.add_subparsers(dest="rcmd", required=True)
    rl = rsub.add_parser("list")
    rl.add_argument("--all", action="store_true", help="include expired and retired")
    ra = rsub.add_parser("add", help="add a confirmed rule of your own")
    ra.add_argument("--agent", required=True)
    ra.add_argument("--text", required=True)
    ra.add_argument("--why", required=True)
    for verb in ("confirm", "retire"):
        rsub.add_parser(verb).add_argument("id", type=int)
    ru.set_defaults(fn=cmd_rules)

    cl = sub.add_parser("cleanup", help="remove old logs/snapshots/backups and idle clean worktrees")
    cl.add_argument("--apply", action="store_true", help="actually delete (default: report only)")
    cl.set_defaults(fn=cmd_cleanup)
    tk = sub.add_parser("tick", help="one scheduled run: scan, triage, draft (never sends)")
    tk.add_argument("--force", action="store_true", help="run even outside working hours")
    tk.set_defaults(fn=cmd_tick)
    sub.add_parser("daily", help="daily run: chief of staff, brain, cleanup").set_defaults(fn=cmd_daily)
    sch = sub.add_parser("schedule", help="install/uninstall the launchd jobs")
    ssub = sch.add_subparsers(dest="scmd", required=True)
    si = ssub.add_parser("install")
    si.add_argument("--until", help="HH:MM local; jobs uninstall themselves after this time")
    si.add_argument("--dry-run", action="store_true", help="print the job files; install nothing")
    ssub.add_parser("uninstall")
    ssub.add_parser("status")
    sch.set_defaults(fn=cmd_schedule)
    sc = sub.add_parser("scan", help="scan a surface for new events (read-only)")
    sc.add_argument("surface", choices=["slack", "switchboard", "granola", "notion", "email", "github", "workstreams"])
    sc.add_argument("--dry-run", action="store_true", help=dry)
    sc.set_defaults(fn=cmd_scan)
    it = sub.add_parser("intern", help="triage new events: route or skip (read-only)")
    it.add_argument("--dry-run", action="store_true", help=dry)
    it.set_defaults(fn=cmd_intern)
    lj = sub.add_parser("lead", help="run one lead (read-only; proposes queue items)")
    lj.add_argument("name", choices=sorted(LEADS))
    lj.add_argument("--dry-run", action="store_true", help=dry)
    lj.add_argument("--review", action="store_true",
                    help="re-read the sources of the stalest waiting items now: mark finished ones likely done and "
                         "refresh their summaries")
    lj.set_defaults(fn=cmd_lead)
    ss = sub.add_parser("sessions", help="index Claude Code sessions and link them to queue items")
    sssub = ss.add_subparsers(dest="scmd", required=True)
    sssub.add_parser("index", help="re-read changed transcripts (no model call)")
    sl = sssub.add_parser("link", help="link recent sessions to every lead's open items (one model call)")
    sl.add_argument("--dry-run", action="store_true", help=dry)
    sk = sssub.add_parser("links", help="show the sessions linked to an item")
    sk.add_argument("id", type=int)
    su = sssub.add_parser("unlink", help="remove a link; the lead won't propose it again")
    su.add_argument("id", type=int)
    su.add_argument("session_id")
    ss.set_defaults(fn=cmd_sessions)
    wp = sub.add_parser("ws", help="workstreams: long-running projects, each with its own lead")
    wsub = wp.add_subparsers(dest="wcmd", required=True)
    wsub.add_parser("list")
    wn = wsub.add_parser("new", help="create a workstream; its lead sets it up on its first run")
    wn.add_argument("name")
    wn.add_argument("--description", "-d", help="what it is and what done looks like")
    wn.add_argument("--link", action="append", help="a Slack channel/thread or Switchboard plan link (repeatable)")
    for verb, help_ in (("show", "overview, sources, items"), ("run", "run its lead now"),
                        ("pause", "stop scanning and running its lead"), ("resume", "resume a paused workstream"),
                        ("archive", "close it; open items go back to the queue")):
        x = wsub.add_parser(verb, help=help_)
        x.add_argument("workstream", help="slug or id")
        if verb == "run":
            x.add_argument("--dry-run", action="store_true", help=dry)
    for verb, arg, help_ in (("source", "link", "watch a Slack channel/thread or Switchboard plan"),
                             ("instruct", "text", "tell the lead something; it runs on the next tick")):
        x = wsub.add_parser(verb, help=help_)
        x.add_argument("workstream", help="slug or id")
        x.add_argument(arg)
    wsub.add_parser("suggested", help="workstreams the chief of staff suggested, waiting for you")
    wa = wsub.add_parser("accept", help="create a suggested workstream")
    wa.add_argument("id", type=int)
    wa.add_argument("--name", help="a different name")
    wd = wsub.add_parser("decline", help="reject a suggested workstream")
    wd.add_argument("id", type=int)
    wd.add_argument("--note", help="why, so the chief of staff learns")
    wm = wsub.add_parser("move", help="move an item into a workstream, or back to the queue")
    wm.add_argument("item", type=int)
    wm.add_argument("workstream", help="slug or id, or 'queue'")
    wp.set_defaults(fn=cmd_ws)
    # Deliberately one item per invocation: sending is never batched or scheduled.
    ej = sub.add_parser("execute", help="execute one approved item (the only pass that writes)")
    ej.add_argument("id", type=int, help="the approved item to execute")
    ej.add_argument("--dry-run", action="store_true", help=dry)
    ej.set_defaults(fn=cmd_execute)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (ValueError, KeyError, json.JSONDecodeError, NotImplementedError, ws_errors()) as e:
        sys.exit(f"error: {e}")
    except runner.BudgetExceeded as e:
        sys.exit(f"refused: {e}")


if __name__ == "__main__":
    main()
