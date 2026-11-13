"""Owned one-hop Ethernet/STUN bench context, using the qualified 8c path."""
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from esp_serial import ROOT


@contextmanager
def ethernet_bench(out, address='192.168.42.75'):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
        probe.bind((address,3478))
    values=dict(BOT_ICE_INTERFACE='enp7s0',BOT_ICE_REMOTE_NETWORK_ONLY='1',
                BOT_BENCH_STUN_URL='stun:'+address+':3478',BOT_BENCH_SERVER_URL='http://'+address+':7861')
    previous={key:os.environ.get(key) for key in values}
    stream=(out/'stun-command.log').open('w')
    process=subprocess.Popen([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/netprobe.py'),
        '--stun-bind',address,'--duration','10800','--out',str(out/'stun.jsonl')],stdout=stream,stderr=subprocess.STDOUT)
    try:
        time.sleep(.3)
        if process.poll() is not None:raise RuntimeError('Owned STUN responder exited')
        os.environ.update(values)
        yield
        if process.poll() is not None:raise RuntimeError('Owned STUN responder exited during capture')
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        stream.close()
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
