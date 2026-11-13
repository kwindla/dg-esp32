"""Offline gate mutants and live orchestration witnesses; no device/bot IO."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
import importlib.util
import os
import p4_contention as c
if os.environ.get('DG_P4_CONTENTION_SOURCE'):
    spec=importlib.util.spec_from_file_location('contention_under_test',os.environ['DG_P4_CONTENTION_SOURCE'])
    c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
from make_bundle import digest
from test_wire_contract import capture, serialize, IDENTITY
from wire_contract import Bounds


def perf(gen=1, at=3000000, *, board='p4xc'):
    row=copy.deepcopy(real_perf(board))
    row.update(generation=gen,device_us=at,pc_id='test-peer',per_core_idle_percent=[25,70])
    # Historical AEC-off records predate this additive schema. These modeled
    # driver counters are supplied by the fake device, not claimed as
    # observations in that old capture. The unmodified record must fail below.
    row['capture']=dict(rx_overflows=0,rx_completions=at//20000,read_errors=0,read_timeouts=0,read_frames=320)
    return row


def valid_run(tmp_path, monkeypatch, *, board='p4xc'):
    refs=[dict(p99_us=value,path='fixed-'+str(i),sha256='a'*64) for i,value in enumerate((18000,20000,22000))]
    monkeypatch.setattr(c,'normal_references',lambda _: (Bounds(68411,7000),refs))
    sessions=[]
    for i in range(2):
        wire=capture('esp32p4' if board=='p4xc' else 'esp32s3',False,'bench-display'); wire.records['msg=display-result']['values']['id']=c.REQUEST_ID
        row=wire.intervals[0]; row.update(gen=i+1,n=7499,ok=7500,attempts=7500,end_us=153000000)
        row['hist'][20]=7499
        file=tmp_path/f'generation-{i}-wire.log';file.write_text('\n'.join(serialize(wire))+'\n')
        sessions.append(dict(wire_file=file.name,wire_sha256=digest(file),started_s=i*150,ended_s=(i+1)*150,
            samples=[perf(i+1,board=board),perf(i+1,153000000,board=board)],
            display_status=[dict(bench=[1,30,0],updates=[0,0,1],deadline_misses=0,failed=False,dead=False),
                            dict(bench=[1,30,300],updates=[0,0,4000],deadline_misses=3,failed=False,dead=False)],
            touch=[dict(initialized=True,enabled=False,driver=dict(reads=n,interrupts=0)) for n in (1,7000)],
            bot_events=[dict(event=e,mode="live-test",pc_id="test-peer") for e in ('stt-settings','final-transcript','first-tts-byte')],
            cycles=[dict(at_s=i*150+n*5,http_gets=1,persisted=True,deleted=True) for n in range(30)]))
    acquisition=dict(acquisition_passed=True,board=board,restore=dict(clean=True),limits=c.LIMITS,elapsed_s=300,
        sessions=sessions,restart=[dict(at_s=150,rendered_before=4000,rendered_after=4001)])
    (tmp_path/'serial.log').write_text('P4_SCAN sample\nunderrun happens\n')
    (tmp_path/'bounds.json').write_text('{}')
    return acquisition


def test_gate_passes_and_keeps_restart_generations_separate(tmp_path,monkeypatch):
    acquisition=valid_run(tmp_path,monkeypatch)
    result=c.score(tmp_path,tmp_path/'bounds.json',acquisition=acquisition)
    assert result['checks_passed'] and not result['passed'] and not result['qualification']
    assert [r['generation'] for r in result['generations']]==[1,2]
    assert result['generations'][0]['render_deadline_misses']['delta']==3
    assert result['generations'][0]['resources']['per_core_cpu'][0]['busy_percent']==[75,30]
    assert result['display_underrun_lines']==1 and result['f11']=='unverified'


@pytest.mark.parametrize('mutation,reason',[
    ('duration','five minutes'),('restart','exactly one'),('scan','scan-out'),('heap','heap floor'),
    ('largest','heap floor'),('transient-largest','heap floor'),('alloc','failed allocation'),
    ('overflow','capture overflow'),('underrun','playback underrun'),('cpu','per-core CPU'),
    ('logs','lost diagnostics'),('dtls','DTLS'),('stt','STT'),('fetch','fetch/LittleFS'),
    ('persist','fetch/LittleFS'),('cycle-count','coverage'),('workload-gap','stalled'),
    ('animation','animation'),('fatal','fatal'),('restore','unclean'),('wire','wire capture changed'),
    ('touch','touch poller'),('generation','generation differs'),('short','short contention'),('limits','limits differ')])
def test_each_missing_or_failed_gate_refuses(tmp_path,monkeypatch,mutation,reason):
    a=valid_run(tmp_path,monkeypatch); s=a['sessions'][0]; p=s['samples'][0]
    if mutation=='duration': a['elapsed_s']=299
    elif mutation=='restart': a['restart']=[]
    elif mutation=='scan': a['restart'][0]['rendered_after']=0
    elif mutation=='heap': p['heap']['internal']['minimum_free']=81919
    elif mutation=='largest': p['heap']['internal']['largest']=32767
    elif mutation=='transient-largest': p['heap']['internal']['sampled_largest_minimum']=32767
    elif mutation=='alloc': p['failed_allocations']['total']=1
    elif mutation=='overflow': p['capture']['rx_overflows']=1
    elif mutation=='underrun': p['i2s_underflows']=1
    elif mutation=='cpu':
        for p in s['samples']: p['per_core_idle_percent']=[None,None]
    elif mutation=='logs': p['diagnostic_log_drops']=1
    elif mutation=='dtls': s['samples'][-1]['dtls_duration_us']=0
    elif mutation=='stt': s['bot_events']=[e for e in s['bot_events'] if e['event']!='final-transcript']
    elif mutation=='fetch': s['cycles'][0]['http_gets']=0
    elif mutation=='persist': s['cycles'][0]['persisted']=False
    elif mutation=='cycle-count': s['cycles']=s['cycles'][:2]
    elif mutation=='workload-gap': s['cycles']=s['cycles'][:8]+s['cycles'][12:]
    elif mutation=='animation': s['display_status'][-1]['updates'][2]=1
    elif mutation=='fatal': (tmp_path/'serial.log').write_text('Guru Meditation')
    elif mutation=='restore': a['restore']['clean']=False
    elif mutation=='wire': (tmp_path/s['wire_file']).write_text('changed')
    elif mutation=='touch': s['touch'][0]['initialized']=False
    elif mutation=='generation': p['generation']=9
    elif mutation=='short': s['samples'][-1]['device_us']=10000000
    elif mutation=='limits': a['limits']=dict(c.LIMITS,stall_us=90000)
    with pytest.raises(ValueError,match=reason): c.score(tmp_path,tmp_path/'bounds.json',acquisition=a)


def test_p99_pair_failure_cannot_be_hidden_by_clean_maximum(tmp_path,monkeypatch):
    a=valid_run(tmp_path,monkeypatch)
    monkeypatch.setattr(c,'normal_references',lambda _: (Bounds(68411,7000),[dict(p99_us=28001)]*3))
    with pytest.raises(ValueError,match='p99'): c.score(tmp_path,tmp_path/'bounds.json',acquisition=a)


def test_real_f11_requires_observation_counts_and_no_injection():
    before=dict(initialized=True,enabled=True,driver=dict(injected=0,double_taps=0),requests=0,generation=1)
    after=dict(before,driver=dict(injected=0,double_taps=1),requests=1,generation=2)
    assert c.f11(before,after)=='unverified'
    assert c.f11(before,after,True)=='observed'
    after['driver']['injected']=1
    with pytest.raises(ValueError,match='synthetic'): c.f11(before,after,True)
    after['driver']['injected']=0;after['generation']=1
    with pytest.raises(ValueError,match='reconnect'): c.f11(before,after,True)


def test_reference_hashes_and_bounds_are_real_and_fixed():
    # Pure offline re-derivation of all three retained normal captures.
    from board_smoke import BOUNDS_FILE
    if not Path('/home/khkramer/src/evidence/echoear/p4/step-11/calibration/runs/normal-1/result.json').exists():
        pytest.skip('coordinator reference archive not installed')
    bounds,refs=c.normal_references(BOUNDS_FILE)
    assert bounds.stall_us==68411 and len(refs)==3 and all(r['p99_us']>0 for r in refs)


def rewrite_wire(tmp_path, session, change=None, *, rows=None):
    from wire_contract import parse_capture
    path=tmp_path/session['wire_file']; value=parse_capture(path.read_text().splitlines())
    if change:change(value.intervals[0])
    if rows is not None:value.intervals=rows(value.intervals)
    path.write_text('\n'.join(serialize(value))+'\n');session['wire_sha256']=digest(path)


@pytest.mark.parametrize('kind,reason', [('max','exact maximum exceeds stall bound'),
    ('tail','trailing open gap exceeds stall bound'),('coverage','coverage'),
    ('count','500 required intervals'),('median','median outside frame cadence')])
def test_actual_measured_gate_cannot_be_removed(tmp_path,monkeypatch,kind,reason):
    a=valid_run(tmp_path,monkeypatch)
    def change(row):
        if kind=='max':
            row['hist'][20]-=1;row['hist'][68]=1;row['max_us']=68412;row['end_us']+=50000
        elif kind=='tail':row['last_ok_age_us']=68412;row['end_us']+=48412
        elif kind=='coverage':row['end_us']=163000000
        elif kind=='count':row.update(n=499,ok=500,attempts=500,end_us=13000000);row['hist'][20]=499
        elif kind=='median':row['hist'][17]=row['hist'][20];row['hist'][20]=0;row['max_us']=17000
    rewrite_wire(tmp_path,a['sessions'][0],change)
    with pytest.raises(ValueError,match=reason):c.score(tmp_path,tmp_path/'bounds.json',acquisition=a)


@pytest.mark.parametrize('mutation,reason',[
    ('one-reference','p99'),('restart-window','restart lacks'),('same-gen','new generation'),
    ('frozen-zero','frozen exactly once'),('frozen-two','frozen exactly once'),('one-session','exactly two'),
    ('touch-enabled','touch poller'),('touch-irq','polled touch'),('tts','STT/reply'),
    ('pcid','bot session differs'),('mode','live bot profile'),('delete','fetch/LittleFS'),
    ('bench','scene workload'),('perf-failed','display unavailable'),('perf-dead','display unavailable'),
    ('status-failed','failed render'),('status-dead','failed render'),('middle-stall','stalled between'),
    ('first-cycle','coverage'),('last-cycle','coverage'),('late-underrun','playback underrun')])
def test_review_survivors_refuse_for_intended_reason(tmp_path,monkeypatch,mutation,reason):
    a=valid_run(tmp_path,monkeypatch);s=a['sessions'][0]
    if mutation=='one-reference':monkeypatch.setattr(c,'normal_references',lambda _: (Bounds(68411,7000),[dict(p99_us=n) for n in (20000,18000,27001)]))
    elif mutation=='restart-window':a['restart'][0]['at_s']=160
    elif mutation=='same-gen':
        rewrite_wire(tmp_path,a['sessions'][1],lambda row:row.update(gen=1))
        for row in a['sessions'][1]['samples']:row['generation']=1
    elif mutation=='frozen-zero':rewrite_wire(tmp_path,s,rows=lambda _:[])
    elif mutation=='frozen-two':rewrite_wire(tmp_path,s,rows=lambda rows:[rows[0],dict(rows[0],gen=99)])
    elif mutation=='one-session':a['sessions']=a['sessions'][:1]
    elif mutation=='touch-enabled':s['touch'][0]['enabled']=True
    elif mutation=='touch-irq':s['touch'][-1]['driver']['interrupts']=1
    elif mutation=='tts':s['bot_events']=[e for e in s['bot_events'] if e['event']!='first-tts-byte']
    elif mutation=='pcid':s['samples'][-1]['pc_id']='other-peer'
    elif mutation=='mode':s['bot_events'][0]['mode']='offline-test'
    elif mutation=='delete':s['cycles'][4]['deleted']=False
    elif mutation=='bench':s['display_status'][0]['bench']=[0,30]
    elif mutation.startswith('perf-'):s['samples'][-1]['display'][mutation[5:]]=True
    elif mutation.startswith('status-'):s['display_status'][-1][mutation[7:]]=True
    elif mutation=='middle-stall':s['display_status'].insert(1,copy.deepcopy(s['display_status'][0]))
    elif mutation=='first-cycle':s['cycles'][0]['at_s']=10
    elif mutation=='last-cycle':s['cycles'][-1]['at_s']=135
    elif mutation=='late-underrun':s['samples'][-1]['i2s_underflows']=1
    with pytest.raises(ValueError,match=reason):c.score(tmp_path,tmp_path/'bounds.json',acquisition=a)


@pytest.mark.parametrize('key,value',[('stall_us',68412),('p99_delta_us',7001),('coverage',.96)])
def test_fixed_tuple_is_not_monkeypatched_away(tmp_path,key,value):
    from board_smoke import BOUNDS_FILE
    fixture=json.loads(BOUNDS_FILE.read_text());fixture['variants']['normal'][key]=value
    path=tmp_path/'bounds.json';path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError,match='fixed normal bounds differ'):c.normal_references(path)


def real_perf(board='p4xc'):
    run='step-12b/fix-1/coordinator-voice-p4xc' if board=='p4xc' else 'step-13a/fix-1/coordinator-smoke-normal'
    source=Path.home()/'src/evidence/echoear/p4'/run/'main-serial.log'
    if not source.is_file():pytest.skip('real step-12b AEC-off PERF archive absent')
    rows=[json.loads(line.split('PERF ',1)[1]) for line in source.read_text().splitlines() if line.startswith('PERF {')]
    assert rows and 'aec' not in rows[-1]
    return rows[-1]


def real_display():
    source=Path.home()/'src/evidence/step9a/r2/production-stop-cleanup/serial.log'
    if not source.is_file():pytest.skip('real DISPLAY archive absent')
    return next(json.loads(line[8:]) for line in source.read_text().splitlines() if line.startswith('DISPLAY {'))


def test_old_real_aec_off_record_names_missing_counter_before_window():
    with pytest.raises(ValueError,match='missing board-neutral PERF.capture counters'):
        c.resources([real_perf(),real_perf()])


def test_additive_capture_keeps_historical_aec_block_identical():
    import subprocess
    path=Path(__file__).resolve().parents[1]/'main/performance.cpp'
    before=subprocess.check_output(['git','show','414204d:main/performance.cpp'],cwd=path.parents[1],text=True)
    current=path.read_text();anchor='#if CONFIG_DG_ECHOEAR_AEC\n    const auto aec=dg_audio::aec_status();'
    assert current.split(anchor,1)[1].split('#endif',1)[0]==before.split(anchor,1)[1].split('#endif',1)[0]
    additive=current.split(anchor,1)[0].split('const auto capture=dg_audio::microphone_status();',1)[1]
    assert all('"'+key+'",capture.'+key in additive for key in ('rx_overflows','rx_completions','read_errors','read_timeouts','read_frames'))


def test_scoped_bot_process_selection_ignores_uv_and_other_projects(tmp_path):
    repo=tmp_path/'dg-esp32';server=repo/'server';server.mkdir(parents=True)
    proc=tmp_path/'proc';proc.mkdir()
    def process(pid,command,cwd,executable):
        directory=proc/str(pid);directory.mkdir()
        (directory/'cmdline').write_bytes(b'\0'.join(a.encode() for a in command)+b'\0')
        (directory/'cwd').symlink_to(cwd);(directory/'exe').symlink_to(executable)
    process(100,['uv','run','python',str(server/'bot.py')],repo,'/usr/bin/uv')
    process(101,['python3','server/bot.py'],repo,'/usr/bin/python3')
    process(200,['python3','bot.py','--port','7860'],tmp_path/'unrelated','/usr/bin/python3')
    process(201,['uv','run','bot.py','--port','7871'],tmp_path/'unrelated','/usr/bin/uv')
    assert c.repo_bot_processes(repo,101,proc)==101
    with pytest.raises(ValueError,match='Python child') as error:c.repo_bot_processes(repo,100,proc)
    assert str(server/'bot.py') in str(error.value) and '--bot-repo '+str(repo) in str(error.value)
    process(102,['python3',str(server/'demo_bot.py')],repo,'/usr/bin/python3')
    with pytest.raises(ValueError,match='another bot'):c.repo_bot_processes(repo,101,proc)


def test_static_display_bytes_are_derived_from_bundle_elf():
    path=Path.home()/'src/evidence/echoear/p4/step-13b/final-bundles/bundle-p4xc-normal/dg-esp32.elf'
    if not path.is_file():pytest.skip('P4 ELF archive absent')
    result=c.display_static_ram(path)
    assert result['bytes']==sum(obj['bytes'] for obj in result['objects'].values())
    assert {'panel_history','init_commands'}<=set(result['objects']) and result['bytes']>0
    assert result['external_bss_bytes']==0 and result['layout']=='legacy-internal'
    assert result['elf_sha256']==digest(path)


def test_recovered_display_layout_measures_external_bss_and_internal_bookkeeping():
    path=Path.home()/'src/evidence/echoear/p4/step-13a/fix-2/bundle-p4xc-normal/dg-esp32.elf'
    if not path.is_file():pytest.skip('recovered P4 display ELF archive absent')
    result=c.display_static_ram(path)
    assert result['layout']=='external-bss' and result['external_bss_bytes']>0
    assert 0<result['bytes']<256
    assert result['bytes']==sum(obj['bytes'] for obj in result['objects'].values() if obj['section'].startswith('.dram'))
    assert result['external_bss_bytes']==result['objects']['panel_storage']['bytes']
    assert result['objects']['panel_storage']['type']=='SHT_NOBITS'
    assert result['elf_sha256']==digest(path)


@pytest.mark.parametrize('board',['p4xc','echoear'])
def test_real_board_schema_requires_live_completions_before_zero_is_measured(board):
    samples=[perf(at=3000000,board=board),perf(at=153000000,board=board)]
    report=c.resources(samples,board=board)
    assert report['capture_counter_live'] is True and report['capture_completions_delta']>0
    assert report['capture_overflows']==0 and report['capture_overflows_result']=='measured'
    assert report['capture_decision']=='decision-2026-11-01-capture-counter-liveness'
    # Stop registering either RX callback: both driver counters remain zero.
    for row in samples:row['capture'].update(rx_completions=0,rx_overflows=0)
    if board=='p4xc':
        with pytest.raises(ValueError,match='mic capture counter not live.*rx_completions'):c.resources(samples,board=board)
    else:
        report=c.resources(samples,board=board)
        assert report['capture_counter_live'] is False and report['capture_completions_delta']==0
        assert report['capture_overflows'] is None
        assert report['capture_overflows_result']=='unmeasured (report-only)'


@pytest.mark.parametrize('board',['p4xc','echoear','future-board'])
def test_dead_counter_policy_is_per_generation_and_future_boards_fail_closed(tmp_path,monkeypatch,board):
    # A large count carried from generation one does not prove generation two.
    a=valid_run(tmp_path,monkeypatch,board='echoear' if board=='echoear' else 'p4xc')
    a['board']=board
    for row in a['sessions'][1]['samples']:row['capture']['rx_completions']=900000
    if board=='future-board':
        with pytest.raises(ValueError,match='counter not live'):c.resources(a['sessions'][1]['samples'],board=board)
    elif board!='echoear':
        with pytest.raises(ValueError,match='counter not live'):c.score(tmp_path,tmp_path/'bounds.json',acquisition=a)
    else:
        result=c.score(tmp_path,tmp_path/'bounds.json',acquisition=a)
        assert result['checks_passed']
        assert result['generations'][0]['resources']['capture_overflows_result']=='measured'
        assert result['generations'][1]['resources']['capture_overflows_result']=='unmeasured (report-only)'


def test_live_s3_callback_still_gates_actual_overflows():
    samples=[perf(board='echoear'),perf(at=153000000,board='echoear')]
    samples[-1]['capture']['rx_overflows']=1
    with pytest.raises(ValueError,match='mic capture overflow'):c.resources(samples,board='echoear')


def test_pair_retains_s3_unmeasured_status_and_refuses_dead_p4(tmp_path,monkeypatch):
    directories=[tmp_path/'s3',tmp_path/'p4'];acquisitions=[]
    for directory,board in zip(directories,('echoear','p4xc')):
        directory.mkdir();a=valid_run(directory,monkeypatch,board=board)
        if board=='echoear':
            for session in a['sessions']:
                for row in session['samples']:row['capture']['rx_completions']=0
        (directory/'acquisition.json').write_text(json.dumps(a))
        (directory/'identity.json').write_text(json.dumps(IDENTITY));acquisitions.append(a)
    result=c.pair(*directories,directories[0]/'bounds.json')
    assert result['exact_wire_passed'] and result['checks_passed'] and not result['qualification']
    evidence=result['capture_overflow_evidence']
    assert [row['board'] for row in evidence]==['echoear','p4xc']
    assert all(g['capture_overflows'] is None and g['capture_overflows_result']=='unmeasured (report-only)' for g in evidence[0]['generations'])
    assert all(g['capture_overflows']==0 and g['capture_counter_live'] is True for g in evidence[1]['generations'])
    for row in acquisitions[1]['sessions'][1]['samples']:row['capture']['rx_completions']=0
    (directories[1]/'acquisition.json').write_text(json.dumps(acquisitions[1]))
    with pytest.raises(ValueError,match='counter not live'):c.pair(*directories,directories[0]/'bounds.json')
