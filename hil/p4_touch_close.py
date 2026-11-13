#!/usr/bin/env python3
"""Offline replay of F11 run-1 and a cited parity-document addendum."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import subprocess

from p4_parity import BOARDS,COMMIT,digest,load,pinned_bundle,require,write_new
from p4_parity_touch import touch_witness
from p4_parity_restore import restore_proof
from p4_parity_serial import candidate_serial

ACQUISITION_COMMIT='8cb42b866b79173e0fe338ce963068d6fd5f6af3'
PLAN_SHA256='337516a0f0dbae1a2a8ed18b257ff3055cb9a65784838b947db54fb0e63963f0'
CLOSE_BRIEF=Path.home()/'src/dg-esp32/proj-2026-10-30-2143/codex-jobs/f11-close.md'


def archived_sources(plan):
    import p4_touch_verify as touch
    require(set(plan['source_sha256'])==set(touch.sources()),'acquisition source inventory differs')
    for name,expected in plan['source_sha256'].items():
        raw=subprocess.check_output(['git','show',ACQUISITION_COMMIT+':hil/'+name],cwd=Path(__file__).parent)
        require(hashlib.sha256(raw).hexdigest()==expected,'archived acquisition source differs: '+name)
        if name!='p4_touch_verify.py':require(digest(Path(__file__).with_name(name))==expected,'frozen replay helper changed: '+name)


def run_timing(run,ready,opened,before,scored_status,closed_at):
    """Marker mtimes share wall time; timeline samples share monotonic time."""
    mtimes={name:(run/name).stat().st_mtime_ns for name in ('ready-for-go.json','GO','window-opened.json')}
    first=next((s for s in scored_status if s['phase']=='window' and
        s['status']['driver']['taps']>before['driver']['taps']),None)
    require(first is not None,'first tap observation missing')
    require(mtimes['ready-for-go.json']<=mtimes['GO']<=mtimes['window-opened.json'] and
        ready['prepared_at']<=opened['opened_at']<=first['at']<=closed_at,'run-1 timing order differs')
    return dict(marker_mtime_ns=mtimes,ready_to_go_s=(mtimes['GO']-mtimes['ready-for-go.json'])/1e9,
        go_to_window_open_s=(mtimes['window-opened.json']-mtimes['GO'])/1e9,
        window_open_to_first_tap_observed_s=first['at']-opened['opened_at'],
        first_tap_sample_index=first['index'],first_tap_observed_at=first['at'])


def verify_board(plan_file,plan,board,run):
    import p4_touch_verify as touch
    run=Path(run);row=load(run/'touch.json');image=plan['images'][board]
    require(row['kind']=='user-paced-f11-result' and row['board']==board and row['variant']=='demo' and
        row['acquisition_passed'] is True and row['touch_verified'] is True and row['status']=='passed' and
        row['retryable'] is False and not row.get('error') and not row.get('cleanup_errors'),'clean F11 pass required')
    require(Path(row['plan_file']).resolve()==Path(plan_file).resolve() and row['plan_sha256']==digest(plan_file),
        'F11 result plan differs')
    require(row['identity']==plan['product_identity']==load(run/'identity.json'),'F11 product identity differs')
    manifest=pinned_bundle(image['bundle'],board,'demo')
    require(row['bundle']==image['bundle'] and row['elf_sha256']==image['elf_sha256']==manifest['elf_sha256'] and
        digest(Path(image['bundle'])/'manifest.json')==image['manifest_sha256'],'F11 demo pin differs')
    require(load(run/'before.json')==row['before'] and load(run/'after.json')==row['after'],'raw F11 brackets differ')
    require(row['go_observed'] is True and row['window_seconds']==600 and
        0<row['elapsed_s']<=600 and row['elapsed_s']==row['closed_at']-row['opened_at'] and
        digest(run/'GO')==row['go_sha256'],'GO/window evidence differs')
    opened=load(run/'window-opened.json');ready=load(run/'ready-for-go.json')
    require(ready['board']==board and ready['window_seconds']==600 and opened['opened_at']==row['opened_at'] and
        opened['deadline']==row['opened_at']+600 and opened['go_sha256']==row['go_sha256'] and
        opened['serial_start']==row['serial_start'],'window marker differs')
    lines=candidate_serial(run);raw=(run/'serial.log').read_bytes()
    offset=row['serial_start']['byte_offset'];require(type(offset) is int and 0<=offset<=len(raw),'invalid serial boundary')
    scored=raw[offset:].decode(errors='replace').splitlines()[:len(row['serial_witness'])]
    saved=row['serial_witness']
    require(saved and saved==(run/'window-serial.log').read_text().splitlines() and len(scored)==len(saved) and
        scored[:-1]==saved[:-1],'scored serial slice differs')
    partial=scored[-1]!=saved[-1]
    # The reader may complete a diagnostic line after the old snapshot. Ready
    # and status records must remain complete; only a diagnostic tail may be a prefix.
    require(not partial or ('dg-diag:' in saved[-1] and scored[-1].startswith(saved[-1]) and
        not re.search(r'\bSTATE\s+Ready\b|DEMO_TOUCH_STATUS',saved[-1])),'scored serial tail differs')
    statuses=[load_status(line) for line in lines if line.startswith('DEMO_TOUCH_STATUS {')]
    require(row['before'] in statuses and row['after'] in statuses,'touch brackets detached from serial')
    timeline=[json.loads(line) for line in (run/'status-timeline.jsonl').read_text().splitlines()]
    scored_status=[r for r in timeline if r['phase'] in ('go-baseline','window')]
    require(scored_status and scored_status[0]['status']==row['before'] and scored_status[-1]['status']==row['after'],
        'F11 timeline brackets differ')
    witness=touch_witness(row['before'],row['after'],saved)
    require(witness==row['witness'] and touch.delta(row['before'],row['after'])==row['delta'],'F11 witness/counters differ')
    proof=restore_proof(run,row,plan)
    return dict(board=board,verified=True,result_file=str(run/'touch.json'),result_sha256=digest(run/'touch.json'),
        witness=witness,delta=row['delta'],contact_precheck=row['contact_precheck'],
        first_contact_taps=next((s['status']['driver']['taps']-row['before']['driver']['taps'] for s in scored_status
            if s['status']['driver']['taps']>row['before']['driver']['taps']),0),
        partial_diagnostic_tail=partial,elapsed_s=row['elapsed_s'],
        timing=run_timing(run,ready,opened,row['before'],scored_status,row['closed_at']),
        restore_proof=proof)


def load_status(line):
    return json.loads(line.split(' ',1)[1])


def verify(plan_file):
    import p4_touch_verify as touch
    plan_file=Path(plan_file).resolve()
    require(digest(plan_file)==PLAN_SHA256,'run-1 plan SHA-256 differs')
    plan=load(plan_file)
    require(plan['kind']=='user-paced-f11-plan' and plan['commit']==COMMIT and plan['qualification'] is False,
        'pinned F11 plan required')
    require(plan['brief_sha256']==digest(plan['brief_file']) and plan['product_identity_sha256']==digest(plan['product_identity_file']) and
        plan['product_identity']==load(plan['product_identity_file']),'F11 plan input changed')
    archived_sources(plan)
    with (Path(plan['root'])/'attempts.jsonl').open('r') as stream:
        _,registered,finished=touch.entries(stream,plan_file,plan)
    require(len(registered)==2 and set(registered)==set(finished) and
        {r['board'] for r in registered.values()}==set(BOARDS) and all(r['attempt']==1 for r in registered.values()),
        'run-1 must retain one complete original per board')
    actual={str(p.resolve()) for p in Path(plan['root']).rglob('attempt-*') if p.is_dir()}
    require(actual==set(registered),'retained F11 attempt inventory differs')
    boards={}
    for run,registration in registered.items():
        require(finished[run]['disposition']=='passed','ledger does not record F11 pass');touch.verify_sources(finished[run])
        boards[registration['board']]=verify_board(plan_file,plan,registration['board'],run)
    return dict(kind='f11-close-proof',qualification=False,plan_file=str(plan_file),plan_sha256=digest(plan_file),
        acquisition_commit=ACQUISITION_COMMIT,firmware_commit=COMMIT,ledger_sha256=digest(Path(plan['root'])/'attempts.jsonl'),
        reader_sha256=digest(__file__),close_brief_sha256=digest(CLOSE_BRIEF),boards=boards)


def document(text,proof):
    require(set(proof['boards'])==set(BOARDS) and all(r['verified'] for r in proof['boards'].values()),'both F11 passes required')
    lines=text.splitlines();indices=[i for i,line in enumerate(lines) if line.startswith('| F11 ')]
    require(len(indices)==1 and '## F11 verified: touch-verify run-1' not in text,'one original F11 row required')
    plan='[touch-verify run-1 plan '+proof['plan_sha256']+']('+proof['plan_file']+')'
    results='; '.join('['+board+' touch.json]('+proof['boards'][board]['result_file']+')' for board in BOARDS)
    lines[indices[0]]='| F11 Physical double tap | pass | pass | '+plan+'; '+results+'; earlier unverified attempts retained below |'
    for i,line in enumerate(lines):
        if line.startswith('- F11 ') and '**unverified**' in line:
            lines[i]=line.replace('- F11 ','- Historical step-16 F11 ').replace(
                'User-paced redesign and S3 controller check remain carry-overs.',
                'The earlier unverified attempt remains disclosed; current F11 coverage is superseded by run-1 below.')
    lines+=['','## F11 verified: touch-verify run-1','',
        'This evidence-backed addendum changes F11 only, under the [F11 close brief]('+str(CLOSE_BRIEF)+').',
        'Both boards passed on the refactored step-15 demo pins at `'+COMMIT+'`.',
        'Plan SHA256: `'+proof['plan_sha256']+'` ('+plan+').','']
    for board in BOARDS:
        row=proof['boards'][board];d=row['delta'];w=row['witness']
        contact=('standalone single pre-check observed' if row['contact_precheck']['standalone_single_observed'] else
            'first observed contact was the double tap itself ('+str(row['first_contact_taps'])+' valid taps; no standalone single pre-check)')
        lines+=['- **'+board+' pass**: '+contact+'; total valid tap contacts +'+str(d['taps'])+', double_taps +'+str(d['double_taps'])+
            ', accepted requests +'+str(d['requests'])+', injected +'+str(d['injected'])+', request_rejected +'+
            str(d['request_rejected'])+'; Ready generation '+str(w['generation_before'])+' → '+str(w['generation_after'])+
            '; acquisition window closed '+f"{row['elapsed_s']:.2f}"+' s after WINDOW OPEN. [touch.json]('+row['result_file']+'), SHA256 `'+row['result_sha256']+'`.']
    lines+=['','Computed run-1 intervals (seconds):','',
        '| Board | READY → GO | GO → window-open marker | WINDOW OPEN → first tap observed |',
        '|---|---:|---:|---:|']
    for board in BOARDS:
        t=proof['boards'][board]['timing']
        lines+=['| '+board+' | '+f"{t['ready_to_go_s']:.3f}"+' | '+f"{t['go_to_window_open_s']:.3f}"+
            ' | '+f"{t['window_open_to_first_tap_observed_s']:.3f}"+' |']
    lines+=['','READY → GO and GO → window-open marker use the retained filesystem modification times of `ready-for-go.json`, `GO` and `window-opened.json`.',
        'These mtimes are not hash-sealed. WINDOW OPEN → first tap observed uses `opened_at` and the first scored `status-timeline.jsonl` sample with an increased valid-tap counter;',
        'it is the poll observation time, not an exact physical contact timestamp. Acquisition-close times above also start at `opened_at`, not GO.']
    lines+=['','The original step-16 main and follow-up attempts remain **unverified** and disclosed above.',
        'Their 45 s windows did not establish the physical witness; the retained evidence does not measure coordinator relay latency. The redesign waits for user-ready GO and then opens a 600 s window.',
        'Run-1 used acquisition harness `'+proof['acquisition_commit']+'` (`8cb42b8`). GO was written by a pre-armed waiter after READY; the post-READY user-ready GO step was skipped.',
        'The computed intervals above disclose that deviation. These retained physical results stand.',
        'Future runs start fresh Live at GO and retain new baselines after no-request idle closures, without extending the 600 s scoring deadline.',
        'The original physical witness, unchanged injection count and clean restore proofs were replayed offline against the archived acquisition sources.','']
    return '\n'.join(lines)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--document',type=Path)
    args=parser.parse_args(argv);proof=verify(args.plan);write_new(args.out,proof)
    if args.document:args.document.write_text(document(args.document.read_text(),proof))
    return 0


if __name__=='__main__':raise SystemExit(main())
