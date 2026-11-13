import json
from types import SimpleNamespace
import pytest
import host_wifi_diagnostics as wifi


def raw(text='',available=True,stderr=''):
    return dict(stdout=text,stderr=stderr,available=available,start_s=1.,end_s=1.1,returncode=0 if available else 1)


def test_link_values_disconnect_and_unavailable():
    text='Connected to B4:20:46:F4:EA:E5 (on wlp8s0)\n\tfreq: 5260\n\tsignal: -45.5 dBm\n'
    assert wifi.parse_link(raw(text))==dict(available=True,connected=True,bssid='b4:20:46:f4:ea:e5',frequency_mhz=5260,signal_dbm=-45.5)
    assert wifi.parse_link(raw('Not connected.'))['connected'] is False
    assert wifi.parse_link(raw('bad output'))['available'] is False
    assert wifi.parse_link(raw(available=False))['bssid'] is None


@pytest.mark.parametrize('journal_ok,stderr,count',[(True,'',2),(False,'permission denied',None),(True,'Not seeing other users messages',None)])
def test_boundary_keeps_raw_link_and_cumulative_journal_without_gating(tmp_path,monkeypatch,journal_ok,stderr,count):
    calls=[]
    def query(argv,timeout=2):
        calls.append(argv)
        if argv[0]=='iw':return raw('Connected to b4:20:46:f4:ea:e5 (on wlp8s0)\n freq: 5260\n signal: -45 dBm')
        return raw('device (wlp8s0): supplicant interface state: completed -> scanning\n'
                   'device (wlp8s0): supplicant interface state: scanning -> completed\n'
                   'device (other): supplicant interface state: x -> y\n',journal_ok,stderr)
    monkeypatch.setattr(wifi,'query',query);diag=wifi.HostWifiDiagnostics(tmp_path)
    for label in ('before','after'):
        diag.boundary(tmp_path,label)
        value=json.loads((tmp_path/f'{label}-host-wifi.json').read_text())
        assert value['supplicant_state_changes']==count
        assert value['link']['frequency_mhz']==5260 and value['diagnostic_only']
        assert 'completed -> scanning' in value['journal_command']['stdout']
    assert calls[1]==['journalctl','-u','NetworkManager','--since',diag.since,'--no-pager','-o','short-unix']
    report=wifi.block_report(tmp_path,tmp_path,0,2,[])
    assert report['supplicant_state_changes_delta']==(0 if count is not None else None)


def test_cached_scan_poll_is_two_hz_and_never_creates_bench_output(tmp_path,monkeypatch):
    now=[0.];monkeypatch.setattr(wifi.time,'monotonic',lambda:now[0]);calls=[]
    def query(argv,timeout):
        calls.append((now[0],argv,timeout))
        value=raw('BSS aa:bb:cc:dd:ee:ff(on wlp8s0)\nBSS aa:bb:cc:dd:ee:00(on wlp8s0)\n')
        value.update(start_s=now[0],end_s=now[0]);return value
    monkeypatch.setattr(wifi,'query',query)
    out=tmp_path/'absent';diag=wifi.HostWifiDiagnostics(out)
    class Cancel:
        def is_set(self):return len(calls)>=4
        def wait(self,n):now[0]+=n
        def set(self):pass
    diag.cancel=Cancel();diag.collect()
    assert [c[0] for c in calls]==[0,.5,1,1.5]
    assert all(c[1]==['iw','dev','wlp8s0','scan','dump'] and c[2]==.4 for c in calls)
    assert not out.exists() and len(diag.pending)==4
    out.mkdir();diag.stop();samples,errors=wifi.load_samples(out)
    assert not errors and [r['bss_count'] for r in samples]==[2]*4
    assert (out/'host-wifi-diagnostics.json').exists()


def test_failures_and_partial_scan_log_are_unavailable_not_zero(tmp_path,monkeypatch):
    monkeypatch.setattr(wifi,'query',lambda *a,**k:raw(available=False,stderr='denied'))
    sample=wifi.HostWifiDiagnostics(tmp_path).sample()
    assert not sample['available'] and sample['bss_count'] is None
    (tmp_path/'host-wifi-scan.jsonl').write_text(json.dumps(sample)+'\n{partial\n')
    rows,errors=wifi.load_samples(tmp_path)
    assert len(rows)==1 and len(errors)==1
    summary=wifi.scan_summary(rows,0,2)
    assert summary['bss_count_range'] is None and summary['failed_samples']==1


def test_scan_summary_reports_changes_and_sampling_gaps():
    samples=[dict(start_s=t,available=True,bss_count=n) for t,n in [(1,1),(1.5,4),(3,4)]]
    result=wifi.scan_summary(samples,0,4)
    assert result['bss_count_range']==[1,4] and result['max_sample_gap_s']==1.5
    assert result['count_changes']==[dict(at_s=1.5,before=1,after=4)]


def test_timeout_is_retained_not_raised(monkeypatch):
    def fail(*a,**k):raise TimeoutError('test timeout')
    monkeypatch.setattr(wifi.subprocess,'run',fail)
    result=wifi.query(['iw','dev','wlp8s0','scan','dump'])
    assert not result['available'] and 'test timeout' in result['error']


def test_missing_or_broken_host_diagnostics_do_not_change_soak_verdict(tmp_path,monkeypatch):
    import echoear_aec_link_soak as soak
    import echoear_aec_accept as accept
    def save(path,value):path.write_text(json.dumps(value))
    save(tmp_path/'declaration.json',dict(measurement_limit='test',host_wifi_diagnostics=wifi.POLICY))
    save(tmp_path/'result.json',dict(completed=True,restored_elf_sha256=soak.NORMAL_HASH,mixer_equal=True,lock_released=True,
         resources={'all_passed':True},rf_active={'passed':True},sctp_recovery={'passed':True}))
    monkeypatch.setattr(accept,'rows',lambda p:[])
    monkeypatch.setattr(soak,'uplink_excursion',lambda *a:dict(valid=True,stall=False,per_second=[],excursions=[]))
    monkeypatch.setattr(soak,'uplink_loss',lambda *a:dict(valid=True))
    monkeypatch.setattr(soak,'trace_bursts',lambda *a:dict(valid=True,missing_bursts=0,late_bursts=0,unresolved_bursts=0))
    for i in range(1,7):
        folder=tmp_path/f'block-{i:02d}';folder.mkdir()
        save(folder/'window.json',dict(id=i,mode='no-dump' if i%2 else 'dump',host_window_s=[0,300]))
        for label,t in [('before',0),('after',300000000)]:
            save(folder/f'{label}.json',dict(pc_id='pc',device_us=t,late=0,lost=0,post_plc_zeros=0))
        save(folder/'integrity.json',{'passed':True});save(folder/'prompts.json',[dict(finished_s=i)]*5)
        save(folder/'before-host-wifi.json',[]) # malformed/absent remains diagnostic
    result=soak.analyze(tmp_path)
    assert result['valid'] and result['verdict']=='export exonerated under declared rule'
    assert len(result['blocks'])==6 and result['host_wifi_diagnostics']['scan_read_errors']
    assert all(b['host_wifi']['diagnostic_only'] and not b['host_wifi']['scan']['available'] for b in result['blocks'])
