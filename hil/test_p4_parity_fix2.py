"""Fix-2 regressions: host-only; canonical pins and copied retained witnesses."""
from collections import deque
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import p4_parity as parity
import p4_parity_campaign as campaign
import p4_parity_liveness as live
from p4_parity_restore import restore_proof
from p4_parity_display_score import score_display
from p4_parity_serial import window,text,journal,serial_trace
from test_p4_parity import populate_liveness_proof
from test_p4_parity_fix1 import real_plan,copy_restore,display_case,rebind_storage_fixture,put
from test_p4_liveness import p4_run


def test_serial_trace_preserves_reader_ring_and_raw_cursor_timestamps(tmp_path):
    ring=deque([(1,1.,'first')],maxlen=3)
    board=SimpleNamespace(_cv=threading.Condition(),_lines=ring)
    with serial_trace(board,tmp_path/'serial-entries.jsonl'):
        with board._cv:board._lines.append((2,2.,'second'))
        (tmp_path/'serial.log').write_bytes(b'first\r\nsecond\r\n')
        rows=journal(tmp_path)
        assert rows[1]==dict(cursor=2,monotonic_ns=2*10**9,line='second')
    assert board._lines is ring and list(ring)==[(1,1.,'first'),(2,2.,'second')]


def live_args(plan,root):
    p=parity.load(plan);bot=root/'fault-bot';bot.mkdir()
    identity={k:p['fault_bot_policy'][k] for k in ('bot_source_sha','policy_source_sha','profile_manifest')}
    identity['bot_instance']='boot:101:1'
    put(bot/'identity.json',identity);(bot/'dc-events.jsonl').write_text('')
    (bot/'bot.log').write_text('');(bot/'bot.pid').write_text('101')
    put(bot/'ready.json',dict(identity=identity,observer_sha256=p['fault_bot_policy']['observer_sha256'],up_monotonic_ns=1))
    return SimpleNamespace(plan=plan,board='p4xc',bundle=Path(p['images']['p4xc/demo']['bundle']),
        out=root/'liveness/p4xc-demo/attempt-01',bot_dir=bot,bot_identity=bot/'identity.json',
        bot_repo=Path.home()/'src/dg-esp32',restart_dir=root/'new-bot',restore_record=Path(p['restore_record']),
        restore_record_sha256=p['restore_record_sha256'],bounds=live.smoke.BOUNDS_FILE,drain_seconds=150,
        reference_only=False,headless=False,calibration=False)


@pytest.mark.parametrize('change',['missing-ready','wrong-ready','wrong-restore','restart-dir','busy-lock','tools','bot-process'])
def test_host_liveness_refusal_leaves_ledger_unchanged(real_plan,monkeypatch,change):
    import boards
    plan,root=real_plan;a=live_args(plan,root)
    # Device-free host dependencies only; no service/lock is opened by tests.
    monkeypatch.setattr(boards,'require_bench',lambda:123)
    monkeypatch.setattr(live.smoke,'live_bot_identity',lambda *args:101)
    monkeypatch.setattr(live.smoke.LiveIO,'preflight_tools',lambda self:None)
    monkeypatch.setattr(live.smoke.LiveIO,'preflight_restore',lambda self:None)
    monkeypatch.setattr(boards,'checked_esptool_python',lambda:'/usr/bin/python3')
    monkeypatch.setenv('ESPTOOL_PYTHON','/usr/bin/python3')
    if change=='missing-ready':(a.bot_dir/'ready.json').unlink()
    elif change=='wrong-ready':
        marker=parity.load(a.bot_dir/'ready.json');marker['up_monotonic_ns']='bad';put(a.bot_dir/'ready.json',marker)
    elif change=='wrong-restore':a.restore_record_sha256='f'*64
    elif change=='restart-dir':a.restart_dir.mkdir()
    elif change=='busy-lock':monkeypatch.setattr(boards,'require_bench',lambda:(_ for _ in ()).throw(RuntimeError('busy')))
    elif change=='tools':monkeypatch.setattr(live.smoke.LiveIO,'preflight_tools',lambda self:(_ for _ in ()).throw(ValueError('tools missing')))
    else:monkeypatch.setattr(live.smoke,'live_bot_identity',lambda *args:(_ for _ in ()).throw(ValueError('bot missing')))
    argv=['live','--board',a.board,'--plan',str(plan),'--bundle',str(a.bundle),'--out',str(a.out),
        '--bot-dir',str(a.bot_dir),'--bot-identity',str(a.bot_identity),'--restart-dir',str(a.restart_dir),
        '--restore-record',str(a.restore_record),'--restore-record-sha256',a.restore_record_sha256]
    with pytest.raises((ValueError,RuntimeError,OSError)):live.main(argv)
    assert not (root/'attempts.jsonl').exists() and not a.out.exists()


def test_liveness_valid_host_preflight_does_not_register(real_plan,monkeypatch):
    import p4_parity_preflight
    plan,root=real_plan;a=live_args(plan,root);called=[]
    monkeypatch.setattr(p4_parity_preflight,'host_checks',lambda *args:called.append(args))
    prepared=live.preflight(a)
    assert prepared[1]['profile']=='demo' and called and not (root/'attempts.jsonl').exists()


@pytest.mark.parametrize('change',['missing-erle','host-error','valid'])
def test_smoke_host_preflight_happens_before_registration(real_plan,monkeypatch,change):
    import p4_parity_smoke as smoke
    import p4_parity_preflight
    plan,root=real_plan;p=parity.load(plan);identity=p['product_identity_file']
    variant='aec-demo' if change=='missing-erle' else 'normal';run=root/'smoke'/('p4xc-'+variant)/'attempt-01'
    called=[]
    def host(*args):
        called.append('preflight')
        if change=='host-error':raise ValueError('bot not launched')
    monkeypatch.setattr(p4_parity_preflight,'host_checks',host)
    def execute(args,plan):
        called.append('acquire');assert (root/'attempts.jsonl').is_file()
        run.mkdir(parents=True);put(run/'result.json',dict(board='p4xc',variant=variant,acquisition_passed=False))
        return 1
    monkeypatch.setattr(smoke,'execute',execute)
    argv=['--plan',str(plan),'--board','p4xc','--variant',variant,'--out',str(run),
        '--bot-dir',str(root/'bot'),'--bot-identity',identity]
    if change=='valid':assert smoke.main(argv)==1 and called==['preflight','acquire']
    else:
        with pytest.raises(ValueError):smoke.main(argv)
        assert not (root/'attempts.jsonl').exists() and not run.exists() and 'acquire' not in called


@pytest.mark.parametrize('phase',['candidate','restore'])
@pytest.mark.parametrize('change',['none','address','flash-input','order','port','chip','destination','failed-log',
    'truncated-verify','truncated-erase','truncated-read','read-size'])
def test_storage_operation_arguments_order_and_completion(tmp_path,phase,change):
    a=copy_restore(tmp_path,'p4xc');directory=tmp_path if phase=='candidate' else next(tmp_path.glob('restore-storage-*'))
    bundle=Path(a['bundle']);rebind_storage_fixture(directory,bundle)
    if change=='none':assert restore_proof(tmp_path,a)['storage']['verified_bytes']==4194304;return
    path=directory/'candidate-storage-erase.json';row=parity.load(path);commands=row['commands']
    if change=='address':commands[1][-2:]=['0x9000','0x6000']
    elif change=='flash-input':commands[0][-1]='/wrong/dg-esp32.bin'
    elif change=='order':commands[0],commands[1]=commands[1],commands[0]
    elif change=='port':commands[1][commands[1].index('--port')+1]='/dev/wrong'
    elif change=='chip':commands[1][commands[1].index('--chip')+1]='esp32s3'
    elif change=='destination':commands[2][-1]=str(directory/'detached-readback.bin')
    elif change=='failed-log':
        for label in ('verify','erase','readback'):(directory/('candidate-storage-'+label+'.log')).write_text('FAILED: operation was not performed\n')
    elif change.startswith('truncated'):
        label=change.split('-')[1];label='readback' if label=='read' else label
        (directory/('candidate-storage-'+label+'.log')).write_text('Staying in bootloader.\n')
    else:
        log=directory/'candidate-storage-readback.log';log.write_text(log.read_text().replace('Read 4194304','Read 4096'))
    put(path,row)
    with pytest.raises(ValueError):restore_proof(tmp_path,a)


@pytest.mark.parametrize('board',parity.BOARDS)
@pytest.mark.parametrize('position',['before','between','after'])
@pytest.mark.parametrize('fatal',['Guru Meditation Error: restore boot panicked','Stack canary watchpoint triggered',
    'abort() was called at PC 0x123','assert failed: recovery'])
def test_restore_fatal_anywhere_cannot_hide_behind_final_good_boot(tmp_path,board,position,fatal):
    a=copy_restore(tmp_path,board);path=tmp_path/('restore-serial.log' if board=='p4xc' else 'restore-1-serial.log')
    lines=path.read_text().splitlines();last=max(i for i,l in enumerate(lines) if 'elf_sha256=' in l)
    lines.insert(0 if position=='before' else last if position=='between' else len(lines),fatal)
    path.write_text('\n'.join(lines)+'\n')
    with pytest.raises(ValueError,match='fatal diagnostic'):restore_proof(tmp_path,a)


@pytest.mark.parametrize('category',['smoke','liveness','display','touch','erle'])
@pytest.mark.parametrize('raw',[b'{"acquisition_passed":',b'\xffnot json'])
def test_abandon_malformed_original_collect_all_table_preserves_bytes(real_plan,category,raw):
    plan,root=real_plan;variant='demo' if category=='touch' else 'normal';run=root/category/'p4xc'/ 'attempt-01'
    campaign.register(plan,category,'p4xc',variant,run);run.mkdir(parents=True)
    name='result.json' if category in ('smoke','erle') else 'touch.json' if category=='touch' else 'acquisition.json'
    primary=run/name;primary.write_bytes(raw);before=parity.digest(primary)
    parity.main(['abandon','--plan',str(plan),'--run',str(run),'--reason','interrupted raw write'])
    parity.collect_all(plan,root/'receipts')
    files=list((root/'receipts').glob('*.json'));assert len(files)==1
    receipt=parity.receipt(files[0]);assert receipt['status']=='fail' and receipt['sources'][str(primary)]==before
    parity.main(['table','--plan',str(plan),'--receipt',str(files[0]),'--out',str(root/'table.md')])
    assert str(run) in (root/'table.md').read_text() and primary.read_bytes()==raw and parity.digest(primary)==before
    with pytest.raises(ValueError,match='scored/unfinished'):campaign.register(plan,category,'p4xc',variant,run.parent/'attempt-02')


def inconclusive(run):
    a=copy_restore(run,'p4xc');a.update(acquisition_passed=False,inconclusive=True,error='C6ResetObserved: report only',
        c6_window=dict(start_cursor=10,start_ns=10*10**9),failure_ns=11*10**9,failure_cursor=13,
        c6_resets=[dict(cursor=12,host_ns=10*10**9,line='C6 watchdog reset observed',boot=False)])
    entries=[dict(cursor=i,monotonic_ns=9*10**9+i*10**8,line='ordinary diagnostic') for i in range(1,14)]
    entries[9]['line']='STATE Ready gen=1';entries[11]['line']='C6 watchdog reset observed';entries[12]['line']='SESSION FAIL gen=1'
    a['c6_window']['start_ns']=entries[9]['monotonic_ns'];a['c6_resets'][0]['host_ns']=entries[11]['monotonic_ns']
    a['failure_ns']=entries[12]['monotonic_ns']
    (run/'serial.log').write_text(text(entries));(run/'serial-entries.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries))
    return a


@pytest.mark.parametrize('change',['none','cleanup','cleanup-list','restore','factory','missing-raw','no-causal-reset'])
def test_only_validated_causal_reset_with_clean_recovery_is_unverified(real_plan,change):
    plan,root=real_plan;run=root/'liveness/p4xc-normal/attempt-01'
    with campaign.attempt(plan,'liveness','p4xc','normal',run):
        a=inconclusive(run)
        if change=='cleanup':a['cleanup_error']='disconnect failed'
        elif change=='cleanup-list':a['cleanup_errors']=['disconnect failed']
        elif change=='restore':a['restore'].update(clean=False,attempts=['restore failed']);put(run/'restore.json',a['restore'])
        elif change=='factory':a['restore']['action']='p4-factory';put(run/'restore.json',a['restore'])
        elif change=='missing-raw':(run/'restore.json').unlink()
        elif change=='no-causal-reset':a['c6_resets'][0]['host_ns']=12*10**9
        put(run/'acquisition.json',a)
    row=parity.bound_receipt(plan,'liveness','p4xc','normal',run)
    assert row['status']==('unverified' if change=='none' else 'fail')
    assert row['attempt_binding']['disposition']==('inconclusive' if change=='none' else 'failed')
    if change!='none':assert '| unverified | fail |' in next(l for l in parity.render([row],plan).splitlines() if l.startswith('| F19 '))


@pytest.mark.parametrize('change',['flag-only','cleanup','raw-restore','scored'])
def test_untouched_flag_alone_never_downgrades_failed_acquisition(tmp_path,change):
    a=dict(board='p4xc',variant='normal',acquisition_passed=False,device_untouched=True)
    if change!='flag-only':a.update(status='preflight-noisy',retryable=True,score_produced=False,room=dict(noise_contaminated=True))
    if change=='cleanup':a['cleanup_errors']=['failed cleanup']
    elif change=='raw-restore':put(tmp_path/'restore.json',dict(clean=False))
    elif change=='scored':a['score_produced']=True
    put(tmp_path/'result.json',a)
    assert parity.collect('smoke','p4xc','normal',tmp_path)['status']=='fail'


def test_frozen_smoke_room_sibling_is_bound_to_its_registered_original(real_plan):
    plan,root=real_plan;run=root/'smoke/p4xc-aec-demo/attempt-01';room=run.with_name(run.name+'-room')
    with campaign.attempt(plan,'smoke','p4xc','aec-demo',run):
        room.mkdir(parents=True);run.mkdir()
        put(room/'result.json',dict(noise_contaminated=True))
        put(run/'result.json',dict(board='p4xc',variant='aec-demo',acquisition_passed=False,
            device_untouched=True,status='preflight-noisy',retryable=True,score_produced=False,room=dict(noise_contaminated=True)))
    row=parity.bound_receipt(plan,'smoke','p4xc','aec-demo',run)
    assert row['status']=='unverified' and str(room/'result.json') in row['sources']
    put(room/'result.json',dict(noise_contaminated=False))
    with pytest.raises(ValueError,match='result changed'):campaign.inventory(plan)


def insert_display(directory,a,extra,update_wire):
    rows=parity.load(directory/'events.json')  # Preserve event source unmodified.
    lines=(directory/'run-serial.log').read_text().splitlines();at=a['wire_window']['end_cursor'];lines[at:at]=extra
    entries=[dict(cursor=i+1,monotonic_ns=i+1,line=line) for i,line in enumerate(lines)]
    a['run_window']=window(entries,0,len(entries));a['wire_window']=window(entries,a['wire_window']['start_cursor'],at+len(extra))
    commands=[json.loads(l) for l in (directory/'commands.jsonl').read_text().splitlines()]
    for c in commands:
        for key in ('start_cursor','end_cursor'):
            if c[key]>=at:c[key]+=len(extra)
    (directory/'commands.jsonl').write_text(''.join(json.dumps(c)+'\n' for c in commands))
    (directory/'serial-entries.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries))
    for name in ('serial.log','run-serial.log'):(directory/name).write_text('\n'.join(lines)+'\n')
    if update_wire:(directory/'wire.log').write_text('\n'.join(lines[a['wire_window']['start_cursor']:at+len(extra)])+'\n')
    put(directory/'acquisition.json',a)


@pytest.mark.parametrize('board',parity.BOARDS)
@pytest.mark.parametrize('change',['missing','contradiction','duplicate','swapped','session-loss','window'])
def test_display_wire_must_equal_original_active_window(tmp_path,board,change):
    a=display_case(tmp_path,board,'normal');assert score_display(tmp_path,board,'normal')['checks_passed']
    if change=='missing':
        path=tmp_path/'run-serial.log';path.write_text('\n'.join(l for l in path.read_text().splitlines() if 'WIRE_CONTRACT ' not in l)+'\n')
    elif change=='contradiction':insert_display(tmp_path,a,['WIRE_CONTRACT start={"audio":{"v":999}}','SESSION FAIL: contradictory retained session'],False)
    elif change=='duplicate':
        line=next(l for l in (tmp_path/'wire.log').read_text().splitlines() if 'WIRE_CONTRACT start=' in l)
        insert_display(tmp_path,a,[line],True)
    elif change=='swapped':
        peer=tmp_path/'peer';display_case(peer,'echoear' if board=='p4xc' else 'p4xc','normal')
        (tmp_path/'wire.log').write_bytes((peer/'wire.log').read_bytes())
    elif change=='session-loss':insert_display(tmp_path,a,['SESSION FAIL: lost before intentional disconnect'],True)
    else:
        a['wire_window']['start_cursor']+=1;put(tmp_path/'acquisition.json',a)
    with pytest.raises((ValueError,KeyError,TypeError)):score_display(tmp_path,board,'normal')
    assert parity.collect('display',board,'normal',tmp_path)['status']=='fail'


def test_persisted_assets_can_be_evicted_from_ram_and_both_pair_legs_still_pass(tmp_path):
    from p4_parity_display import pair
    runs=[]
    for board in parity.BOARDS:
        directory=tmp_path/board;display_case(directory,board,'normal')
        for name in ('serial.log','run-serial.log'):
            path=directory/name;path.write_text(path.read_text().replace(',0,true,true,true,"none"]',',0,false,true,true,"none"]'))
        entries=[json.loads(l) for l in (directory/'serial-entries.jsonl').read_text().splitlines()]
        for e in entries:e['line']=e['line'].replace(',0,true,true,true,"none"]',',0,false,true,true,"none"]')
        (directory/'serial-entries.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries))
        assert score_display(directory,board,'normal')['checks_passed'];runs.append(directory)
    assert pair(*runs)['exact_wire_passed']


def real_liveness(plan,run):
    a=copy_restore(run,'p4xc','demo');base=p4_run(run);base.update(a);a=base
    manifest=parity.load(Path(a['bundle'])/'manifest.json')
    a.update(elf_sha256=manifest['elf_sha256'],variant='demo',aec_enabled=False,reference_only=False)
    populate_liveness_proof(run,a,None,planned=plan)
    put(run/'image.json',manifest)
    return a


@pytest.mark.parametrize('change',['none','wire-swap','serial-swap','interval-gen','sample-gen','ready-gen','close','boot','channel','active-end-loss'])
def test_real_plan_liveness_generation_window_resource_and_channel_binding(real_plan,change):
    plan,root=real_plan;run=root/'liveness/p4xc-demo/attempt-01'
    with campaign.attempt(plan,'liveness','p4xc','demo',run):
        a=real_liveness(plan,run);assert live.score(run,acquisition=a)['checks_passed']
        s=a['sessions'][-1];wire=run/s['wire_file']
        assert s['generation']!=a['sessions'][0]['generation']
        if change=='wire-swap':wire.write_bytes((run/a['sessions'][0]['wire_file']).read_bytes());s['wire_sha256']=parity.digest(wire)
        elif change=='serial-swap':(run/(s['label']+'-serial.log')).write_bytes((run/'main-serial.log').read_bytes())
        elif change=='interval-gen':
            old='UPLINK_INTERVALS gen='+str(s['generation']);new='UPLINK_INTERVALS gen=1'
            for name in ('serial.log',s['label']+'-serial.log',s['wire_file']):
                path=run/name;path.write_text(path.read_text().replace(old,new))
            entries=[json.loads(l) for l in (run/'serial-entries.jsonl').read_text().splitlines()]
            for e in entries:
                if e['cursor']>s['serial_window']['start_cursor']:e['line']=e['line'].replace(old,new)
            (run/'serial-entries.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries));s['wire_sha256']=parity.digest(wire)
        elif change=='sample-gen':s['samples'][0]['generation']=1
        elif change=='ready-gen':s['generation']=1
        elif change=='close':s['ended_ns']+=1
        elif change=='boot':s['boot_cursor']=0
        elif change=='channel':s['channel']=a['sessions'][0]['channel']
        elif change=='active-end-loss':
            from p4_parity_serial import select
            entries=[json.loads(l) for l in (run/'serial-entries.jsonl').read_text().splitlines()]
            entries[s['stop_cursor']-1]['line']='SESSION FAIL: lost at final active cursor'
            (run/'serial-entries.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries))
            (run/'serial.log').write_text(text(entries));segment=select(entries,s['serial_window'])
            (run/(s['label']+'-serial.log')).write_text(text(segment))
            caps=[l for l in (run/(s['label']+'-bot.log')).read_text().splitlines() if 'DEMO_CAPABILITY ' in l]
            _,saved,_=live.liveness_capture([r['line'] for r in segment],caps,a['board'],
                live.smoke.bounds_for(a['bounds_path'],a['variant']))
            wire.write_text('\n'.join(saved)+'\n');s['wire_sha256']=parity.digest(wire)
        put(run/'acquisition.json',a)
    row=parity.bound_receipt(plan,'liveness','p4xc','demo',run)
    assert row['status']==('pass' if change=='none' else 'fail')
    if change=='active-end-loss':assert 'active session failed' in row['reason']
    if change=='none':assert parity.receipt(_receipt(root,row))['status']=='pass'


def _receipt(root,row):
    path=root/'receipt.json';put(path,row);return path
