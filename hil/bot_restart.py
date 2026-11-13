"""Shared owned-bot stop/restart and device recovery proof; no access on import."""
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]


def stop_owned(process, *, group=False, record=None):
    record={} if record is None else record
    record.update(pid=process.pid,requested_at=time.monotonic(),term_grace_s=10,
                  signals=[],fallback_kill=False)
    if process.poll() is not None:raise RuntimeError('Owned bot already exited before declared stop')
    def send(sig):
        if group:
            if os.getpgid(process.pid)!=process.pid:raise RuntimeError('Bot does not own its process group')
            os.killpg(process.pid,sig)
        else:process.send_signal(sig)
        record['signals'].append(dict(signal=sig.name,host_s=time.monotonic()))
    send(signal.SIGTERM)
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        record['fallback_kill']=True;send(signal.SIGKILL);process.wait(timeout=5)
    record.update(exited_at=time.monotonic(),returncode=process.returncode)
    return record


def lifecycle_sequence(lines):
    wanted=iter(('Disconnected','Starting','Ready'));state=next(wanted);chosen=[]
    for _,host_s,line in lines:
        match=re.search(r'\bSTATE (\w+) gen=(\d+) desired=1 t=(\d+)',line)
        if match and match[1]==state:
            chosen.append(dict(state=state,generation=int(match[2]),device_us=int(match[3]),host_s=host_s))
            try:state=next(wanted)
            except StopIteration:break
    return dict(passed=len(chosen)==3 and all(b['device_us']>a['device_us'] for a,b in zip(chosen,chosen[1:])),states=chosen)


def completed_screens(lines):
    from echoear_touch_proof import phase_sequence
    rows=[]
    for _,_,line in lines:
        if line.startswith('DISPLAY_DEMO_RENDERED '):rows.append(json.loads(line.split(' ',1)[1]))
    return dict(available=True,**phase_sequence(rows))


def restart_and_recover(board,process,start_bot,turn,*,initial_generation,group=False,screen_proof=completed_screens,record=None):
    """start_bot(remaining_seconds) returns the new owned bot handle.

    The declared loss/recovery window begins immediately before SIGTERM and
    ends at the first fresh Ready, at explicit failure cleanup, or after180s.
    Process exit and HTTP-ready restart have a separate <=30s requirement.
    """
    record={} if record is None else record
    cursor=board.cursor;record.update(passed=False,cursor=cursor)
    record['window']=board.display_monitor.begin_bot_down('owned-bot-restart',180)
    try:
        record['stop']={};stop_owned(process,group=group,record=record['stop'])
        remaining=30-(time.monotonic()-record['stop']['requested_at'])
        if remaining<=0:raise TimeoutError('Bot restart budget exhausted before startup')
        new_bot=start_bot(remaining)
        record.update(restarted_at=time.monotonic())
        record['restart_gap_s']=record['restarted_at']-record['stop']['requested_at']
        if record['restart_gap_s']>30:raise TimeoutError('Bot was not HTTP-ready within30s of SIGTERM')
        remaining=record['window']['deadline_host_s']-time.monotonic()
        record['ready']=board.wait_for(r'STATE Ready gen=\d+ desired=1 t=\d+',remaining,since=cursor)
        board.display_monitor.end_bot_down('Ready observed')
        lines=board.lines_since(cursor)
        record['lifecycle']=lifecycle_sequence(lines)
        record['screens']=screen_proof(lines) if screen_proof else dict(available=False,passed=False,reason='ELF predates status-screen implementation')
        screen_deadline=time.monotonic()+5
        while screen_proof and not record['screens']['passed'] and time.monotonic()<screen_deadline:
            time.sleep(.05);record['screens']=screen_proof(board.lines_since(cursor))
        record['turn']=turn(new_bot)
        board.lines_since(cursor) # surface every other monitor/reader error
        record['monitor']=board.display_monitor.result()
        states=record['lifecycle']['states']
        record['checks']=dict(restarted_within_30s=record['restart_gap_s']<=30,
            auto_reconnect=record['lifecycle']['passed'],screens=record['screens']['passed'],
            fresh_generation=len(states)==3 and states[0]['generation']>initial_generation and
                states[1]['generation']>states[0]['generation'] and states[2]['generation']==states[1]['generation'],
            turn=record['turn']['passed'],monitor=record['monitor']['passed'])
        record['recovery_without_screens_passed']=all(v for k,v in record['checks'].items() if k!='screens')
        record['passed']=all(record['checks'].values())
        return record
    finally:
        board.display_monitor.end_bot_down('restart arm cleanup')
        record['monitor']=board.display_monitor.result()
        if not record['monitor']['passed']:
            record['passed']=False;record['recovery_without_screens_passed']=False
            if 'checks' in record:record['checks']['monitor']=False


class DemoBot:
    """Demo runner with bounded startup; cleanup only targets its owned group."""
    def __init__(self,out):self.out=Path(out);self.proc=None;self.reader=None
    def start(self,timeout=60):
        from demo_session import copy_bot_output
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1',7861))==0:raise RuntimeError('Port7861 already owned')
        self.out.mkdir(parents=True,exist_ok=True)
        env=dict(os.environ,SERVER_PORT='7861',PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',PYTHONUNBUFFERED='1',LOGURU_COLORIZE='NO')
        self.proc=subprocess.Popen([str(ROOT/'server/.venv/bin/python'),str(ROOT/'server/demo_bot.py'),'--host','0.0.0.0','--port','7861'],
            cwd=ROOT/'server',env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
        self.reader=threading.Thread(target=copy_bot_output,args=(self.proc.stdout,self.out),daemon=True);self.reader.start()
        deadline=time.monotonic()+timeout
        try:
            while time.monotonic()<deadline:
                if self.proc.poll() is not None:raise RuntimeError('Owned demo bot exited during startup')
                try:
                    with urllib.request.urlopen('http://127.0.0.1:7861/',timeout=min(.5,max(.01,deadline-time.monotonic()))) as response:
                        if response.status<500:return self
                except OSError:pass
                time.sleep(.05)
            raise TimeoutError('Owned demo bot startup exceeded budget')
        except BaseException:self.close();raise
    def close(self):
        if self.proc:
            if self.proc.poll() is None:stop_owned(self.proc,group=True)
            if self.reader:self.reader.join(timeout=3)
            self.proc.stdout.close();self.proc=None
    def __enter__(self):return self.start()
    def __exit__(self,*_):self.close()
