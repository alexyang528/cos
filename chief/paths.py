import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def home() -> Path:
    # COS_HOME lets a dry run use a throwaway data dir without touching the real queue.
    return Path(os.environ.get("COS_HOME", ROOT / "data"))


def db_path() -> Path:
    return home() / "cos.db"


def backups_dir() -> Path:
    return home() / "backups"


def logs_dir() -> Path:
    return home() / "logs"


def inbox_dir() -> Path:
    return home() / "inbox"


def snapshots_dir() -> Path:
    return home() / "snapshots"


def sessions_dir() -> Path:
    return home() / "sessions"


def claude_projects_dir() -> Path:
    return Path(os.environ.get("COS_CLAUDE_PROJECTS", Path.home() / ".claude" / "projects"))


def ensure_dirs() -> None:
    for d in (home(), backups_dir(), logs_dir(), inbox_dir(), snapshots_dir(), sessions_dir()):
        d.mkdir(parents=True, exist_ok=True)
