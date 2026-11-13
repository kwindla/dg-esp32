#!/usr/bin/env python3
"""Ordered 8e0 bench campaign. Own serial and only the offline bot on 7861.

All addresses are required runtime arguments; no bench addresses in firmware.
Spark uses its existing SSH host identity, /tmp files, and SO_BINDTODEVICE.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import shlex
from pathlib import Path
import subprocess
import threading
import time
import traceback

from esp_serial import EspSerial, ROOT
import netprobe


def dump_json(path, value):
    Path(path).write_text(json.dumps(value,indent=2)+'\n')


def probe_stats(esp):
    cursor=esp.cursor
    esp.command('netprobe stats',timeout=10)
    line=esp.wait_for(r'^NETPROBE ',10,since=cursor)
    return json.loads(line.split(' ',1)[1])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wifi-host',required=True);p.add_argument('--ethernet-host',required=True)
    p.add_argument('--spark',default='spark');p.add_argument('--spark-ip',required=True)
    p.add_argument('--spark-host-key',required=True);p.add_argument('--spark-interface',default='wlP9s9')
    p.add_argument('--gateway',required=True);p.add_argument('--duration',type=float,default=610)
    p.add_argument('--short-duration',type=float,default=60);p.add_argument('--port',default='/dev/ttyACM0')
    p.add_argument('--out',type=Path,required=True);p.add_argument('--only',choices=['baseline','large','webrtc-wifi','webrtc-ethernet','repeat','occupancy'])
    p.add_argument('--from-arm',choices=['baseline','large','webrtc-wifi','webrtc-ethernet','repeat','occupancy'],default='baseline')
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o',f'HostName={a.spark_ip}','-o',f'HostKeyAlias={a.spark_host_key}',a.spark]
    # Pipe script to /tmp without installs or SSH configuration edits.
    remote_script=f'/tmp/dg-netprobe-{os.getpid()}.py'
    subprocess.run(ssh+['cat > '+shlex.quote(remote_script)],input=(ROOT/'hil/netprobe.py').read_bytes(),check=True,timeout=20)
    specs=[f'esp-0,{a.wifi_host},47100',f'esp-1,{a.ethernet_host},47101',
           f'spark-0,{a.wifi_host},47102',f'spark-1,{a.ethernet_host},47103']
    arms=[('baseline',130,None),('large',1200,None),('webrtc-wifi',130,'wlp8s0'),
          ('webrtc-ethernet',130,'enp7s0'),('repeat',130,None),('occupancy',130,None)]
    bot=None
    with EspSerial(a.port,log_path=a.out/'serial.log') as esp:
        cursor=esp.cursor;esp.reset(timeout=15)
        esp.wait_for('READY transport-console',60,since=cursor)
        esp.command('disconnect',timeout=45);esp.command('netprobe stop');esp.command('audio 0');esp.command('quiet 1')
        remaining=False
        for name,size,interface in arms:
            if name==a.from_arm:remaining=True
            if not remaining:continue
            if a.only and name!=a.only:
                continue
            directory=a.out/name;directory.mkdir(parents=True,exist_ok=False)
            duration=a.short_duration if name=='occupancy' else a.duration
            metadata=dict(name=name,bytes=size,duration=duration,voice_uplink_enabled=bool(interface),started_utc=datetime.now(timezone.utc).isoformat(),
                board_moved=False,network_changed=False,wired_option='enp7s0; Eero NAT requires ESP-initiated UDP mapping',
                host_wifi_band_note='Record iw link actual band; do not assume boule uses 2.4 GHz',spark_interface=a.spark_interface)
            dump_json(directory/'metadata.json',metadata)
            spark=None;ping=None;botlog=None;sparklog=None;stun=None;stunlog=None;stop_poll=None;thread=None;trace_log=None
            try:
                dump_json(directory/'host-before.json',netprobe.wifi_snapshot('wlp8s0'))
                spark_before=subprocess.run(ssh+[shlex.join(['iw','dev',a.spark_interface,'link'])+'; '+shlex.join(['iw','dev',a.spark_interface,'station','dump'])],capture_output=True,text=True,timeout=20)
                (directory/'spark-before.txt').write_text(spark_before.stdout+spark_before.stderr)
                if name=='occupancy':
                    done=subprocess.run(['iw','dev','wlp8s0','scan'],capture_output=True,text=True,timeout=25)
                    (directory/'scan.txt').write_text(f'returncode={done.returncode}\n'+done.stdout+done.stderr)
                if interface:
                    # Readiness must belong to our bot, never another bench
                    # or acceptance-test process already listening on 7861.
                    import socket
                    with socket.socket() as check:
                        if check.connect_ex(('127.0.0.1',7861))==0:
                            raise RuntimeError('Port 7861 occupied before campaign bot launch')
                    ice_args=[]
                    if interface=='enp7s0':
                        stunlog=(directory/'stun-command.log').open('w')
                        stun=subprocess.Popen([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/netprobe.py'),
                            '--stun-bind',a.ethernet_host,'--duration',str(duration+180),'--out',str(directory/'stun.jsonl')],
                            stdout=stunlog,stderr=subprocess.STDOUT)
                        time.sleep(.2)
                        if stun.poll() is not None:raise RuntimeError('LAN STUN responder failed')
                        ice_args=['--ice-servers','stun:'+a.ethernet_host+':3478']
                    botlog=(directory/'bot.log').open('w')
                    env={**os.environ,'BOT_MODE':'offline-test','BOT_ICE_INTERFACE':interface,'BOT_ICE_REMOTE_NETWORK_ONLY':'1' if interface=='enp7s0' else '0','SERVER_PORT':'7861',
                         'BOT_ICE_DEBUG_PATH':str(directory.resolve()/'ice-debug.log'),'RTP_EVIDENCE_DIR':str(directory.resolve()/'rtp'),'RTP_EVIDENCE_MAX_PACKETS':'100000',
                         'EVENTS_PATH':str(directory.resolve()/'events.jsonl'),'OFFLINE_SKIP_GREETING':'1','OFFLINE_BENCH_CONTINUOUS_AUDIO':'1','OFFLINE_IDLE_TIMEOUT_SECS':'1800'}
                    (directory/'rtp').mkdir()
                    bot=subprocess.Popen([str(ROOT/'server/.venv/bin/python'),'bot.py','-t','webrtc','--host','0.0.0.0','--port','7861']+ice_args,cwd=ROOT/'server',env=env,stdout=botlog,stderr=subprocess.STDOUT)
                    import urllib.request
                    for attempt in range(90):
                        if bot.poll() is not None:
                            raise RuntimeError('Offline bot exited; see bot.log')
                        try:
                            urllib.request.urlopen('http://127.0.0.1:7861/',timeout=1)
                            break
                        except Exception:
                            time.sleep(.5)
                    else:
                        raise RuntimeError('Offline bot readiness timeout')
                    address=a.wifi_host if interface=='wlp8s0' else a.ethernet_host
                    esp.command('server-url http://'+address+':7861')
                    esp.command('peer-option arrival-trace 1');esp.command('audio 1');esp.command('connect',timeout=95)
                    esp.command('play-long '+str(min(30,int(duration//60)+1)),timeout=20)
                    esp.command('stats',timeout=15)
                before=probe_stats(esp);dump_json(directory/'device-before.json',before)
                cursor=esp.cursor;esp.command('wifi-stats',timeout=10)
                (directory/'wifi-tx-before.log').write_text('\n'.join(line for _,_,line in esp.lines_since(cursor))+'\n')
                pinglog=(directory/'gateway-ping.txt').open('w')
                ping=subprocess.Popen(['ping','-n','-D','-O','-i','0.02','-I','wlp8s0',a.gateway],stdout=pinglog,stderr=subprocess.STDOUT)
                remote_out=f'/tmp/dg-netprobe-{os.getpid()}-{name}.jsonl'
                sparklog=(directory/'spark-command.log').open('w')
                remote=['python3',remote_script,'--client','--interface',a.spark_interface,'--duration',str(duration),
                        '--bytes',str(size),'--out',remote_out,'--endpoint',f'spark-0,{a.wifi_host},47102','--endpoint',f'spark-1,{a.ethernet_host},47103']
                samples=[];trace_log=(directory/'trace-serial.log').open('w');control_errors=[];stop_poll=threading.Event()
                def ready():
                    nonlocal spark
                    esp.command(f'netprobe start {a.wifi_host} 47100 50 {size} 1 {a.ethernet_host} 47101',timeout=10)
                    spark=subprocess.Popen(ssh+[shlex.join(remote)],stdout=sparklog,stderr=subprocess.STDOUT)
                def poll():
                    # Serial commands remain on one worker; retain trace batches
                    # every 10 s so the 8192-record RTP ring never wraps.
                    next_trace=time.monotonic()+10
                    began=time.monotonic();reported_minute=0
                    try:
                        while not stop_poll.wait(1):
                            samples.append(dict(host_ns=time.perf_counter_ns(),stats=probe_stats(esp)))
                            if interface and time.monotonic()>=next_trace:
                                cursor=esp.cursor;esp.command('dump-arrivals',timeout=15)
                                for _,_,line in esp.lines_since(cursor):
                                    trace_log.write(line+'\n')
                                trace_log.flush();next_trace=time.monotonic()+10
                            elapsed=int(time.monotonic()-began)
                            if elapsed//60>reported_minute:
                                print(f'{name}: {elapsed}/{duration:.0f}s',flush=True)
                                reported_minute=elapsed//60
                    except BaseException as exc:
                        control_errors.append(str(exc))
                thread=threading.Thread(target=poll)
                def ready_and_poll():
                    ready();thread.start()
                def finish():
                    stop_poll.set();thread.join(25)
                    if thread.is_alive():
                        raise RuntimeError('Serial poller did not stop')
                    esp.command('netprobe stop')
                    # Keep host sockets open through the reference transmitter's
                    # full lifetime, even if SSH startup was slow on a smoke run.
                    if spark:spark.wait(timeout=30)
                netprobe.run_streams(specs,duration,50,size,directory/'host.jsonl',ready=ready_and_poll,finish=finish)
                stop_poll.set();thread.join(25)
                if thread.is_alive():
                    raise RuntimeError('Serial poller did not stop')
                esp.command('netprobe stop');after=probe_stats(esp)
                dump_json(directory/'device-after.json',after);dump_json(directory/'device-samples.json',samples)
                cursor=esp.cursor;esp.command('wifi-stats',timeout=10)
                (directory/'wifi-tx-after.log').write_text('\n'.join(line for _,_,line in esp.lines_since(cursor))+'\n')
                cursor=esp.cursor;esp.command('netprobe dump',timeout=90)
                # Full serial log, rather than the 20k-line capture cursor, is
                # authoritative. Each probe dump fits within the capture deque.
                lines='\n'.join(line for _,_,line in esp.lines_since(cursor))
                (directory/'probe-dump.log').write_text(lines+'\n')
                device_rows=netprobe.decode_dump(lines)
                if interface:
                    cursor=esp.cursor;esp.command('dump-arrivals',timeout=15)
                    for _,_,line in esp.lines_since(cursor):trace_log.write(line+'\n')
                    esp.command('rtp-stats',timeout=15);esp.command('stats',timeout=15);esp.command('disconnect',timeout=45);esp.command('audio 0')
                trace_log.close()
                if spark.wait(timeout=20)!=0:
                    raise RuntimeError('Spark probe failed; see spark-command.log')
                with (directory/'spark.jsonl').open('wb') as output:
                    subprocess.run(ssh+['cat '+shlex.quote(remote_out)],stdout=output,check=True,timeout=30)
                dump_json(directory/'analysis.json',netprobe.analyze_streams(directory/'host.jsonl',device_rows=device_rows,device_stats=after,reference_path=directory/'spark.jsonl'))
                if control_errors:
                    raise RuntimeError('; '.join(control_errors))
                dump_json(directory/'host-after.json',netprobe.wifi_snapshot('wlp8s0'))
                done=subprocess.run(ssh+[shlex.join(['iw','dev',a.spark_interface,'link'])+'; '+shlex.join(['iw','dev',a.spark_interface,'station','dump'])],capture_output=True,text=True,timeout=20)
                (directory/'spark-after.txt').write_text(done.stdout+done.stderr)
                print(name+': capture complete',flush=True)
            except BaseException:
                (directory/'FAILED.txt').write_text(traceback.format_exc())
                raise
            finally:
                if stop_poll:stop_poll.set()
                if thread and thread.ident:thread.join(25)
                if trace_log and not trace_log.closed:trace_log.close()
                if ping:
                    ping.send_signal(2);ping.wait(timeout=5);pinglog.close()
                if spark and spark.poll() is None:
                    spark.terminate();spark.wait(timeout=10)
                if sparklog:sparklog.close()
                if bot:
                    bot.terminate()
                    try:bot.wait(timeout=15)
                    except subprocess.TimeoutExpired:bot.kill();bot.wait()
                    bot=None
                if botlog:botlog.close()
                if stun:
                    stun.terminate();stun.wait(timeout=5)
                if stunlog:stunlog.close()
                try:
                    esp.command('netprobe stop',timeout=10)
                    if interface:esp.command('disconnect',timeout=45);esp.command('audio 0')
                except Exception:pass

if __name__=='__main__':
    main()
