#!/usr/bin/env python3
"""The duo bot's log for a run: parsers for its lines, and the report made from them.

    python3 hil/duo_e2e/journal.py run-unify-bot.sh ~/src/evidence/duo/e2e/runs/a1-functional [run-dir ...]

A run directory holds `start` and `end` (the window, as `date '+%Y-%m-%d %H:%M:%S'`
writes it) and, when the run played a cohort, `marks.json` and `played.json`.
The command reads the bot's lines for the window from the user journal (only
the coordinator runs that), prints the score, the latency summary and the
error lines, and writes report.json into the run directory. `--lines FILE`
takes saved lines instead of the journal.

The formats, each read from the code that writes the line:

  every line     pipecat/runner/run.py adds stderr to loguru without a format, so the default applies:
                 "2026-11-10 01:23:45.678 | INFO     | module:function:line - message"
  transcript     server/duo_bot.py, on_user_turn_stopped: "Transcript: user: <text>", once per user turn
  events         server/duo_bot.py, build_worker event(): "DUO <kind> <dict repr>", a Python repr, not JSON
  route          server/duo_route.py, _decide_route: kind "route" with token, target, reason, p, elapsed_ms.
                 It is logged before the claim, so its target is what the bot proposed, not what it did.
  character      server/duo_bot.py, _publish: kind "outcome", whose "character" is the one now active.
                 reason 'addressed' is a committed route switch, 'reverted' takes one back.
  timing         server/duo_bot.py, DuoTimingObserver._emit: "DUO timing <json>"
"""
import argparse
import ast
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
from typing import NamedTuple

from cohort import CHARACTERS, outside
import latency
import score

LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \| (\w+) *\| \S+?:(\S+?):\d+ - (.*)")
TRANSCRIPT = "Transcript: user:"
JSON_KINDS = ("timing", "gc", "gc-freeze")  # Written with json.dumps; every other kind is a dict repr.
# When a user turn ends, the fence admits its route and a task logs its
# transcript within a few passes of the event loop: milliseconds apart, a
# garbage-collection pause at most. Consecutive user turns end seconds apart.
ADMISSION_SLACK_S = 1.0


class Record(NamedTuple):
    t: float  # Seconds since the epoch, from the bot's local wall clock.
    level: str
    function: str
    message: str


def records(lines):
    """Every line loguru stamped. Traceback and other continuation lines are not records."""
    found = []
    for line in lines:
        match = LINE.match(line)
        if match:
            stamp, level, function, message = match.groups()
            found.append(Record(datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f").timestamp(), level, function, message))
    return found


def duo(message):
    """(kind, payload) of a `DUO <kind> <payload>` message whose payload is a mapping; None for anything else."""
    if not message.startswith("DUO "):
        return None
    kind, _, payload = message[4:].partition(" ")
    try:
        data = json.loads(payload) if kind in JSON_KINDS else ast.literal_eval(payload)
    except (ValueError, SyntaxError):
        return None
    return (kind, data) if isinstance(data, dict) else None


def events(found):
    return [event for event in (duo(record.message) for record in found) if event]


def turns(found, start="angel"):
    """One dict per user turn in log order: when it ended, what was heard, who was active, how it was routed.

    `active` is the character of the last `outcome` before the transcript
    (`start` until the first one): it follows auto switches, taps and
    rollbacks alike. A route belongs to the turn that ended when the route was
    admitted, which is its log time less its `elapsed_ms`. Log order would
    mispair them: a route superseded by the next turn is logged after that
    turn's transcript. A turn no route claims stays `routed=False`: the bot
    answered it without asking who was addressed.

    `target` is what the route proposed. `switched` is the character the bot
    then switched to, or None, and `did` says how it ended: "switched",
    "stayed", "claim-lost" (a target was proposed but the claim lost to the
    deadline, so nothing changed) or "reverted" (switched, then taken back).
    The coordinator finishes one route's switch before it decides the next, so
    an `outcome` with reason 'addressed' between two route lines belongs to the
    first of them.
    """
    active, heard, routes, deciding = start, [], [], None
    for record in found:
        if record.message.startswith(TRANSCRIPT):
            transcript = record.message[len(TRANSCRIPT):].strip()
            if transcript not in ("", "None"):  # An empty turn runs no reply, so it is never routed.
                heard.append(dict(t=record.t, transcript=transcript, active=active, routed=False,
                                  target=None, reason="unrouted", p=None, did="stayed", switched=None))
            continue
        kind, data = duo(record.message) or (None, None)
        if kind == "outcome" and data.get("character") in CHARACTERS:
            active = data["character"]
            if deciding and data.get("reason") == "addressed" and data["character"] == deciding["data"].get("target"):
                deciding["did"] = "switched"
            elif deciding and data.get("reason") == "reverted" and deciding["did"] == "switched":
                deciding["did"] = "reverted"
        elif kind == "route":
            deciding = dict(admitted=record.t - data.get("elapsed_ms", 0) / 1000, data=data,
                            did="claim-lost" if data.get("target") else "stayed")
            routes.append(deciding)
    for route in routes:
        admitted, data = route["admitted"], route["data"]
        near = [turn for turn in heard if not turn["routed"] and abs(turn["t"] - admitted) <= ADMISSION_SLACK_S]
        if near:
            turn = min(near, key=lambda turn: abs(turn["t"] - admitted))
            turn.update(routed=True, target=data.get("target"), reason=data.get("reason") or "switch", p=data.get("p"),
                        did=route["did"], switched=data.get("target") if route["did"] == "switched" else None)
            if data.get("reason") == "vocative-rule":
                # DUO_VOCATIVE_RULE decided this turn: what Jev alone would have done with it.
                turn["jev"] = data.get("jev_reason") or "switch"
    return heard


def report(lines, marks=None, played=None, start="angel"):
    """Everything read from one run's lines, as plain data: the turns, the latencies, the errors, the score."""
    found = records(lines)
    heard, seen = turns(found, start), events(found)
    kinds, routed = Counter(kind for kind, _ in seen), sum(turn["routed"] for turn in heard)
    result = dict(records=len(found), turns=len(heard), routed=routed,
                  routes_without_transcript=kinds["route"] - routed, events=dict(sorted(kinds.items())),
                  timing=latency.timing([data for kind, data in seen if kind == "timing"]),
                  barge_in=latency.barge_in(found), errors=latency.errors(lines), heard=heard)
    ruled = [turn for turn in heard if "jev" in turn]
    switched = [turn for turn in ruled if turn["did"] == "switched"]
    result["ruled"] = dict(applied=len(ruled), switched=len(switched),
                           changed=sum(turn["jev"] != "switch" for turn in switched))
    if marks is not None:
        rows, stray = score.score(marks, heard, played)
        result.update(score=score.summary(rows), rows=rows, extra=sum(row.get("extra", 0) for row in rows),
                      stray=[turn["transcript"] for turn in stray], unplayed=len(marks) - len(rows),
                      aligned="time" if played is not None else "order")
    return result


def render(label, result):
    """The report as the lines the coordinator reads."""
    timing, barge = result["timing"], result["barge_in"]
    out = [f"{label} turns={result['turns']} routed={result['routed']} replies={timing['replies']} "
           f"decisions={timing['decisions']} errors={len(result['errors'])}"]
    for key, decisions in timing["keys"].items():
        for decision, stats in decisions.items():
            out.append(f"  {key:13s} {decision:12s} n={stats['n']:2d} "
                       f"p50={stats['p50']} p95={stats['p95']} max={stats['max']}")
    out.append(f"  barge-ins={barge['n']} p50={barge['p50']} p95={barge['p95']} max={barge['max']} "
               f"mean={barge['mean']} overdue={barge['overdue']} all={barge['all']}")
    out.append(f"  events={result['events']}")
    if result["routes_without_transcript"]:
        out.append(f"  routes without a transcript={result['routes_without_transcript']}")
    if result["ruled"]["applied"]:
        out.append(f"  vocative rule: applied to {result['ruled']['applied']} turns, {result['ruled']['switched']} "
                   f"switched, {result['ruled']['changed']} of those Jev alone would not have made")
    if "rows" in result:
        out.append("  score " + " ".join(f"{name}={count}" for name, count in result["score"].items()) +
                   f" extra={result['extra']} stray={len(result['stray'])} unplayed={result['unplayed']}"
                   f" (aligned by {result['aligned']})")
        for row in result["rows"]:
            head = f"  {row['phrase']:2d} {row['result']:10s} expected={row['expected']:5s}"
            if row["result"] == "missing":
                out.append(f"{head} said: {row['text']}")
                continue
            more = f" (+{row['extra']} more in its window)" if row["extra"] else ""
            rule = f" (by rule; Jev alone: {row['jev']})" if "jev" in row else ""
            if row["did"] in ("claim-lost", "reverted"):  # Proposed, but the character did not change.
                rule += f" BUT {row['did']}"
            out.append(f"{head} active={row['active']:5s} route={row['target'] or row['reason']}{rule} p={row['p']} "
                       f"heard: {row['transcript']}{more}")
            if not row["verbatim"]:
                out.append(f"{'':16s}said: {row['text']}")
    out.extend(f"  ERR {line}" for line in result["errors"][:8])
    return out


def fetch(tag, start, end):
    """The bot's lines for a window, from the user journal. Coordinator only."""
    return subprocess.run(["journalctl", "--user", "--since", start, "--until", end, "-o", "cat", "-t", tag],
                          capture_output=True, encoding="utf-8", errors="replace", check=True).stdout.splitlines()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score and summarize duo bot runs from the bot's log.")
    parser.add_argument("tag", help="the bot's journal identifier, such as run-unify-bot.sh")
    parser.add_argument("runs", nargs="+", metavar="run-dir")
    parser.add_argument("--lines", help="a file of saved bot lines to read instead of the journal")
    parser.add_argument("--start-character", choices=CHARACTERS, help="who was active before the window's first "
                        "outcome line (default: what the run recorded, or angel)")
    args = parser.parse_args(argv)
    total = Counter()
    for run in [outside(run) for run in args.runs]:  # Every path is checked before the first report is written.
        window = {} if args.lines else {name: (run / name).read_text().strip() for name in ("start", "end")}
        lines = (Path(args.lines).read_text(errors="replace").splitlines() if args.lines else
                 fetch(args.tag, window["start"], window["end"]))
        marks, played, statuses = ((json.loads(path.read_text()) if path.exists() else None)
                                   for path in (run / "marks.json", run / "played.json", run / "status.json"))
        # A web run says hello inside its window. An S3 run joins a session that
        # is already up, and its first status says who was active then.
        start = args.start_character or (statuses[0]["status"]["character"] if statuses else "angel")
        result = report(lines, marks and marks["marks"], played, start)
        (run / "report.json").write_text(json.dumps(dict(tag=args.tag, **window, **result), indent=1) + "\n")
        print("\n".join(render(run.name, result)))
        if not result["records"]:
            print(f"  no bot lines: is {args.tag} the bot's journal identifier, and is the window right?")
        total.update(result.get("score", {}))
    if len(args.runs) > 1 and total:
        print("total " + " ".join(f"{name}={total[name]}" for name in score.CLASSES))


if __name__ == "__main__":
    main()
