from dataclasses import dataclass

NOW = "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
# Every items column as of v12, for copying the table when it is rebuilt.
_ITEM_COLS_V12 = (
    "id, lead, kind, title, body, original_body, action, priority, status, feedback, url, route_id, "
    "created_by_run_id, exec_run_id, exec_error, created_at, updated_at, decided_at, executed_at, rationale, "
    "summary, sessions_checked_at, workstream_id, goal_id, filed_by, prompt, original_prompt, workdir, "
    "launched_at, launch_error, worktree_path, worktree_branch, ask, requester, reference, timeline"
)
_ITEM_COLS_V13 = _ITEM_COLS_V12 + ", done_reason, checked_at, reopened_at"


def _items_v13(statuses: str) -> str:
    """The items table as of v13, with the given status CHECK list. v13 and v14 differ only in the statuses."""
    return f"""
            CREATE TABLE items_new (
                id INTEGER PRIMARY KEY,
                lead TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('reply','pr','decision','fyi','task')),
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                original_body TEXT,
                action TEXT,
                priority INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 0 AND 2),
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                    {statuses})),
                feedback TEXT,
                url TEXT,
                route_id INTEGER REFERENCES routes(id),
                created_by_run_id INTEGER REFERENCES runs(id),
                exec_run_id INTEGER REFERENCES runs(id),
                exec_error TEXT,
                created_at TEXT NOT NULL DEFAULT {NOW},
                updated_at TEXT NOT NULL DEFAULT {NOW},
                decided_at TEXT,
                executed_at TEXT,
                rationale TEXT,
                summary TEXT,
                sessions_checked_at TEXT,
                workstream_id INTEGER REFERENCES workstreams(id),
                goal_id INTEGER REFERENCES ws_goals(id),
                filed_by TEXT,
                prompt TEXT,
                original_prompt TEXT,
                workdir TEXT,
                launched_at TEXT,
                launch_error TEXT,
                worktree_path TEXT,
                worktree_branch TEXT,
                ask TEXT,
                requester TEXT,
                reference TEXT,
                timeline TEXT,
                done_reason TEXT,
                checked_at TEXT,
                reopened_at TEXT
            )"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    # Rebuilding a table other tables reference needs foreign keys off for the swap (SQLite's documented
    # procedure); the runner checks every reference again before committing.
    foreign_keys_off: bool = False


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "initial schema",
        (
            f"""
            CREATE TABLE kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT {NOW}
            )""",
            f"""
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY,
                job TEXT NOT NULL CHECK (job IN ('scan','intern','todo','lead','chief','execute','cleanup')),
                lead TEXT,
                item_id INTEGER,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','ok','error','dry_run')),
                started_at TEXT NOT NULL DEFAULT {NOW},
                finished_at TEXT,
                cost_usd REAL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                num_turns INTEGER,
                session_id TEXT,
                error TEXT,
                log_path TEXT
            )""",
            "CREATE INDEX runs_by_job ON runs(job, lead, started_at)",
            f"""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL CHECK (source IN ('slack','email','github','notion','switchboard')),
                source_id TEXT NOT NULL,
                kind TEXT,
                url TEXT,
                author TEXT,
                text TEXT,
                occurred_at TEXT,
                payload TEXT NOT NULL DEFAULT '{{}}',
                first_seen_at TEXT NOT NULL DEFAULT {NOW},
                UNIQUE (source, source_id)
            )""",
            # UNIQUE(event_id) is what guarantees an event is routed at most once.
            f"""
            CREATE TABLE routes (
                id INTEGER PRIMARY KEY,
                event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
                decision TEXT NOT NULL CHECK (decision IN ('route','skip')),
                lead TEXT,
                summary TEXT,
                url TEXT,
                reason TEXT,
                run_id INTEGER REFERENCES runs(id),
                created_at TEXT NOT NULL DEFAULT {NOW},
                picked_up_at TEXT,
                picked_up_run_id INTEGER REFERENCES runs(id),
                CHECK (decision = 'skip' OR lead IS NOT NULL)
            )""",
            "CREATE INDEX routes_inbox ON routes(lead, picked_up_at)",
            f"""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                lead TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('reply','pr','decision','fyi','task')),
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                original_body TEXT,
                action TEXT,
                priority INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 0 AND 3),
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                    'pending','approved','rejected','changes_requested',
                    'executing','done','failed','dismissed')),
                feedback TEXT,
                url TEXT,
                route_id INTEGER REFERENCES routes(id),
                created_by_run_id INTEGER REFERENCES runs(id),
                exec_run_id INTEGER REFERENCES runs(id),
                exec_error TEXT,
                created_at TEXT NOT NULL DEFAULT {NOW},
                updated_at TEXT NOT NULL DEFAULT {NOW},
                decided_at TEXT,
                executed_at TEXT
            )""",
            "CREATE INDEX items_queue ON items(status, priority DESC, created_at)",
            f"""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                system TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT,
                due TEXT,
                url TEXT,
                synced_at TEXT NOT NULL DEFAULT {NOW},
                UNIQUE (system, external_id)
            )""",
            f"""
            CREATE TABLE rules (
                id INTEGER PRIMARY KEY,
                agent TEXT NOT NULL,
                text TEXT NOT NULL,
                why TEXT NOT NULL,
                source TEXT NOT NULL CHECK (source IN ('user','agent','chief')),
                source_ref TEXT,
                status TEXT NOT NULL CHECK (status IN ('provisional','confirmed','expired','retired')),
                created_at TEXT NOT NULL DEFAULT {NOW},
                expires_at TEXT,
                confirmed_at TEXT,
                confirmed_by TEXT,
                CHECK (status != 'provisional' OR expires_at IS NOT NULL)
            )""",
        ),
    ),
    Migration(
        2,
        "denials, item rationale, lead outcome per route",
        (
            "ALTER TABLE runs ADD COLUMN denials TEXT",
            "ALTER TABLE items ADD COLUMN rationale TEXT",
            "ALTER TABLE routes ADD COLUMN lead_outcome TEXT",
        ),
    ),
    # SQLite can't alter a CHECK constraint, so the table is rebuilt. Old scale 0..3 (3 most urgent) becomes
    # 0..2 (0 most urgent): 3 -> 0, 2/1 -> 1, and every fyi -> 2.
    Migration(
        3,
        "priority: P0 most urgent, P2 = FYI",
        (
            f"""
            CREATE TABLE items_new (
                id INTEGER PRIMARY KEY,
                lead TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('reply','pr','decision','fyi','task')),
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                original_body TEXT,
                action TEXT,
                priority INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 0 AND 2),
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                    'pending','approved','rejected','changes_requested',
                    'executing','done','failed','dismissed')),
                feedback TEXT,
                url TEXT,
                route_id INTEGER REFERENCES routes(id),
                created_by_run_id INTEGER REFERENCES runs(id),
                exec_run_id INTEGER REFERENCES runs(id),
                exec_error TEXT,
                created_at TEXT NOT NULL DEFAULT {NOW},
                updated_at TEXT NOT NULL DEFAULT {NOW},
                decided_at TEXT,
                executed_at TEXT,
                rationale TEXT
            )""",
            """
            INSERT INTO items_new (id, lead, kind, title, body, original_body, action, priority, status, feedback,
                url, route_id, created_by_run_id, exec_run_id, exec_error, created_at, updated_at, decided_at,
                executed_at, rationale)
            SELECT id, lead, kind, title, body, original_body, action,
                CASE WHEN kind = 'fyi' THEN 2 WHEN priority = 3 THEN 0 ELSE 1 END,
                status, feedback, url, route_id, created_by_run_id, exec_run_id, exec_error, created_at, updated_at,
                decided_at, executed_at, rationale
            FROM items""",
            "DROP TABLE items",
            "ALTER TABLE items_new RENAME TO items",
            "CREATE INDEX items_queue ON items(status, priority, created_at)",
        ),
    ),
    Migration(
        4,
        "item summary of what's pending on me",
        (
            "ALTER TABLE items ADD COLUMN summary TEXT",
            # Items created before this get the intern's one-liner as a starting point.
            "UPDATE items SET summary = (SELECT r.summary FROM routes r WHERE r.id = items.route_id) "
            "WHERE summary IS NULL AND route_id IS NOT NULL",
        ),
    ),
    Migration(
        5,
        "Claude Code session index and item links",
        (
            # One row per transcript file. size + mtime decide whether it needs re-reading; skipped rows
            # (headless runs of this system, empty sessions) are kept so they aren't re-read every tick.
            f"""
            CREATE TABLE claude_sessions (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                skipped INTEGER NOT NULL DEFAULT 0,
                cwd TEXT,
                entrypoint TEXT,
                git_branch TEXT,
                title TEXT,
                started_at TEXT,
                last_at TEXT,
                prompt_count INTEGER NOT NULL DEFAULT 0,
                opening_prompts TEXT NOT NULL DEFAULT '[]',
                prs TEXT NOT NULL DEFAULT '[]',
                refs TEXT NOT NULL DEFAULT '[]',
                urls TEXT NOT NULL DEFAULT '[]',
                digest_path TEXT,
                indexed_at TEXT NOT NULL DEFAULT {NOW}
            )""",
            "CREATE INDEX claude_sessions_recent ON claude_sessions(skipped, last_at)",
            # removed_at marks a link I took off; the lead never proposes that pair again.
            f"""
            CREATE TABLE item_sessions (
                item_id INTEGER NOT NULL REFERENCES items(id),
                session_id TEXT NOT NULL,
                why TEXT,
                run_id INTEGER REFERENCES runs(id),
                created_at TEXT NOT NULL DEFAULT {NOW},
                removed_at TEXT,
                PRIMARY KEY (item_id, session_id)
            )""",
            "ALTER TABLE items ADD COLUMN sessions_checked_at TEXT",
        ),
    ),
    # SQLite can't alter a CHECK constraint, so events is rebuilt to allow the granola source.
    Migration(
        6,
        "granola events",
        (
            f"""
            CREATE TABLE events_new (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL CHECK (source IN ('slack','email','github','notion','switchboard','granola')),
                source_id TEXT NOT NULL,
                kind TEXT,
                url TEXT,
                author TEXT,
                text TEXT,
                occurred_at TEXT,
                payload TEXT NOT NULL DEFAULT '{{}}',
                first_seen_at TEXT NOT NULL DEFAULT {NOW},
                UNIQUE (source, source_id)
            )""",
            "INSERT INTO events_new SELECT id, source, source_id, kind, url, author, text, occurred_at, payload, "
            "first_seen_at FROM events",
            "DROP TABLE events",
            "ALTER TABLE events_new RENAME TO events",
        ),
        foreign_keys_off=True,
    ),
    Migration(
        7,
        "workstreams",
        (
            # description is mine (trusted); charter may be drafted by the lead (charter_by says who wrote it).
            f"""
            CREATE TABLE workstreams (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                charter TEXT NOT NULL DEFAULT '',
                charter_by TEXT CHECK (charter_by IN ('user','lead')),
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
                latest TEXT,
                latest_at TEXT,
                last_run_at TEXT,
                brief_on TEXT,
                created_at TEXT NOT NULL DEFAULT {NOW},
                updated_at TEXT NOT NULL DEFAULT {NOW},
                archived_at TEXT
            )""",
            # cursor: last Slack ts seen, or the Switchboard head revision. Moved only by Python after a good scan.
            f"""
            CREATE TABLE ws_sources (
                id INTEGER PRIMARY KEY,
                workstream_id INTEGER NOT NULL REFERENCES workstreams(id),
                kind TEXT NOT NULL CHECK (kind IN ('slack_channel','slack_thread','switchboard_plan')),
                ref TEXT NOT NULL,
                label TEXT,
                url TEXT,
                status TEXT NOT NULL CHECK (status IN ('proposed','active','removed','rejected')),
                added_by TEXT NOT NULL CHECK (added_by IN ('user','lead')),
                why TEXT,
                cursor TEXT,
                last_scanned_at TEXT,
                created_at TEXT NOT NULL DEFAULT {NOW},
                decided_at TEXT,
                UNIQUE (workstream_id, kind, ref)
            )""",
            # A source is watched by at most one workstream, so routing by source is unambiguous.
            "CREATE UNIQUE INDEX ws_sources_one_owner ON ws_sources(kind, ref) WHERE status = 'active'",
            f"""
            CREATE TABLE ws_goals (
                id INTEGER PRIMARY KEY,
                workstream_id INTEGER NOT NULL REFERENCES workstreams(id),
                week TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('proposed','open','done','dropped','carried','rejected')),
                health TEXT CHECK (health IN ('on_track','at_risk','blocked','looks_done')),
                progress TEXT,
                proposed_by TEXT NOT NULL CHECK (proposed_by IN ('user','lead')),
                carried_from INTEGER REFERENCES ws_goals(id),
                created_at TEXT NOT NULL DEFAULT {NOW},
                updated_at TEXT NOT NULL DEFAULT {NOW},
                closed_at TEXT
            )""",
            "CREATE INDEX ws_goals_by_week ON ws_goals(workstream_id, week)",
            f"""
            CREATE TABLE ws_updates (
                id INTEGER PRIMARY KEY,
                workstream_id INTEGER NOT NULL REFERENCES workstreams(id),
                run_id INTEGER REFERENCES runs(id),
                text TEXT NOT NULL,
                url TEXT,
                occurred_at TEXT,
                created_at TEXT NOT NULL DEFAULT {NOW}
            )""",
            "CREATE INDEX ws_updates_by_ws ON ws_updates(workstream_id, created_at)",
            f"""
            CREATE TABLE ws_instructions (
                id INTEGER PRIMARY KEY,
                workstream_id INTEGER NOT NULL REFERENCES workstreams(id),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT {NOW},
                picked_up_run_id INTEGER REFERENCES runs(id),
                picked_up_at TEXT,
                response TEXT
            )""",
            f"""
            CREATE TABLE ws_activity (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES ws_sources(id),
                ext_id TEXT NOT NULL,
                author TEXT,
                text TEXT,
                url TEXT,
                occurred_at TEXT,
                payload TEXT NOT NULL DEFAULT '{{}}',
                first_seen_at TEXT NOT NULL DEFAULT {NOW},
                processed_run_id INTEGER REFERENCES runs(id),
                UNIQUE (source_id, ext_id)
            )""",
            "CREATE INDEX ws_activity_unprocessed ON ws_activity(source_id, processed_run_id)",
            # filed_by: how the item or route got into its workstream (source, intern, lead, user).
            "ALTER TABLE items ADD COLUMN workstream_id INTEGER REFERENCES workstreams(id)",
            "ALTER TABLE items ADD COLUMN goal_id INTEGER REFERENCES ws_goals(id)",
            "ALTER TABLE items ADD COLUMN filed_by TEXT",
            "CREATE INDEX items_by_workstream ON items(workstream_id, status)",
            "ALTER TABLE routes ADD COLUMN workstream_id INTEGER REFERENCES workstreams(id)",
            "ALTER TABLE routes ADD COLUMN filed_by TEXT",
            "CREATE INDEX routes_ws_inbox ON routes(workstream_id, picked_up_at)",
        ),
    ),
    Migration(
        8,
        "Claude Code sessions linked to workstreams",
        (
            # removed_at marks a link I took off; the pass never proposes that pair again.
            f"""
            CREATE TABLE ws_sessions (
                workstream_id INTEGER NOT NULL REFERENCES workstreams(id),
                session_id TEXT NOT NULL,
                why TEXT,
                run_id INTEGER REFERENCES runs(id),
                created_at TEXT NOT NULL DEFAULT {NOW},
                removed_at TEXT,
                PRIMARY KEY (workstream_id, session_id)
            )""",
            "ALTER TABLE workstreams ADD COLUMN sessions_checked_at TEXT",
        ),
    ),
    Migration(
        9,
        "agent prompts on items, launched as Claude Code sessions",
        (
            # prompt: what the lead drafted for an agent, or my edit of it; original_prompt keeps the lead's
            # draft once I edit it, so the brain can learn from the difference. workdir is a config key.
            "ALTER TABLE items ADD COLUMN prompt TEXT",
            "ALTER TABLE items ADD COLUMN original_prompt TEXT",
            "ALTER TABLE items ADD COLUMN workdir TEXT",
            "ALTER TABLE items ADD COLUMN launched_at TEXT",
            "ALTER TABLE items ADD COLUMN launch_error TEXT",
        ),
    ),
    Migration(
        10,
        "open reply items become tasks without their drafted text",
        (
            # No suggested responses at all (user decision 2026-11-17), including ones queued before leads stopped
            # drafting them. The summary, link, evidence and any prompt stay; items with my note stay with the lead.
            "UPDATE items SET kind = 'task', action = NULL, body = '', "
            "status = CASE WHEN status IN ('approved','failed') THEN 'pending' ELSE status END, "
            f"updated_at = {NOW} "
            "WHERE kind = 'reply' AND status IN ('pending','changes_requested','approved','failed')",
        ),
    ),
    Migration(
        11,
        "the Orca worktree a session was started in",
        (
            "ALTER TABLE items ADD COLUMN worktree_path TEXT",
            "ALTER TABLE items ADD COLUMN worktree_branch TEXT",
        ),
    ),
    Migration(
        12,
        "what the item pane shows first: the ask, who asked, a reference, and a timeline",
        (
            # ask: the request in one line, as a question when it is one. timeline: JSON
            # [{"at": ISO time, "until": ISO time or null, "text": one line}], oldest first. All optional: the pane
            # falls back to the title and the summary's lines.
            "ALTER TABLE items ADD COLUMN ask TEXT",
            "ALTER TABLE items ADD COLUMN requester TEXT",
            "ALTER TABLE items ADD COLUMN reference TEXT",
            "ALTER TABLE items ADD COLUMN timeline TEXT",
        ),
    ),
    # SQLite can't alter a CHECK constraint, so items is rebuilt to allow likely_done: a lead re-read the source
    # and thinks the work is finished; it waits for me to confirm. done_reason is the lead's evidence, checked_at
    # when a lead last re-read the item's source, reopened_at when I last said it isn't done.
    Migration(
        13,
        "leads mark items they find already finished as likely done",
        (
            _items_v13("'pending','likely_done','approved','rejected','changes_requested','executing','done',"
                       "'failed','dismissed'"),
            f"INSERT INTO items_new ({_ITEM_COLS_V12}) SELECT {_ITEM_COLS_V12} FROM items",
            "DROP TABLE items",
            "ALTER TABLE items_new RENAME TO items",
            "CREATE INDEX items_queue ON items(status, priority, created_at)",
            "CREATE INDEX items_by_workstream ON items(workstream_id, status)",
        ),
        foreign_keys_off=True,
    ),
    # Rebuilt again for expired: an item I never acted on within config.EXPIRE_AFTER_DAYS. Not a decision of mine,
    # but the daily brain and chief read it as a sign the item wasn't worth raising.
    Migration(
        14,
        "items I never act on expire",
        (
            _items_v13("'pending','likely_done','approved','rejected','changes_requested','executing','done',"
                       "'failed','dismissed','expired'"),
            f"INSERT INTO items_new ({_ITEM_COLS_V13}) SELECT {_ITEM_COLS_V13} FROM items",
            "DROP TABLE items",
            "ALTER TABLE items_new RENAME TO items",
            "CREATE INDEX items_queue ON items(status, priority, created_at)",
            "CREATE INDEX items_by_workstream ON items(workstream_id, status)",
        ),
        foreign_keys_off=True,
    ),
    Migration(
        15,
        "when a lead marked an item likely done, so it closes on its own if I never answer",
        (
            "ALTER TABLE items ADD COLUMN likely_done_at TEXT",
            "UPDATE items SET likely_done_at = updated_at WHERE status = 'likely_done'",
        ),
    ),
    # Workstreams the chief of staff suggests from related queue items. Kept apart from workstreams until I approve
    # one, so nothing that scans, routes or runs leads ever sees a suggestion. item_ids is a JSON list.
    Migration(
        16,
        "workstreams the chief of staff suggests",
        (
            f"""
            CREATE TABLE ws_proposals (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                why TEXT NOT NULL DEFAULT '',
                item_ids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','rejected')),
                note TEXT,
                workstream_id INTEGER REFERENCES workstreams(id),
                run_id INTEGER REFERENCES runs(id),
                created_at TEXT NOT NULL DEFAULT {NOW},
                decided_at TEXT
            )""",
            "CREATE INDEX ws_proposals_status ON ws_proposals(status)",
        ),
    ),
    # What I tell the chief of staff directly. picked_up_at: a chief run took it and applied its actions;
    # finished_at: the agents it started have finished too. actions is a JSON list of what was actually done.
    Migration(
        17,
        "instructions to the chief of staff",
        (
            f"""
            CREATE TABLE chief_instructions (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT {NOW},
                picked_up_at TEXT,
                finished_at TEXT,
                run_id INTEGER REFERENCES runs(id),
                response TEXT,
                actions TEXT
            )""",
        ),
    ),
    # The chief's findings are about the system, not work for me, so they leave the queue for its own page.
    # needs_me: a code change, config or recurring failure only I can fix; otherwise the chief fixed it with the
    # provisional rules in rule_ids. Open findings already in the queue move over; their items close without a
    # decided_at, so the brain doesn't read the move as me dismissing them.
    Migration(
        18,
        "chief of staff findings move from the queue to system notes",
        (
            f"""
            CREATE TABLE chief_notes (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                needs_me INTEGER NOT NULL DEFAULT 1,
                rule_ids TEXT NOT NULL DEFAULT '[]',
                run_id INTEGER REFERENCES runs(id),
                created_at TEXT NOT NULL DEFAULT {NOW},
                dismissed_at TEXT
            )""",
            "INSERT INTO chief_notes(title, body, run_id, created_at) "
            "SELECT title, body, created_by_run_id, created_at FROM items WHERE lead = 'chief' "
            "AND status IN ('pending','likely_done','changes_requested','approved','failed') ORDER BY id",
            f"UPDATE items SET status = 'dismissed', feedback = 'moved to the chief of staff page', updated_at = {NOW} "
            "WHERE lead = 'chief' AND status IN ('pending','likely_done','changes_requested','approved','failed')",
        ),
    ),
    Migration(
        19,
        "the lead's priority, kept once I change it",
        # Set on my first change, like original_body: the lead's call, for the brain to compare with mine. While
        # it is set, a lead's revision keeps my priority.
        ("ALTER TABLE items ADD COLUMN original_priority INTEGER",),
    ),
)

assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1)), "migration versions must be contiguous"
