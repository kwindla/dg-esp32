import copy,json,math
from pathlib import Path
import numpy as np
import pytest
from scipy.signal import butter,lfilter
from echoear_aec_playback_template import filter_source,declared_corner,candidate_config,KEY,RULE
from echoear_aec_campaign_analysis import context_playback
from echoear_aec_campaign_budget import budget,validate_budget
from echoear_aec_metrics import frame_power
from echoear_aec_bench import load_capture
from test_aec_campaign import plan
from test_aec_link_soak_launch import launch
from freeze_aec_campaign_v8_3_1 import launch_script
from echoear_aec_host_gate import POLICY

BASE=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9')


@pytest.mark.parametrize('corner',[0,100,500,1000])
def test_source_filter_matches_second_order_butterworth_causally(corner):
    source=np.random.default_rng(42).normal(0,500,16000);saved=source.copy()
    expected=lfilter(*butter(2,corner,fs=16000,btype='highpass'),source) if corner else source
    actual=filter_source(source,corner)
    assert np.allclose(actual,expected,atol=1e-8) and np.array_equal(source,saved)
    quiet=np.zeros(16000);quiet[8000]=1
    assert np.all(filter_source(quiet,corner)[:8000]==0) # no acausal pre-ringing


def test_config_is_frozen_candidate_field_not_literal_and_fails_closed():
    p=plan();hz=p['candidate']['playback_config']['config'][KEY]
    assert declared_corner(p)==hz
    p['candidate']['playback_config']['config'][KEY]=300
    assert declared_corner(p)==300
    for value in [None,True,-1,8000,'500']:
        p['candidate']['playback_config']['config'][KEY]=value
        with pytest.raises(ValueError):declared_corner(p)
    p=plan();p['candidate']['playback_config']['elf_sha256']='wrong'
    with pytest.raises(ValueError):declared_corner(p)
    p=plan();p['gates']['missing_playback'].pop('source_template_rule')
    assert declared_corner(p) is None


def test_retained_52_contexts_source_onsets_and_stop10():
    root=BASE/'campaign-06/qualification-01'
    if not root.exists():pytest.skip('retained evidence unavailable')
    p=plan();hz=declared_corner(p);saved=json.loads((root/'result.json').read_text());changes=[];unchanged=0;total=0
    cells=list(saved['cases'])+json.loads((root/'qualification-01-stop-in-window-a1/result.json').read_text())['cases']
    for c in cells:
        contexts=[e for e in c['events'] if e['event']=='tts-context-pcm']
        for index,ctx in enumerate(contexts):
            src=np.fromfile(root/'evaluation'/ctx['path'],dtype='<i2').astype(float)
            first=lambda x:int(np.flatnonzero(frame_power(x)>1024)[0])*320
            old,new=first(src),first(filter_source(src,hz));total+=1
            if old==new:unchanged+=1
            else:changes.append((c['id'],index,old,new))
            if c['id']=='qualification-01-stop-10' and index==1:
                _,_,ref,blocks=load_capture(root/c['capture_dir']/'capture.npz');anchors=json.loads((root/c['capture_dir']/'anchors.json').read_text())
                rec=[json.loads(s) for f in (root/'sessions').glob('*/events.jsonl') for s in f.read_text().splitlines() if s]
                t=min(e['monotonic_ns'] for e in rec if e.get('kind')=='tts-transport-write' and e.get('context_id')==ctx['context_id'] and e.get('pc_id')==ctx['pc_id'] and e.get('non_silent'))/1e9
                assert not context_playback(src,ref,blocks,anchors,t)['passed']
                result=context_playback(src,ref,blocks,anchors,t,hz)
                assert result['passed'] and result['match']['valid']
    # Actual causal second-order firmware filter: review's 49/52 prediction
    # does not reproduce. Preserve measured changes, without tuning thresholds.
    assert (total,unchanged)==(52,44)
    assert {c[0] for c in changes}=={'qualification-01-echo-08','qualification-01-stop-05','qualification-01-stop-06','qualification-01-stop-08','qualification-01-stop-10','qualification-01-sentence-01','qualification-01-sentence-05','qualification-01-stop-in-window'}
    assert all(b>a for _,_,a,b in changes)


def test_spares_caps_and_budget_exactly_derived():
    p=plan();assert p['time_estimate']['v831_budget']==budget(p)
    for sid in ['qualification-01','qualification-normal']:
        for kind,expected in [('stop',7),('sentence',6)]:
            cells=[c for c in p['spares'] if c['session_id']==sid and c['kind']==kind]
            assert len(cells)==expected and len({c['id'] for c in cells})==expected
    assert sum(c['session_id']=='qualification-02' for c in p['spares'])==4
    assert p['noise_gate']['max_retries']==2 and p['noise_gate']['pause_consecutive_cells']==3
    b=budget(p)
    assert b['total_planning_all_attempts_s']==36900
    assert b['total_timeout_overhead_envelope_s']==108190
    assert b['total_planning_all_attempts_s']<b['available_0415_to_next_0300_s']<b['total_timeout_overhead_envelope_s']
    assert all(v['rtp_evidence_max_packets']>=100*v['timeout_overhead_envelope_s'] for v in b['sessions'].values())
    p['time_estimate']['v831_budget']['sessions']['qualification-01']['rtp_evidence_max_packets']=200000
    with pytest.raises(ValueError):validate_budget(p)


def test_only_decisions19_contract_fields_changed():
    old=json.loads((BASE/'freeze-v8.3/declaration-v8.3.json').read_text())
    # Pin this historical transition; v8.4 has its own contract-diff test.
    import subprocess
    new=json.loads(subprocess.check_output(['git','show','2f674e8:hil/plans/echoear-aec-accept-v1.json'],cwd=Path(__file__).resolve().parents[1],text=True))
    for key in ('cases','controls','reconnects','sessions','bot_config','noise_gate','rf','diagnostics','link_probe','fixture_policy','open_near_end_level'):
        assert old[key]==new[key],key
    for key in old['gates']:
        if key!='missing_playback':assert old['gates'][key]==new['gates'][key]
    m=copy.deepcopy(new['gates']['missing_playback']);m.pop('source_template_rule');m.pop('source_template_basis')
    assert old['gates']['missing_playback']==m
    blocks=copy.deepcopy(new['blocks']);next(b for b in blocks if b['id']=='normal-voice')['spares_per_kind']=dict(stop=4,sentence=2)
    assert blocks==old['blocks']
    f=copy.deepcopy(new['failure_policy']);f['retakes']=old['failure_policy']['retakes'];assert f==old['failure_policy']
    assert new['spares'][:len(old['spares'])]==old['spares'] # same ordered existing IDs
    for key in ('firmware_commit','elf_sha256','image_bundle_sha256','review_build_record'):
        assert old['candidate'][key]==new['candidate'][key]


def test_v831_launch_guard(launch):
    import hashlib
    policy=dict(POLICY,connection='test-profile');launch['declaration'].write_text(json.dumps(dict(host_environment=policy)))
    digest=hashlib.sha256(launch['declaration'].read_bytes()).hexdigest()
    launch['path'].write_text(launch_script(digest,launch['declaration'],launch['output'],policy))
    r,calls=launch['run'](FAKE_IW='Connected to '+POLICY['bssid']+' (on wlp8s0)\n freq: 5180\n signal: -53 dBm\n')
    assert r.returncode==0,r.stderr
    assert len([c for c in calls if c['name']=='uv'])==1 and not launch['output'].exists()
