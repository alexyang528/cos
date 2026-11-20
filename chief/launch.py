"""Start a Claude Code session from a queue item's agent prompt, and reopen linked sessions.

Where they open is config.LAUNCHER:
- desktop: a new Code session in the Claude desktop app (claude://code/new), with the prompt drafted for me to send.
  Resume opens the app's own record of the session (claude://code/continue), or a terminal if the app has none.
- terminal: claude in a new terminal window, run from a .command file (no Automation permission needed), optionally
  in a new git worktree (claude --worktree).
- orca: a new Orca worktree (`orca worktree create --agent`) or the folder's main checkout (`orca agent launch`).

Whichever starts it, the session picks its own ID, so the prompt ends with a tag ("[cos item #42]"). The session
indexer reads the tag and links the session to the item, and to the item's workstream, without a model call.
"""

import json
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from . import config, db, paths, store

TAG = re.compile(r"\[cos item #(\d+)\]")
# The indexer only sees a session once Claude has written its transcript, so look a few times after launching.
LINK_RETRIES_S = (20, 60, 180)
# Creating a worktree may wait on the repo's setup hook (e.g. pnpm install) before the agent starts.
WORKTREE_TIMEOUT_S = 300
PROMPT_DELIVERED = ("journaled", "handed-to-terminal")
TERMINAL_WARMUP_S = 3


class LaunchError(Exception):
    pass


def tag(item_id: int) -> str:
    return f"[cos item #{item_id}]"


def tagged_items(prompts: list[str]) -> set[int]:
    return {int(m) for p in prompts for m in TAG.findall(p)}


def workdir_path(key: str) -> Path:
    if key not in config.SESSION_WORKDIRS:
        raise ValueError(f"unknown workdir {key!r}; expected one of {sorted(config.SESSION_WORKDIRS)}")
    path = Path(config.SESSION_WORKDIRS[key]).expanduser()
    if not path.is_dir():
        raise ValueError(f"{path} doesn't exist")
    return path


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].rstrip("-") or "task"


def worktree_name(item) -> str:
    return f"cos-{item['id']}-{slug(item['title'])}"


def _orca(*args: str, timeout: int = 90) -> dict:
    orca = shutil.which("orca") or config.ORCA_BIN
    try:
        r = subprocess.run([orca, *args, "--json"], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise LaunchError(f"Orca's CLI isn't at {orca}")
    except subprocess.TimeoutExpired:
        raise LaunchError(f"orca {args[0]} {args[1]} timed out")
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {}
    if r.returncode != 0 or not out.get("ok"):
        err = out.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise LaunchError(f"orca {args[0]} {args[1]}: {msg or r.stderr.strip() or 'failed'}")
    return out.get("result") or {}


def ensure_registered(path: Path) -> None:
    """Orca can only launch into a folder it knows; add it the first time it's used."""
    known = {Path(r["path"]).resolve() for r in _orca("repo", "list").get("repos", [])}
    if path.resolve() not in known:
        _orca("repo", "add", "--path", str(path), "--kind", "git" if (path / ".git").exists() else "folder")


def _prompt_problem(result: dict) -> str | None:
    """The launch went through but the brief may not have reached the agent."""
    outcome = ((result.get("launch") or {}).get("prompt") or {}).get("outcome")
    if outcome and outcome not in PROMPT_DELIVERED:
        return f"Orca started the session, but the prompt wasn't delivered ({outcome}); paste it in yourself."
    return None


def _created_worktree(result: dict) -> tuple[str | None, str | None]:
    w = result.get("worktree") if isinstance(result.get("worktree"), dict) else result
    path = w.get("path") or (str(w.get("id") or "").split("::", 1)[1] if "::" in str(w.get("id") or "") else None)
    branch = w.get("branch")
    return path, branch.removeprefix("refs/heads/") if branch else None


def launch(conn, item_id: int, prompt: str, workdir: str, *, worktree: bool | None = None) -> None:
    """Start a session for the item. worktree=None uses the folder's default (config.WORKTREE_BY_DEFAULT). The desktop
    app can't be asked for a worktree, so it's ignored there."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("write a prompt for the agent first")
    item = store.get_item(conn, item_id)
    if item["status"] not in store.LAUNCHABLE:
        raise store.InvalidTransition(f"item {item_id} is {item['status']}; only open items can start a session")
    path = workdir_path(workdir)
    if config.LAUNCHER == "desktop":
        worktree = False
    elif worktree is None:
        worktree = workdir in config.WORKTREE_BY_DEFAULT
    if worktree and not (path / ".git").exists():
        raise ValueError(f"{path} isn't a git repo, so it can't have worktrees")
    brief = f"{prompt}\n\n{tag(item_id)}"
    wt_path = wt_branch = warning = None
    try:
        if config.LAUNCHER == "orca":
            wt_path, wt_branch, warning = _launch_orca(item, path, brief, worktree)
        elif config.LAUNCHER == "terminal":
            _launch_terminal(item, path, brief, worktree)
        else:
            _open(f"claude://code/new?{urlencode({'q': brief, 'folder': str(path)})}")
    except LaunchError as e:
        store.record_launch(conn, item_id, prompt=prompt, workdir=workdir, error=str(e))
        raise
    store.record_launch(conn, item_id, prompt=prompt, workdir=workdir, warning=warning,
                        worktree_path=wt_path, worktree_branch=wt_branch)


def _launch_orca(item, path: Path, brief: str, worktree: bool) -> tuple[str | None, str | None, str | None]:
    ensure_registered(path)
    if worktree:
        result = _orca("worktree", "create", "--repo", f"path:{path}", "--name", worktree_name(item), "--no-parent",
                       "--agent", "claude", "--prompt", brief, "--activate",
                       "--comment", f"From cos item #{item['id']}: {item['title']}" + (f" {item['url']}" if item["url"] else ""),
                       timeout=WORKTREE_TIMEOUT_S)
        return *_created_worktree(result), _prompt_problem(result)
    result = _orca("agent", "launch", "--agent", "claude", "--worktree", f"path:{path}", "--prompt", brief, "--focus")
    return None, None, _prompt_problem(result)


def _launch_terminal(item, path: Path, brief: str, worktree: bool) -> None:
    # The prompt goes in a file next to the script, so its quotes and newlines need no shell escaping.
    stem = _launch_dir() / f"item-{item['id']}-{int(time.time())}"
    prompt_file = stem.with_suffix(".prompt.md")
    prompt_file.write_text(brief)
    args = [_claude_bin(), "--name", str(item["title"])[:60]]
    if worktree:
        args += ["--worktree", worktree_name(item)]
    _run_in_terminal(stem, path, f'{shlex.join(args)} "$(cat {shlex.quote(str(prompt_file))})"')


def _launch_dir() -> Path:
    d = paths.home() / "launch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _claude_bin() -> str:
    # The web app runs under launchd, so resolve claude here rather than rely on the new shell's PATH.
    return shutil.which("claude") or "claude"


def _run_in_terminal(stem: Path, cwd: Path, command: str) -> None:
    """Open a terminal window that runs the command in cwd, then leaves a login shell there.

    The terminal runs a .command file by typing its path into a new login shell. On a cold start a slow shell profile
    can garble that, so a terminal that isn't running yet is started (hidden) and given a moment first.
    """
    script = stem.with_suffix(".command")
    # Claude Code marks the processes it starts; a session that inherits the marker doesn't save its transcript, so
    # cos could never link it. Clear them in case cos itself was started from a Claude session.
    script.write_text("#!/bin/bash\n"
                      "for v in $(compgen -e | grep -E '^(CLAUDECODE|CLAUDE_CODE_|CLAUDE_PID|CLAUDE_EFFORT|CLAUDE_AGENT_)'); "
                      "do unset \"$v\"; done\n"
                      f"cd {shlex.quote(str(cwd))} || exit 1\n{command}\nexec \"${{SHELL:-/bin/zsh}}\" -l\n")
    script.chmod(0o700)
    if not _app_running(config.TERMINAL_APP):
        subprocess.run(["open", "-g", "-j", "-a", config.TERMINAL_APP], capture_output=True)
        time.sleep(TERMINAL_WARMUP_S)
    r = subprocess.run(["open", "-a", config.TERMINAL_APP, str(script)], capture_output=True, text=True)
    if r.returncode != 0:
        raise LaunchError(f"couldn't open {config.TERMINAL_APP}: {r.stderr.strip() or 'open failed'}")


def _app_running(app: str) -> bool:
    r = subprocess.run(["osascript", "-e", f'application "{app}" is running'], capture_output=True, text=True)
    return r.stdout.strip() == "true"


def _open(url: str) -> None:
    r = subprocess.run(["open", url], capture_output=True, text=True)
    if r.returncode != 0:
        raise LaunchError(f"couldn't open the Claude app: {r.stderr.strip() or 'open failed'}")


SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _git_top(path: Path) -> Path | None:
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def resume(conn, session_id: str) -> str:
    """Open a Claude Code session again; returns where it opened. Claude keeps a session's transcript under the folder
    it ran in, so `claude --resume` must run from that same folder."""
    if not SESSION_ID.match(session_id or ""):
        raise ValueError("not a Claude session id")
    row = conn.execute("SELECT cwd, title FROM claude_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(f"no session {session_id}")
    cwd = Path(row["cwd"] or "")
    if not row["cwd"] or not cwd.is_dir():
        raise ValueError(f"the session's folder {row['cwd']!r} no longer exists")
    if config.LAUNCHER == "desktop":
        local_id = desktop_session(session_id)
        if local_id:
            _open(f"claude://code/continue?{urlencode({'session': local_id})}")
            return "the Claude app"
    if config.LAUNCHER == "orca":
        _resume_orca(cwd, session_id, row["title"])
        return "Orca"
    # A session the desktop app never saw (started in Orca or a terminal) resumes in a terminal.
    _run_in_terminal(_launch_dir() / f"resume-{session_id[:8]}-{int(time.time())}", cwd,
                     shlex.join([_claude_bin(), "--resume", session_id]))
    return config.TERMINAL_APP


DESKTOP_SESSIONS = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"


def desktop_session(cli_session_id: str) -> str | None:
    """The desktop app's own id (local_...) for a session it ran, found from the Claude Code session id it records."""
    for f in DESKTOP_SESSIONS.glob("*/*/local_*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if cli_session_id in (d.get("cliSessionId"), d.get("lastAssistantCliSessionId")):
            return d.get("sessionId") or f.stem
    return None


def _resume_orca(cwd: Path, session_id: str, title: str | None) -> None:
    """A terminal tab in the Orca workspace holding the folder: an Orca worktree, or a repo's main checkout, which
    is added to Orca the first time if it isn't there yet."""
    workspace = _git_top(cwd) or cwd
    command = (f"cd {shlex.quote(str(cwd))} && " if cwd != workspace else "") + f"claude --resume {session_id}"
    args = ("terminal", "create", "--worktree", f"path:{workspace}", "--title", str(title or "Claude")[:60],
            "--command", command, "--focus")
    try:
        _orca(*args)
    except LaunchError as e:
        if "selector_not_found" not in str(e):
            raise
        ensure_registered(workspace)
        _orca(*args)


def link_soon() -> None:
    """Index transcripts a few times after a launch, so the new session shows on its item within minutes
    instead of on the next tick."""
    from . import sessions

    def work():
        for delay in LINK_RETRIES_S:
            time.sleep(delay)
            conn = db.open_db()
            try:
                sessions.index(conn)
            except Exception as e:  # the tick indexes too; this is only to make the link show up sooner
                print(f"launch: indexing after launch failed: {e}")
            finally:
                conn.close()

    threading.Thread(target=work, daemon=True).start()
