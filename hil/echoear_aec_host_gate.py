"""Prospective campaign host gates. All queries are read-only; never scan actively."""
import json
import math
from pathlib import Path
import host_wifi_diagnostics as wifi

BSSID='b4:20:46:f4:ea:e5'
POLICY=dict(interface='wlp8s0',bssid=BSSID,minimum_signal_dbm=-60,supplicant_state_changes=0,
            scan_hz=2,scan_diagnostic_only=True,rule='host-environment-v1')
V85_POLICY=dict(POLICY,rule='host-environment-v8.5',connection='daily-633',
    connection_uuid='fb56e504-5ac1-4e83-8e79-1a9884889dbd')

LEGACY_CADENCE=dict(rule='soak-01-lattice-v1',hold='qualification-01',period_s=304.4058455722058,
    phase_s=3656270.038243419,host_boot_id='676ff572-1edc-4588-9651-129d4054bd87',
    phase_window_s=[-5,20],required_consecutive_cycles=2,
    counter_fallback='Trace stays off. Four or more late/missing slots summed across PERF intervals overlapping one lattice window are conservative possible-run witnesses, not measured consecutive runs. Two adjacent cycles stop before more scoring; ambiguous intervals spanning multiple lattice windows cannot establish two events.')

CADENCE={k:v for k,v in LEGACY_CADENCE.items() if k!='counter_fallback'}
CADENCE.update(rule='soak-01-uplink-lattice-v2',counter_diagnostic='Report-only PERF late/missing counter sums and flagged pairs; only two adjacent qualifying-uplink cycles pause.')


def active_profile_witness(policy):
    """Read the active identity around a UUID-addressed saved-profile query.

    Never activate a profile or request a scan. Both identity samples must match
    the declaration, so a profile switch during the query fails closed as well.
    """
    command=['nmcli','--escape','no','-g','GENERAL.CONNECTION,GENERAL.CON-UUID',
             'device','show',policy['interface']]
    before=wifi.query(command)
    profile=wifi.query(['nmcli','--escape','no','-g',
        'connection.id,connection.uuid,802-11-wireless.bssid',
        'connection','show','uuid',policy['connection_uuid']])
    after=wifi.query(command)
    return dict(before=before,profile=profile,after=after)


def active_profile_checks(witness,policy):
    def fields(command):
        if not isinstance(command,dict) or command.get('available') is not True or command.get('stderr','').strip():return None
        return command.get('stdout','').strip().splitlines()
    witness=witness or {}
    expected=[policy['connection'],policy['connection_uuid']]
    profile=fields(witness.get('profile'))
    return dict(active_profile_before=fields(witness.get('before'))==expected,
        active_profile_after=fields(witness.get('after'))==expected,
        active_profile_lock=bool(profile and len(profile)==3 and profile[:2]==expected
                                 and profile[2].lower()==policy['bssid']))


def boundary_verdict(value, lock, policy, active_profile=None):
    link=value.get('link',{});signal=link.get('signal_dbm')
    checks=dict(saved_lock=lock.get('available') is True and not lock.get('stderr','').strip() and lock.get('stdout','').strip().lower()==policy['bssid'],
        association=link.get('available') is True and link.get('connected') is True and link.get('bssid')==policy['bssid'],
        frequency=type(link.get('frequency_mhz')) is int and link['frequency_mhz']>0,
        signal=type(signal) in (int,float) and math.isfinite(signal) and signal>=policy['minimum_signal_dbm'],
        supplicant=value.get('journal_available') is True and value.get('supplicant_state_changes')==0)
    if policy.get('rule')=='host-environment-v8.5':
        checks.update(active_profile_checks(active_profile,policy))
    return dict(**(dict(active_profile=active_profile) if policy.get('rule')=='host-environment-v8.5' else {}),
                passed=all(checks.values()),checks=checks,host_wifi=value,lock_command=lock,
                failure_class='harness/environment',credit=False)


def lattice(events, policy, boot_id):
    if boot_id!=policy['host_boot_id']:
        return dict(passed=False,reason='Host boot changed: historical monotonic lattice unavailable; re-examine host',matches=[])
    phase,period=policy['phase_s'],policy['period_s'];low,high=policy['phase_window_s'];matches=[]
    for event in events:
        lo,hi=event['window_s']
        first=math.ceil((lo-phase-high)/period-1e-9);last=math.floor((hi-phase-low)/period+1e-9)
        if first==last:
            matches.append(dict(event=event,cycle=first,predicted_s=phase+period*first))
    cycles={v['cycle'] for v in matches if v['event']['kind']=='qualifying-uplink'}
    counter={}
    for v in matches:
        e=v['event']
        if e['kind']!='counter-possible-late-run':continue
        key=(v['cycle'],e.get('pc_id'),e.get('generation'))
        totals=counter.setdefault(key,dict(late=0,missing=0))
        for k in totals:totals[k]+=e[k]
    counter_cycles={key[0] for key,totals in counter.items() if min(totals.values())>=4}
    diagnostic={}
    if policy['rule']=='soak-01-lattice-v1':cycles |= counter_cycles
    elif policy['rule']=='soak-01-uplink-lattice-v2':
        combined=cycles|counter_cycles
        diagnostic['counter_evaluation']=dict(report_only=True,totals=[dict(cycle=k[0],pc_id=k[1],generation=k[2],**v) for k,v in counter.items()],
            flagged_cycles=sorted(counter_cycles),consecutive_cycle_pairs=[[n,n+1] for n in sorted(counter_cycles) if n+1 in counter_cycles],
            legacy_combined_pairs=[[n,n+1] for n in sorted(combined) if n+1 in combined])
    else:raise ValueError('Unknown periodicity rule')
    pairs=[[n,n+1] for n in sorted(cycles) if n+1 in cycles]
    return dict(**diagnostic,passed=not pairs,matches=matches,consecutive_cycle_pairs=pairs,policy=policy,
                reason='304s recurrence: stop and re-examine host' if pairs else None)


class HostGate:
    def __init__(self, hold):
        self.hold=hold;self.policy=hold.p['host_environment'];self.diag=wifi.HostWifiDiagnostics(hold.out)
        self.records=[];self.failed=False;self.perf=[];self.cursor=0;self.cadence=[]
    def start(self):
        self.diag.start()
        self.boundary(self.hold.out,'hold-start')
    def boundary(self,folder,label,*,enforce=True):
        from echoear_aec_campaign import EnvironmentalPause
        if self.policy.get('rule')=='host-environment-v8.5':
            name=str((folder/label).relative_to(self.hold.out))
            self.hold.result.setdefault('host_boundary_attempts',[]).append(name)
            self.hold.save()
        self.diag.boundary(folder,label)
        try:value=json.loads((folder/f'{label}-host-wifi.json').read_text())
        except (OSError,ValueError):value=dict(available=False,error='Missing/unreadable host boundary')
        lock=wifi.query(['nmcli','--escape','no','-g','802-11-wireless.bssid','connection','show',self.policy['connection']])
        active=active_profile_witness(self.policy) if self.policy.get('rule')=='host-environment-v8.5' else None
        verdict=boundary_verdict(value,lock,self.policy,active)
        try:(folder/f'{label}-host-gate.json').write_text(json.dumps(verdict,indent=2)+'\n')
        except OSError as exc:
            self.failed=True;self.hold.result['host_environment']=dict(passed=False,error=str(exc))
            raise EnvironmentalPause('Cannot retain host environment witness') from exc
        self.records.append(dict(path=str((folder/f'{label}-host-gate.json').relative_to(self.hold.out)),passed=verdict['passed']))
        self.failed |= not verdict['passed']
        self.hold.result['host_environment']=dict(passed=not self.failed,boundaries=self.records)
        self.hold.save()
        if enforce and self.failed:raise EnvironmentalPause('Host environment gate failed: no credit; re-examine host')
    def periodicity(self):
        from echoear_aec_campaign import EnvironmentalPause
        from echoear_aec_accept import rows
        from echoear_aec_transport_environment import uplink_excursion
        p=self.hold.p['first_hold_periodicity']
        if self.hold.s['id']!=p['hold']:return
        # Retain raw PERF intervals. Counter-only candidate firmware does not
        # expose per-slot timing; preserve counter sums as report-only diagnostics.
        entries=self.hold.board.lines_since(self.cursor)
        for cursor,host_s,line in entries:
            self.cursor=cursor
            if line.startswith('PERF '):
                v=json.loads(line[5:]);self.perf.append(dict(host_s=host_s,value=v))
        events=[]
        packets=rows(self.hold.out/'rtp/socket-rtp.jsonl')
        for pc in {r['pc_id'] for r in packets}:
            own=[r for r in packets if r['pc_id']==pc];lo=min(r['observed_ms'] for r in own)/1000;hi=max(r['observed_ms'] for r in own)/1000
            if hi-lo<5:continue
            link=uplink_excursion(own,pc,lo,hi)
            if not link['valid']:raise EnvironmentalPause('Periodicity socket witness unavailable; no further scoring')
            events.extend(dict(kind='qualifying-uplink',pc_id=pc,window_s=[e['start_s'],e['start_s']]) for e in link['excursions'])
        for a,b in zip(self.perf,self.perf[1:]):
            x,y=a['value'],b['value']
            if (x.get('pc_id'),x.get('generation'))!=(y.get('pc_id'),y.get('generation')):continue
            late=y.get('late',0)-x.get('late',0);missing=y.get('playout',{}).get('missing_slots',0)-x.get('playout',{}).get('missing_slots',0)
            if late>=0 and missing>=0 and max(late,missing)>0:
                events.append(dict(kind='counter-possible-late-run',pc_id=x.get('pc_id'),generation=x.get('generation'),window_s=[a['host_s'],b['host_s']],late=late,missing=missing))
        verdict=lattice(events,p,Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        self.cadence.append(verdict)
        (self.hold.out/'periodicity.json').write_text(json.dumps(dict(checks=self.cadence,perf=self.perf),indent=2)+'\n')
        self.hold.result['periodicity']=dict(passed=all(v['passed'] for v in self.cadence),checks=len(self.cadence))
        self.hold.save()
        if not verdict['passed']:raise EnvironmentalPause(verdict['reason'])
    def stop(self):self.diag.stop()


def prelaunch(policy):
    """No output directory, sampler, network mutation, serial or bench lock."""
    lock=wifi.query(['nmcli','--escape','no','-g','802-11-wireless.bssid','connection','show',policy['connection']])
    raw=wifi.query(['iw','dev','wlp8s0','link'])
    value=dict(link=wifi.parse_link(raw),link_command=raw,journal_available=True,supplicant_state_changes=0,
               scope='prelaunch; hold journal interval starts at hold entry')
    active=active_profile_witness(policy) if policy.get('rule')=='host-environment-v8.5' else None
    return boundary_verdict(value,lock,policy,active)




def audit(root,policy):
    """Recheck retained boundary inputs, including required entry/end records."""
    import re
    root=Path(root);checks=[]
    required={'hold-start','hold-end','connected','disconnected'}
    required.update(str(p.relative_to(root))[:-len('-host-gate.json')] for p in root.rglob('*-host-gate.json'))
    required.update(str(p.relative_to(root))[:-len('-wifi-stats.json')] for p in root.rglob('*-wifi-stats.json'))
    saved_result=json.loads((root/'result.json').read_text()) if (root/'result.json').exists() else {}
    if policy.get('rule')=='host-environment-v8.5':required.update(saved_result.get('host_boundary_attempts',[]))
    for name in sorted(required):
        path=root/(name+'-host-gate.json')
        if (policy.get('rule')=='host-environment-v8.5' and not path.exists()
                and name in ('connected','disconnected') and saved_result.get('error_class')
                and name not in saved_result.get('host_boundary_attempts',[])
                and not (root/(name+'.json')).exists()):
            checks.append(dict(boundary=name,status='not-reached',passed=None));continue
        try:
            saved=json.loads((root/(name+'-host-gate.json')).read_text())
            value=json.loads((root/(name+'-host-wifi.json')).read_text())
            link=wifi.parse_link(value['link_command']);journal=value['journal_command']
            available=journal['available'] and not journal['stderr'].strip()
            count=sum(bool(re.search(r'\bwlp8s0\b.*supplicant.*state',line)) for line in journal['stdout'].splitlines()) if available else None
            actual=dict(value,link=link,journal_available=available,supplicant_state_changes=count)
            check=boundary_verdict(actual,saved['lock_command'],policy,saved.get('active_profile'))
            check['passed'] &= saved['host_wifi']==value and saved['passed']==check['passed']
            checks.append(dict(boundary=name,passed=check['passed']))
        except (OSError,ValueError,KeyError,TypeError) as e:checks.append(dict(boundary=name,passed=False,error=str(e)))
    return dict(passed=bool(checks) and all(c['passed'] for c in checks if c.get('status')!='not-reached'),boundaries=checks)


if __name__=='__main__':
    import sys
    result=prelaunch(json.loads(Path(sys.argv[1]).read_text())['host_environment'])
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['passed'] else 2)
