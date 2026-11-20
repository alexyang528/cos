from dataclasses import dataclass


@dataclass(frozen=True)
class Lead:
    name: str
    source: str
    model: str
    budget_usd: float


LEADS: dict[str, Lead] = {
    lead.name: lead
    for lead in (
        Lead("slack", "slack", "sonnet", 3.00),
        Lead("email", "email", "sonnet", 3.00),
        Lead("notion", "notion", "sonnet", 3.00),
        Lead("switchboard", "switchboard", "sonnet", 3.00),
        Lead("github", "github", "sonnet", 3.00),
        Lead("granola", "granola", "sonnet", 3.00),
    )
}

# Leads are split by surface, so routing is a fixed mapping; the intern only decides route vs skip.
SOURCE_TO_LEAD = {lead.source: lead.name for lead in LEADS.values()}

# The chief of staff can own items too (orphans, routing audits) without being a surface lead.
QUEUE_OWNERS = set(LEADS) | {"chief", "todo", "workstream"}

# Every workstream's items are owned by "workstream"; items.workstream_id says which one. Its runs and rules
# use this per-workstream agent name.
def ws_agent(slug: str) -> str:
    return f"ws:{slug}"
