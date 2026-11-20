"""PreToolUse hook for the execute pass: the approved calls may run once each, in order, with exactly the
approved arguments.

Invoked as `python guard.py <expected.json>` with the hook payload on stdin. Exit 2 blocks the call.
expected.json: {"calls": [{"tool": ..., "args": {...}, "defaults": {...}, "free": [...]}, ...]}
- args: must be present with exactly these values
- defaults: may be omitted, or present with exactly this value
- free: may be present with any value
Stdlib only, no package imports, so it runs under any python3.
"""

import json
import os
import sys

MISSING = object()


def block(reason):
    sys.stderr.write("BLOCKED by cos guard: " + reason)
    sys.exit(2)


def main():
    expected_path = sys.argv[1]
    try:
        with open(expected_path) as f:
            calls = json.load(f)["calls"]
        payload = json.load(sys.stdin)
    except Exception as e:  # fail closed
        block("could not read guard inputs: %s" % e)

    counter = expected_path + ".calls"
    done = int(open(counter).read()) if os.path.exists(counter) else 0
    if done >= len(calls):
        block("every approved call was already attempted; do not retry, report the outcome instead")
    want = calls[done]

    tool = payload.get("tool_name")
    args = payload.get("tool_input") or {}
    if tool != want["tool"]:
        block("next approved call is %s, not %s" % (want["tool"], tool))
    for key, value in want["args"].items():
        if args.get(key, MISSING) != value:
            block("argument %r must be %r, got %r" % (key, value, args.get(key)))
    for key, value in want.get("defaults", {}).items():
        if key in args and args[key] != value:
            block("argument %r may only be %r, got %r" % (key, value, args[key]))
    extra = set(args) - set(want["args"]) - set(want.get("defaults", {})) - set(want.get("free", []))
    if extra:
        block("unexpected arguments %s" % sorted(extra))

    with open(counter, "w") as f:
        f.write(str(done + 1))
    sys.exit(0)


if __name__ == "__main__":
    main()
