"""Remove old logs, snapshots, guard files and backups, and idle worktrees.

A worktree is removed only if it has no uncommitted changes, its branch has an upstream with nothing
unpushed, and it has been idle for WORKTREE_IDLE_DAYS. Anything else is kept and reported. Never --force.
"""

import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, paths

WORKTREES_DIR = paths.ROOT / "worktrees"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def worktree_verdict(wt: Path) -> tuple[bool, str]:
    if _git(wt, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return False, "not a git worktree"
    st = _git(wt, "status", "--porcelain")
    if st.returncode != 0 or st.stdout.strip():
        return False, "has uncommitted changes"
    if _git(wt, "rev-parse", "--abbrev-ref", "@{u}").returncode != 0:
        return False, "branch has no upstream, so unpushed work can't be ruled out"
    ahead = _git(wt, "rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode != 0 or ahead.stdout.strip() != "0":
        return False, f"{ahead.stdout.strip() or '?'} unpushed commits"
    last = _git(wt, "log", "-1", "--format=%ct").stdout.strip()
    idle_days = (time.time() - max(int(last or 0), wt.stat().st_mtime)) / 86400
    if idle_days < config.WORKTREE_IDLE_DAYS:
        return False, f"active {idle_days:.1f} days ago"
    return True, f"clean, pushed, idle {idle_days:.0f} days"


def run(*, dry_run: bool = True) -> None:
    verb = "would remove" if dry_run else "removed"
    now = datetime.now()

    old_logs = [d for d in paths.logs_dir().glob("20??-??-??")
                if d.is_dir() and datetime.strptime(d.name, "%Y-%m-%d") < now - timedelta(days=config.LOG_RETENTION_DAYS)]
    old_snaps = [f for f in paths.snapshots_dir().glob("*.json")
                 if f.stat().st_mtime < time.time() - config.SNAPSHOT_RETENTION_DAYS * 86400]
    exec_dir = paths.home() / "exec"
    old_guards = [f for f in exec_dir.glob("item-*") if f.stat().st_mtime < time.time() - 7 * 86400] \
        if exec_dir.exists() else []
    backups = sorted(paths.backups_dir().glob("cos-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    old_backups = backups[config.BACKUPS_KEEP:]

    for group, items in (("log days", old_logs), ("snapshots", old_snaps), ("guard files", old_guards),
                         ("backups", old_backups)):
        if not dry_run:
            for p in items:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        print(f"cleanup: {verb} {len(items)} old {group}")

    for log in paths.logs_dir().glob("launchd-*.log"):
        if log.stat().st_size > 5_000_000:
            if not dry_run:
                log.write_text(log.read_text()[-1_000_000:])
            print(f"cleanup: {'would trim' if dry_run else 'trimmed'} {log.name}")

    if not WORKTREES_DIR.exists():
        print("cleanup: no worktrees directory")
        return
    for wt in sorted(p for p in WORKTREES_DIR.iterdir() if p.is_dir()):
        ok, why = worktree_verdict(wt)
        if not ok:
            print(f"cleanup: keep worktree {wt.name} ({why})")
            continue
        if not dry_run:
            common = _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
            r = subprocess.run(["git", "--git-dir", common, "worktree", "remove", str(wt)], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"cleanup: could not remove worktree {wt.name}: {r.stderr.strip()}")
                continue
        print(f"cleanup: {verb} worktree {wt.name} ({why})")
