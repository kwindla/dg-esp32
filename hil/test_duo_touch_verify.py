"""Fake clock and console tests for the physical-only GO window."""
import copy
import json
from types import SimpleNamespace
import pytest
import duo_touch_verify as touch
from test_duo_smoke import status,proof,switched
from p4_touch_verify import DRIVER


def driver(**changes):
    row=dict(initialized=True,enabled=True,phase=1,generation=3,pending=False,requests=0,request_rejected=0,phase_ignored=0,
             driver={k:0 for k in DRIVER})
    for k,v in changes.items():
        if k in DRIVER:row['driver'][k]=v
        else:row[k]=v
    return row


class TouchIO:
    def __init__(self,out,*,go_at=2,event_at=None,bad=None):
        self.out=out;out.mkdir(exist_ok=True);self.now=0;self.go_at=go_at;self.event_at=event_at;self.bad=bad
        self.args=SimpleNamespace(board='echoear');self.commands=[];self.events=[];self.tap_latencies=[]
        self.before,self.after,self.prior,self.shown,self.events,self.wire=switched(physical=True)
    def clock(self):return self.now
    def pause(self,seconds):
        assert 0<seconds<=1
        self.now+=seconds
        if self.go_at is not None and self.now>=self.go_at and not (self.out/'GO').exists():(self.out/'GO').write_text('ready\n')
    def command(self,text):self.commands.append(text)
    def mark(self):return self.now
    def snapshot(self):
        changed=self.event_at is not None and self.now>=self.event_at
        after=copy.deepcopy(self.after if changed else self.before);p=copy.deepcopy(self.shown if changed else self.prior)
        if changed and self.bad=='injection':after.update(taps_injected=1,taps_physical=0)
        if changed and self.bad=='generation':after['generation']=4;p['generation']=4
        if changed and self.bad=='session':p['session']='other'
        if changed and self.bad=='extra':after['taps_physical']=2
        return after,p
    def json_command(self,*args):
        changed=self.event_at is not None and self.now>=self.event_at
        return driver(taps=2,double_taps=1,injected=int(self.bad=='driver-injection'),read_errors=int(self.bad=='read-error')) if changed else driver()
    def evidence(self,mark):
        wire=copy.deepcopy(self.wire)
        for row in wire:row['monotonic_ns']=int((self.event_at or self.now)*1e9)
        return self.events,wire,[]


def test_go_gates_scoring_and_physical_witness_ends_window(tmp_path):
    io=TouchIO(tmp_path,go_at=20,event_at=25)
    row=touch.observe(tmp_path,io,emit=lambda *a,**kw:None)
    assert row['touch_verified'] and row['opened_at']==20 and row['closed_at']==25 and not row['window_complete']
    assert row['witness']['delta']['taps_physical']==1 and row['witness']['delta']['taps_injected']==0
    assert io.commands[0]=='touch-enable 1' and io.commands[-1]=='touch-enable 0'


def test_early_tap_is_retained_but_not_scored_and_window_is_full_600(tmp_path):
    io=TouchIO(tmp_path,go_at=20,event_at=5)
    row=touch.observe(tmp_path,io,emit=lambda *a,**kw:None)
    assert not row['touch_verified'] and row['window_complete'] and io.now==620
    assert row['before']['status']['taps_physical']==1 and not row['retryable']
    samples=[json.loads(s) for s in (tmp_path/'status-timeline.jsonl').read_text().splitlines()]
    assert any(s['phase']=='waiting-for-go' and s['status']['taps_physical']==1 for s in samples)


def test_go_timeout_does_not_open_window(tmp_path):
    io=TouchIO(tmp_path,go_at=None)
    row=touch.observe(tmp_path,io,emit=lambda *a,**kw:None)
    assert row['status']=='go-timeout' and io.now==900 and not (tmp_path/'window-opened.json').exists()


@pytest.mark.parametrize('bad',['injection','driver-injection','read-error','generation','session'])
def test_contaminated_or_reconnected_window_fails_and_disables_touch(tmp_path,bad):
    io=TouchIO(tmp_path,event_at=3,bad=bad)
    with pytest.raises(ValueError):touch.observe(tmp_path,io,emit=lambda *a,**kw:None)
    assert io.commands[-1]=='touch-enable 0'


def test_extra_physical_taps_never_selected_as_best_trial(tmp_path):
    io=TouchIO(tmp_path,event_at=3,bad='extra')
    row=touch.observe(tmp_path,io,emit=lambda *a,**kw:None)
    assert not row['touch_verified'] and not row['retryable'] and row['window_complete']


def test_existing_or_symlink_go_is_refused(tmp_path):
    io=TouchIO(tmp_path);(tmp_path/'GO').touch()
    with pytest.raises(ValueError,match='only after'):touch.observe(tmp_path,io)


def test_missing_bot_commit_cannot_pass_physical_witness():
    b,a,p,q,e,w=switched(physical=True)
    with pytest.raises(ValueError):touch.witness(b,a,p,q,[],w,driver(),driver(taps=2,double_taps=1))


def test_touch_instructions_require_contact_precheck_and_one_second_and_report_latency(tmp_path):
    io=TouchIO(tmp_path,event_at=5)
    row=touch.observe(tmp_path,io,emit=lambda *a,**kw:None)
    ready=json.loads((tmp_path/'ready-for-go.json').read_text())
    assert 'CONTACT PRE-CHECK' in ready['instructions'] and 'at least 1 s' in ready['instructions']
    assert row['touch_verified'] and io.tap_latencies[0]['milliseconds_bounds']==[0,1000]


@pytest.mark.parametrize('bad',['status','transaction'])
def test_physical_witness_rejects_contradictory_status_or_unrelated_transaction(bad):
    b,a,p,q,events,wire=switched(physical=True)
    if bad=='status':a.update(character='angel',rev=999)
    else:events[1]['transaction']='unrelated'
    with pytest.raises(ValueError):touch.witness(b,a,p,q,events,wire,driver(),driver(taps=2,double_taps=1))


def test_failed_physical_witness_keeps_available_latency_bounds(tmp_path):
    io=TouchIO(tmp_path,event_at=5);io.events=[]  # no matching bot commit
    row=touch.observe(tmp_path,io,emit=lambda *a,**kw:None)
    assert not row['touch_verified'] and io.tap_latencies[0]['milliseconds_bounds']==[0,1000]
