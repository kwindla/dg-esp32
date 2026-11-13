#!/usr/bin/env python3
"""Owned board-only tc rules, exact restoration and durable emergency cleanup."""
import argparse
import fcntl
import ipaddress
import json
import signal
import subprocess
import time
from pathlib import Path

TC='/usr/sbin/tc'
ROOT_HANDLE='8e01:'
FILTER_PREF='48100'


def command(*args, mutate=False, check=True):
    argv=(["sudo","-n"] if mutate else [])+[TC]+list(args)
    return subprocess.run(argv,text=True,capture_output=True,check=check).stdout


def snapshot(interface='wlp8s0',ifb='ifb0'):
    return {dev:{kind:json.loads(command('-j',kind,'show','dev',dev))
                 for kind in ('qdisc','class','filter')}
            for dev in (interface,ifb)}


def commands(board_ip, args, direction='both', interface='wlp8s0', ifb='ifb0',selector=()):
    address=str(ipaddress.IPv4Address(board_ip))+'/32'
    if direction not in ('downlink','uplink','both'):raise ValueError('direction')
    if not args:raise ValueError('netem arguments required')
    result=[]
    def shaped(dev, match):
        result.extend([
            ['qdisc','add','dev',dev,'root','handle',ROOT_HANDLE,'prio','bands','3',
             'priomap']+['0']*16,
            ['qdisc','add','dev',dev,'parent','8e01:3','handle','8e03:','netem']+list(args),
            ['filter','add','dev',dev,'parent',ROOT_HANDLE,'protocol','ip','pref',FILTER_PREF,
             'u32','match','ip',match,address]+list(selector)+['flowid','8e01:3']])
    if direction in ('downlink','both'):shaped(interface,'dst')
    if direction in ('uplink','both'):
        # IFB also filters the board IP; redirect only the matching ingress flow.
        shaped(ifb,'src')
        result.extend([
            ['qdisc','add','dev',interface,'handle','ffff:','ingress'],
            ['filter','add','dev',interface,'parent','ffff:','protocol','ip','pref',FILTER_PREF,
             'u32','match','ip','src',address]+list(selector)+['action','mirred','egress','redirect','dev',ifb]])
    return result


def impairment_requested(args):
    # Accounting-only loss 0% is the only non-impairment configuration.
    return list(args)!=['loss','0%']


def impairment_flow_gate(before,after,direction,*,session_seconds=None,rtp_bytes=None):
    directions=('tx','rx') if direction=='both' else ('tx',) if direction=='downlink' else ('rx',)
    delta={key:after.get(key)-before.get(key) if isinstance(before.get(key),int) and isinstance(after.get(key),int) else None for key in directions}
    # When full RTP byte evidence is available, require half that flow. Without
    # it require >=100 decimal kB/min; a connectivity check cannot qualify.
    floor={key:rtp_bytes[key]*.5 if rtp_bytes and isinstance(rtp_bytes.get(key),(int,float)) and rtp_bytes[key]>0
        else 100000*session_seconds/60 if session_seconds is not None and session_seconds>0 else None for key in directions}
    valid=all(value is not None and floor[key] is not None and value>=floor[key] for key,value in delta.items())
    return dict(valid=valid,board_bytes=delta,minimum_board_bytes=floor,direction=direction,session_seconds=session_seconds,
        reason=None if valid else 'Impairment invalid: owned flow below 50% RTP bytes / 100 kB per minute, or evidence missing')


def assert_selected_interface(directory,pc_id,interface,timeout=5):
    """Fail before fault injection on missing/wrong nominated-pair evidence."""
    path=Path(directory)/'ice-pairs.jsonl';deadline=time.monotonic()+timeout
    while True:
        try:
            rows=[json.loads(line) for line in path.read_text().splitlines()]
        except (FileNotFoundError,json.JSONDecodeError):rows=[]
        row=next((r for r in reversed(rows) if r.get('pc_id')==pc_id),None)
        if row is not None:
            selected=row.get('selected_interfaces')
            if (row.get('interface')!=interface or not row.get('pairs') or not selected
                    or len(selected)!=len(row['pairs']) or any(dev!=interface for dev in selected)):
                raise RuntimeError('Selected ICE pair is not on impaired interface '+interface)
            return row
        if time.monotonic()>=deadline:raise RuntimeError('Missing selected ICE pair at session start')
        time.sleep(.05)


class Netem:
    def __init__(self,board_ip,args,direction='both',*,state,interface='wlp8s0',ifb='ifb0',selector=(),account_both=True,rtp_directory=None):
        self.account_both=account_both
        self.board_ip=board_ip;self.args=list(args);self.direction=direction
        self.state=Path(state);self.interface=interface;self.ifb=ifb
        self.selector=tuple(selector)
        self.previous_handlers={};self.lock=None;self.before=None
        self.applied=[]
        self.cancelled_signal=None
        self.impairment_periods=[];self.phase_before=None;self.phase_args=None
        self.arm_flow_before=None;self.impairment_flow=None
        self.arm_started=None;self.phase_started=None
        self.rtp_directory=Path(rtp_directory) if rtp_directory is not None else None
        self.arm_rtp_before=None;self.phase_rtp_before=None

    def __enter__(self):
        self.state.parent.mkdir(parents=True,exist_ok=True)
        self.lock=open('/tmp/dg-esp32-tc.lock','a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.before=snapshot(self.interface,self.ifb)
            for dev,rows in self.before.items():
                roots=[q for q in rows['qdisc'] if q.get('root')]
                if len(roots)!=1 or roots[0]['handle']!='0:' or roots[0]['kind'] not in ('noqueue','fq_codel'):
                    raise RuntimeError('Refusing to replace a non-default qdisc: '+dev)
                if len(rows['qdisc'])!=1 or rows['filter'] or rows['class']:
                    raise RuntimeError('Existing tc hierarchy cannot be restored safely: '+dev)
            self._save('applying')
            for sig in (signal.SIGINT,signal.SIGTERM):
                self.previous_handlers[sig]=signal.signal(sig,self._signal)
            owned=commands(self.board_ip,self.args,self.direction,self.interface,self.ifb,self.selector)
            if self.account_both and self.direction!='both':
                opposite='uplink' if self.direction=='downlink' else 'downlink'
                owned+=commands(self.board_ip,['loss','0%'],opposite,self.interface,self.ifb,self.selector)
            for cmd in owned:
                # Journal first so partial application is still owned on interruption.
                self.applied.append(cmd);self._save('applying')
                command(*cmd,mutate=True)
            self._start_phase(self.args)
            self._save('active')
            return self
        except BaseException:
            self.__exit__(None,None,None)
            raise

    def _save(self,status,**extra):
        data=dict(status=status,board_ip=self.board_ip,interface=self.interface,ifb=self.ifb,
                  direction=self.direction,before=self.before,commands=self.applied,impairment_periods=self.impairment_periods,impairment_flow=self.impairment_flow,**extra)
        temp=self.state.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2)+'\n');temp.replace(self.state)

    def _signal(self,signum,frame):
        self.cancelled_signal=signum
        self.__exit__(None,None,None)
        raise SystemExit(128+signum)

    def _flow_bytes(self):
        result={}
        for key,dev in (('tx',self.interface),('rx',self.ifb)):
            try:
                rows=json.loads(command('-s','-j','qdisc','show','dev',dev))
                child=next((r for r in rows if r.get('handle')=='8e03:' and r.get('parent')=='8e01:3'),{})
                result[key]=child.get('bytes')
            except (OSError,subprocess.SubprocessError,ValueError,TypeError):
                result[key]=None
        return result

    def _start_phase(self,args):
        if self.arm_flow_before is None:
            self.arm_flow_before=self._flow_bytes();self.arm_started=time.monotonic()
            self.arm_rtp_before=self._rtp_bytes()
        self.phase_args=list(args)
        self.phase_before=self._flow_bytes() if impairment_requested(args) else None
        self.phase_started=time.monotonic()
        self.phase_rtp_before=self._rtp_bytes()

    def _rtp_bytes(self):
        if self.rtp_directory is None:return {}
        result={}
        # TX is full serialized RTP; RX UDP records include SRTP overhead,
        # giving a conservative (slightly higher) half-flow requirement.
        for key,name in (('tx','downlink.jsonl'),('rx','socket-rtp.jsonl')):
            path=self.rtp_directory/name
            try:
                rows=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
                values=[row.get('rtp_bytes') for row in rows]
                result[key]=sum(values) if all(isinstance(value,int) and value>=0 for value in values) else None
                if key=='rx' and (self.rtp_directory/'socket-status.jsonl').exists():result[key]=None
            except (OSError,json.JSONDecodeError):result[key]=None
        return result

    def _rtp_delta(self,before):
        after=self._rtp_bytes()
        return {key:after[key]-value if isinstance(value,int) and isinstance(after.get(key),int) and after[key]>=value else None
            for key,value in (before or {}).items()}

    def _finish_phase(self):
        if self.phase_before is None:return
        gate=impairment_flow_gate(self.phase_before,self._flow_bytes(),self.direction,
            session_seconds=time.monotonic()-self.phase_started,rtp_bytes=self._rtp_delta(self.phase_rtp_before))
        gate['args']=self.phase_args
        self.impairment_periods.append(gate);self.phase_before=None

    def update(self,args):
        if self.cancelled_signal is not None:
            # Cleanup callers may clear loss after the signal handler has
            # already restored the qdiscs. Do not mask SystemExit with tc's
            # missing-qdisc error and accidentally continue the campaign.
            if list(args)==['loss','0%']:return
            raise SystemExit(128+self.cancelled_signal)
        self._finish_phase()
        for dev in ([self.interface] if self.direction=='downlink' else
                    [self.ifb] if self.direction=='uplink' else [self.interface,self.ifb]):
            command('qdisc','change','dev',dev,'parent','8e01:3','handle','8e03:','netem',*args,mutate=True)

        self._start_phase(args)
        self._save('active')

    def __exit__(self,*exc):
        try:
            if self.phase_before is not None:self._finish_phase()
            if self.impairment_periods:
                self.impairment_flow=impairment_flow_gate(self.arm_flow_before,self._flow_bytes(),self.direction,
                    session_seconds=time.monotonic()-self.arm_started,rtp_bytes=self._rtp_delta(self.arm_rtp_before))
            if self.before is not None:
                restore(self.before,self.interface,self.ifb,self.applied)
                after=snapshot(self.interface,self.ifb)
                if after!=self.before:
                    self._save('restoration-failed',after=after)
                    raise RuntimeError('tc state differs from original; retained state '+str(self.state))
                self._save('restored',after=after)
        finally:
            for sig,handler in self.previous_handlers.items():signal.signal(sig,handler)
            self.previous_handlers={}
            if self.lock is not None:self.lock.close();self.lock=None
        if exc[0] is None and self.impairment_flow is not None and not self.impairment_flow['valid']:
            raise RuntimeError('Invalid impairment arm: owned flow below required byte floor or counters missing; qdiscs restored')


def restore(before,interface,ifb,applied):
    # Only delete our known root. A changed/foreign hierarchy is left untouched.
    owned_devices={cmd[cmd.index('dev')+1] for cmd in applied if cmd[:2]==['qdisc','add'] and 'root' in cmd}
    for dev in owned_devices:
        qdiscs=json.loads(command('-j','qdisc','show','dev',dev))
        owned=next((q for q in qdiscs if q.get('root') and q.get('handle')==ROOT_HANDLE and q['kind']=='prio'),None)
        if owned:command('qdisc','del','dev',dev,'root','handle',ROOT_HANDLE,mutate=True)
    qdiscs=json.loads(command('-j','qdisc','show','dev',interface))
    ingress_owned=any(cmd[:2]==['qdisc','add'] and 'ingress' in cmd for cmd in applied)
    if ingress_owned and any(q['kind']=='ingress' and q['handle']=='ffff:' for q in qdiscs):
        command('filter','del','dev',interface,'parent','ffff:','protocol','ip','pref',FILTER_PREF,mutate=True,check=False)
        other=json.loads(command('-j','filter','show','dev',interface,'parent','ffff:'))
        if other:raise RuntimeError('Foreign ingress filters appeared; refusing to remove them')
        command('qdisc','del','dev',interface,'ingress',mutate=True)
    # Deletion reinstates the kernel default qdisc with handle 0. Require exact
    # equality; never recreate an approximate hierarchy or silently accept drift.


def main():
    p=argparse.ArgumentParser();p.add_argument('--cleanup',action='store_true')
    p.add_argument('--state',type=Path,required=True);a=p.parse_args()
    if not a.cleanup:p.error('CLI only supports --cleanup; use the context manager for tests')
    data=json.loads(a.state.read_text())
    with open('/tmp/dg-esp32-tc.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        restore(data['before'],data['interface'],data['ifb'],data['commands'])
        after=snapshot(data['interface'],data['ifb'])
        assert after==data['before'],'Cleanup did not restore original tc state'
        data.update(status='restored',after=after);a.state.write_text(json.dumps(data,indent=2)+'\n')


if __name__=='__main__':main()
