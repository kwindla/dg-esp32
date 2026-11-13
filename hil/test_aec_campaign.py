import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import numpy as np
import echoear_aec_campaign as runner
from echoear_aec_campaign_analysis import (echo_verdict,generation_audit,campaign_verdict,
    context_playback,transport,probe_measure)

PLAN=Path(__file__).parent/'plans/echoear-aec-accept-v1.json'

def plan():return json.loads(PLAN.read_text())

def cell(id,kind='stop',volume=-6):
    return dict(id=id,kind=kind,volume_db=volume,expected_class='eligible')


def test_exact_minimal_contract_and_draft_cannot_touch_bench(tmp_path,monkeypatch):
    p=plan();assert runner.validate_spec(p)
    monkeypatch.setattr(runner,'check_esptool',lambda:pytest.fail('hardware preflight called'))
    with pytest.raises(ValueError,match='Draft'):runner.run(PLAN,tmp_path/'never-created')
    assert not (tmp_path/'never-created').exists()
    p.update(status='declared',qualification=True)
    with pytest.raises(ValueError,match='authorization'):runner.validate_campaign(p)
    for mutate in (lambda p:p['spares'].pop(),lambda p:p['cases'].pop(),lambda p:p.update(boot_ref_gain_db=0),lambda p:p['sessions'].reverse()):
        q=plan();mutate(q)
        with pytest.raises(ValueError):runner.validate_spec(q)


def test_noise_retries_retain_identity_and_never_look_at_scores():
    q=runner.Schedule([cell('a'),cell('b')],[cell('spare1'),cell('spare2')],plan()['noise_gate'])
    group,a=q.next();q.finish(group,a,False,{'a':'eligible'})
    group,a=q.next();q.finish(group,a,True,{'b':'eligible'}) # miss also stays eligible
    assert q.journal[-1]['replacements']==[]
    group,a=q.next();assert group['run_id']=='a' and a==2
    q.finish(group,a,True,{'a':'eligible'})
    assert q.next() is None and not q.queue.noisy_cells
    assert [x['attempt'] for x in q.journal]==[1,1,2]


@pytest.mark.parametrize('kind',['unscorable-near-onset','unscorable-future','no-opportunity','not-delivered-no-active-tts'])
def test_only_independent_witness_failures_use_ordered_spares(kind):
    q=runner.Schedule([cell('a')],[cell('spare1'),cell('spare2')],plan()['noise_gate'])
    for expected in ('a','spare1','spare2'):
        group,a=q.next();assert group['run_id']==expected
        q.finish(group,a,True,{expected:kind})
    assert q.next() is None and q.unresolved==['spare2']
    assert [r['spare'] for j in q.journal for r in j['replacements']]==['spare1','spare2']


def test_noise_limit_pause_and_acquisition_failure_neutral():
    q=runner.Schedule([cell(x) for x in 'abcd'],[],plan()['noise_gate'])
    for state in (False,None,False,False):
        g,a=q.next();q.finish(g,a,state,{})
    assert q.queue.paused and q.next() is None
    assert q.queue.noisy_cells==['a','c','d'] and q.unresolved==['b']
    q=runner.Schedule([cell('x')],[],plan()['noise_gate'])
    for a in (1,2,3):
        g,actual=q.next();assert actual==a;q.finish(g,a,False,{})
    assert q.next() is None and q.queue.exhausted==['x']


def test_pair_is_one_noise_cell_and_three_attempts_are_declared_before_connect():
    p=plan();echo=[c for c in p['cases'] if c['kind']=='echo' and c['volume_db']==-6]
    groups=runner.capture_groups(echo)
    assert len(groups)==8 and [len(g['cells']) for g in groups]==[1,2,2,1,1,1,1,1]
    protocol=runner.protocol(p['cases']+p['spares']+p['controls'],p['reconnects'])
    assert len(protocol['echo_replies'])==3*(len(p['cases'])+len(p['spares'])+len(p['controls'])+3)
    assert not any(c['id'].endswith('-next') for c in protocol['echo_replies'])
    assert 'qualification-01-stop-01-a1' in protocol['real_bot_ids']
    assert 'qualification-01-echo-01-a1' not in protocol['real_bot_ids']


def test_first500_gate_is_strictly_source_conditional_and_postguard_is_afe():
    whole=dict(scorable=True,integrated_reduction_db=21)
    first=dict(scorable=True,integrated_reduction_db=1.3)
    post=dict(scorable=True,residual_dbfs=-51)
    assert echo_verdict(whole,first,-30,post)['passed']
    assert not echo_verdict(whole,first,-29.9,post)['passed']
    assert not echo_verdict(whole,first,None,post)['passed']
    assert not echo_verdict(whole,first,-40,dict(post,residual_dbfs=-49))['passed']


def evidence():
    events=[];recorded=[]
    for i in range(2):
        t=10+i*10;cid=f'c{i}'
        for event,offset,extras in [('tts-context-request',0,{}),('tts-generation-metric-start',.1,{}),('tts-context-metric',.2,dict(metric_type='TTFBMetricsData',data=dict(value=.1,processor='ProactiveConnectionTTS#0',model='flux-kelsey-en'))),('tts-context-metric',.3,dict(metric_type='TTFAMetricsData',data=dict(ttfb=.1,ttfa=.2,leading_silence=.1,processor='ProactiveConnectionTTS#0',model='flux-kelsey-en'))),('tts-generation-stopped',2,{}),('case-sealed',3,{})]:
            events.append(dict(event=event,t=t+offset,pc_id='p',case_id=f'case{i}',context_id=cid,**extras))
        recorded.append(dict(kind='tts-transport-write',pc_id='p',context_id=cid,non_silent=True,monotonic_ns=int((t+.4)*1e9)))
    return events,recorded


def test_every_real_next_generation_is_in_lead_gate_and_context_reconciliation():
    e,r=evidence();assert generation_audit(e,r)['passed']
    next_metric=next(x for x in e if x['event']=='tts-context-metric' and x['context_id']=='c1' and x['metric_type']=='TTFAMetricsData')
    next_metric['data']['leading_silence']=.8
    assert not generation_audit(e,r)['passed']
    next_metric['data']['leading_silence']=.1
    assert not generation_audit(e,r[:-1])['passed']
    assert not generation_audit(e,r+[dict(r[0],context_id='unowned')])['passed']


def test_reconnect_overlaps_refusals_and_teardown_stay_visible():
    e,r=evidence()
    def event(name,t):return dict(event=name,t=t,pc_id='p')
    e.extend([event('tts-reconnect-start',15),event('tts-reconnect-ready',16)])
    e.sort(key=lambda x:x['t'])
    assert generation_audit(e,r)['passed']
    assert not generation_audit(e,r,[(15.5,18)])['passed']
    e.append(event('tts-reconnect-refused',50));value=generation_audit(e,r)
    assert value['passed'] and not value['sessions'][0]['whole_session_clean']
    e.append(event('tts-reconnect-refused',21));assert not generation_audit(e,r)['passed']


def passing_holds(p):
    rows=[]
    for c in p['cases']+p['controls']:
        if c['kind']=='probe':continue
        rows.append(dict(id=c['id']+'-a1',cell_id=c['id'],kind=c['kind'],volume_db=c['volume_db'],voice_class=c.get('voice_class','firm'),clean=True,expected_class=c.get('expected_class'),opportunity='eligible' if c.get('expected_class')=='eligible' else 'echo-only',barge_success=True,quick_gap=dict(witnessed=c.get('quick_gap_s') is not None)))
    rows += [dict(id=c['id'],kind='soak-echo',volume_db=-6,clean=True) for c in runner.soak_cells(p)]
    return [dict(id='qualification-01',passed=True,cases=rows,reconnects=[dict(passed=True)]*3,probes=[dict(noise=True,passed=True)]*2,soak=dict(passed=True)),dict(id='qualification-normal',passed=True,cases=[]),dict(id='qualification-02',passed=True,cases=[]),dict(id='soak-01',passed=True,cases=[])]


def test_complete_verdict_qualifies_minus6_only_and_does_not_hide_misses():
    p=plan();p['qualification']=True;h=passing_holds(p)
    assert campaign_verdict(p,h)['qualification']
    assert campaign_verdict(p,h)['qualified_volumes_db']==[-6]
    for c in h[0]['cases']:
        if c['volume_db']==0:c['barge_success']=False
    assert campaign_verdict(p,h)['passed'] # exposure is reported
    stops=[r for r in h[0]['cases'] if r['kind']=='stop' and r['volume_db']==-6 and r.get('expected_class')=='eligible']
    stops[0]['barge_success']=False;assert campaign_verdict(p,h)['passed']
    stops[1]['barge_success']=False;assert not campaign_verdict(p,h)['passed']
    stops[1]['barge_success']=True;stops[0]['opportunity']='unscorable-future'
    assert not campaign_verdict(p,h)['passed'] # 9/9 cannot substitute for10


@pytest.mark.parametrize('missing',['near','silent-cancel','echo','soak-echo','stop','sentence'])
def test_missing_block_member_cannot_qualify(missing):
    p=plan();h=passing_holds(p);index=next(i for i,c in enumerate(h[0]['cases']) if c['kind']==missing);h[0]['cases'].pop(index)
    assert not campaign_verdict(p,h)['passed']


def test_transport_total_drops_can_bound_burst_but_missing_counters_cannot():
    a=dict(received=100,lost=0,playout_dropped=0);b=dict(received=1100,lost=1,playout_dropped=3)
    assert transport(a,b)['passed']
    assert not transport(a,dict(b,playout_dropped=4))['passed']
    assert not transport({},b)['passed']


def test_pcm_generation_match_does_not_credit_unrelated_old_speech():
    rng=np.random.default_rng(5);source=rng.normal(0,500,16000);ref=np.zeros(48000);ref[16000:32000]=source
    blocks=np.array([[i,i+320,i*62.5,(i+320)*62.5,0] for i in range(0,len(ref),320)])
    anchors=[dict(coordinate=i*50000,lower_s=i*.05,upper_s=i*.05+.001) for i in range(61)]
    assert context_playback(source,ref,blocks,anchors,.9)['passed']
    assert not context_playback(rng.normal(0,500,16000),ref,blocks,anchors,.9)['passed']
    assert not context_playback(source,ref,blocks,anchors,0)['passed']


def test_reconnect_stops_capture_before_waiting_for_slow_worker(tmp_path):
    import threading,time
    from echoear_aec_reconnect import capture_reconnect
    stopped=threading.Event();connected=threading.Event();calls=[]
    class Board:
        def lines_since(self,cursor):return [(0,0,'PERF {}')]
        def wait_for(self,*a,**k):connected.set();stopped.wait(2);raise TimeoutError('Ready timeout')
    def command(board,text,timeout):calls.append((text,timeout));return 0,0
    def schedule(paths,start,offset,fn,**kw):return dict(jabra=fn(),skipped=False)
    def stop(*args):stopped.set();return dict(coordinate=1,lower_s=0,upper_s=0)
    began=time.monotonic()
    with pytest.raises(TimeoutError):capture_reconnect(Board(),command,lambda *a:None,lambda:[],lambda *a:None,tmp_path,
        dict(id='slow',second_reply_window_seconds=8,capture_cap_seconds=3.2),
        start=lambda *a:dict(coordinate=0,lower_s=0,upper_s=0),stop=stop,anchor=lambda b:dict(coordinate=0,lower_s=0,upper_s=0),schedule=schedule,
        sleep=lambda n:time.sleep(.001 if n==2.3 else n))
    assert stopped.is_set() and connected.is_set() and time.monotonic()-began<1
    assert all(timeout<=.2 for _,timeout in calls)


def test_soak_keeps_fixed_deadlines_full_duration_and_rejects_export_overrun(tmp_path,monkeypatch):
    clock=[0.];requests=[]
    monkeypatch.setattr(runner,'snapshot',lambda *a:None)
    class Hold:
        p=plan();board=None;out=tmp_path
        def capture(self,g,a,scheduled_s,resource_tick):
            assert clock[0]<=scheduled_s-2.3
            clock[0]=scheduled_s;requests.append(scheduled_s)
            for _ in range(3):clock[0]+=8;resource_tick()
            return True,[]
    result=runner.run_soak(Hold(),clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
    assert result['passed'] and result['duration_s']>=600 and requests==list(range(10,551,60))
    class Slow(Hold):
        def capture(self,*a,**kw):clock[0]=kw['scheduled_s']+60;return True,[]
    clock[0]=0
    with pytest.raises(RuntimeError,match='export exceeded'):runner.run_soak(Slow(),clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))

def test_full_driver_orders_all_blocks_and_always_exits_holds(tmp_path,monkeypatch):
    p=plan();p.update(status='declared',qualification=True,review_approval=p['review_of_record'],campaign_authorized=True)
    declaration=tmp_path/'plan.json';declaration.write_text(json.dumps(p));calls=[];seen=[];clock=[0.]
    class Hold:
        def __init__(self,p,s,out):self.p=p;self.s=s;self.out=out;self.board=None;self.saved=[]
        def __enter__(self):self.out.mkdir();calls.append(('enter',self.s['id']));return self
        def __exit__(self,*args):calls.append(('restore',self.s['id']))
        def link_probe(self):calls.append(('link-probe',self.s['id']))
        def connect(self,cells,reconnects):
            self.protocol=runner.protocol(cells,reconnects);calls.append(('connect',self.s['id']))
        def capture(self,g,a,**kw):
            outcomes=[]
            for c in g['cells']:
                row=dict(c,run_id=c['id']+f'-a{a}')
                assert row['run_id'] in {s['id'] for s in self.protocol['echo_replies']}
                outcomes.append(row);self.saved.append(row);seen.append(row)
            if 'scheduled_s' in kw:
                clock[0]=kw['scheduled_s']
                for _ in range(3):clock[0]+=8;kw['resource_tick']()
            calls.append(('capture',g['run_id']))
            return True,outcomes
        def reconnect(self,c):calls.append(('reconnect',c['id']))
        def finish(self):calls.append(('finish',self.s['id']))
        def probe(self,c):calls.append(('probe',c['id']))
    def analyze(root):
        return dict(cases=[dict(id=c['run_id'],opportunity='eligible') for c in seen])
    monkeypatch.setattr(runner,'validate_campaign',lambda p:runner.validate_spec(p))
    monkeypatch.setattr(runner,'check_esptool',lambda:None)
    monkeypatch.setattr(runner,'analyze_block',analyze)
    monkeypatch.setattr(runner,'snapshot',lambda *a:None)
    monkeypatch.setattr(runner.signal,'signal',lambda *a:None)
    original=runner.run_soak
    monkeypatch.setattr(runner,'run_soak',lambda h:original(h,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n)))
    import echoear_aec_campaign_analysis
    monkeypatch.setattr(echoear_aec_campaign_analysis,'analyze_campaign',lambda root:dict(qualification=False,test=True))
    runner.run(declaration,tmp_path/'run',hold_factory=Hold)
    assert [v for k,v in calls if k=='enter']==['qualification-01','qualification-normal','qualification-02','soak-01']
    assert len([1 for k,v in calls if k=='capture'])==84
    assert len([1 for k,v in calls if k=='reconnect'])==3
    assert len([1 for k,v in calls if k=='probe'])==2
    assert len([1 for k,v in calls if k=='restore'])==4
    assert len([1 for k,v in calls if k=='link-probe'])==4
    for sid in [v for k,v in calls if k=='enter']:
        assert calls.index(('connect',sid))<calls.index(('link-probe',sid))<calls.index(('finish',sid))
    assert calls.index(('restore','qualification-01'))<calls.index(('enter','qualification-normal'))<calls.index(('enter','qualification-02'))
    for s in ('qualification-01','qualification-02'):
        assert calls.index(('finish',s))<calls.index(('probe',s+'-probe-01'))<calls.index(('restore',s))


def test_soak_noise_retry_is_after_original_600s_and_keeps_attempt_number(tmp_path,monkeypatch):
    clock=[0.];requests=[];monkeypatch.setattr(runner,'snapshot',lambda *a:None)
    class Hold:
        p=plan();board=None;out=tmp_path
        def capture(self,g,a,scheduled_s,resource_tick):
            clock[0]=scheduled_s;requests.append((g['run_id'],a,scheduled_s))
            for _ in range(3):clock[0]+=8;resource_tick()
            return (not(g['run_id']=='soak-reply-02' and a==1)),[]
    value=runner.run_soak(Hold(),clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
    assert value['passed'] and len(requests)==11
    assert requests[-1][:2]==('soak-reply-02',2) and requests[-1][2]>=600

def test_failed_reconnect_exports_before_propagating_cap_failure(tmp_path,monkeypatch):
    import echoear_aec_reconnect as reconnect
    order=[]
    monkeypatch.setattr(reconnect,'capture_reconnect',lambda *a,**k:(_ for _ in ()).throw(TimeoutError('cap')))
    def dump(board,path):order.append('export');path.write_bytes(b'retained')
    monkeypatch.setattr(reconnect,'dump_capture',dump)
    with pytest.raises(TimeoutError):reconnect.run_reconnect(None,None,lambda *a:({},[]),None,lambda:[],None,tmp_path,dict(id='failed'),-60,{},None,{})
    assert order==['export'] and (tmp_path/'failed/capture.npz').read_bytes()==b'retained'
    assert 'TimeoutError' in json.loads((tmp_path/'failed/acquisition-failure.json').read_text())['error']


@pytest.mark.parametrize('failure',[RuntimeError('cell failed'),runner.LinkProbeFailed('LINK PROBE FAILED, retry later')])
def test_hold_cleanup_keeps_lock_through_verified_restore_and_mixer(tmp_path,monkeypatch,failure):
    calls=[];h=runner.BenchHold(plan(),plan()['sessions'][0],tmp_path)
    h.touched=True;h.bot=SimpleNamespace(stop=lambda:calls.append('stop-bot'));h.board=SimpleNamespace(close=lambda:calls.append('close-serial'))
    h.lock=SimpleNamespace(__exit__=lambda *args:calls.append('unlock'))
    (tmp_path/'mixer-before.state').write_text('saved mixer')
    monkeypatch.setattr(runner,'flash',lambda *a:calls.append('flash-normal'))
    monkeypatch.setattr(runner,'boot',lambda *a:SimpleNamespace(close=lambda:calls.append('verified-boot')))
    def alsa(args,**kw):
        calls.append('mixer-'+args[-2])
        if args[-2]=='store':Path(args[2]).write_text('saved mixer')
    monkeypatch.setattr(runner.subprocess,'run',alsa)
    h.__exit__(type(failure),failure,None)
    assert calls==['stop-bot','close-serial','flash-normal','verified-boot','mixer-restore','mixer-store','unlock']
    assert h.result['restored_elf_sha256']==runner.NORMAL_HASH and h.result['mixer_equal'] and h.result['lock_released']


def test_candidate_builder_discards_cached_gain_and_never_flashes(tmp_path,monkeypatch):
    import build_echoear_aec_candidate as builder
    root=tmp_path/'tree';(root/'build/accept/config').mkdir(parents=True)
    config=root/'build/config-accept';config.write_text('CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=24\n')
    monkeypatch.setattr(builder,'ROOT',root)
    def idf(args,**kw):
        assert args[0]=='idf.py' and args[-1]=='build' and not config.exists()
        assert 'CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15' in (root/'build/accept.defaults').read_text()
        base=root/'build/accept'
        (base/'config/sdkconfig.h').write_text('#define CONFIG_DG_ECHOEAR_AEC 1\n#define CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB 15\n#define CONFIG_DG_DEMO_AUTOSTART 1\n#define CONFIG_DG_WIFI_OBSERVE 1\n')
        (base/'dg-esp32.elf').write_bytes(b'host-test ELF identity')
        (base/'dg-esp32.bin').write_bytes(b'host-test image')
        (base/'flasher_args.json').write_text(json.dumps(dict(flash_files={'0x10000':'dg-esp32.bin'},write_flash_args=[])))
    monkeypatch.setattr(builder.subprocess,'run',idf)
    monkeypatch.setattr(builder.subprocess,'check_output',lambda args,**kw:'a'*40 if kw.get('text') else b'')
    manifest=builder.build(tmp_path/'bundle')
    assert manifest['boot_ref_gain_db']==15 and manifest['bench_used'] is False and manifest['boot_verified'] is False

def test_overlap_ends_at_near_fixture_end_when_bot_lasts_longer():
    from echoear_aec_campaign_analysis import overlap_bounds
    value=overlap_bounds([.2,.22],[1.5,1.52],[1.8,1.85])
    assert value['scorable'] and value['lower_s']==pytest.approx(1.28) and value['upper_s']==pytest.approx(1.32)
    assert overlap_bounds([.2,.22],[1.5,1.52],[.1,.15])['upper_s']==0
    assert not overlap_bounds([.2,.22],None,[1.8,1.85])['scorable']


def test_export_timeout_and_45s_only_across_export(tmp_path,monkeypatch):
    assert runner.export_timeout(plan(),10.2)==pytest.approx(31.52)
    assert runner.export_timeout(plan(),7.34)==pytest.approx(24.084)
    clock=[0.];monkeypatch.setattr(runner,'snapshot',lambda *a:None)
    class Hold:
        p=plan();board=None;out=tmp_path
        def capture(self,g,a,scheduled_s,resource_tick):
            clock[0]=scheduled_s+5;resource_tick(force=True)
            clock[0]+=35;resource_tick(force=True)
            return True,[]
    result=runner.run_soak(Hold(),clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
    assert result['passed'] and sum(g['during_export'] for g in result['resource_gaps'])==10
    class MissingBefore(Hold):
        def capture(self,g,a,scheduled_s,resource_tick):
            clock[0]=scheduled_s+40
            return True,[]
    clock[0]=0
    with pytest.raises(RuntimeError,match='resource coverage'):runner.run_soak(MissingBefore(),clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
    class TooLong(Hold):
        def capture(self,g,a,scheduled_s,resource_tick):
            clock[0]=scheduled_s;resource_tick(force=True)
            clock[0]+=45.01;resource_tick(force=True)
            return True,[]
    clock[0]=0
    with pytest.raises(RuntimeError,match='resource coverage'):runner.run_soak(TooLong(),clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))


def test_quick_gap_labels_never_fail_reply_but_each_volume_needs_one():
    from echoear_aec_campaign_analysis import quick_gap
    assert quick_gap(1.9)['classification']=='quick-gap witnessed'
    for gap in (None,0,-.1,2,2.6):assert quick_gap(gap)['classification']=='not quick'
    p=plan();holds=passing_holds(p)
    echo=[c for c in holds[0]['cases'] if c['kind']=='echo']
    for c in echo:c['quick_gap']=quick_gap(2.5)
    assert not campaign_verdict(p,holds)['passed']
    for volume in (-6,0):next(c for c in echo if c['volume_db']==volume)['quick_gap']=quick_gap(1.5)
    assert campaign_verdict(p,holds)['passed']
    assert 'passed' not in quick_gap(2.5)


@pytest.mark.parametrize('kind',sorted(runner.HarnessFailure.allowed))
def test_harness_failures_share_noise_budget_and_check_device_first(kind,tmp_path,monkeypatch):
    calls=[]
    class Hold:
        out=tmp_path; p=plan()
        def capture(self,g,a):
            calls.append(('acquire',a))
            if a==1:raise runner.HarnessFailure(kind,'independent failure')
            if a==2:return False,[]
            return True,[dict(g['cells'][0],run_id='a-a3')]
        def recover_harness(self,g,a,exc):calls.append(('device-check',a));return dict(kind=exc.kind,retry_safe=True)
    monkeypatch.setattr(runner,'analyze_block',lambda root:dict(cases=[dict(id='a-a3',opportunity='eligible')]))
    state=runner.run_schedule(Hold(),[cell('a')],[cell('spare')],plan()['noise_gate'])
    assert calls==[('acquire',1),('device-check',1),('acquire',2),('acquire',3)]
    assert [j['attempt'] for j in state['journal']]==[1,2,3]
    assert all(not j['replacements'] for j in state['journal'])
    assert state['journal'][0]['harness_failure']['kind']==kind


@pytest.mark.parametrize('hard',['I2S fault','Resource floor','Liveness alarm','Observer loss','Unclassified timeout'])
def test_hard_stops_never_retry_or_consume_spares(hard,tmp_path):
    class Hold:
        out=tmp_path
        def capture(self,*a):raise RuntimeError(hard)
        def recover_harness(self,*a):pytest.fail('Hard stop was classified as retry')
    with pytest.raises(RuntimeError,match=hard):runner.run_schedule(Hold(),[cell('a')],[],plan()['noise_gate'])
    state=json.loads((tmp_path/'schedule.json').read_text())
    assert state['inflight']==dict(id='a',attempt=1) and not state['journal']


def test_dirty_counters_prevent_harness_retry(tmp_path):
    class Hold:
        out=tmp_path
        def capture(self,*a):raise runner.HarnessFailure('rf-pre-gate','busy')
        def recover_harness(self,*a):raise RuntimeError('Device counters not clean')
    with pytest.raises(RuntimeError,match='not clean'):runner.run_schedule(Hold(),[cell('a')],[],plan()['noise_gate'])
    assert json.loads((tmp_path/'schedule.json').read_text())['journal']==[]


def test_three_mixed_noise_harness_failures_pause_and_resume_only_pending(tmp_path):
    class Hold:
        out=tmp_path
        def capture(self,g,a):
            if g['run_id']=='b':return False,[]
            raise runner.HarnessFailure('capture-deadline','deadline')
        def recover_harness(self,g,a,exc):return dict(kind=exc.kind,retry_safe=True)
    cases=[cell(c) for c in 'abcd']
    with pytest.raises(runner.EnvironmentalPause):runner.run_schedule(Hold(),cases,[],plan()['noise_gate'])
    prior=json.loads((tmp_path/'schedule.json').read_text())
    assert prior['pending']==[dict(id='d',attempt=1),dict(id='a',attempt=2),dict(id='b',attempt=2),dict(id='c',attempt=2)]
    resumed=runner.Schedule(cases,[],plan()['noise_gate'],prior)
    assert not resumed.queue.noisy_cells
    assert [(g['run_id'],a) for g,a in resumed.queue.pending]==[('d',1),('a',2),('b',2),('c',2)]


def test_resume_preserves_spare_order_completed_misses_and_lifetime_attempts():
    cases=[cell(c) for c in 'abcde'];spares=[cell('spare1'),cell('spare2')]
    q=runner.Schedule(cases,spares,plan()['noise_gate'])
    g,a=q.next();q.finish(g,a,True,{'a':'eligible'})
    g,a=q.next();q.finish(g,a,True,{'b':'unscorable-future'})
    for _ in range(3):g,a=q.next();q.finish(g,a,False,{})
    resumed=runner.Schedule(cases,spares,plan()['noise_gate'],q.state())
    assert [(g['run_id'],a) for g,a in resumed.queue.pending]==[('spare1',1),('c',2),('d',2),('e',2)]
    g,a=resumed.next();resumed.finish(g,a,True,{'spare1':'no-opportunity'})
    assert resumed.journal[-1]['replacements'][0]['spare']=='spare2'
    assert not {'a','b'} & {g['run_id'] for g,a in resumed.queue.pending}


def test_only_single_marked_gap_and_jabra_duration_are_classified(monkeypatch):
    blocks=np.array([[0,320,0,20,0],[320,640,20,40,1]])
    anchors=[dict(coordinate=0),dict(coordinate=40)]
    with pytest.raises(runner.HarnessFailure,match='One marked'):runner.check_capture_timing(blocks,anchors)
    blocks[0,4]=1
    with pytest.raises(ValueError,match='timing gap'):runner.check_capture_timing(blocks,anchors)
    blocks[0,2]=-1
    with pytest.raises(ValueError,match='precedes'):runner.check_capture_timing(blocks,anchors)
    def bad(*args):raise RuntimeError('Jabra playback duration mismatch: retained witness')
    monkeypatch.setattr(runner,'play_wav',bad)
    with pytest.raises(runner.HarnessFailure) as exc:runner.play_fixture(None,None)
    assert exc.value.kind=='jabra-duration'
    def missing(*args):raise RuntimeError('ALSA disappeared')
    monkeypatch.setattr(runner,'play_wav',missing)
    with pytest.raises(RuntimeError,match='disappeared'):runner.play_fixture(None,None)


def test_evaluation_ack_allows_ten_seconds_but_capture_can_bound_it(tmp_path,monkeypatch):
    clock=[0.];h=runner.BenchHold(plan(),plan()['sessions'][0],tmp_path);h.evaluation=tmp_path
    monkeypatch.setattr(runner.time,'monotonic',lambda:clock[0]);monkeypatch.setattr(runner.time,'sleep',lambda n:clock.__setitem__(0,clock[0]+n))
    h.events=lambda:[dict(number=h.number,t=clock[0])] if clock[0]>=8 else []
    h.send('health',None);assert 8<=clock[0]<10
    clock[0]=0
    with pytest.raises(runner.HarnessFailure) as exc:h.send('health',None,timeout_s=1)
    assert exc.value.kind=='evaluation-ack-timeout' and clock[0]<1.1


def test_fable_approval_and_real_reply_preflight_are_hash_bound(monkeypatch):
    p=plan();p['review_approval']=p['review_of_record']
    # The retained preflight binds the old whole bot file. New profile source
    # must invalidate it; do not refreeze or relabel measured evidence in a test.
    with pytest.raises(ValueError,match='prompt changed'):
        runner.validate_review_preflight(p,plan())
    # In-memory positive-control fixture only, not a new measured preflight.
    p['preflight_contract']['real_reply']['prompt_file_sha256']=runner.sha(runner.ROOT/'server/demo_bot.py')
    runner.validate_review_preflight(p,p)
    assert p['preflight_contract']['real_reply']['max_s']==4.9
    assert p['preflight_contract']['real_reply']['retained_replies']==46
    for approval in ('Astra-approved',dict(p['review_approval'],sha256='0'*64),dict(p['review_approval'],reviewer='Astra')):
        with pytest.raises(ValueError,match='Fable'):runner.validate_review_preflight(dict(p,review_approval=approval),plan())
    actual=runner.sha
    monkeypatch.setattr(runner,'sha',lambda path:'0'*64 if str(path).endswith('demo_bot.py') else actual(path))
    with pytest.raises(ValueError,match='prompt changed'):runner.validate_review_preflight(p,plan())


def bind_prior(root,p):
    root.mkdir(exist_ok=True)
    (root/'declaration.json').write_text(json.dumps(p))
    return dict(path=str(root),declaration_sha256=runner.sha(root/'declaration.json'),files_sha256={str(f.relative_to(root)):runner.sha(f) for f in root.rglob('*') if f.is_file()})


def test_resume_hashes_every_file_and_rejects_hard_unrestored_run(tmp_path):
    from echoear_aec_campaign_resume import resume_state,run_chain,remaining
    p=plan();old=tmp_path/'old';old.mkdir();hold=old/'qualification-01';hold.mkdir()
    result=dict(completed=True,lock_released=True,mixer_equal=True,cleanup_errors=[],restored_elf_sha256=runner.NORMAL_HASH,environmental_pause='noise',error='EnvironmentalPause: noise',reconnects=[],probes=[])
    (hold/'result.json').write_text(json.dumps(result));(hold/'capture.npz').write_bytes(b'original evidence')
    p['resume_from']=bind_prior(old,plan())
    assert run_chain(p)==[old] and 'qualification-01' in resume_state(p)
    (hold/'capture.npz').write_bytes(b'changed')
    with pytest.raises(ValueError,match='evidence hash'):resume_state(p)
    result['completed']=False;(hold/'result.json').write_text(json.dumps(result));p['resume_from']=bind_prior(old,plan())
    with pytest.raises(ValueError,match='integrity/cleanup'):resume_state(p)
    assert remaining([dict(id='one'),dict(id='two')],{'one':dict(noise=True,passed=False,attempt=1)},'probes')==[dict(id='two')]
    assert remaining([dict(id='one')],{'one':dict(noise=False,attempt=2)},'probes')==[dict(id='one',start_attempt=3)]
    with pytest.raises(ValueError,match='budget exhausted'):remaining([dict(id='one')],{'one':dict(noise=False,attempt=3)},'probes')


def test_soak_resume_acquires_only_remaining_ids_at_carried_attempts(tmp_path,monkeypatch):
    clock=[0.];seen=[];monkeypatch.setattr(runner,'snapshot',lambda *args:None)
    class Hold:
        p=plan();board=None;out=tmp_path
        def capture(self,g,a,scheduled_s,resource_tick):
            seen.append((g['run_id'],a,scheduled_s));clock[0]=scheduled_s+5;resource_tick()
            return True,[]
    prior=dict(attempts=[dict(id='soak-reply-01',attempt=1,noise=False)],pending=[dict(id='soak-reply-09',attempt=1),dict(id='soak-reply-01',attempt=2)],exhausted=[],inflight=None)
    result=runner.run_soak(Hold(),prior=prior,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
    assert seen==[('soak-reply-01',2,10),('soak-reply-09',1,490)]
    assert result['passed'] and result['duration_s']>=600 and result['requests_this_hold']==2


def test_resume_rejects_budget_reset_and_preflight_omission():
    q=runner.Schedule([cell('a')],[],plan()['noise_gate'])
    g,a=q.next();q.finish(g,a,False,{})
    state=q.state();state['pending'][0]['attempt']=1
    with pytest.raises(ValueError,match='lifetime journal'):runner.Schedule([cell('a')],[],plan()['noise_gate'],state)
    runner.validate_timing_preflight(plan(),dict(timing_contract=plan()['preflight_contract']))
    for evidence in ({},dict(timing_contract={'real_reply':dict(max_s=5)})):
        with pytest.raises(ValueError,match='timing/reply bound'):runner.validate_timing_preflight(plan(),evidence)


@pytest.mark.parametrize('dirty',[None,'i2s','resources','delivery','transport','admission','observer','liveness'])
def test_real_harness_recovery_checks_counters_before_retry(tmp_path,monkeypatch,dirty):
    h=runner.BenchHold(plan(),plan()['sessions'][0],tmp_path)
    h.attempt_before={};h.attempt_started_ns=0;h.attempt_cases=[];h.attempt_command_number=0
    g=dict(run_id='a',cells=[cell('a')]);(tmp_path/'a-a1').mkdir()
    h.events=lambda:[dict(event='rtp-observer-health',t=1,passed=dirty!='observer',sctp=dict(supported=True,flight_fix_enabled=True,records=1,truncated=False,unavailable=False))]+([dict(event='liveness-alarm',t=1)] if dirty=='liveness' else [])
    h.send=lambda *args:None;h.recorded=lambda:[]
    monkeypatch.setattr(runner.time,'sleep',lambda n:None)
    def retry_boundary(*args,admission_window=None):
        if admission_window is not None:admission_window.update(end_ns=1_000_000_000)
        return {},[]
    monkeypatch.setattr(runner,'boundary',retry_boundary)
    def i2s(*args):
        if dirty=='i2s':raise RuntimeError('I2S fault')
    monkeypatch.setattr(runner,'require_zero_i2s',i2s)
    monkeypatch.setattr(runner,'check_reply_resources',lambda *a,**k:dict(all_passed=dirty!='resources'))
    monkeypatch.setattr(runner,'live_admission',lambda *a,**k:dict(passed=dirty!='admission'))
    import echoear_aec_campaign_analysis as analysis
    monkeypatch.setattr(analysis,'delivery_counters',lambda *a:dict(passed=dirty!='delivery'))
    monkeypatch.setattr(analysis,'transport',lambda *a:dict(passed=dirty!='transport'))
    failure=runner.HarnessFailure('rf-pre-gate','busy')
    if dirty:
        with pytest.raises(RuntimeError):h.recover_harness(g,1,failure)
    else:assert h.recover_harness(g,1,failure)['retry_safe']
    record=json.loads((tmp_path/'a-a1/acquisition-failure.json').read_text())
    assert record['retry_safe']==(dirty is None)


def test_full_resume_driver_skips_completed_ids_and_holds(tmp_path,monkeypatch):
    import echoear_aec_campaign_resume as resume
    import echoear_aec_campaign_analysis as analysis
    p=plan();seen=[];entered=[]
    states={}
    for s in p['sessions'][:-1]:
        cases=[c for c in p['cases']+p['controls'] if c['session_id']==s['id'] and c['kind']!='probe']
        spares=[c for c in p['spares'] if c['session_id']==s['id']]
        queue=runner.Schedule(cases,spares,p['noise_gate'])
        while (item:=queue.next()) is not None:
            g,a=item
            # One remaining noise attempt, with two budget slots already spent.
            noise=not (g['run_id']=='qualification-02-stop-01' and a<3)
            if g['run_id']=='qualification-02-stop-01' and a==3:
                queue.queue.pending.appendleft(item);queue.inflight=None;break
            queue.finish(g,a,noise,{c['id']:'eligible' for c in g['cells']})
        states[s['id']]=dict(schedule=queue.state(),reconnects={c['id']:dict(attempt=1,noise_passed=True) for c in p['reconnects'] if c['session_id']==s['id']},probes={c['id']:dict(attempt=1,noise=True) for c in p['controls'] if c['kind']=='probe' and c['session_id']==s['id']})
    states['soak-01']=dict(soak_complete=True)
    class Hold:
        def __init__(self,p,s,out):self.out=out;self.s=s
        def __enter__(self):self.out.mkdir();entered.append(self.s['id']);return self
        def __exit__(self,*a):pass
        def connect(self,*a):pass
        def link_probe(self):pass
        def capture(self,g,a):
            seen.append((g['run_id'],a));return True,[dict(c,run_id=c['id']+f'-a{a}') for c in g['cells']]
        def finish(self):pass
        def probe(self,*a):pytest.fail('Completed probe reacquired')
        def reconnect(self,*a):pytest.fail('Completed reconnect reacquired')
    monkeypatch.setattr(runner,'validate_campaign',lambda p:None)
    monkeypatch.setattr(resume,'resume_state',lambda p:states)
    monkeypatch.setattr(runner,'analyze_block',lambda root:dict(cases=[dict(id='qualification-02-stop-01-a3',opportunity='eligible')]))
    monkeypatch.setattr(runner,'check_esptool',lambda:None)
    monkeypatch.setattr(runner.signal,'signal',lambda *a:None)
    monkeypatch.setattr(analysis,'analyze_campaign',lambda root:{})
    declaration=tmp_path/'declaration.json';declaration.write_text(json.dumps(p))
    runner.run(declaration,tmp_path/'resumed',hold_factory=Hold)
    assert entered==['qualification-02'] and seen==[('qualification-02-stop-01',3)]


def test_real_capture_rf_failure_happens_before_any_record_or_stimulus(tmp_path,monkeypatch):
    h=runner.BenchHold(plan(),plan()['sessions'][0],tmp_path);calls=[]
    monkeypatch.setattr(runner,'boundary',lambda *a:({},[]))
    monkeypatch.setattr(runner.host_nic,'snapshot',lambda:{})
    monkeypatch.setattr(runner.host_nic,'gate',lambda *a:dict(passed=False))
    monkeypatch.setattr(runner,'command',lambda b,c,*a:calls.append(c))
    monkeypatch.setattr(runner.time,'sleep',lambda n:None)
    monkeypatch.setattr(runner,'capture_start',lambda *a:pytest.fail('Recording began on failed RF gate'))
    with pytest.raises(runner.HarnessFailure) as exc:h.capture(dict(run_id='a',cells=[cell('a')]),1)
    assert exc.value.kind=='rf-pre-gate' and calls==['playback-volume -6']
    assert (tmp_path/'a-a1/rf-pre-gate.json').exists()


@pytest.mark.parametrize('complete,transport_ok,global_event',[(True,True,None),(False,True,None),(True,False,None),
    *[(True,True,event) for event in ('liveness-alarm','ErrorFrame','FatalErrorFrame','evaluation-error','tts-reconnect-refused','tts-reconnect-failed','BotStoppedSpeakingFrame')]])
def test_real_capture_exports_with_measured_preflight_timeout_and_forced_samples(tmp_path,monkeypatch,complete,transport_ok,global_event):
    clock=[0.];exports=[];calls=[];h=runner.BenchHold(plan(),plan()['sessions'][-1],tmp_path)
    h.baseline=-60;h.qref=np.zeros(64000)
    monkeypatch.setattr(runner.time,'monotonic',lambda:clock[0]);monkeypatch.setattr(runner.time,'monotonic_ns',lambda:int(clock[0]*1e9));monkeypatch.setattr(runner.time,'sleep',lambda n:clock.__setitem__(0,clock[0]+n))
    monkeypatch.setattr(runner,'boundary',lambda *a:({},[]));monkeypatch.setattr(runner.host_nic,'snapshot',lambda:{})
    monkeypatch.setattr(runner.host_nic,'gate',lambda *a:dict(passed=True));monkeypatch.setattr(runner,'command',lambda *a:None)
    monkeypatch.setattr(runner,'capture_start',lambda b:dict(coordinate=0));monkeypatch.setattr(runner,'clock_anchor',lambda b:dict(coordinate=int(clock[0]*1e6)))
    def stop(b):calls.append('stop');return dict(coordinate=int(clock[0]*1e6))
    monkeypatch.setattr(runner,'capture_stop',stop)
    def dump(b,path,timeout):calls.append('export');exports.append(timeout)
    monkeypatch.setattr(runner,'dump_capture',dump)
    blocks=np.array([[0,64000,0,1,0]])
    monkeypatch.setattr(runner,'load_capture',lambda p:(np.zeros(64000),np.zeros(64000),np.zeros(64000),blocks))
    monkeypatch.setattr(runner,'quiet_gate',lambda *a,**k:dict(passed=True))
    monkeypatch.setattr(runner,'reference_threshold',lambda *a:1)
    monkeypatch.setattr(runner,'require_zero_i2s',lambda *a:None)
    monkeypatch.setattr(runner,'check_reply_resources',lambda *a,**k:dict(all_passed=True));monkeypatch.setattr(runner,'live_admission',lambda *a,**k:dict(passed=True))
    import echoear_aec_campaign_analysis as analysis
    monkeypatch.setattr(analysis,'delivery_counters',lambda *a:dict(passed=True))
    monkeypatch.setattr(analysis,'transport',lambda *a:dict(passed=transport_ok,loss_fraction=0 if transport_ok else .0089))
    h.recorded=lambda:[];h.send=lambda *a,**k:None
    events=[dict(case_id='soak-reply-01-a1',event='frame',frame='BotStartedSpeakingFrame',t=2.3),dict(case_id='soak-reply-01-a1',event='frame',frame='BotStoppedSpeakingFrame',t=4)]
    events.append(dict(case_id='soak-reply-01-a1',event='rtp-observer-health',passed=True,sctp=dict(supported=True,flight_fix_enabled=True,records=1,truncated=False,unavailable=False)))
    if not complete:events=[e for e in events if e.get('frame')!='BotStoppedSpeakingFrame']
    if global_event:
        events.append(dict(case_id=None,event='frame' if global_event.endswith('Frame') else global_event,frame=global_event,t=1000))
    h.evaluation=tmp_path/'evaluation';h.evaluation.mkdir()
    (h.evaluation/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    alarm=global_event and global_event!='BotStoppedSpeakingFrame'
    def tick(force=False):
        if force:calls.append('sample')
    args=dict(run_id='soak-reply-01',cells=[runner.soak_cells(plan())[0]])
    if alarm:
        with pytest.raises(RuntimeError,match='Active bot/transport error'):h.capture(args,1,scheduled_s=2.3,resource_tick=tick)
        assert clock[0]<3 and not h.result['cases']
    elif complete and not transport_ok:
        with pytest.raises(RuntimeError,match='transport failure'):h.capture(args,1,scheduled_s=2.3,resource_tick=tick)
        saved=json.loads((tmp_path/'soak-reply-01-a1/result.json').read_text())
        assert saved['checks']['transport']['passed'] is False
        assert h.result['hard_failure']['checks']['transport']['loss_fraction']==.0089
        assert not h.result['cases']
    elif complete:
        noise,_=h.capture(args,1,scheduled_s=2.3,resource_tick=tick);assert noise
    else:
        with pytest.raises(runner.HarnessFailure) as exc:h.capture(args,1,scheduled_s=2.3,resource_tick=tick)
        assert exc.value.kind=='capture-deadline' and clock[0]<28
    elapsed=json.loads((tmp_path/'soak-reply-01-a1/capture-duration.json').read_text())['elapsed_s']
    assert exports==[pytest.approx(2.6*elapsed+5)] and calls==['stop','sample','export','sample']
    timing=json.loads((tmp_path/'soak-reply-01-a1/attempt-window.json').read_text())
    assert timing['capture_end_s']==pytest.approx(elapsed)
    if complete and not alarm:
        assert timing['after_boundary_s']>=timing['capture_end_s']
        assert timing['window_s'][1]==timing['after_boundary_s']
    else:assert timing['after_boundary_s'] is None


def test_resume_idle_epoch_does_not_invent_missing_generation_or_hide_request():
    events=[dict(event='peer-binding',pc_id='idle',t=1)]
    value=generation_audit(events,[])
    assert value['passed'] and value['sessions'][0]['idle_no_generation_requested']
    assert not generation_audit(events+[dict(event='script-request',pc_id='idle',t=2)],[])['passed']
    assert not generation_audit(events+[dict(event='liveness-alarm',pc_id='idle',t=2)],[])['passed']
    assert not generation_audit(events+[dict(event='tts-reconnect-start',pc_id='idle',t=2),dict(event='tts-reconnect-ready',pc_id='idle',t=3)],[])['passed']


def test_resumed_verdict_combines_partial_holds_without_discarding_old_failures():
    p=plan();holds=passing_holds(p)
    original=holds[0];resumed=dict(id=original['id'],passed=True,cases=original['cases'][10:])
    original['cases']=original['cases'][:10];holds.append(resumed)
    assert campaign_verdict(p,holds)['passed']
    original['passed']=False
    assert not campaign_verdict(p,holds)['passed']
    original['passed']=True;resumed['cases'].append(original['cases'][0])
    assert not campaign_verdict(p,holds)['passed'] # cannot count original + reacquired clean cell


@pytest.mark.parametrize('diagnostic_error',[False,True])
def test_boundary_retains_wifi_dump_without_a_new_gate(tmp_path,monkeypatch,diagnostic_error):
    calls=[]
    board=SimpleNamespace(cursor=12,lines_since=lambda cursor:[(13,0,'wifi driver retained dump')])
    def command(*args):
        calls.append(args[1])
        if diagnostic_error:raise TimeoutError('wifi-stats diagnostic timeout')
    monkeypatch.setattr(runner,'command',command)
    monkeypatch.setattr(runner,'admission_boundary',lambda *args:(calls.append('boundary') or ({'device_us':1},[])))
    assert runner.boundary(board,tmp_path,'before')==({'device_us':1},[])
    assert calls==['wifi-stats','boundary']
    assert 'wifi driver retained dump' in (tmp_path/'before-wifi-stats.log').read_text()
    assert json.loads((tmp_path/'before-wifi-stats.json').read_text())['completed'] is not diagnostic_error


def test_noise_journal_uses_explicit_verdict_and_replays_legacy_rows():
    q=runner.Schedule([cell('a'),cell('b')],[],plan()['noise_gate'])
    q.finish(*q.next(),False,{'a':'eligible'})
    state=q.state()
    assert state['journal'][0]['noise_gate_passed'] is False and 'noise' not in state['journal'][0]
    runner.Schedule([cell('a'),cell('b')],[],plan()['noise_gate'],state)
    state['journal'][0]['noise']=state['journal'][0].pop('noise_gate_passed')
    runner.Schedule([cell('a'),cell('b')],[],plan()['noise_gate'],state)


def test_coordinator_decisions_are_hash_bound(monkeypatch):
    p=plan();p['review_approval']=p['review_of_record']
    actual=runner.sha
    decision=p['review_of_record']['coordinator_decisions']['path']
    monkeypatch.setattr(runner,'sha',lambda path:'0'*64 if str(path)==decision else actual(path))
    with pytest.raises(ValueError,match='Coordinator decisions'):runner.validate_review_preflight(p,plan())


@pytest.mark.parametrize('residual_dbfs',[-56,-45])
def test_highpass_residual_is_diagnostic_with_identical_masks_and_delay(monkeypatch,residual_dbfs):
    import echoear_aec_campaign_analysis as analysis
    from echoear_aec_metrics import reduction
    rate=16000;n=64000;t=np.arange(n)/rate
    raw=3000*np.sin(2*np.pi*1000*t);ref=np.zeros(n);ref[3200:51200]=raw[3200:51200]
    afe=32768*10**(residual_dbfs/20)*np.sqrt(2)*np.sin(2*np.pi*30*t)
    quiet=np.zeros(32000);source=raw[3200:51200]
    mask=np.zeros(n//320,dtype=bool);mask[10:160]=True
    gates=[True]*30+[False]*(n//320-30)
    monkeypatch.setattr(analysis,'source_gate',lambda *a:gates)
    play=dict(passed=True,source_extent=[3200,51200],source_first_speech_sample=0)
    result=analysis.echo_measure(raw,afe,ref,quiet,source,play,'',np.zeros((1,5)))
    first=mask.copy();first[35:]=False
    post=mask & np.array([not x for x in gates])
    for name,selected in [('whole',mask),('first_500ms',first),('post_guard',post)]:
        full=reduction(raw,afe,selected)
        assert {k:v for k,v in result[name].items() if k!='highpass100_residual_dbfs'}==full
        assert result[name]['highpass100_residual_dbfs']<full['residual_dbfs']-30
    assert result['passed'] is (residual_dbfs==-56)
    assert result['residual_diagnostic_filter']['gate'] is False
