#!/usr/bin/env python3
"""Check exact requested values; redact all local values in diagnostic diffs."""
import argparse
from pathlib import Path
import sys
import re

ROOT = Path(__file__).resolve().parents[1]

def settings(path):
    result = {}
    for line in path.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = key + "=" + value
        elif match := re.fullmatch(r"# (CONFIG_\w+) is not set", line):
            result[match[1]] = match[1] + "=n"
    return result

def validate(values):
    activity=values.get("CONFIG_DG_PLAYBACK_ACTIVITY_DBFS")
    target=values.get("CONFIG_DG_PLAYBACK_TARGET_DBFS")
    if activity and target and int(activity.split("=",1)[1])>int(target.split("=",1)[1]):
        print("FAIL: playback activity dBFS must be <= target dBFS")
        return True
    return False

def check(project):
    expected = {}
    private = set()
    for name in ("sdkconfig.defaults", "sdkconfig.local"):
        path = project / name
        if name == "sdkconfig.local" and not path.exists():
            path = ROOT / name  # same fallback as scripts/idf.sh
        if not path.exists():
            continue
        values = settings(path)
        expected.update(values)
        if name.endswith(".local"):
            private.update(values)
    actual = settings(project / "sdkconfig")
    invalid=validate(actual)
    failures = []
    skipped = 0
    for key, line in expected.items():
        if key in private and key not in actual and project.resolve() != ROOT:
            print(f"{key}: not applicable (absent from sub-project configuration)")
            skipped += 1
            continue
        if actual.get(key) != line:
            failures.append(key)
            print("- " + (key + "=<redacted>" if key in private else line))
            print("+ " + (key + "=<redacted or missing>" if key in private
                           else actual.get(key, key + "=<missing>")))
    print(f"{'FAIL' if failures else 'PASS'}: {len(expected)} requested settings; "
          f"{len(failures)} differences; {skipped} not applicable (local values redacted)")
    return bool(failures) or invalid

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--project", type=Path, default=ROOT)
    p.add_argument("--validate-only",type=Path)
    a = p.parse_args()
    if a.validate_only:
        sys.exit(validate(settings(a.validate_only)))
    sys.exit(check(a.project if a.project.is_absolute() else ROOT / a.project))
