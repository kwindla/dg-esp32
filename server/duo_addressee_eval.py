"""Labelled Jev evaluation with frozen acceptance and evidence outside git.

Example (live; uses settings.load_credentials):
  uv run python duo_addressee_eval.py --set tune --output-dir ~/src/evidence/duo/auto-switch/eval-TIMESTAMP --label tune-1
Use --validate-only to check both fixture sets without loading credentials.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from loguru import logger

from duo_addressee import (
    Addressee, DEFAULT_AUTO_SWITCH_FLOOR, DEFAULT_ROUTE_BUDGET_S, DEFAULT_JEV_MODEL,
    addressee_question, addressee_state, configuration, decide, split_context,
)
from settings import ROOT, load_credentials

CLASSES = {
    "named_direct", "hear_from", "epithets", "misheard_names", "corrections",
    "idioms", "talking_about", "followups", "greetings_thanks", "one_word_empty",
}
FIXTURES = Path(__file__).with_name("fixtures")
MAX_LIVE_REQUESTS = 599


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def question_digest():
    data = json.dumps(addressee_question().model_dump(), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False,
                               allow_nan=False) + "\n")


def load_cases(name):
    path = FIXTURES / f"duo_addressee_{name}.json"
    document = json.loads(path.read_text())
    cases = document["cases"]
    if document.get("schema_version") != 1 or document.get("set") != name or len(cases) < 40:
        raise ValueError(f"{name}: need schema 1 and at least 40 cases")
    ids, coverage = set(), Counter()
    for case in cases:
        if case["id"] in ids or case["class"] not in CLASSES:
            raise ValueError(f"{name}: duplicate ID or unknown class")
        ids.add(case["id"])
        if case["current"] not in {"angel", "devil"}:
            raise ValueError(f"{name}: invalid current speaker")
        other = "devil" if case["current"] == "angel" else "angel"
        if case["expected"] not in {other, "stay"} or not isinstance(case["latest"], str):
            raise ValueError(f"{name}: invalid expected target or latest text")
        if not isinstance(case["prior"], list):
            raise ValueError(f"{name}: prior must contain context messages")
        messages = case["prior"] + [{"role": "user", "content": case["latest"]}]
        prior, latest = split_context(messages)
        state = addressee_state(prior, latest, case["current"])
        if latest != case["latest"] or len(state["conversation"]) > 8:
            raise ValueError(f"{name}: context split/state mismatch")
        coverage[case["class"], case["current"]] += 1
    if any(not coverage[cls, current] for cls in CLASSES for current in ("angel", "devil")):
        raise ValueError(f"{name}: each class must cover both directions")
    return cases


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank percentiles retain observed tail samples.
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def latency(rows):
    successful = [row["route_ms"] for row in rows if not row["fallback"]]
    return dict(n=len(rows), successful_n=len(successful),
                p50_ms=percentile(successful, .5), p95_ms=percentile(successful, .95),
                all_attempts_p50_ms=percentile([r["route_ms"] for r in rows], .5),
                all_attempts_p95_ms=percentile([r["route_ms"] for r in rows], .95))


def summarize(rows, cold_rows, budget):
    per_class = {}
    for cls in sorted(CLASSES):
        group = [row for row in rows if row["class"] == cls]
        switch = [row for row in group if row["expected"] != "stay"]
        per_class[cls] = dict(
            n=len(group), expected_switches=len(switch),
            correct_switches=sum(row["predicted"] == row["expected"] for row in switch),
            false_switches=sum(row["predicted"] != "stay" and row["predicted"] != row["expected"]
                               for row in group),
            missed_switches=sum(row["predicted"] != row["expected"] for row in switch),
            fallbacks=sum(row["fallback"] for row in group),
            reasons=dict(Counter(row["decision"]["reason"] or "selected" for row in group)),
        )
    checks = {"zero_false_switches_on_stay": all(row["predicted"] == "stay"
               for row in rows if row["expected"] == "stay")}
    for cls, threshold in (("named_direct", .9), ("hear_from", .9),
                           ("epithets", .75), ("corrections", .75)):
        group = per_class[cls]
        checks[f"{cls}_switch_rate"] = bool(group["expected_switches"] and
            group["correct_switches"] / group["expected_switches"] >= threshold)
    fallbacks = sum(row["fallback"] for row in rows)
    checks["budget_fallback_rate_at_most_2_percent"] = bool(rows) and fallbacks / len(rows) <= .02
    p95 = percentile([row["route_ms"] for row in rows], .95)
    checks["route_p95_within_budget"] = p95 is not None and p95 <= budget * 1000
    return dict(per_class=per_class, fallbacks=fallbacks, fallback_rate=fallbacks / len(rows) if rows else None,
                timeouts=sum(row["decision"]["reason"] == "timeout" for row in rows),
                latency=dict(cold=latency(cold_rows), warm=latency(rows)),
                checks=checks, passed=all(checks.values()))


class Campaign:
    def __init__(self, directory):
        self.directory = directory.resolve()
        if self.directory == ROOT or ROOT in self.directory.parents:
            raise ValueError("Evidence must be outside the repository")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "campaign.json"
        frozen = digest(FIXTURES / "duo_addressee_accept.json")
        self.data = json.loads(self.path.read_text()) if self.path.exists() else dict(
            created_utc=datetime.now(timezone.utc).isoformat(), accept_sha256=frozen,
            live_requests=0, runs=[])
        if self.data["accept_sha256"] != frozen:
            raise ValueError("Frozen acceptance digest changed; refusing all live calls")
        write_json(self.path, self.data)

    def begin(self, label, name, planned_requests):
        if any(run["label"] == label for run in self.data["runs"]):
            raise ValueError("Run label already exists")
        if name == "accept" and any(run["set"] == "accept" for run in self.data["runs"]):
            raise ValueError("Acceptance has already been attempted; do not rerun or tune against it")
        if self.data["live_requests"] + planned_requests > MAX_LIVE_REQUESTS:
            raise ValueError("Campaign would exceed the live-request limit")
        self.data["runs"].append(dict(label=label, set=name, completed=False))
        write_json(self.path, self.data)

    def request(self):
        if self.data["live_requests"] >= MAX_LIVE_REQUESTS:
            raise RuntimeError("Live-request limit reached")
        self.data["live_requests"] += 1
        write_json(self.path, self.data)

    def finish(self):
        self.data["runs"][-1]["completed"] = True
        write_json(self.path, self.data)


async def evaluate_case(router, case, floor, budget, campaign, cohort):
    campaign.request()
    started = time.perf_counter()
    deadline = asyncio.get_running_loop().time() + budget
    answer, error_type = None, None
    try:
        async with asyncio.timeout_at(deadline):
            prior, latest = split_context(case["prior"] + [{"role": "user", "content": case["latest"]}])
            state = addressee_state(prior, latest, case["current"])
            answer = await router.ask(state)
        decision = decide(answer, case["current"], floor, has_baseline=True)
    except Exception as error:
        error_type = type(error).__name__
        decision = decide(error, case["current"], floor, has_baseline=True)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return dict(id=case["id"], **{"class": case["class"]}, current=case["current"],
                latest=case["latest"], expected=case["expected"],
                predicted=decision.target or "stay", decision=vars(decision),
                choice=answer.choice if answer is not None else None,
                probabilities=answer.probabilities if answer is not None else None,
                confidence=answer.confidence if answer is not None else None,
                error_type=error_type, fallback=decision.reason in {"error", "timeout"},
                route_ms=elapsed_ms, cohort=cohort, case=case)


async def run(args, cases, config, campaign):
    floor, budget, model = config["auto_switch_floor"], config["route_budget_s"], config["jev_model"]
    load_credentials(approved_only=True)
    key = os.getenv("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY is absent from the approved credential source")
    planned = len(cases) + args.cold_samples + 1
    campaign.begin(args.label, args.set, planned)
    directory = campaign.directory / args.label
    directory.mkdir()
    manifest = dict(set=args.set, model=model, floor=floor, budget_s=budget,
                    question_sha256=question_digest(),
                    question=addressee_question().model_dump(),
                    case_sha256={name: digest(FIXTURES / f"duo_addressee_{name}.json")
                                 for name in ("tune", "accept")},
                    planned_live_requests=planned, started_utc=datetime.now(timezone.utc).isoformat(),
                    latency_definition="Admission through context split, uncached ask, decision and cancellation join; cold has a fresh HTTP pool and no connect; warm follows connect.",
                    fallback_definition="error or timeout; below-floor is counted separately in reasons")
    write_json(directory / "manifest.json", manifest)
    rows, cold_rows = [], []
    output = directory / "cases.jsonl"
    with output.open("x") as stream:
        # Cold probes use tune cases only, even when acceptance is selected.
        tune = load_cases("tune")
        for index in range(args.cold_samples):
            router = Addressee(key, budget=budget, model=model)
            try:
                row = await evaluate_case(router, tune[index % len(tune)], floor, budget,
                                          campaign, "cold")
                cold_rows.append(row)
                stream.write(json.dumps(row, allow_nan=False) + "\n")
                stream.flush()
            finally:
                await router.close()
        router = Addressee(key, budget=budget, model=model)
        try:
            campaign.request()
            await router.connect()
            for case in cases:
                row = await evaluate_case(router, case, floor, budget, campaign, "warm")
                rows.append(row)
                stream.write(json.dumps(row, allow_nan=False) + "\n")
                stream.flush()
        finally:
            await router.close()
    summary = dict(**manifest, **summarize(rows, cold_rows, budget),
                   run_live_requests=planned, campaign_live_requests=campaign.data["live_requests"])
    write_json(directory / "summary.json", summary)
    campaign.finish()
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    print(f"Evidence: {directory}")
    return 0 if args.set == "tune" or summary["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=("tune", "accept"), default="tune")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--label", default="run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--floor", type=float, default=float(os.getenv("DUO_AUTO_SWITCH_FLOOR", DEFAULT_AUTO_SWITCH_FLOOR)))
    parser.add_argument("--budget", type=float, default=float(os.getenv("DUO_ROUTE_BUDGET_S", DEFAULT_ROUTE_BUDGET_S)))
    parser.add_argument("--model", default=os.getenv("DUO_JEV_MODEL", DEFAULT_JEV_MODEL))
    parser.add_argument("--cold-samples", type=int, default=0)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    cases = {name: load_cases(name) for name in ("tune", "accept")}
    if args.validate_only:
        print(json.dumps({name: dict(n=len(rows), sha256=digest(FIXTURES / f"duo_addressee_{name}.json"))
                          for name, rows in cases.items()}, indent=2))
        return 0
    if args.output_dir is None or not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        parser.error("Live evaluation needs an external --output-dir and a safe --label")
    if args.cold_samples < 0 or (args.set == "accept" and args.cold_samples):
        parser.error("Cold probes must be nonnegative and belong to tune runs only")
    config = configuration(dict(DUO_AUTO_SWITCH_FLOOR=str(args.floor),
                                DUO_ROUTE_BUDGET_S=str(args.budget), DUO_JEV_MODEL=args.model))
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    return asyncio.run(run(args, cases[args.set], config, Campaign(args.output_dir)))


if __name__ == "__main__":
    raise SystemExit(main())
