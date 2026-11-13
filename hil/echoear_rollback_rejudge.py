#!/usr/bin/env python3
"""Read retained rollback evidence; create only a new, report-only rejudge.json."""
import argparse
import json
from pathlib import Path
import re

import echoear_rollback as r


def normalize(text):
    return ' '.join(re.sub(r'[^a-z0-9 ]',' ',text.casefold()).split())


PROMPTS=dict(cold='what is the capital of france', **{'warm-1':'name two colors',
    'warm-2':'what is two plus two','talk-over':'short description of the ocean',
    'near-recovery':'what is the capital of japan','reconnected':'name one fruit'})


def file_hashes(root):
    return {str(p.relative_to(root)):r.sha(p) for p in sorted(root.rglob('*')) if p.is_file() and p!=root/'rejudge.json'}


# gap 3: rollback-01's retained evidence restored the then restore-of-record 9936da45 (not the current NORMAL_HASH).
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45_HASH as RETAINED_NORMAL_HASH


def evaluate(root):
    """No validation of a live checkout, socket check, driver or device import."""
    root=Path(root)
    saved=json.loads((root/'result.json').read_text())
    windows=json.loads((root/'turns.json').read_text())
    all_rows=sorted(r.events(root),key=lambda e:e['monotonic_ns'])
    serial=(root/'rollback-serial.log').read_text().splitlines()
    snapshots={}
    for name in ('before-smoke','after-idle','before-reconnect','after-reconnect','after-smoke'):
        path=root/(name+'.json')
        if not path.exists():
            snapshots[name]=dict(status='not evaluated',reason='Snapshot absent');continue
        data=json.loads(path.read_text());raw={k:v for k,v in data.items() if k!='audio'}
        matching=[i+1 for i,line in enumerate(serial) if line.startswith('PERF ') and json.loads(line[5:])==raw]
        snapshots[name]=dict(status='evaluated',reached=True,serial_match=len(matching)==1,
                             serial_lines=matching,data=data)
    results=[];protocol=[]
    for window in windows:
        name=window['id'];lo,hi=window['start_ns'],window['end_ns']
        own=[e for e in all_rows if lo<=e['monotonic_ns']<=hi]
        lines=json.loads((root/(name+'-serial.json')).read_text())
        witness=r.turn_witness(own,lines,name=='talk-over')
        individual=r.per_turn_witnesses(own,lines,lo,hi)
        note=window.get('operator_note','')
        operator=note.startswith('PASS ') and len(note)>=10
        for turn in individual:
            turn['audible_reply_witness']=dict(instrumented_complete=turn['complete_reply'],
                operator_pass=operator,operator_note=note,operator_scope='whole declared window',
                separate_per_turn_note=len(individual)==1,
                limitation='Shared note for both replies; no independent per-reply audibility attestation' if len(individual)>1 else None)
        witness.update(id=name,start_ns=lo,end_ns=hi,operator_note=note,individual_turns=individual,
            original_window_pc_ids=window['pc_ids'],original_window_passed=window['passed'])
        witness['passed'] &= operator and bool(individual) and all(t['passed'] for t in individual)
        results.append(witness)
        matched=[t['index'] for t in individual if PROMPTS[name] in normalize(t['user_transcripts'][0])]
        protocol.append(dict(id=name,expected_prompt=PROMPTS[name],actual_user_turns=len(individual),
            matching_turn_indices=matched,declared_window_stimulus_followed=len(individual)==1 and matched==[1],
            functional_witness_passed=witness['passed']))
    snapshots_valid=all(s.get('serial_match') for s in snapshots.values())
    def data(name):return snapshots[name].get('data',{})
    end=snapshots['after-smoke'].get('serial_lines',[])
    # Original final checks ran immediately after this snapshot, before cleanup's
    # Idle transition. Do not count the separate normal-image restore as a reset.
    smoke_lines=serial[:end[0]] if len(end)==1 else []
    reconnect=r.reconnect_witness(results,smoke_lines,data('before-reconnect'),data('after-reconnect'))
    epochs=[r.counter_check(data(a),data(b)) for a,b in
            [('before-smoke','before-reconnect'),('after-reconnect','after-smoke')]]
    audit=r.final_audit((root/'bot.log').read_text(),smoke_lines,epochs)
    idle_path=root/'idle.json'
    idle=json.loads(idle_path.read_text()) if idle_path.exists() else {}
    idle_start=snapshots['before-smoke'].get('serial_lines',[])
    idle_end=snapshots['after-idle'].get('serial_lines',[])
    idle_lines=serial[idle_start[0]:idle_end[0]] if len(idle_start)==len(idle_end)==1 else []
    idle_ok=bool(idle.get('passed') and idle.get('seconds')==r.POLICY['idle_s']
        and idle.get('end_ns',0)-idle.get('start_ns',0)>=r.POLICY['idle_s']*1e9
        and idle_lines and not any(re.search(r'\bSTATE (Idle|Disconnected|Failed)',line) for line in idle_lines)
        and data('before-smoke').get('generation')==data('after-idle').get('generation')
        and data('before-smoke').get('pc_id')==data('after-idle').get('pc_id'))
    cleanup=saved.get('cleanup_attempts',[])
    restore_serial=root/'restore-1-serial.log'
    restore_boot=restore_serial.exists() and 'elf_sha256='+RETAINED_NORMAL_HASH in restore_serial.read_text()
    cleanup_ok=bool(cleanup and all(not c.get('errors') and c.get('owned_bot_stopped')
        and c.get('restored_elf_sha256')==RETAINED_NORMAL_HASH for c in cleanup)
        and saved.get('lock_released') and restore_boot)
    journal_matches=json.loads((root/'counter-epochs.json').read_text())==epochs[:1]
    setup_ok=any('elf_sha256='+RETAINED_NORMAL_HASH in line for line in smoke_lines) and saved.get('setup',{}).get('boot_elf_sha256')==RETAINED_NORMAL_HASH and saved.get('setup',{}).get('public_env')==r.PUBLIC_ENV
    ids=[w['id'] for w in windows]
    checks=dict(all_declared_windows_present=ids==[name for name,_ in r.CHECKS],
        all_individual_turns_complete=all(t['passed'] for w in results for t in w['individual_turns']),
        all_window_functional_witnesses=all(w['passed'] for w in results),
        reconnect_peer_and_generation=reconnect['passed'],snapshot_serial_binding=snapshots_valid,
        idle=idle_ok,setup_identity_policy=setup_ok,cleanup=cleanup_ok,
        first_counter_journal_matches=journal_matches,**audit['checks'])
    exact=all(p['declared_window_stimulus_followed'] for p in protocol)
    return dict(schema='rollback-01-rejudge-v1',report_only=True,hardware_access=False,qualification=False,
        original_result_passed=saved.get('passed'),original_error=saved.get('error'),
        functional_claims_supported=all(checks.values()),functional_checks=checks,
        all_declared_checks_passed=all(checks.values()) and exact,declared_stimulus_sequence_followed=exact,
        protocol_windows=protocol,windows=results,reconnect=reconnect,final_audit=audit,
        snapshots={name:{k:v for k,v in s.items() if k!='data'} for name,s in snapshots.items()},
        original_execution=dict(after_smoke_snapshot='reached; retained and checked against raw PERF' if end else 'not evaluated',
            reconnect='reached; original all-window peer-set check raised',
            half_duplex_policy='not reached; evaluated now from retained bot.log',
            liveness='not reached; evaluated now from retained bot.log',
            boot_and_fatal_audit='not reached; evaluated now through after-smoke raw serial boundary' if end else 'not evaluated',
            first_counter_epoch='reached; counter-epochs.json retained; recomputed now',
            second_counter_epoch='not reached; evaluated now from retained after-reconnect/after-smoke snapshots' if end else 'not evaluated',
            result_smoke_field='not assigned because smoke() raised before return; original result.json unchanged'),
        counter_epoch_journal_matches=journal_matches,
        idle_witness=dict(passed=idle_ok,retained=idle),cleanup_witness=dict(passed=cleanup_ok,restore_boot_hash_present=restore_boot),
        interpretation=('Functional half-duplex rollback witnesses pass' if all(checks.values()) else 'Functional evidence failed or incomplete')+'; inspect protocol_windows separately for off-script/extra turns. Operator audibility notes are window-scoped. Semantic answer correctness is not an audio/transport check.')


def rejudge(root):
    root=Path(root).resolve();output=root/'rejudge.json'
    if output.exists() or output.is_symlink():raise FileExistsError('Refuse to overwrite rejudge.json')
    before=file_hashes(root)
    result=evaluate(root)
    after=file_hashes(root)
    if before!=after:raise RuntimeError('Original retained evidence changed during analysis')
    result.update(analysis_commit=r.head(),analysis_code_sha256={str(p.relative_to(r.ROOT)):r.sha(p) for p in
        (r.ROOT/'hil/echoear_rollback.py',r.ROOT/'hil/echoear_rollback_rejudge.py')},
        original_files_sha256=before,original_files_unchanged=True)
    with output.open('x') as stream:stream.write(json.dumps(result,indent=2,allow_nan=False)+'\n')
    if before!=file_hashes(root):raise RuntimeError('Original evidence changed while publishing rejudge')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('retained_run',type=Path)
    result=rejudge(parser.parse_args().retained_run)
    print(json.dumps({k:result[k] for k in ('functional_claims_supported','all_declared_checks_passed','original_files_unchanged')}))
