import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import config, paths, store
from .policy import PassSpec, build_command


class BudgetExceeded(Exception):
    pass


@dataclass
class AgentResult:
    run_id: int
    ok: bool
    data: dict[str, Any] | None
    text: str
    cost_usd: float
    error: str | None
    denials: list[dict] = field(default_factory=list)
    turns: int = 0


def daily_spend(conn: sqlite3.Connection) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE started_at >= strftime('%Y-%m-%dT00:00:00Z','now')"
    ).fetchone()[0]


def extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    candidates = [text]
    candidates += re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if "{" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for c in candidates:
        try:
            value = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


REPAIR_PROMPT = ("Your final message was not a valid JSON object (it may have an unbalanced bracket or an unescaped "
                 "quote). Reply with only the corrected JSON object, same content, no prose and no tool calls.")


def _repair_json(spec: PassSpec, session_id: str, stem) -> tuple[dict[str, Any] | None, float]:
    try:
        proc = subprocess.run(build_command(spec, resume=session_id), input=REPAIR_PROMPT, capture_output=True,
                              text=True, timeout=config.RUN_TIMEOUT_S, cwd=paths.ROOT)
    except subprocess.TimeoutExpired:
        return None, 0.0
    stem.with_suffix(".repair.json").write_text(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, 0.0
    cost = float(out.get("total_cost_usd") or 0.0)
    if out.get("is_error") or out.get("subtype") != "success":
        return None, cost
    return extract_json(out.get("result") or ""), cost


def preview(spec: PassSpec, prompt: str) -> str:
    import shlex
    return f"$ {shlex.join(build_command(spec))} <<'PROMPT'\n{prompt}\nPROMPT"


def run_agent(conn: sqlite3.Connection, spec: PassSpec, prompt: str, *, job: str, lead: str | None = None,
              item_id: int | None = None) -> AgentResult:
    spent = daily_spend(conn)
    # Charge the worst case up front so one run can't blow through the daily cap.
    if spent + spec.budget_usd > config.DAILY_BUDGET_USD:
        raise BudgetExceeded(
            f"daily cap ${config.DAILY_BUDGET_USD:.2f}: spent ${spent:.2f}, {spec.name} may cost up to ${spec.budget_usd:.2f}")

    run_id = store.start_run(conn, job, lead=lead, item_id=item_id, model=spec.model)
    day_dir = paths.logs_dir() / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = day_dir / f"{run_id:06d}-{spec.name.replace(':', '-')}"
    stem.with_suffix(".prompt.md").write_text(prompt)
    log_path = stem.with_suffix(".json")

    try:
        proc = subprocess.run(build_command(spec), input=prompt, capture_output=True, text=True,
                              timeout=config.RUN_TIMEOUT_S, cwd=paths.ROOT)
    except subprocess.TimeoutExpired:
        store.finish_run(conn, run_id, status="error", error=f"timeout after {config.RUN_TIMEOUT_S}s")
        return AgentResult(run_id, False, None, "", 0.0, "timeout")

    log_path.write_text(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        err = f"exit {proc.returncode}, unparseable output: {(proc.stderr or proc.stdout)[:300]}"
        store.finish_run(conn, run_id, status="error", error=err, log_path=str(log_path))
        return AgentResult(run_id, False, None, proc.stdout, 0.0, err)

    usage = out.get("usage") or {}
    cost = float(out.get("total_cost_usd") or 0.0)
    denials = out.get("permission_denials") or []
    text = out.get("result") or ""
    error = None
    if out.get("is_error") or out.get("subtype") != "success":
        error = f"{out.get('subtype')}: {text[:300]}"
    data = extract_json(text) if error is None else None
    if error is None and data is None and out.get("session_id"):
        # Usually one missing bracket in otherwise finished work; asking for a corrected copy is far cheaper than
        # losing the run.
        repaired, extra = _repair_json(spec, out["session_id"], stem)
        cost += extra
        data = repaired
    if error is None and data is None:
        error = "final message had no JSON object"

    store.finish_run(
        conn, run_id,
        status="ok" if error is None else "error",
        cost_usd=cost,
        input_tokens=sum(usage.get(k) or 0 for k in
                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
        output_tokens=usage.get("output_tokens"),
        num_turns=out.get("num_turns"),
        session_id=out.get("session_id"),
        error=error,
        log_path=str(log_path),
        denials=json.dumps(denials) if denials else None,
    )
    return AgentResult(run_id, error is None, data, text, cost, error, denials, int(out.get("num_turns") or 0))
