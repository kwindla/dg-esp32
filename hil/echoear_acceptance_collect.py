#!/usr/bin/env python3
"""On-disk acceptance collector (r10 Fable r9 N6; r11 Astra r10 #1/#2, Fable r10 B1/N3; r12 Astra r11 #1, Fable r11
B1/N2/N3/N5). Host-only, read-only. Not on any run-time path.

Binding. Collection is bound to ONE acceptance binding, named by path and by the sha256 recorded in the user's go
decision (not a fresh `sha256sum` of the file: the decision record is the authority, Fable r11 N5). The binding
must exist, hash to that sha256, be `counted: true`, `purpose: 'acceptance'`, `execution_authorized: true`, and
declare `output_root` (written by prepare_requal_n1.py). The collected root is taken from the binding ONLY, so a
second root produced under the same binding is never collected (Fable r11 B1).

Stage-FAIL policy (Fable r11 N3, decision 134 `no_retakes`). A scored stage FAIL ends the campaign; there are no
retakes. Re-population means a new output root, a new binding and all seven slots. When the binding says
`no_retakes: true`, a non-empty `authorized_prior_failures` is a binding problem unless the binding also names the
user decision that authorises those exact priors (`authorized_prior_decision`).

Layout (the acceptance launcher writes exactly this):
  <output_root>/<slot>/attempt-N/                 the run's output directory (result.json inside)
  <output_root>/<slot>/attempt-N.typescript       script(1) transcript ending with a complete `EXITED <n>` line
  <output_root>/<slot>/attempt-N-identity/        item2's pre-flash identity directory
  <output_root>/<slot>/attempt-N.rf-pre-gate.json launcher RF pre-gate record (informational)

Attempt history (per slot). Only canonical names `attempt-<n>` count (r13, Fable r12 B1); any other spelling of
a number is a problem. The attempt numbers are the UNION over directories, typescripts and identity
directories; they must be contiguous from 1. Every number needs its output directory and its typescript; an orphan
typescript or identity directory, or a gap, is a problem. The newest attempt must hold a final record whose
typescript EXITED equals its exit_code. Every earlier attempt must be accounted for, its typescript EXITED again
equal to its recorded exit_code:
- a CLEAN deferral (Astra r11 #1; since r13 the run-time replay guard applies the same test, Fable r12 N2):
  item2: deferred true, no scored runs/points, deferral_resources.passed true, no interrupted, no restore_failed,
  restore clean on its first attempt or device untouched, no error other than the A6 eligibility deferral message,
  exit_code 2;
  item1: the pre-touch RF pre-gate deferral: deferred true, scored false, device untouched, rf_pre_gate.passed false,
  error exactly the RF pre-gate message, exit_code 1 (item1's runner exits 1 there; left unchanged at run time).
  Because an item1 FAIL also exits 1, the attempt is corroborated (r13, Fable r12 N4): its own in-hold
  rf-pre-gate.json says passed false, no flash.log exists, and its typescript prints the deferral error. A
  consistent hand edit of all three is not detectable here;
- a segment named, with the same sha256, in the newest attempt's continued_from, which is itself a record of this
  acceptance binding (path, sha256, counted, purpose), of this stage, the bound image, inside this slot (r13, Astra
  r12 B1; the same check runs in item2 execute() before acquisition): a quiet_live unscored setup failure (decision
  134 D6: 1+4, 0+5) or, under a decision-138 plan, a clean deferral after scored originals/points in any
  multi-original or multi-arm item2 stage (decision 138b; the continuation's record carries stage_aggregate);
- a prior the binding authorises by resolved path and sha256 (see the stage-FAIL policy).
Anything else is unaccounted. Then echoear_requalification.acceptance_verdict runs on the selected records.

Location (r14, Fable r13 N1). item2 execute() and item1 refuse, before the lock, any --out other than
<output_root>/<slot>/attempt-<n> of their binding, and record the resolved path as out_path. The newest record, every
earlier deferral and every continuation segment must carry out_path equal to its own resolved location, so a
same-binding record produced in another root and copied into the declared root is unaccounted.

Mid-population deferrals. Under the frozen r1 plan (r14, Fable r13 N2, policy) only a deferral with NO scored run or
point is clean; a deferral after any scored original is not replayable, never a continuation source and unaccounted
here, so like a stage FAIL it ends the campaign. SUPERSEDED under a decision-138/139 plan revision (decision 138b): a
clean deferral after scored originals/points (resumable_deferral, including prior_hold_problems) is a continuation
point for strict, quiet_offline, quiet_live, a6_echo_stress and a6_hot_stt, collected as `continued`; the D6
three-segment limit becomes at most `runs` segments (at most 2 quiet_live setup-failure continuations).
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

EXITED = re.compile(r'^EXITED (\d+)\r?$', re.M)
from echoear_item2_campaign import ITEM1_DEFERRAL_ERROR, A6_DEFERRAL_ERROR, clean_deferral, continuation_problems  # noqa: F401  (r13: shared with run time)


def typescript_exit(path):
    """Last complete terminal marker line `EXITED <n>`; None if the file or a complete marker line is missing."""
    if path is None or not Path(path).exists():return None
    found = EXITED.findall(Path(path).read_text(errors='replace'))
    return int(found[-1]) if found else None


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def binding_problems(path, expected_sha256):
    path = Path(path)
    if not path.is_file():return None, ['acceptance binding does not exist: '+str(path)]
    actual = sha(path); binding = json.loads(path.read_text()); problems = []
    if actual != expected_sha256:problems.append('acceptance binding sha256 %s != expected %s' % (actual, expected_sha256))
    if binding.get('counted') is not True:problems.append('binding is not counted')
    if binding.get('purpose') != 'acceptance':problems.append('binding purpose is not acceptance')
    if binding.get('execution_authorized') is not True:problems.append('binding is not authorised')
    if not binding.get('output_root'):problems.append('binding declares no output_root')
    if binding.get('no_retakes') is True and binding.get('authorized_prior_failures') and not binding.get('authorized_prior_decision'):
        problems.append('authorized_prior_failures with no_retakes and no authorising decision')
    return binding, problems


SLOT_FILE = re.compile(r'attempt-(\d+)(-identity|\.typescript)?')


def attempt_numbers(slot_root, problems=None):
    """{n: {'dir': path|None, 'typescript': path|None, 'identity': path|None}} over the union of entries.

    r13 (Fable r12 B1): only the canonical spelling `attempt-<n>` (n >= 1, no leading zeros) is an attempt; any other
    spelling of a number (attempt-01, attempt-0) is a problem, so two entries can never alias one number."""
    rows = {}; problems = [] if problems is None else problems
    for entry in sorted(slot_root.iterdir()) if slot_root.is_dir() else ():
        m = SLOT_FILE.fullmatch(entry.name)
        if not m:continue
        if m[1] != str(int(m[1])) or int(m[1]) < 1:
            problems.append('non-canonical attempt name %r (only attempt-<n>, n >= 1, no leading zeros)' % entry.name);continue
        kind = {None: 'dir', '-identity': 'identity', '.typescript': 'typescript'}[m[2]]
        if kind != 'typescript' and not entry.is_dir():continue
        rows.setdefault(int(m[1]), dict(dir=None, typescript=None, identity=None))[kind] = entry
    return rows


def continuable(slot, plan):
    """quiet_live (decision 134 D6) always; every multi-original/multi-arm item2 stage under a decision-138 plan."""
    from echoear_item2_campaign import resume_enabled
    return slot == 'quiet_live' or (slot not in ('item1', 'a6_analog_probe') and resume_enabled(plan))


def resumable(slot, record):
    from echoear_item2_campaign import resumable_deferral
    if slot.startswith('a6_'):
        from echoear_item2_campaign import prior_hold_problems
        own = record.get('segment_points', record.get('points')) or []
        # r16 (Astra r15 B1): a continued A6 deferral must also have passed its hold gates (profile, resources, NIC,
        # cleanup); a later clean deferral never washes out a failed earlier gate.
        return bool(own) and record.get('next_original_slot') == len(record.get('points') or [])+1 and \
            clean_deferral(slot, dict(record, points=[], runs=[])) and not prior_hold_problems(slot, record)
    return resumable_deferral(slot, record)


def item1_deferral_corroborated(entry):
    """r13 (Fable r12 N4): an item1 pre-touch deferral is corroborated by its in-hold rf-pre-gate.json (passed false),
    no flash.log, and the deferral error in its typescript (both a deferral and a FAIL exit 1)."""
    d, ts = entry['dir'], entry['typescript']
    try:gate = json.loads((d/'rf-pre-gate.json').read_text())
    except (OSError, ValueError):return False
    text = Path(ts).read_text(errors='replace') if ts is not None and Path(ts).is_file() else ''
    return gate.get('passed') is False and not (d/'flash.log').exists() and 'RF pre-gate failed before flash; unscored' in text


def collect(plan, binding_path, expected_sha256, typescripts=None):
    from echoear_requalification import acceptance_verdict
    typescripts = dict(typescripts or {})
    binding_path = Path(binding_path).resolve()
    binding, problems_b = binding_problems(binding_path, expected_sha256)
    root = Path((binding or {}).get('output_root') or '/nonexistent-output-root'); real_root = root.resolve()
    if binding is not None and not root.is_dir():problems_b.append('binding output_root does not exist: '+str(root))
    authorised = (binding or {}).get('authorized_prior_failures') or {}
    slots = {}
    for slot in ['item1']+[s['id'] for s in plan['order']]:
        slot_root = root/slot; problems = []; record = None
        if slot_root.is_symlink():problems.append('slot directory is a symlink')
        numbers = attempt_numbers(slot_root, problems)
        if not numbers and not problems:
            slots[slot] = dict(record=None, problems=['no attempts'], history=[]);continue
        if not numbers:
            slots[slot] = dict(record=None, problems=problems+['no attempts'], history=[]);continue
        n = max(numbers)
        missing = [k for k in range(1, n+1) if k not in numbers]
        if missing:problems.append('attempt numbers are not contiguous: missing %s' % missing)
        for k, entry in sorted(numbers.items()):
            if entry['dir'] is None:problems.append('attempt-%d has no output directory (orphan %s)' % (
                k, 'typescript' if entry['typescript'] else 'identity directory'))
            elif entry['dir'].is_symlink() or not entry['dir'].resolve().is_relative_to(real_root):
                problems.append('attempt-%d escapes the declared root' % k)
            if entry['typescript'] is None and k in numbers and entry['dir'] is not None:
                problems.append('attempt-%d has no typescript' % k)
        newest = numbers[n]; result_path = (newest['dir'] or slot_root/f'attempt-{n}')/'result.json'
        if not result_path.is_file():
            problems.append('newest attempt-%d has no result.json' % n)
        else:
            if result_path.is_symlink() or not result_path.resolve().is_relative_to(real_root):problems.append('result.json escapes the declared root')
            record = json.loads(result_path.read_text())
            if binding is not None:
                if record.get('release_binding') != str(binding_path):problems.append('record release_binding is not the acceptance binding')
                if record.get('binding_sha256') != expected_sha256:problems.append('record binding_sha256 differs')
                if record.get('counted') is not True or record.get('purpose') != 'acceptance':problems.append('record is not a counted acceptance record')
                image = binding.get('demo_elf_sha256') if slot == 'item1' else binding.get('normal_elf_sha256')
                if (record.get('elf_sha256') if slot == 'item1' else record.get('normal_elf_sha256')) != image:problems.append('record image differs from the binding')
            if slot != 'item1' and record.get('stage') != slot:problems.append('record stage differs from the slot')
            # r14 (Fable r13 N1): the run recorded where it wrote; a same-binding record copied in from another root fails.
            if record.get('out_path') != str(result_path.resolve().parent):
                problems.append('record out_path %r is not its location %s' % (record.get('out_path'), result_path.resolve().parent))
            if slot == 'quiet_live' and 'quiet_live_aggregate' not in record:problems.append('newest quiet_live attempt has no aggregate')
        continued = {c.get('path'): c.get('result_sha256') for c in (record or {}).get('continued_from') or [] if isinstance(c, dict)}
        history_rows = []
        for k in range(1, n):
            entry = numbers.get(k); row = dict(attempt=k)
            if entry is None or entry['dir'] is None:
                row['state'] = 'missing';history_rows.append(row);continue
            r = entry['dir']/'result.json'
            if not r.is_file():
                row['state'] = 'incomplete';problems.append('earlier attempt-%d has no result.json (unaccounted)' % k)
            else:
                earlier = json.loads(r.read_text()); digest = sha(r); code = typescript_exit(entry['typescript'])
                if code is None or code != earlier.get('exit_code'):
                    problems.append('earlier attempt-%d typescript EXITED %s != record exit_code %s' % (k, code, earlier.get('exit_code')))
                if clean_deferral(slot, earlier) and (slot != 'item1' or item1_deferral_corroborated(entry)):
                    row['state'] = 'deferral'
                    if earlier.get('out_path') != str(r.resolve().parent):   # r14 (Fable r13 N1)
                        row['state'] = 'unaccounted';problems.append('earlier attempt-%d out_path is not its location' % k)
                elif continuable(slot, plan) and continued.get(str(entry['dir'].resolve())) == digest:
                    # r13 (Astra r12 B1): the continued bytes must also be a segment of THIS acceptance binding.
                    bad = continuation_problems(earlier, r, slot_root, dict(release_binding=str(binding_path), binding_sha256=expected_sha256,
                                                counted=True, purpose='acceptance'), (binding or {}).get('normal_elf_sha256'), stage=slot)
                    # r15 (decision 138b): a continued deferral must itself be a clean deferral carrying scored originals.
                    if earlier.get('deferred') is True and not resumable(slot, earlier):
                        bad = bad+['continued deferral is not clean (decision 138b)']
                    row['state'] = 'unaccounted' if bad else 'continued'
                    problems += ['continued attempt-%d: %s' % (k, b) for b in bad]
                elif authorised.get(str(r.resolve())) == digest:row['state'] = 'authorised-prior'
                else:
                    row['state'] = 'unaccounted';problems.append('earlier attempt-%d (%s) is not a clean deferral, continuation or authorised prior' % (
                        k, 'PASS' if earlier.get('passed') else 'FAIL'))
            history_rows.append(row)
        if continued and record is not None and slot != 'quiet_live' and 'stage_aggregate' not in record:
            problems.append('newest continued attempt has no stage aggregate')
        if record is not None:
            listed = set(continued); seen = {str(e['dir'].resolve()) for kk, e in numbers.items() if kk < n and e['dir'] is not None}
            if listed - seen:problems.append('continued_from names a segment outside this slot history')
        ts = typescripts.get(slot) or newest['typescript'] or slot_root/f'attempt-{n}.typescript'
        code = typescript_exit(ts)
        if code is None:problems.append('no complete EXITED line: '+str(ts))
        elif record is not None and code != record.get('exit_code'):problems.append(f'typescript EXITED {code} != record exit_code {record.get("exit_code")}')
        slots[slot] = dict(record=record, path=str(result_path), attempt=n, history=history_rows, typescript=str(ts),
                           typescript_exit=code, problems=problems)
    verdict = acceptance_verdict(plan, slots['item1']['record'], {k: v['record'] for k, v in slots.items() if k != 'item1' and v['record'] is not None})
    collection_ok = not problems_b and all(not v['problems'] for v in slots.values())
    return dict(passed=bool(verdict['passed'] and collection_ok), binding=str(binding_path), output_root=str(root),
                binding_problems=problems_b, verdict=verdict,
                slots={k: {kk: vv for kk, vv in v.items() if kk != 'record'} for k, v in slots.items()})


if __name__ == '__main__':
    from echoear_requalification import load_plan
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--binding', type=Path, required=True)
    p.add_argument('--binding-sha256', required=True, help='the sha256 recorded in the go decision, not a fresh sha256sum')
    p.add_argument('--typescript', action='append', default=[], help='slot=path (newest attempt only)')
    a = p.parse_args()
    result = collect(load_plan(a.plan), a.binding, a.binding_sha256, dict(t.split('=', 1) for t in a.typescript))
    print(json.dumps(result, indent=2)); raise SystemExit(0 if result['passed'] else 1)
