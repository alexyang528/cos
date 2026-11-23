import tomllib
from pathlib import Path

DAILY_BUDGET_USD = 25.0
RUN_TIMEOUT_S = 900

# Everything personal lives in me.toml (gitignored); me.example.toml documents the fields.
ME_FILE = Path(__file__).resolve().parent.parent / "me.toml"
if not ME_FILE.exists():
    raise SystemExit(f"missing {ME_FILE}: copy me.example.toml to me.toml and fill it in")
_me = tomllib.loads(ME_FILE.read_text())

ME_NAME: str = _me["name"]
ME_FIRST = ME_NAME.split()[0]
ME_EMAIL: str = _me["email"]
ME_ABOUT = " ".join(_me["about"].split())
GITHUB_REPO: str = _me.get("github_repo", "")
SLACK_USER_ID: str = _me["slack"]["user_id"]
# Slack apps I invoke in threads (the Claude bot). Replies after my @-mention of one are to the bot, not to me.
SLACK_BOT_IDS = tuple(_me["slack"].get("bot_ids", []))
# The workspace's team ID (T...), for opening Slack links straight in the Slack app. Optional: when unset it is read
# from the Slack app's own state for the link's workspace.
SLACK_TEAM_ID: str = _me["slack"].get("team_id", "")

# First scan looks back this far; later scans resume from the stored cursor.
SLACK_FIRST_SCAN_LOOKBACK_H = 24
# Scheduling. The tick scans, triages and drafts; it never sends. Sending is only ever a per-item click.
# The tick runs at this minute past each working hour: 8:00, 9:00 ... 19:00 on work days. launchd only starts it
# then, and a tick started any other way (e.g. after the Mac wakes) exits outside these hours.
TICK_MINUTE = 0
WORK_HOURS = (8, 19)          # local time, first and last hour a tick runs (inclusive)
WORK_DAYS = (0, 1, 2, 3, 4)   # Monday=0
DAILY_AT = (18, 30)           # chief of staff, brain distill, cleanup
WEB_PORT = 8787
SCAN_SURFACES = ("slack", "switchboard", "granola", "notion", "email", "github")
ENABLED_LEADS = ("slack", "switchboard", "granola", "notion", "email", "github")

# Cleanup. Worktrees are only ever removed when clean, pushed and idle.
LOG_RETENTION_DAYS = 30
SNAPSHOT_RETENTION_DAYS = 14
BACKUPS_KEEP = 10
WORKTREE_IDLE_DAYS = 7
# Overlap between scans; duplicates are dropped by UNIQUE(source, source_id).
SCAN_OVERLAP_S = 600

# Granola: each scan looks back this far (events dedupe by meeting id), and a note is left alone until it
# has been unchanged this long, so the lead reads the finished AI summary rather than one still generating.
GRANOLA_LOOKBACK_DAYS = 2
GRANOLA_SETTLE_MIN = 10

# GitHub: review requests and reviews of my PRs older than this never reach the queue, so the first scan doesn't
# file every stale request. Requests to my teams are ignored; only ones naming me count.
GITHUB_LOOKBACK_DAYS = 14

INTERN_BATCH = 40
LEAD_BATCH = 15

# Each lead re-reads the source of its waiting items to find ones already finished (they become likely_done, for me
# to confirm). An item is re-read at most once per REVIEW_EVERY_H, and one review run takes at most REVIEW_BATCH.
REVIEW_EVERY_H = 8
REVIEW_BATCH = 10

# A waiting item I haven't touched in this many days (no decision, launch, note, or work in a linked Claude session)
# expires; the brain and the chief read that as a sign it wasn't worth raising. A likely-done item closes as done
# this long after the lead marked it, unless I answer. The pane warns in the last few days.
EXPIRE_AFTER_DAYS = 14
EXPIRE_WARN_DAYS = 3

# Linking Claude Code sessions to queue items: only sessions active this recently are candidates, and one
# linking pass sees at most this many (most recent first).
SESSION_LOOKBACK_DAYS = 30
SESSION_LINK_BATCH = 30
# A new item is first checked against this recent window (plus older sessions that link its source).
SESSION_FIRST_CHECK_DAYS = 7

# Workstreams. Their leads share the daily cap but may spend at most WS_DAILY_BUDGET_USD of it, and run after
# the surface leads in each tick so the queue is never starved.
WS_MODEL = "sonnet"
WS_LEAD_BUDGET_USD = 3.00
WS_DAILY_BUDGET_USD = 10.00
# The first tick at or after this hour on a work day writes each workstream's morning brief.
WS_BRIEF_HOUR = 8
# A newly watched Slack source starts this far back.
WS_FIRST_SCAN_LOOKBACK_H = 72
# Sources per activity scan (least recently scanned first), messages kept per source, and activity rows per lead run.
WS_SCAN_BATCH = 12
WS_SCAN_MESSAGES_PER_SOURCE = 60
WS_ACTIVITY_BATCH = 80
# One pass links new Claude Code sessions to every workstream and its items at once, so cost doesn't grow
# with the number of workstreams.
WS_SESSIONS_BUDGET_USD = 2.00

# Agent prompts. A lead drafts one for open-ended work; I launch it as a Claude Code session (LAUNCHER below), in one
# of these folders (the lead picks; I can change it).
SESSION_WORKDIRS = {w["name"]: w["path"] for w in _me["workdirs"]}
WORKDIR_USE = {w["name"]: w.get("use_for", "") for w in _me["workdirs"]}
DEFAULT_WORKDIR = next((w["name"] for w in _me["workdirs"] if w.get("default")), _me["workdirs"][0]["name"])
# Folders where a launch with no worktree choice (the CLI) starts in a new worktree, its own branch and checkout
# (Orca or terminal launchers only). The web app's checkbox defaults to on. cos never removes these worktrees.
WORKTREE_BY_DEFAULT = set(SESSION_WORKDIRS)
# Where agent sessions open: the Claude desktop app, a terminal window, or Orca (see me.example.toml).
LAUNCHERS = {"desktop": "the Claude app", "terminal": "a terminal", "orca": "Orca"}
LAUNCHER: str = _me.get("launcher", "desktop")
if LAUNCHER not in LAUNCHERS:
    raise SystemExit(f"me.toml: launcher must be one of {', '.join(LAUNCHERS)}, not {LAUNCHER!r}")
TERMINAL_APP: str = _me.get("terminal_app", "Terminal")
# The web app runs under launchd, whose PATH doesn't include Orca's bundled CLI.
ORCA_BIN = "/Applications/Orca.app/Contents/Resources/bin/orca"

# Notion has no inbox, so the scan looks in two places: tickets where I'm the DRI (if I set a ticket database), and
# pages that name me (search matches my name, not true @-mentions; the intern keeps only ones asking for my input).
NOTION_USER_ID: str = _me["notion"]["user_id"]
NOTION_INBOUND_DS: str = _me["notion"].get("tickets_data_source", "")
# Ticket statuses that need nothing more from the DRI. Known Issue is parked, so it counts as closed here.
NOTION_TICKET_CLOSED = ("Done", "Duplicate", "Won't Do", "De-escalated", "Moved to Incident", "Triaged to Backlog",
                        "Known Issue")
NOTION_FIRST_SCAN_LOOKBACK_D = 7

# Gmail: direct mail from people and partner Zendesk ticket updates, both judged by the intern.
# Calendar traffic, automated senders and the non-Primary tabs are filtered out in scan_email before any model call.
GMAIL_FIRST_SCAN_LOOKBACK_D = 1
