"""The tick (what each scheduled run does) and the launchd jobs that trigger it.

Jobs: com.cos.tick (hourly at TICK_MINUTE: scan, triage, draft), com.cos.daily (chief of staff, brain,
cleanup), com.cos.web (the queue app, kept alive). No job ever executes an item: sending is a per-item click.
"""

import fcntl
import os
import plistlib
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, paths, store
from .runner import BudgetExceeded

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LABELS = ("com.cos.tick", "com.cos.daily", "com.cos.web")
UNTIL_KEY = "schedule.until"


def in_work_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return now.weekday() in config.WORK_DAYS and config.WORK_HOURS[0] <= now.hour <= config.WORK_HOURS[1]


def _until_passed(conn) -> bool:
    until = store.kv_get(conn, UNTIL_KEY)
    return bool(until) and datetime.now(timezone.utc) >= datetime.fromisoformat(until)


class _Lock:
    """One tick at a time; a slow tick makes the next one skip instead of piling up."""

    def __init__(self, name: str):
        self.path = paths.home() / f"{name}.lock"
        self.fh = None

    def __enter__(self) -> bool:
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def __exit__(self, *exc) -> None:
        self.fh.close()


def _step(label, fn) -> bool:
    """Run one step; keep going on ordinary failures, stop the tick when the daily cap is hit."""
    try:
        fn()
    except BudgetExceeded as e:
        print(f"{label}: stopped, {e}")
        return False
    except Exception as e:  # one broken step must not block the others
        print(f"{label}: error {type(e).__name__}: {e}")
    return True


def tick(conn, *, force: bool = False) -> None:
    from . import (intern, lead, memory, scan_email, scan_github, scan_granola, scan_notion, scan_slack,
                   scan_switchboard, scan_workstreams, sessions, workstreams, ws_lead)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if _until_passed(conn):
        print(f"[{stamp}] tick: past the scheduled end time, uninstalling jobs")
        uninstall()
        return
    if not force and not in_work_hours():
        print(f"[{stamp}] tick: outside working hours, nothing to do")
        return
    with _Lock("tick") as got:
        if not got:
            print(f"[{stamp}] tick: previous tick still running, skipping")
            return
        print(f"[{stamp}] tick")
        expired = memory.expire_rules(conn)
        if expired:
            print(f"rules: {expired} provisional rules expired")
        stale = store.expire_stale(conn)
        if stale:
            print(f"items untouched for {config.EXPIRE_AFTER_DAYS} days: expired "
                  f"{[r['id'] for r in stale if r['status'] == 'pending']}, closed as done "
                  f"{[r['id'] for r in stale if r['status'] == 'likely_done']}")
        scanners = {"slack": scan_slack, "switchboard": scan_switchboard, "granola": scan_granola, "notion": scan_notion,
                    "email": scan_email, "github": scan_github}
        for surface in config.SCAN_SURFACES:
            if not _step(f"scan {surface}", lambda s=surface: scanners[s].run(conn)):
                return
        if not _step("intern", lambda: intern.run(conn)):
            return
        for name in config.ENABLED_LEADS:
            if not _step(f"lead {name}", lambda n=name: lead.run(conn, n)):
                return
        _step("sessions index", lambda: print("sessions: re-read %d transcripts, %d linkable" % sessions.index(conn)))
        if not _step("sessions link", lambda: sessions.link(conn)):
            return
        # Workstreams run last, so their spend never delays the queue's leads.
        if not _step("scan workstreams", lambda: scan_workstreams.run(conn)):
            return
        for w in workstreams.active(conn):
            if not _step(f"workstream {w['slug']}", lambda w=w: ws_lead.run(conn, w)):
                return
        # Closing out finished items comes last: it only tidies the queue, so it gets whatever budget is left.
        for name in config.ENABLED_LEADS:
            if not _step(f"review {name}", lambda n=name: lead.review_open(conn, n)):
                return


def run_lead_now(name: str) -> str:
    """Run one lead immediately (after an instruction from the web app), sharing the tick lock."""
    from . import db, lead
    with _Lock("tick") as got:
        if not got:
            return "busy"  # a tick is running; it runs this lead too, so the instruction is picked up there
        conn = db.open_db()
        try:
            lead.run(conn, name)
        finally:
            conn.close()
    return "ran"


def run_workstream_now(ws_id: int, reasons: list[str] | None = None) -> str:
    """Run one workstream's lead now (after an instruction or a Run click in the web app), sharing the tick lock."""
    from . import db, ws_lead
    with _Lock("tick") as got:
        if not got:
            return "busy"  # the running tick runs due workstream leads too, so the instruction is picked up there
        conn = db.open_db()
        try:
            ws_lead.run(conn, ws_id, reasons=reasons)
        finally:
            conn.close()
    return "ran"


def daily(conn) -> None:
    from . import chief, cleanup, memory
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if _until_passed(conn):
        print(f"[{stamp}] daily: past the scheduled end time, uninstalling jobs")
        uninstall()
        return
    print(f"[{stamp}] daily")
    _step("chief", lambda: chief.run(conn))
    _step("brain", lambda: memory.distill(conn))
    _step("cleanup", lambda: cleanup.run(dry_run=False))


def _plist(label: str, args: list[str], **extra) -> dict:
    uv = shutil.which("uv")
    claude = shutil.which("claude")
    if not uv or not claude:
        raise ValueError(f"need uv and claude on PATH (uv={uv}, claude={claude})")
    path_dirs = [str(Path(claude).parent), str(Path(uv).parent), "/opt/homebrew/bin", "/usr/bin", "/bin"]
    env = {"PATH": ":".join(dict.fromkeys(path_dirs)), "HOME": str(Path.home())}
    if os.environ.get("COS_HOME"):
        env["COS_HOME"] = os.environ["COS_HOME"]
    logs = paths.logs_dir()
    return {
        "Label": label,
        "ProgramArguments": [uv, "run", "-q", str(paths.ROOT / "cos.py"), *args],
        "WorkingDirectory": str(paths.ROOT),
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs / f"launchd-{label.split('.')[-1]}.log"),
        "StandardErrorPath": str(logs / f"launchd-{label.split('.')[-1]}.log"),
        "ProcessType": "Background",
        **extra,
    }


def tick_times() -> list[dict]:
    """One launchd start time per working hour of each work day (launchd counts weekdays from Sunday = 0)."""
    first, last = config.WORK_HOURS
    return [{"Weekday": day + 1, "Hour": hour, "Minute": config.TICK_MINUTE}
            for day in config.WORK_DAYS for hour in range(first, last + 1)]


def plists() -> dict[str, dict]:
    return {
        "com.cos.tick": _plist("com.cos.tick", ["tick"], StartCalendarInterval=tick_times()),
        "com.cos.daily": _plist("com.cos.daily", ["daily"],
                                StartCalendarInterval={"Hour": config.DAILY_AT[0], "Minute": config.DAILY_AT[1]}),
        "com.cos.web": _plist("com.cos.web", ["serve", "--port", str(config.WEB_PORT)],
                              RunAtLoad=True, KeepAlive=True),
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(conn, *, until: str | None = None, dry_run: bool = False) -> None:
    files = plists()
    end = None
    if until:
        hh, mm = (int(x) for x in until.split(":"))
        local = datetime.now().astimezone()
        end = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if end <= local:
            end += timedelta(days=1)
    if dry_run:
        for label, data in files.items():
            print(f"--- {AGENTS_DIR / (label + '.plist')}")
            print(plistlib.dumps(data).decode())
        if end:
            print(f"[dry-run] jobs would uninstall themselves at {end.isoformat(timespec='minutes')}")
        return
    paths.ensure_dirs()
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    store.kv_set(conn, UNTIL_KEY, end.astimezone(timezone.utc).isoformat() if end else "")
    for label, data in files.items():
        target = AGENTS_DIR / f"{label}.plist"
        subprocess.run(["launchctl", "bootout", f"{_domain()}/{label}"], capture_output=True)
        target.write_bytes(plistlib.dumps(data))
        # bootout returns before a running job (the web app) has exited; bootstrap fails with EIO until it has.
        for _ in range(10):
            r = subprocess.run(["launchctl", "bootstrap", _domain(), str(target)], capture_output=True, text=True)
            if r.returncode == 0:
                break
            time.sleep(1)
        print(f"{label}: {'loaded' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()}")
    print(f"installed; ends at {end.isoformat(timespec='minutes')}" if end else "installed; runs until uninstalled")


def uninstall() -> None:
    for label in LABELS:
        target = AGENTS_DIR / f"{label}.plist"
        subprocess.run(["launchctl", "bootout", f"{_domain()}/{label}"], capture_output=True)
        existed = target.exists()
        target.unlink(missing_ok=True)
        print(f"{label}: {'removed' if existed else 'not installed'}")


def status(conn) -> None:
    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    for label in LABELS:
        installed = (AGENTS_DIR / f"{label}.plist").exists()
        loaded = any(line.endswith(label) for line in listed.splitlines())
        print(f"{label}: {'installed' if installed else 'not installed'}, {'loaded' if loaded else 'not loaded'}")
    until = store.kv_get(conn, UNTIL_KEY)
    print(f"ends at: {until or 'never (until uninstalled)'}")
    print(f"working hours now: {in_work_hours()}")
