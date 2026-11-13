"""Fixed-bound negative controls, synthetic data only; never enter the bench."""
import json
import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import p4_liveness as live
from make_bundle import digest
from test_wire_contract import capture, serialize
from wire_contract import Bounds


def sample(free=160000):
    return dict(heap=dict(internal=dict(free=free,minimum_free=100000,largest=70000,sampled_largest_minimum=60000)),
                failed_allocations=dict(total=0,internal=0),power=dict(available=0,valid=False))


def drained(free=160000,seconds=150):
    final=sample(free)
    return dict(after_cycle=20,requested_seconds=seconds,started_seconds=1000,wait_ended_seconds=1000+seconds,
        sample_started_seconds=1000+seconds,sample_ended_seconds=1001+seconds,held=dict(held=True),sample=final,
        settling=dict(converged=True,policy=live.SETTLED,samples=[copy.deepcopy(final) for _ in range(3)]))


def controls():
    sessions=[];events=[]
    for i in range(2):
        now=100+i*100;channel='bot:'+str(i)
        sessions.append(dict(boot=0,generation=1+2*i,channel=channel,ready_ns=now,ended_ns=now+50,epoch=i+10))
        events.extend([dict(kind='display-ready',epoch=i+10,channel=channel,monotonic_ns=now),
            dict(kind='ping',seq=1,channel=channel,monotonic_ns=now+1,direction='sent'),
            dict(kind='pong',seq=1,channel=channel,monotonic_ns=now+2,direction='received')])
        if i:
            events.extend([dict(kind='stale-display-probe',id='step14-stale-2',epoch=10,current_epoch=11,channel=channel,monotonic_ns=now+3),
                dict(kind='display-result',id='step14-stale-2',epoch=10,seq=1,accepted=False,error='stale-epoch',channel=channel,monotonic_ns=now+4)])
    return sessions,events


def test_sequence_restarts_per_generation_and_stale_display_is_dropped():
    sessions,events=controls();result=live.control_check(sessions,events)
    assert result['pongs']==2 and result['stale_display_drops']==1
    assert result['stale_pongs']==result['stale_display_applied']==0


@pytest.mark.parametrize('case,reason',[('duplicate','duplicate'),('pong-before-ping','unmatched'),
    ('stale-pong','stale pong'),('generation','generation'),('applied','stale display'),
    ('missing-drop','drop witness'),('missing-pongs','ping/pong'),('wrong-epoch','epoch fence')])
def test_control_mutants_fail_closed(case,reason):
    sessions,events=controls()
    if case=='duplicate':events.insert(3,dict(events[2],monotonic_ns=103))
    elif case=='pong-before-ping':events[2]['seq']=2
    elif case=='stale-pong':events.append(dict(kind='pong',seq=2,channel='bot:0',direction='received',monotonic_ns=300));events.insert(2,dict(kind='ping',seq=2,channel='bot:0',direction='sent',monotonic_ns=102))
    elif case=='generation':sessions[1]['generation']=1
    elif case=='applied':events[-1]['accepted']=True
    elif case=='missing-drop':events.pop()
    elif case=='missing-pongs':events=[row for row in events if row['kind']!='pong']
    else:events[-1]['epoch']=11
    with pytest.raises(Exception,match=reason):live.control_check(sessions,events)


def test_cold_boot_can_restart_generation_but_not_reuse_channel():
    sessions,events=controls();sessions[1].update(boot=1,generation=1)
    assert live.control_check(sessions,events)['generations']==2
    sessions[1]['channel']=sessions[0]['channel']
    with pytest.raises(Exception,match='channel reused'):live.control_check(sessions,events)


def test_heap_floors_include_sampled_fragmentation_and_twenty_cycle_decline():
    assert live.heap_check([sample()])['minimum_internal']==100000
    report=live.heap_decline([sample(160000-i*200) for i in range(21)],drained(156000))
    assert report['report_only_metrics']['in_run_running_peak_decline_bytes']==4000
    assert report['post_cycle1_to_drained_decline_bytes']==3800
    with pytest.raises(Exception,match='4 KiB'):live.heap_decline([sample(160000-i*216) for i in range(21)],drained(155680))
    data=sample();data['heap']['internal']['sampled_largest_minimum']=32767
    with pytest.raises(Exception,match='sampled internal'):live.heap_check([data])
    data=sample();data['failed_allocations']['total']=1
    with pytest.raises(Exception,match='failed allocation'):live.heap_check([data])


def reference_run(tmp_path):
    sessions,events=controls()
    labels=['main','resume','cold','wifi','bot-before','bot-after']+['cycle-'+str(i) for i in range(1,21)]+['idle']
    # Repeat independent channel witnesses, preserving monotonic sequence order.
    sessions=[];events=[]
    for i,label in enumerate(labels):
        now=100+i*100;channel='bot:'+str(i)
        sessions.append(dict(label=label,boot=0,generation=i*2+1,channel=channel,epoch=100+i,
                             ready_ns=now,ended_ns=now+50,samples=[sample(),sample()]))
        events.extend([dict(kind='ping',seq=1,channel=channel,monotonic_ns=now+1,direction='sent'),
                       dict(kind='pong',seq=1,channel=channel,monotonic_ns=now+2,direction='received')])
        if i:
            rid='step14-stale-'+str(i)
            events.extend([dict(kind='stale-display-probe',id=rid,epoch=99+i,current_epoch=100+i,channel=channel,monotonic_ns=now+3),
                dict(kind='display-result',id=rid,epoch=99+i,seq=1,accepted=False,error='stale-epoch',channel=channel,monotonic_ns=now+4)])
    path=tmp_path/'control.jsonl';path.write_text(''.join(json.dumps(row)+'\n' for row in events))
    (tmp_path/'serial.log').write_text('W (1784) H_SDIO_DRV: Reset slave using GPIO[54]\n')
    return dict(acquisition_passed=True,restore=dict(clean=True),limits=live.LIMITS,board='echoear',
        elf_sha256=live.REFERENCE,reference_only=True,sessions=sessions,control_sha256=digest(path),
        cycles=[dict(index=i,connected=True,disconnected=True,settled_seconds=100+i) for i in range(1,21)],
        settled_samples=[sample() for _ in range(21)],idle_elapsed_s=360,
        drain_seconds=150,heap_drain=drained(),idle_started_seconds=1152,
        recovery=dict(wifi_rejoin_s=30,wifi_ready_s=60,bot_ready_s=60,idle_ready_s=60),
        f20=dict(held=dict(held=True),resumed=True,cold=True,assets=dict(passed=True)))


@pytest.mark.parametrize('field',['wifi_rejoin_s','wifi_ready_s','bot_ready_s','idle_ready_s'])
def test_identical_recovery_bounds_include_boundary_and_reject_overrun(tmp_path,field):
    acquisition=reference_run(tmp_path)
    assert live.score(tmp_path,acquisition=acquisition)['checks_passed']
    acquisition['recovery'][field]+=.001
    with pytest.raises(Exception,match='fixed bound'):live.score(tmp_path,acquisition=acquisition)


@pytest.mark.parametrize('case,reason',[('short-idle','six-minute'),('cycle-failure','zero failures'),
    ('missing-cycle','20 cycles'),('assets','F20'),('restore','unclean'),('limits','bounds changed'),
    ('wrong-reference','image of record'),('changed-control','capture changed')])
def test_missing_recovery_evidence_fails(case,reason,tmp_path):
    acquisition=reference_run(tmp_path)
    if case=='short-idle':acquisition['idle_elapsed_s']=359.999
    elif case=='cycle-failure':acquisition['cycles'][3]['connected']=False
    elif case=='missing-cycle':acquisition['cycles'].pop()
    elif case=='assets':acquisition['f20']['assets']['passed']=False
    elif case=='restore':acquisition['restore']['clean']=False
    elif case=='limits':acquisition['limits']=dict(live.LIMITS,idle_s=1)
    elif case=='wrong-reference':acquisition['elf_sha256']='a'*64
    else:(tmp_path/'control.jsonl').write_text('changed')
    with pytest.raises(Exception,match=reason):live.score(tmp_path,acquisition=acquisition)


def test_liveness_wire_profile_validates_product_and_stale_result_without_changing_frozen_profiles():
    product=capture('esp32p4',False,'product')
    display=capture('esp32p4',False,'bench-display').records['msg=display-result']
    display['values']['id']='step14-stale-2';product.records['msg=display-result']=display
    lines=serialize(product)+['PHASE closed']
    capability=[line for line in lines if 'DEMO_CAPABILITY ' in line]
    # serialize's capability may precede the start; feed it separately, as the
    # real bot and serial files do, rather than fabricating a serial bot line.
    serial=[line for line in lines if 'DEMO_CAPABILITY ' not in line]
    live.liveness_capture(serial,capability,'p4xc',Bounds(68411,7000))


@pytest.mark.parametrize('failed',[False,True,'reset','reset-before-window','reset-after-failure','reset-old-cursor','cleanup-reset',
    'drain-missing','drain-short','drain-reordered'])
def test_live_gate_failure_still_exits_the_armed_restore_hold(tmp_path,monkeypatch,failed):
    import boards
    import p4_product
    import p4_step14
    from test_wire_contract import IDENTITY
    monkeypatch.setattr(boards,'require_bench',lambda:0)
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *a:nullcontext())
    monkeypatch.setattr(p4_step14,'restoring',lambda *a:nullcontext())
    monkeypatch.setattr(live,'verify_bundle',lambda *a:(dict(profile='demo',aec_enabled=False,elf_sha256=live.REFERENCE),{}))
    bot=tmp_path/'bot';bot.mkdir();(bot/'dc-events.jsonl').write_text('')
    identity=bot/'identity.json';identity.write_text(json.dumps(IDENTITY))
    (bot/'ready.json').write_text(json.dumps(dict(identity=IDENTITY,
        observer_sha256=digest(live.Path(live.__file__).with_name('p4_liveness_bot.py')))))
    bounds=tmp_path/'bounds.json';bounds.write_text('{}')
    args=SimpleNamespace(board='echoear',reference_only=True,bundle=tmp_path/'bundle',out=tmp_path/'run',
        bot_dir=bot,bot_identity=identity,restart_dir=tmp_path/'replacement',bounds=bounds,
        restore_record=None,restore_record_sha256=None,drain_seconds=150)
    class Hold:
        success=False;record={};released=False;touched=False
        def __enter__(self):return self
        def touch(self):self.touched=True
        def __exit__(self,*unused):
            if failed=='cleanup-reset':(args.out/'serial.log').write_text('C6 watchdog reset observed\n')
            self.record=dict(clean=True,action='echoear-55152f25')
        def release_signals(self):self.released=True
    guard=Hold()
    class IO:
        def __init__(self,*args):pass
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def hold(self):return guard
        def flash(self,*args):assert guard.touched
    monkeypatch.setattr(live,'LivenessIO',IO)
    def acquire(args,*unused):
        result=reference_run(args.out);result.pop('restore')
        if isinstance(failed,str) and failed.startswith('reset'):
            (args.out/'serial.log').write_text('C6 watchdog reset observed\n')
            stamp=5 if failed=='reset-before-window' else 12 if failed=='reset-after-failure' else 10
            raise live.C6ResetObserved('report-only reset',resets=[dict(cursor=5 if failed=='reset-old-cursor' else 12,host_ns=stamp*10**9,
                line='C6 watchdog reset observed',boot=False)],
                window=dict(start_cursor=10,start_ns=10*10**9),failure_ns=11*10**9,failure_cursor=13)
        if failed=='cleanup-reset':raise ValueError('original unrelated failure')
        if failed is True:result['cycles'][1]['connected']=False
        if failed=='drain-missing':result.pop('heap_drain')
        elif failed=='drain-short':result['heap_drain']['wait_ended_seconds']-=1
        elif failed=='drain-reordered':result['idle_started_seconds']=1000
        return result
    monkeypatch.setattr(live,'acquire',acquire)
    expected=0 if failed is False else 2 if failed=='reset' else 1
    assert live.live(args)==expected
    result=json.loads((args.out/'acquisition.json').read_text())
    assert guard.touched and guard.released and guard.success is (failed is False)
    assert result['restore']['action']=='echoear-55152f25'
    assert (args.out/'score.json').exists() is (failed is False)
    if failed=='reset':
        assert result['inconclusive'] and not result['acquisition_passed'] and 'C6ResetObserved' in result['error']
        assert live.score(args.out)['inconclusive']
    elif failed is not False:assert not result.get('inconclusive')
    if str(failed).startswith('drain-'):assert 'heap drain evidence unavailable' in result['error']


def p4_run(tmp_path):
    a=reference_run(tmp_path);a.update(board='p4xc',reference_only=False,elf_sha256='a'*64)
    bounds=tmp_path/'bounds.json';bounds.write_bytes(live.smoke.BOUNDS_FILE.read_bytes())
    a.update(bounds_path=str(bounds),bounds_sha256=digest(bounds))
    for index,session in enumerate(a['sessions']):
        wire=capture('esp32p4',False,'product')
        if index:
            result=capture('esp32p4',False,'bench-display').records['msg=display-result']
            result['values']['id']='step14-stale-'+str(index);wire.records['msg=display-result']=result
        text=serialize(wire);capability=[line for line in text if 'DEMO_CAPABILITY ' in line]
        text=[line for line in text if 'DEMO_CAPABILITY ' not in line]+['PHASE closed']+capability
        path=tmp_path/(session['label']+'-wire.log');path.write_text('\n'.join(text)+'\n')
        session.update(wire_verified=True,wire_file=path.name,wire_sha256=digest(path))
    return a


def test_p4_score_runs_every_wire_path_and_timing_is_report_only(tmp_path):
    a=p4_run(tmp_path);report=live.score(tmp_path,acquisition=a)
    assert report['checks_passed'] and len(report['wire_timing'])==27
    assert report['resources']['report_only'] is False and report['heap_decline']['report_only'] is False
    session=a['sessions'][1];path=tmp_path/session['wire_file']
    path.write_text(path.read_text().replace('max_us=20000','max_us=999999'))
    session['wire_sha256']=digest(path)
    report=live.score(tmp_path,acquisition=a)
    assert report['checks_passed'] and report['wire_timing'][1]['status']=='violation (report-only)'


@pytest.mark.parametrize('case,reason',[('wire-flag','missing product wire'),('wire-hash','wire capture changed'),
    ('bounds-hash','fixed bounds changed'),('probe-id','unexpected display result'),('result-schema','schema changed'),
    ('power-available','P4 power'),('power-valid','P4 power'),('reference-bypass','reference-only')])
def test_p4_score_negatives_cover_the_gated_path(tmp_path,case,reason):
    a=p4_run(tmp_path);session=a['sessions'][1];path=tmp_path/session['wire_file']
    if case=='wire-flag':session['wire_verified']=False
    elif case=='wire-hash':path.write_text(path.read_text()+'changed\n')
    elif case=='bounds-hash':live.Path(a['bounds_path']).write_text('{}')
    elif case=='probe-id':
        path.write_text(path.read_text().replace('step14-stale-1','unrelated-request'));session['wire_sha256']=digest(path)
    elif case=='result-schema':
        text=path.read_text();lines=[]
        for line in text.splitlines():
            if 'WIRE_CONTRACT msg=display-result ' in line:
                body=json.loads(line.split('msg=display-result ',1)[1]);body['schema']['accepted']='integer'
                line='WIRE_CONTRACT msg=display-result '+json.dumps(body)
            lines.append(line)
        path.write_text('\n'.join(lines)+'\n');session['wire_sha256']=digest(path)
    elif case=='power-available':session['samples'][0]['power']['available']=1
    elif case=='power-valid':session['samples'][0]['power']['valid']=True
    else:a['reference_only']=True
    with pytest.raises(ValueError,match=reason):live.score(tmp_path,acquisition=a)


@pytest.mark.parametrize('case,reason',[('probe-current','old epoch'),('probe-current-field','old epoch'),
    ('unmatched','unmatched stale'),('ping-order','ping sequence'),('direction','device receipt'),
    ('overlap','overlapping'),('no-probes','drop witness'),('error','epoch fence')])
def test_remaining_control_guards(case,reason):
    sessions,events=controls()
    if case=='probe-current':
        events[-2]['epoch']=sessions[1]['epoch'];events[-1]['epoch']=sessions[1]['epoch']
    elif case=='probe-current-field':events[-2]['current_epoch']+=1
    elif case=='unmatched':events[-1]['id']='step14-stale-unmatched'
    elif case=='ping-order':events.insert(2,dict(events[1],monotonic_ns=events[1]['monotonic_ns']))
    elif case=='direction':events[2]['direction']='sent'
    elif case=='overlap':sessions[0]['ended_ns']=sessions[1]['ready_ns']+1
    elif case=='error':events[-1]['error']='none'
    else:events=[e for e in events if e['kind'] not in ('stale-display-probe','display-result')]
    with pytest.raises(ValueError,match=reason):live.control_check(sessions,events)


def test_heap_decline_halves_length_and_uptick_semantics_independently():
    for values in ([160000]+[153000]*10+[160000]*10,
                   [160000,157000,157100,154100,154200,151200,151300]+[151300]*14):
        report=live.heap_decline([sample(v) for v in values],drained(values[1]))
        assert report['checks_passed'] and report['report_only_metrics']['in_run_running_peak_decline_bytes']>4096
    with pytest.raises(ValueError,match='baseline plus'):live.heap_decline([sample()]*20,drained())
    # Running-peak drawdown is now explicitly report-only, even across upticks.
    values=[160000,156500,156600,153100,157000,160000]+[160000]*15
    assert live.heap_decline([sample(v) for v in values],drained())['report_only_metrics']['in_run_running_peak_decline_bytes']==6900
    data=sample();data['heap']['internal']['largest']=32767
    with pytest.raises(ValueError,match='internal heap floor'):live.heap_check([data])


@pytest.mark.parametrize('case,reason',[('held','F20'),('cold','F20'),('label','session evidence')])
def test_f20_and_session_label_guards(tmp_path,case,reason):
    a=p4_run(tmp_path)
    if case=='held':a['f20']['held']['held']=False
    elif case=='cold':a['f20']['cold']=False
    else:a['sessions'][-1]['label']='wrong'
    with pytest.raises(ValueError,match=reason):live.score(tmp_path,acquisition=a)


def test_s3_heap_findings_are_report_only_but_p4_still_gates(tmp_path):
    a=reference_run(tmp_path);a['sessions'][0]['samples'][0]['heap']['internal']['minimum_free']=1
    a['settled_samples']=[sample(160000-i*500) for i in range(21)]
    a['heap_drain']=drained(150000)
    report=live.score(tmp_path,acquisition=a)
    assert report['checks_passed'] and report['resources']['report_only'] and report['heap_decline']['report_only']
    assert not report['resources']['checks_passed'] and not report['heap_decline']['checks_passed']
    with pytest.raises(ValueError,match='heap floor'):live.sample_check(a['sessions'][0]['samples'][0],'p4xc')


def test_settled_heap_uses_convergence_and_times_out_on_unstable_p4():
    clock=SimpleNamespace(value=0);samples=iter([sample(v) for v in (160000,150000,149900,149800)])
    io=SimpleNamespace(args=SimpleNamespace(board='p4xc'),clock=lambda:clock.value,
        snapshot=lambda board:next(samples),pause=lambda board,seconds:setattr(clock,'value',clock.value+seconds))
    chosen,evidence=live.settled_heap(None,io)
    assert chosen['heap']['internal']['free']==149800 and len(evidence['samples'])==4
    count=iter(range(1000));io.snapshot=lambda board:sample(160000-next(count)*1000)
    with pytest.raises(ValueError,match='did not converge'):live.settled_heap(None,io)


def test_c6_detector_excludes_real_routine_boot_lines_and_raises_report_only(monkeypatch):
    assert not live.unsolicited_c6_reset('W (1784) H_SDIO_DRV: Reset slave using GPIO[54]',boot=True)
    assert live.unsolicited_c6_reset('W (1784) H_SDIO_DRV: Reset slave using GPIO[54]')
    assert not live.unsolicited_c6_reset('I (3306) sdio_wrapper: GPIOs: CLK[18] Slave_Reset[54]')
    assert live.unsolicited_c6_reset('C6 watchdog reset observed')
    io=object.__new__(live.LivenessIO);io.serial_seen=0
    io.active=True;io.stopping=False;io.session_gen=1;io.current_window=dict(start_cursor=0,start_ns=10**9)
    board=SimpleNamespace(lines_since=lambda at:[(1,5.,'C6 watchdog reset observed'),(2,5.1,'SESSION FAIL gen=1')])
    with pytest.raises(live.C6ResetObserved):io.poll(board)
    assert io.c6_resets[0]['line']=='C6 watchdog reset observed'


@pytest.mark.parametrize('reset_stamp',[5.,5.1])
def test_reset_after_failure_in_same_poll_batch_does_not_hide_original_failure(reset_stamp):
    io=object.__new__(live.LivenessIO);io.serial_seen=0;io.active=True;io.stopping=False;io.session_gen=1
    io.current_window=dict(start_cursor=0,start_ns=10**9)
    board=SimpleNamespace(lines_since=lambda at:[(1,5.,'SESSION FAIL gen=1'),(2,reset_stamp,'C6 watchdog reset observed')])
    with pytest.raises(ValueError,match='unintentional session loss'):io.poll(board)
    assert not getattr(io,'c6_resets',[])


def test_gpio_reset_is_excluded_only_inside_recorded_boot_window(monkeypatch):
    io=object.__new__(live.LivenessIO);io.serial_seen=0;io.active=True;io.stopping=False;io.session_gen=1
    io.current_window=dict(start_cursor=0,start_ns=10**9);io.boot_windows=[dict(start_cursor=0,end_cursor=2)]
    io.pid=123;io.identity=dict(bot_instance='bot');monkeypatch.setattr(live.smoke,'process_token',lambda pid:'bot')
    board=SimpleNamespace(lines_since=lambda at:[(1,5.,'H_SDIO_DRV: Reset slave using GPIO[54]')])
    io.poll(board);assert not getattr(io,'c6_resets',[])
    io.serial_seen=0;io.boot_windows=[]
    with pytest.raises(live.C6ResetObserved):io.poll(board)


@pytest.mark.parametrize('value',[None,'/custom/backup'])
def test_liveness_parser_always_defines_factory_backup(monkeypatch,value):
    seen=[];monkeypatch.setattr(live,'live',lambda args:seen.append(args) or 0)
    argv=['live','--board','p4xc','--bundle','b','--out','o','--bot-dir','d','--bot-identity','i','--restart-dir','r']
    if value:argv+=['--factory-backup',value]
    assert live.main(argv)==0
    assert seen[0].p4_factory_backup==live.Path(value) if value else seen[0].p4_factory_backup==live.smoke.FACTORY_BACKUP


def test_s3_decline_report_labels_unconverged_points(tmp_path):
    a=reference_run(tmp_path);a['settled_evidence']=[dict(converged=i!=2) for i in range(21)]
    report=live.score(tmp_path,acquisition=a)['heap_decline']
    assert report['settled_points'][2]['converged'] is False
    assert all(row['report_only'] for row in report['settled_points'])


def test_cleanup_error_is_separate_and_does_not_mask_acquisition_failure(tmp_path):
    def command(text,*args):
        if text=='demo hold':raise TimeoutError('cleanup failed')
    board=SimpleNamespace(command=command)
    def begin(*args):raise RuntimeError('original acquisition failure')
    io=SimpleNamespace(open=lambda:nullcontext(board),boot=lambda board:None,provision=lambda board:{},begin=begin)
    with pytest.raises(RuntimeError,match='original acquisition failure'):
        live.acquire(SimpleNamespace(bot_dir=tmp_path,drain_seconds=150),io,{})
    assert io.cleanup_error=='TimeoutError: cleanup failed' and io.active is False


def test_ready_observation_uses_bot_epoch_without_stats_or_display_queries(tmp_path):
    bot=tmp_path/'bot';bot.mkdir();(bot/'bot.log').write_text('');(tmp_path/'serial.log').write_text('')
    (bot/'dc-events.jsonl').write_text(json.dumps(dict(kind='display-ready',epoch=42,channel='peer',monotonic_ns=4000000000))+'\n')
    commands=[];clock=SimpleNamespace(now=1.)
    class Board:
        cursor=1
        def command(self,text,*args):commands.append(text)
        def wait_for_entry(self,*args,**kwargs):return 2,4.,'STATE Ready gen=7'
        def wait_for(self,*args,**kwargs):clock.now=30.;return 'RESTORED assets=16 no_http=1'
    io=object.__new__(live.LivenessIO);io.args=SimpleNamespace(bot_dir=bot);io.out=tmp_path;io.clock=lambda:clock.now
    io.wait=lambda board,condition,*args,**kwargs:condition()
    io.snapshot=lambda board:pytest.fail('stats inside an unfrozen liveness window')
    io.begin(Board(),'resume')
    assert commands==['demo run'] and io.display_ready['epoch']==42 and io.pc_id is None
    assert io.ready_ns/1e9-1.==3.  # request at 1, actual Ready at 4
    assert io.clock()-1.==29.  # downstream harness overhead is excluded


def test_acquisition_freezes_before_any_added_perf_sample(tmp_path):
    state=SimpleNamespace(frozen=False);calls=[];board=SimpleNamespace(command=lambda *args:None)
    class StopAfterSample(Exception):pass
    class IO:
        session_gen=1;ready_ns=4000000000;display_ready=dict(epoch=42);since=0
        args=SimpleNamespace(board='p4xc');identity=dict(profile_manifest=dict(profile='demo'))
        open=lambda self:nullcontext(board)
        boot=lambda *args:None
        provision=lambda *args:{}
        begin=lambda *args:None
        clock=lambda self:13
        pause=lambda *args:None
        def freeze(self,board):state.frozen=True;calls.append('freeze')
        def snapshot(self,board):
            assert state.frozen;calls.append('stats');return dict(sample(),pc_id='peer')
        events=lambda self:(None,[dict(kind='stt-profile',profile='demo')])
        frozen_sample=live.LivenessIO.frozen_sample
        def finish(self,*args,**kwargs):raise StopAfterSample()
    (tmp_path/'bot.log').write_text('')
    with pytest.raises(StopAfterSample):live.acquire(SimpleNamespace(bot_dir=tmp_path,drain_seconds=150),IO(),{})
    assert calls==['freeze','stats']


@pytest.mark.parametrize('board',['p4xc','echoear'])
def test_round2_shaped_series_pass_after_drain_with_old_declines_report_only(tmp_path,board):
    # Exact internal series from round-2 analysis, replayed as liveness-shaped
    # samples. No diagnostic capture is promoted to liveness qualification.
    if board=='p4xc':
        values=[352703,349323,348879,348467,348031,347611,347187,346763,346339,346335,
                346327,346339,346327,346351,346359,346375,346371,346363,346367,346359,346367]
        final=349783
    else:
        values=[170151,167219,167219,167219,167219,167215,167219,167207,167215,167219,
                167203,167207,167207,167219,167211,167211,167219,167219,167211,167219,167215]
        final=167215
    a=p4_run(tmp_path) if board=='p4xc' else reference_run(tmp_path)
    a['settled_samples']=[sample(v) for v in values];a['heap_drain']=drained(final)
    result=live.score(tmp_path,acquisition=a);report=result['heap_decline']
    assert result['checks_passed'] and not result['passed'] and not result['qualification']
    assert report['checks_passed'] and report['report_only']==(board=='echoear')
    assert report['post_cycle1_to_drained_decline_bytes']==values[1]-final
    extra=report['report_only_metrics'];assert extra['report_only']
    assert extra['pre_cycle1_to_cycle20_net_decline_bytes']==values[0]-values[-1]
    assert extra['drain_recovery_bytes']==(3416 if board=='p4xc' else 0)
    if board=='p4xc':assert extra['in_run_running_peak_decline_bytes']>4096


@pytest.mark.parametrize('decline',[4096,4097])
def test_drained_gate_keeps_exact_four_kib_boundary(decline):
    samples=[sample() for _ in range(21)];tail=drained(160000-decline)
    if decline==4096:assert live.heap_decline(samples,tail)['checks_passed']
    else:
        with pytest.raises(ValueError,match='4 KiB'):live.heap_decline(samples,tail)


def test_first_session_cost_is_report_only_and_persistent_accumulation_fails(tmp_path):
    values=[160000]+[150000]*20
    report=live.heap_decline([sample(v) for v in values],drained(150000))
    assert report['checks_passed'] and report['post_cycle1_to_drained_decline_bytes']==0
    assert report['report_only_metrics']['pre_cycle1_to_drained_net_decline_bytes']==10000
    a=p4_run(tmp_path);a['settled_samples']=[sample(160000-i*500) for i in range(21)]
    a['heap_drain']=drained(150000)
    with pytest.raises(ValueError,match='4 KiB'):live.score(tmp_path,acquisition=a)
    a.update(board='echoear',reference_only=True,elf_sha256=live.REFERENCE)
    report=live.score(tmp_path,acquisition=a)
    assert report['checks_passed'] and report['heap_decline']['report_only']
    assert not report['heap_decline']['checks_passed']
    assert report['heap_decline']['post_cycle1_to_drained_decline_bytes']==9500


@pytest.mark.parametrize('board',['p4xc','echoear'])
@pytest.mark.parametrize('case',['missing','sample','settling','wrong-final-sample','short','under-minimum',
    'reordered-wait','sample-before-wait','reordered-sampling','before-cycle20','after-idle','changed-duration',
    'missing-cycle-time','held','wrong-cycle','nonfinite','missing-convergence-samples','false-convergence'])
def test_drain_missing_short_or_reordered_is_unavailable_even_on_reference(tmp_path,board,case):
    a=p4_run(tmp_path) if board=='p4xc' else reference_run(tmp_path)
    tail=a['heap_drain']
    if case=='missing':a.pop('heap_drain')
    elif case=='sample':tail.pop('sample')
    elif case=='settling':tail.pop('settling')
    elif case=='wrong-final-sample':tail['sample']=sample(159999)
    elif case=='short':tail['wait_ended_seconds']-=.001
    elif case=='under-minimum':tail['requested_seconds']=149.999;a['drain_seconds']=149.999
    elif case=='reordered-wait':tail['wait_ended_seconds']=999
    elif case=='sample-before-wait':tail['sample_started_seconds']=1149
    elif case=='reordered-sampling':tail['sample_ended_seconds']=1149
    elif case=='before-cycle20':a['cycles'][-1]['settled_seconds']=1001
    elif case=='after-idle':a['idle_started_seconds']=1150
    elif case=='changed-duration':a['drain_seconds']=180
    elif case=='missing-cycle-time':a['cycles'][-1].pop('settled_seconds')
    elif case=='held':tail['held']['held']=False
    elif case=='wrong-cycle':tail['after_cycle']=19
    elif case=='missing-convergence-samples':tail['settling']['samples'].pop(0)
    elif case=='false-convergence':tail['settling']['samples'][0]['heap']['internal']['free']-=1000
    else:tail['started_seconds']=float('nan')
    with pytest.raises(ValueError,match='heap drain evidence unavailable'):live.score(tmp_path,acquisition=a)


def test_attempt2_fail_shape_without_drain_cannot_be_rescored_as_passing():
    # Attempt 2's baseline-to-final loss remains 4916 B; it has no drain witness.
    values=[160000]+[160000-round(4916*i/20) for i in range(1,21)]
    assert values[0]-values[-1]==4916
    with pytest.raises(ValueError,match='heap drain evidence unavailable'):
        live.heap_decline([sample(v) for v in values])


@pytest.mark.parametrize('board',['p4xc','echoear'])
def test_drained_convergence_is_gated_on_p4_and_labelled_on_reference(tmp_path,board):
    a=p4_run(tmp_path) if board=='p4xc' else reference_run(tmp_path)
    a['heap_drain']['settling']['converged']=False
    if board=='p4xc':
        with pytest.raises(ValueError,match='heap drain evidence unavailable'):live.score(tmp_path,acquisition=a)
    else:
        report=live.score(tmp_path,acquisition=a)['heap_decline']
        assert report['drained_point']['converged'] is False and report['drained_point']['report_only']


@pytest.mark.parametrize('seconds',[0,149.999,float('nan'),float('inf'),True])
def test_short_or_invalid_configured_drain_is_rejected_before_io(seconds,monkeypatch):
    import boards
    monkeypatch.setattr(boards,'require_bench',lambda:pytest.fail('bench preflight before drain validation'))
    with pytest.raises(ValueError):live.live(SimpleNamespace(drain_seconds=seconds))


@pytest.mark.parametrize('seconds',[None,180])
def test_liveness_parser_exposes_minimum_default_and_configurable_drain(monkeypatch,seconds):
    seen=[];monkeypatch.setattr(live,'live',lambda args:seen.append(args) or 0)
    argv=['live','--board','p4xc','--bundle','b','--out','o','--bot-dir','d','--bot-identity','i','--restart-dir','r']
    if seconds is not None:argv+=['--drain-seconds',str(seconds)]
    assert live.main(argv)==0 and seen[0].drain_seconds==(150 if seconds is None else seconds)


@pytest.mark.parametrize('board',['p4xc','echoear'])
@pytest.mark.parametrize('seconds',[150,180])
def test_drain_acquisition_waits_held_and_samples_after_full_duration(tmp_path,board,seconds):
    clock=SimpleNamespace(now=1000.);events=[]
    device=SimpleNamespace(cursor=0,lines_since=lambda at:[])
    def pause(board,seconds):clock.now+=seconds;events.append(('pause',seconds))
    def snapshot(board):
        assert clock.now>=1000+seconds;events.append(('sample',clock.now));return sample(159000)
    io=SimpleNamespace(out=tmp_path,args=SimpleNamespace(board=board),clock=lambda:clock.now,pause=pause,
        snapshot=snapshot,held=lambda board:dict(held=True))
    tail=live.drain_heap(device,io,seconds)
    assert events[0]==('pause',seconds) and len([e for e in events if e[0]=='sample'])==3
    assert tail['sample_started_seconds']>=1000+seconds and tail['sample_ended_seconds']>tail['sample_started_seconds']
    assert json.loads((tmp_path/'heap-drain.json').read_text())==tail


@pytest.mark.parametrize('failure',['short','pause','sample','reconnected'])
def test_drain_acquisition_never_returns_short_failed_or_active_tail(tmp_path,failure):
    clock=SimpleNamespace(now=1000.)
    device=SimpleNamespace(cursor=0,lines_since=lambda at:[(1,1100.,'STATE Ready gen=21')] if failure=='reconnected' else [])
    def pause(board,seconds):
        if failure=='pause':raise TimeoutError('drain interrupted')
        clock.now+=seconds-(1 if failure=='short' else 0)
    def snapshot(board):
        if failure=='sample':raise TimeoutError('drain stats failed')
        return sample()
    io=SimpleNamespace(out=tmp_path,args=SimpleNamespace(board='p4xc'),clock=lambda:clock.now,pause=pause,
        snapshot=snapshot,held=lambda board:dict(held=True))
    with pytest.raises((ValueError,TimeoutError)):live.drain_heap(device,io,150)
    assert (tmp_path/'heap-drain-start.json').exists() and not (tmp_path/'heap-drain.json').exists()
