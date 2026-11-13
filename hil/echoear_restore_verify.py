#!/usr/bin/env python3
"""Gap 3: restore-only verification of the bench restore-of-record (echoear_aec_bench.NORMAL / NORMAL_HASH).

Under the bench lock: verify the frozen bundle's manifest (required) and file hashes, then flash and boot-verify it
through echoear_item2_campaign.RestoreHold, the same protected restore every requal hold uses: once touched, the
lock is never released without a verified restore (a failed flash/boot keeps the lock and waits for the operator's
SIGUSR1 retry; SIGINT/SIGTERM during the restore are only recorded). The verdict then re-reads the successful
attempt's serial log and requires, after the last reset, exactly one firmware boot record
`I (<ms>) dg-esp32: ... elf_sha256=<64 hex>` (one digest field per line) equal to NORMAL_HASH, no conflicting boot record, and an exact
`READY transport-console` line after it. Nothing else runs: no bot, no audio, no mixer change. The result is written to
a fresh directory under ~/src/evidence/echoear/aec-demo/step-10/gap3/. Exit 0 only on a clean first-attempt restore
with that exact boot proof.
"""
import argparse
import json
import re
from pathlib import Path
import time

EVIDENCE = Path.home()/'src/evidence/echoear/aec-demo/step-10/gap3'
RESET = re.compile(r'^rst:0x[0-9a-fA-F]+ ')
# Anchored firmware boot record. The only observed interleaving (r20/smoke-r18 logs) is a truncated bootloader
# `esp_image: segment 1: paddr=… vaddr=… size` fragment ending exactly at `size`; that prefix is allowed, nothing else.
# A line must carry exactly one digest field.
BOOT = re.compile(r'^(?:I \(\d+\) esp_image: segment 1: paddr=[0-9a-f]{8} vaddr=[0-9a-f]{8} size)?'
                  r'I \(\d+\) dg-esp32: ESP32-S3 step 7 audio elf_sha256=([0-9a-f]{64})\s*$')   # exact firmware line (main.cpp)
READY = re.compile(r'^READY transport-console\s*$')


def boot_proof(log, expected):
    """Exact boot witness in one serial log: last reset, then one boot record == expected, then READY."""
    lines = Path(log).read_text(errors='replace').splitlines()
    resets = [i for i, l in enumerate(lines) if RESET.match(l)]
    if not resets:return dict(passed=False, problem='no reset line')
    tail = lines[resets[-1]:]
    boots = [(i, m[1]) for i, l in enumerate(tail) if l.count('elf_sha256=') == 1 and (m := BOOT.match(l))]
    stray = [l for l in tail if 'elf_sha256=' in l and (l.count('elf_sha256=') != 1 or not BOOT.match(l))]
    proof = dict(boot_records=[h for _, h in boots], stray_digest_lines=len(stray))
    if len(boots) != 1 or boots[0][1] != expected or len(expected) != 64 or stray:
        return dict(proof, passed=False, problem='need exactly one anchored boot record equal to the expected digest')
    ready = [i for i, l in enumerate(tail) if READY.match(l)]
    if not ready or ready[0] < boots[0][0]:
        return dict(proof, passed=False, problem='no exact READY transport-console after the boot record')
    return dict(proof, passed=True, boot_line=tail[boots[0][0]], ready_line=tail[ready[0]])


def execute(out, wait=None):
    from echoear_aec_bench import NORMAL, NORMAL_HASH, verify_bundle, write
    from echoear_item2_campaign import RestoreHold
    from step9a_echoear import bench, check_esptool
    out = Path(out).resolve()
    if not out.is_relative_to(EVIDENCE.resolve()):raise ValueError('Output must be under '+str(EVIDENCE))
    if out.exists():raise FileExistsError('Fresh output directory required')
    if not (NORMAL/'manifest.json').is_file():raise FileNotFoundError('restore bundle manifest required: '+str(NORMAL))
    check_esptool(); out.mkdir(parents=True)
    import subprocess
    harness = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[1], capture_output=True,
                             text=True).stdout.strip() or None   # Fable gap3 N1: record the exact harness commit
    result = dict(kind='restore-verify', image=str(NORMAL), expected_elf_sha256=NORMAL_HASH, passed=False,
                  harness_commit=harness, started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    hold = None
    try:
        with bench():
            result['lock_acquired'] = True
            result['bundle_files_sha256'] = verify_bundle(NORMAL, NORMAL_HASH)   # pre-touch: a failure here may release
            proofs = {}
            def verify(out_dir, label):   # inside the protected restore loop: a failed proof keeps the lock and retries
                proofs[label] = proof = boot_proof(out_dir/(label+'-serial.log'), NORMAL_HASH)
                if not proof['passed']:raise ValueError('boot proof failed: '+proof['problem'])
            with RestoreHold(out, wait=wait, verify=verify) as hold:
                hold.touch()   # the hold's __exit__ flashes, boot-verifies and proves; it keeps the lock until verified
            result['restore'] = hold.record
            result['boot_proofs'] = proofs
        result['lock_released'] = True
        attempts = hold.record.get('attempts') or []
        if attempts and attempts[-1].get('error') is None:
            result['boot_proof'] = proofs.get(attempts[-1]['label'])
            result['passed'] = bool(hold.record.get('clean') is True and result['boot_proof'] and result['boot_proof']['passed'])
    except BaseException as exc:
        result['error'] = type(exc).__name__+': '+str(exc)
        result['interrupted'] = isinstance(exc, KeyboardInterrupt)
        if hold is not None:result['restore'] = hold.record
        write(out/'result.json', result)
        raise
    finally:
        if hold is not None:
            if hold.signals:result['signals'] = list(hold.signals)
            hold.release_signals()
    write(out/'result.json', result)
    print(json.dumps(dict(passed=result['passed'], elf_sha256=NORMAL_HASH)), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    try:code = execute(a.out)
    except KeyboardInterrupt:code = 1
    raise SystemExit(code)
