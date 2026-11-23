"""Tool policy for every headless `claude -p` pass.

Defense in depth, in order:
1. --setting-sources project: ignore ~/.claude/settings.json (it has defaultMode=auto and broad MCP allows).
2. --permission-mode dontAsk: any tool not in --allowedTools is refused, never prompted.
3. --disallowedTools: explicit denies, which win over any allow from any source.
Work passes get read tools only. The execute pass gets exactly the write tools for one approved item's action.
"""

from dataclasses import dataclass, field

from . import config
from .leads import LEADS

GMAIL = "mcp__claude_ai_Gmail__"
SLACK = "mcp__claude_ai_Slack__"
GH = "mcp__claude_ai_Github__"
NOTION = "mcp__claude_ai_Notion__"
SB = "mcp__claude_ai_Switchboard__"
CAL = "mcp__claude_ai_Google_Calendar__"
GRANOLA = "mcp__claude_ai_Granola__"
LINEAR = "mcp__claude_ai_Linear__"
PRODDB = "mcp__claude_ai_Check_Production_Database_MCP__"
SNOWFLAKE = "mcp__claude_ai_Check_Snowflake_MCP__"
TEMPORAL = "mcp__claude_ai_Check_Temporal_MCP__"
ZENDESK = "mcp__claude_ai_Check_Zendesk_MCP__"

READ_TOOLS: dict[str, list[str]] = {
    # ToolSearch only loads deferred tool schemas; connector tools are unusable in -p without it.
    "local": ["Read", "Grep", "Glob", "ToolSearch"],
    "slack": [SLACK + t for t in (
        "slack_read_channel", "slack_read_thread", "slack_read_user_profile",
        "slack_search_channels", "slack_search_users", "slack_search_public",
        "slack_search_public_and_private", "slack_read_canvas",
    )],
    "email": [GMAIL + t for t in (
        "get_message", "get_thread", "search_threads", "list_labels", "list_drafts", "get_draft",
    )],
    "github": [GH + t for t in (
        "get_me", "pull_request_read", "issue_read", "list_pull_requests", "search_pull_requests",
        "search_issues", "search_code", "get_file_contents", "list_commits", "get_commit", "list_issues",
    )] + [
        "Bash(gh pr view *)", "Bash(gh pr diff *)", "Bash(gh pr checks *)", "Bash(gh pr list *)",
        "Bash(gh issue view *)", "Bash(gh search *)", "Bash(git log *)", "Bash(git diff *)", "Bash(git show *)",
    ],
    "notion": [NOTION + t for t in (
        "notion-search", "notion-fetch", "notion-get-comments", "notion-get-users", "notion-get-self",
        "notion-list-recent-pages", "notion-list-page-backlinks", "notion-query-data-sources",
    )],
    "switchboard": [SB + t for t in ("whoami", "list_plans", "get_plan", "list_previews", "get_preview")],
    "calendar": [CAL + t for t in ("list_calendars", "list_events", "get_event", "search_events")],
    "granola": [GRANOLA + t for t in (
        "search_meetings", "get_meetings", "search_transcripts", "get_meeting_transcript", "get_account_info",
    )],
    "linear": [LINEAR + t for t in ("whoami", "get_issue", "list_issues", "list_comments", "get_project")],
    # Read-only work data for lead research (approved 2026-11-16). Zendesk writes are globally denied below.
    "research": [
        *(PRODDB + t for t in (
            "query_core", "query_config", "query_resources", "query_ledger", "query_checkbook",
            "get_api_log", "search_api_logs",
        )),
        *(SNOWFLAKE + t for t in (
            "snowflake_query", "snowflake_describe_table", "snowflake_list_databases",
            "snowflake_list_schemas", "snowflake_list_tables",
        )),
        *(TEMPORAL + t for t in (
            "describe_workflow", "get_workflow_history", "get_activity_details", "list_workflows",
            "compute_workflow_id", "list_search_attributes",
        )),
        *(ZENDESK + t for t in (
            "get_ticket", "get_tickets", "get_ticket_comments", "get_view_tickets", "get_knowledge_base",
            "list_ticket_fields", "list_ticket_field_options",
        )),
    ],
}

# Never allowed, in any pass, approved or not.
GLOBAL_DENY: list[str] = [
    *(GMAIL + t for t in (
        "send_message", "reply", "forward", "trash_message", "trash_thread", "delete_draft", "delete_label",
        "mark_message_spam", "mark_thread_spam", "update_label", "create_label",
    )),
    *(GH + t for t in (
        "merge_pull_request", "delete_repository", "delete_file", "create_repository", "fork_repository",
        "push_files", "create_or_update_file", "update_pull_request_branch", "create_branch",
    )),
    *(CAL + t for t in ("create_event", "update_event", "delete_event", "respond_to_event")),
    SLACK + "slack_schedule_message", SLACK + "slack_create_canvas", SLACK + "slack_update_canvas",
    SLACK + "slack_send_message_draft",
    *(ZENDESK + t for t in ("create_ticket", "create_ticket_comment", "update_ticket", "add_ticket_field_option")),
    *(NOTION + t for t in (
        "notion-delete-pages", "notion-delete-databases", "notion-move-pages", "notion-update-page",
        "notion-update-data-source", "notion-restore-version",
    )),
    *(SB + t for t in (
        "launch_preview", "withdraw_approval", "mark_lane_out_of_scope",
        # Off by user decision 2026-11-17: a revision replaces the whole plan body.
        "save_revision",
        "create_plan", "delegate_lane", "take_lane_locally", "release_review", "append_follow_up_lanes",
        "attach_pull_request", "update_plan_settings", "stop_preview", "refresh_preview", "resolve_thread",
    )),
    "Bash(git push *)", "Bash(gh pr merge *)", "Bash(gh api *)", "Bash(rm *)",
]

# Content to send is always items.body; the action JSON only says where it goes.
ACTION_WRITE_TOOLS: dict[str, list[str]] = {
    "gmail_draft": [GMAIL + "create_draft"],
    "slack_reply": [SLACK + "slack_send_message"],
    "github_comment": [GH + "add_issue_comment", GH + "add_reply_to_pull_request_comment"],
    "github_review": [GH + "pull_request_review_write", GH + "add_comment_to_pending_review"],
    "notion_comment": [NOTION + "notion-create-comment"],
    "notion_suggest_edit": [NOTION + "notion-create-suggested-edit"],
    # Comment, then hand the plan back, in that order (user decision 2026-11-17).
    "switchboard_reply": [SB + "comment", SB + "send_to_switchboard"],
    "switchboard_approve_phase": [SB + "approve_phase"],
    # Irreversible; only ever proposed when I instruct it (user decision 2026-11-17).
    "switchboard_close_plan": [SB + "close_plan"],
}
ACTION_SURFACE = {
    "switchboard_close_plan": "switchboard",
    "gmail_draft": "email", "slack_reply": "slack", "github_comment": "github", "github_review": "github",
    "notion_comment": "notion", "notion_suggest_edit": "notion", "switchboard_reply": "switchboard",
    "switchboard_approve_phase": "switchboard",
}
ALL_ACTION_WRITES = sorted({t for tools in ACTION_WRITE_TOOLS.values() for t in tools})
LOCAL_WRITES = ["Edit", "Write", "NotebookEdit", "Bash"]

# A lead may only propose actions on its own surface. Leads don't draft replies (user decision 2026-11-17):
# open-ended work gets an agent prompt I launch as a Claude Code session, and I answer people myself. Only the
# yes/no Switchboard actions remain. The reply action types stay in ACTION_WRITE_TOOLS so items queued before
# that change can still be sent.
LEAD_ACTIONS: dict[str, set[str]] = {
    "slack": set(),
    "email": set(),
    "github": set(),
    "notion": set(),
    "switchboard": {"switchboard_approve_phase", "switchboard_close_plan"},
    "granola": set(),
    "workstream": {"switchboard_approve_phase", "switchboard_close_plan"},
}

# Actions whose item is a yes/no decision rather than text to send.
DECISION_ACTIONS = {"switchboard_approve_phase", "switchboard_close_plan"}

WORK_SURFACES = [
    "local", "slack", "email", "github", "notion", "switchboard", "calendar", "granola", "linear", "research",
]


@dataclass
class PassSpec:
    name: str
    model: str
    budget_usd: float
    allowed: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    settings: str | None = None


def _reads(*surfaces: str) -> list[str]:
    return [t for s in surfaces for t in READ_TOOLS[s]]


def _work_deny() -> list[str]:
    # No subagents: a background one ends the session on a later turn, so the final message is prose instead of
    # the JSON answer and num_turns no longer shows whether the pass read anything.
    return sorted(set(GLOBAL_DENY) | set(ALL_ACTION_WRITES) | {"Edit", "Write", "NotebookEdit", "Agent", "Task"})


def pass_spec(pass_name: str, lead: str | None = None, action: str | None = None) -> PassSpec:
    if pass_name == "scan":
        if lead == "workstreams":
            return PassSpec("scan:workstreams", "haiku", 1.00, ["ToolSearch", *_reads("slack", "switchboard")],
                            _work_deny())
        if lead not in ("slack", "switchboard", "granola", "notion", "email"):
            raise ValueError(f"no connector scan for {lead!r}")
        return PassSpec(f"scan:{lead}", "haiku", 1.00, ["ToolSearch", *_reads(lead)], _work_deny())
    if pass_name == "intern":
        return PassSpec("intern", "haiku", 0.50, ["ToolSearch", *_reads("slack", "email")], _work_deny())
    if pass_name == "todo":
        return PassSpec("todo", "haiku", 0.50, _reads("local", "linear", "notion", "calendar"), _work_deny())
    if pass_name == "brain":
        return PassSpec("brain", "opus", 1.00, _reads("local"), _work_deny())
    if pass_name == "link":
        return PassSpec("link", "haiku", 1.50, _reads(*WORK_SURFACES), _work_deny())
    if pass_name == "chief":
        return PassSpec("chief", "opus", 2.00, _reads(*WORK_SURFACES), _work_deny())
    if pass_name == "lead":
        if lead not in LEADS:
            raise ValueError(f"unknown lead {lead!r}; expected one of {sorted(LEADS)}")
        cfg = LEADS[lead]
        return PassSpec(f"lead:{lead}", cfg.model, cfg.budget_usd, _reads(*WORK_SURFACES), _work_deny())
    if pass_name == "workstream":
        if not lead:
            raise ValueError("a workstream pass needs the workstream's slug")
        return PassSpec(f"lead:ws:{lead}", config.WS_MODEL, config.WS_LEAD_BUDGET_USD, _reads(*WORK_SURFACES),
                        _work_deny())
    if pass_name == "ws_sessions":
        return PassSpec("lead:ws-sessions", config.WS_MODEL, config.WS_SESSIONS_BUDGET_USD, _reads(*WORK_SURFACES),
                        _work_deny())
    if pass_name == "execute":
        if action not in ACTION_WRITE_TOOLS:
            raise ValueError(f"unknown action {action!r}; expected one of {sorted(ACTION_WRITE_TOOLS)}")
        writes = ACTION_WRITE_TOOLS[action]
        denied = (set(GLOBAL_DENY) | set(ALL_ACTION_WRITES) | set(LOCAL_WRITES)) - set(writes)
        return PassSpec(f"execute:{action}", "sonnet", 0.50,
                        _reads("local", ACTION_SURFACE[action]) + writes, sorted(denied))
    raise ValueError(f"unknown pass {pass_name!r}")


def build_command(spec: PassSpec, resume: str | None = None) -> list[str]:
    # Prompt goes on stdin. Tool lists come last because they are variadic flags.
    return [
        "claude", "-p",
        *(["--resume", resume] if resume else []),
        *(["--settings", spec.settings] if spec.settings else []),
        "--model", spec.model,
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--setting-sources", "project",
        "--max-budget-usd", f"{spec.budget_usd:.2f}",
        "--allowedTools", *spec.allowed,
        "--disallowedTools", *spec.denied,
    ]


WRITE_VERBS = (
    "send", "create", "update", "delete", "save", "merge", "approve", "write", "push", "trash", "reply",
    "forward", "label", "unlabel", "mark", "schedule", "add", "resolve", "close", "launch", "stop", "share",
    "move", "restore", "apply", "submit", "fork", "withdraw", "append", "delegate", "take", "release",
    "attach", "refresh", "set", "upload", "duplicate", "convert", "spawn", "comment",
)


def looks_like_write(tool: str) -> bool:
    if tool in LOCAL_WRITES:
        return True
    if not tool.startswith("mcp__"):
        return False
    op = tool.rsplit("__", 1)[-1].removeprefix("notion-").removeprefix("slack_")
    return op.split("_")[0].split("-")[0] in WRITE_VERBS


def check_all() -> list[str]:
    problems: list[str] = []
    global_deny = set(GLOBAL_DENY)
    specs = [pass_spec("scan", lead="slack"), pass_spec("scan", lead="switchboard"), pass_spec("scan", lead="granola"),
             pass_spec("scan", lead="notion"), pass_spec("scan", lead="email"),
             pass_spec("scan", lead="workstreams"), pass_spec("workstream", lead="example"),
             pass_spec("ws_sessions"),
             pass_spec("intern"), pass_spec("todo"), pass_spec("chief"), pass_spec("brain"), pass_spec("link")]
    specs += [pass_spec("lead", lead=name) for name in LEADS]
    for spec in specs:
        allowed = set(spec.allowed)
        for tool in sorted(allowed & (global_deny | set(ALL_ACTION_WRITES))):
            problems.append(f"{spec.name}: allows write tool {tool}")
        for tool in sorted(t for t in allowed if looks_like_write(t)):
            problems.append(f"{spec.name}: allowed tool {tool} looks like a write")
        for tool in sorted((global_deny | set(ALL_ACTION_WRITES)) - set(spec.denied)):
            problems.append(f"{spec.name}: does not explicitly deny {tool}")
    for action, writes in ACTION_WRITE_TOOLS.items():
        spec = pass_spec("execute", action=action)
        allowed = set(spec.allowed)
        extra_writes = {t for t in allowed if looks_like_write(t) or t in ALL_ACTION_WRITES} - set(writes)
        if extra_writes:
            problems.append(f"{spec.name}: allows extra writes {sorted(extra_writes)}")
        if allowed & global_deny:
            problems.append(f"{spec.name}: allows globally denied {sorted(allowed & global_deny)}")
        if not global_deny <= set(spec.denied):
            problems.append(f"{spec.name}: missing global denies")
        if set(writes) & global_deny:
            problems.append(f"{spec.name}: action write tool is globally denied")
    return problems
