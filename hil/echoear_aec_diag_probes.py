"""Unprivileged ICMP evidence, owned child processes and local deadline guard.

Import and deadline validation do not run any process or query a network.
The coordinator, not this module, authorizes the eventual diagnostic launch.
"""
from datetime import datetime, timedelta
from pathlib import Path
import json
import os
import signal
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

TARGETS={'gateway':'192.168.4.1','board':'192.168.4.50'}
ZONE=ZoneInfo('America/Los_Angeles')


def deadline(now, *, budget_minutes=65, restore_minutes=10):
    """Reserve cleanup before 02:45; reject launches that might cross it.

    Morning begins 04:10 after the declared reassociation window. The budget
    is a prospective maximum, not a retrospective change to a run's schedule.
    """
    if now.tzinfo is None:raise ValueError('Timezone-aware launch time required')
    now=now.astimezone(ZONE)
    morning=now.replace(hour=4,minute=10,second=0,microsecond=0)
    if now>=morning:
        end=(now+timedelta(days=1)).replace(hour=2,minute=45,second=0,microsecond=0)
    else:end=now.replace(hour=2,minute=45,second=0,microsecond=0)
    if budget_minutes<=restore_minutes or now+timedelta(minutes=budget_minutes)>end:
        raise ValueError('Insufficient time to finish and restore by 02:45; wait until morning 04:10')
    return dict(launch_local=now.isoformat(),hard_end_local=end.isoformat(),
                acquisition_end_local=min(now+timedelta(minutes=budget_minutes-restore_minutes),
                                          end-timedelta(minutes=restore_minutes)).isoformat(),
                maximum_hold_minutes=budget_minutes,restore_reserve_minutes=restore_minutes)


class PingPair:
    """Raw ping output plus clock-offset witnesses. Never signals other jobs."""
    def __init__(self, folder):
        self.folder=Path(folder);self.children=[];self.handles=[]
        self.stop_event=threading.Event();self.thread=None

    def anchor(self):
        lo=time.monotonic();wall=time.time();hi=time.monotonic()
        self.clock.write(json.dumps(dict(wall_s=wall,mono_lower_s=lo,mono_upper_s=hi))+'\n')
        self.clock.flush()

    def start(self):
        self.folder.mkdir(exist_ok=False)
        try:
            self.clock=(self.folder/'clock.jsonl').open('x');self.handles.append(self.clock)
            self.anchor()
            for name,address in TARGETS.items():
                output=(self.folder/f'{name}.log').open('x');self.handles.append(output)
                child=subprocess.Popen(['ping','-n','-i','0.2','-D',address],
                                       stdout=output,stderr=subprocess.STDOUT,
                                       env=dict(os.environ,LC_ALL='C'))
                self.children.append(child)
            self.thread=threading.Thread(target=self._clock,daemon=True);self.thread.start()
        except BaseException:
            self.stop();raise
        return self

    def _clock(self):
        while not self.stop_event.wait(1):self.anchor()

    def check(self):
        if len(self.children)!=2 or any(p.poll() is not None for p in self.children):
            raise RuntimeError('Diagnostic ping child exited: retain logs and restore')
        if self.thread is None or not self.thread.is_alive():raise RuntimeError('Ping clock recorder stopped')

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=3)
        for child in self.children:
            if child.poll() is None:child.send_signal(signal.SIGINT)
        for child in self.children:
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill();child.wait(timeout=3)
        if hasattr(self,'clock') and not self.clock.closed:self.anchor()
        for handle in self.handles:handle.close()
