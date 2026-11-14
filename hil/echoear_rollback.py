#!/usr/bin/env python3
"""Prepare/validate rollback-01 host-only, or run only after explicit USER-GO."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import time
from time import time as wall_clock   # loguru's clock; bound at import so monotonic fakes leave it real

ROOT=Path(__file__).resolve().parents[1]
BASE=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9')
# gap 3: one restore-of-record constant (no duplicate literal). CAUTION: rollback-01's PERF-schema and liveness
# expectations (PUBLIC_ENV, raw PERF schema below) were written for 9936da45; a live rollback on the new target needs its
# own reviewed session before it is relied on. Retained rollback-01 evidence is judged against the historical constant.
from echoear_aec_bench import NORMAL,NORMAL_HASH
# The Linux bench keeps its historical lock path; macOS uses the per-user temp dir.
BENCH_LOCK_DIR = ('/tmp/claude-1000' if sys.platform != 'darwin' else f'/tmp/claude-{os.getuid()}')

ESPTOOL='/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python'
LOCK=Path(f'{BENCH_LOCK_DIR}/echoear-bench.lock')
PUBLIC_ENV=dict(ECHOEAR_AEC_FULL_DUPLEX='0',ECHOEAR_LIVENESS_SECONDS='0',
    DEMO_TTS_PROACTIVE_RECONNECT='1',DEMO_TTS_RECONNECT_EACH_GENERATION='0',
    DEMO_TTS_TRIM_LEADING='0',DEMO_LLM='local',DEEPGRAM_TTS_VOICE='flux-kelsey-en',
    DEEPGRAM_TTS_SPEED='1.5',DEEPGRAM_TTS_EXPRESSIVITY='2',AUDIO_OUT_10MS_CHUNKS='4',
    BOT_ICE_INTERFACE='wlp8s0',PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
CHECKS=[('cold','Ask: What is the capital of France? Wait for the entire audible answer.'),
        ('warm-1','Ask: Name two colors. Wait for the entire audible answer.'),
        ('warm-2','Ask: What is two plus two? Wait for the entire audible answer.'),
        ('talk-over','Ask for a short description of the ocean. While the reply is AUDIBLE, say Stop twice. Verify the reply continues without interruption.'),
        ('near-recovery','After playback ends, ask: What is the capital of Japan? Verify the complete audible answer.'),
        ('reconnected','After the runner reconnects, ask: Name one fruit. Verify the complete audible answer.')]
COUNTER_PATHS=('failed_allocations.total','console_output.full','console_output.transport_drops',
               'i2s_underflows','i2s_write_errors','i2s_write_timeouts','pcm_queue_drops')
POLICY=dict(counter_paths=list(COUNTER_PATHS),start_exclusion_minutes=[150,250],stimulus='operator spoken prompts; Jabra unused; no mixer changes',
    final_image='normal',estimated_minutes=[10,15],acquisition_deadline_s=720,
    operator_timeout_s=120,idle_s=10,volume_db=-6,
    incomplete='Any missing/failed witness fails smoke; retain evidence; no automatic retakes',
    cleanup='Owned bot stop, serial close, normal flash/full boot-hash verification; lock retained on cleanup failure')


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):Path(path).write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
def head():return subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()


def image_record():
    manifest=json.loads((NORMAL/'manifest.json').read_text())
    if manifest['elf_sha256']!=NORMAL_HASH or manifest['autostart'] is not False:
        raise ValueError('Wrong rollback image identity/autostart')
    files={}
    for name,h in manifest['sha256'].items():
        if sha(NORMAL/name)!=h:raise ValueError('Rollback bundle changed: '+name)
        files[name]=h
    files['manifest.json']=sha(NORMAL/'manifest.json')
    return dict(path=str(NORMAL),elf_sha256=NORMAL_HASH,firmware_commit=manifest['commit'],files_sha256=files)


def launch_script(declaration,digest,output):
    marker=Path(declaration).parent/'USER-GO'
    return '\n'.join(['#!/usr/bin/env bash','# DRAFT; only the coordinator creates USER-GO after the user go.',
        'set -euo pipefail','readonly marker='+shlex.quote(str(marker)),
        'if [[ ! -f "$marker" || -L "$marker" ]]; then',
        "  printf '%s\\n' 'Rollback draft: USER-GO required' >&2",'  exit 2','fi',
        'cd '+shlex.quote(str(ROOT/'server')),
        'export ESPTOOL_PYTHON='+shlex.quote(ESPTOOL),'export PYTHONPATH=../hil:.','export PYTHONUNBUFFERED=1',
        'exec uv run --frozen --offline --with pyserial --with pyelftools --with idf-component-manager==3.1.2 python ../hil/echoear_rollback.py run '+
        shlex.quote(str(declaration))+' --declaration-sha256 '+shlex.quote(digest)])+'\n'


def stop_pattern(declaration,digest):
    literal=lambda x:re.sub(r'([][(){}.^$*+?|\\])',r'\\\1',str(x))
    return (r'^([^ ]*/)?python([0-9.]+)? [^ ]*echoear_rollback[.]py run '+
            literal(declaration)+' --declaration-sha256 '+literal(digest)+'$')


def validate(declaration,digest=None,require_go=False):
    path=Path(declaration).resolve();p=json.loads(path.read_text())
    if digest is not None and sha(path)!=digest:raise ValueError('Declaration hash mismatch')
    if require_go:
        marker=path.parent/'USER-GO'
        if not marker.is_file() or marker.is_symlink():raise ValueError('USER-GO required before any hardware or service access')
    if p['public_env']!=PUBLIC_ENV or p['policy']!=POLICY or p['checks']!=[dict(id=k,instruction=v) for k,v in CHECKS]:
        raise ValueError('Rollback contract changed')
    if p['image']!=image_record() or p['final_image']!=p['image']:raise ValueError('Image/final-state contract changed')
    if p['bot_commit']!=head() or p['runner_commit']!=head():raise ValueError('Pinned checkout mismatch')
    if sha(ROOT/'server/uv.lock')!=p['uv_lock_sha256']:raise ValueError('uv.lock changed')
    required={str(f.relative_to(ROOT)) for pattern in ('hil/*.py','server/*.py') for f in ROOT.glob(pattern)}
    required.update(['server/uv.lock','server/pyproject.toml','docs/echoear-rollback-01.md'])
    if set(p['code_sha256'])!=required:raise ValueError('Incomplete code manifest')
    for name,h in p['code_sha256'].items():
        if sha(ROOT/name)!=h:raise ValueError('Code changed: '+name)
    output=Path(p['output'])
    if output!=BASE/'rollback-01' or output.exists() or output.is_symlink():raise ValueError('Fresh rollback-01 output required')
    if (path.parent/'launch-rollback-01.sh').read_text()!=launch_script(path,sha(path),output):raise ValueError('Launch script mismatch')
    return p


# Decision 141 (Astra review #2): rollback-01's PUBLIC_ENV sets the removed ECHOEAR_AEC_FULL_DUPLEX, and the mute and
# reconnect judges score the pre-141 bot witnesses (DEMO_DUPLEX line, stt-profile config.full_duplex). A fresh run on
# the post-141 bot would acquire evidence and then score INVALID, so preparing or running one is refused before any
# bench access until these judges are revised. Retained evidence is still judged by the old checks.
PRE_141_REFUSAL='rollback-01/mute-01 judge the pre-decision-141 bot; revise this harness before a fresh run'


def refuse_pre_141():
    raise RuntimeError(PRE_141_REFUSAL)


def prepare(destination):
    refuse_pre_141()
    destination=Path(destination).absolute();output=BASE/'rollback-01'
    if destination.exists() or destination.is_symlink() or output.exists() or output.is_symlink():raise FileExistsError('Fresh draft and absent output required')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True):raise ValueError('Commit clean worktree before preparing')
    image=image_record();files={str(f.relative_to(ROOT)):sha(f) for pat in ('hil/*.py','server/*.py') for f in ROOT.glob(pat)}
    for name in ('server/uv.lock','server/pyproject.toml','docs/echoear-rollback-01.md'):files[name]=sha(ROOT/name)
    p=dict(schema='rollback-01-draft-v1',status='draft-not-authorized',qualification=False,output=str(output),
        image=image,final_image=image,bot_commit=head(),runner_commit=head(),uv_lock_sha256=sha(ROOT/'server/uv.lock'),
        public_env=PUBLIC_ENV,policy=POLICY,checks=[dict(id=k,instruction=v) for k,v in CHECKS],code_sha256=files,
        ordered_steps=['USER-GO/hash/code/image validation; operator/quiet-room/campaign handoff confirmation',
          'nonblocking bench flock; MAC identity; flash previous half-duplex bundle; full boot hash',
          'owned pinned bot on free port 7861; override-0 policy line; capture logs and PERF',
          'cold + two warm turns; talk-over no-interruption; near-only recovery',
          '10 seconds connected idle; disconnect/reconnect; another audible reply',
          'witness analysis; owned bot stop; serial close; final normal reflash/full boot hash; release lock'],
        preconditions=['Campaign finished with normal restore/hash, mixer equality and released lock; no other bench owner',
          'User go plus quiet room and an attending operator; no hold across 03:00-04:10 local',
          'Port 7861 free; credentials and local LLM available without logging secrets'],
        claim='Operator-assisted functional rollback smoke only; source-derived compatibility requires this future live witness')
    destination.mkdir(parents=True);declaration=destination/'declaration.json';write(declaration,p)
    launch=destination/'launch-rollback-01.sh';launch.write_text(launch_script(declaration,sha(declaration),output));launch.chmod(0o755)
    (destination/'STOP.txt').write_text('pgrep -af '+shlex.quote(stop_pattern(declaration,sha(declaration)))+'\nNOT EXECUTED. Identify the Python echoear_rollback.py run child with this exact declaration/digest, not uv. Send SIGTERM to that child; cleanup runs before releasing flock. Never SIGKILL or force-unlock. On cleanup failure the runner retains flock and ignores TERM/INT; after investigation send SIGUSR1 to the same child to retry cleanup. No automatic acquisition retry.\n')
    validate(declaration);subprocess.run(['bash','-n',str(launch)],check=True)
    write(destination/'validation.json',dict(passed=True,hardware_access=False,launch_executed=False,user_go_present=False,declaration_sha256=sha(declaration)))
    print(sha(declaration))


def child_environment(public,out):
    prefixes=('ECHOEAR_','DEMO_','RTP_','BOT_','DEEPGRAM_TTS_','AUDIO_OUT_','PIPECAT_','RECORD_','EVENTS_','TTS_REFERENCE_')
    env={k:v for k,v in os.environ.items() if not k.startswith(prefixes) and k!='DG_BENCH_LOCK_FD'}
    env.update(public,ECHOEAR_AEC_SESSION_DIR=str(out/'sessions'))
    return env


def port_free():
    with socket.socket() as s:return s.connect_ex(('127.0.0.1',7861))!=0


def operator_note(instruction,timeout=120):
    print(instruction+'\nType PASS followed by a space and at least five characters describing what you observed, or FAIL with a reason: ',flush=True)
    if not select.select([sys.stdin],[],[],timeout)[0]:raise TimeoutError('Operator witness timeout')
    note=sys.stdin.readline().strip()
    if not note.startswith('PASS ') or len(note)<10:raise RuntimeError('Missing/failed operator witness: '+note)
    return note


def events(out):
    return [json.loads(line) for f in (out/'sessions').glob('session-*/events.jsonl') for line in f.read_text().splitlines()]


def turn_peers(rows):
    """Attribute reply identity only to its user/assistant/transport witnesses."""
    groups={kind:[e for e in rows if e.get('kind')==kind and
                 (kind=='tts-transport-write' or e.get('text'))] for kind in
            ('aggregator-user-turn-stopped','aggregator-assistant-turn-stopped','tts-transport-write')}
    ids={kind:sorted({e['pc_id'] for e in group if e.get('pc_id')}) for kind,group in groups.items()}
    peers=sorted({pc for values in ids.values() for pc in values})
    complete=all(group and all(e.get('pc_id') for e in group) for group in groups.values())
    return dict(passed=bool(complete and len(peers)==1),pc_ids=peers,by_kind=ids)


def per_turn_witnesses(rows,lines,start_ns,end_ns):
    """Split at actual user starts; a later reply cannot rescue an earlier turn."""
    rows=sorted(rows,key=lambda e:e['monotonic_ns'])
    users=[e for e in rows if e.get('kind')=='aggregator-user-turn-stopped' and e.get('text')]
    boundaries=[start_ns]
    for previous,current in zip(users,users[1:]):
        starts=[e['monotonic_ns'] for e in rows if e.get('kind')=='aggregator-user-turn-started'
                and previous['monotonic_ns']<e['monotonic_ns']<=current['monotonic_ns']]
        boundaries.append(min(starts) if starts else current['monotonic_ns'])
    boundaries.append(end_ns+1)
    result=[]
    for index,user in enumerate(users):
        lo,hi=boundaries[index:index+2]
        own=[e for e in rows if lo<=e['monotonic_ns']<hi]
        serial=[line for line in lines if lo<=line[1]*1e9<hi]
        value=turn_witness(own,serial)
        writes=[e for e in own if e.get('kind')=='tts-transport-write']
        last=max((e['monotonic_ns'] for e in writes),default=end_ns+1)
        peer=user.get('pc_id')
        assistants=[e for e in own if e.get('kind')=='aggregator-assistant-turn-stopped' and e.get('text')]
        stops=[e for e in own if e.get('kind')=='server-frame' and e.get('boundary')=='tts-written'
               and e.get('frame')=='BotStoppedSpeakingFrame' and e.get('pc_id')==peer and e['monotonic_ns']>=last]
        contexts={e.get('context_id') for e in writes}
        starts=[e for e in own if e.get('kind')=='server-frame' and e.get('boundary')=='tts-written'
                and e.get('frame')=='TTSStartedFrame' and e.get('pc_id')==peer]
        ends=[e for e in own if e.get('kind')=='server-frame' and e.get('boundary')=='tts-written'
              and e.get('frame')=='TTSStoppedFrame' and e.get('pc_id')==peer]
        context_complete=bool(contexts and None not in contexts
            and contexts=={e.get('context_id') for e in starts}=={e.get('context_id') for e in ends}
            and all(any(s['context_id']==c and s['monotonic_ns']<=min(e['monotonic_ns'] for e in writes if e.get('context_id')==c) for s in starts)
                and any(s['context_id']==c and s['monotonic_ns']>=max(e['monotonic_ns'] for e in writes if e.get('context_id')==c) for s in ends) for c in contexts))
        complete=bool(writes and not any(e.get('drop') for e in writes) and len(assistants)==1
            and stops and assistants[0]['monotonic_ns']>=max(e['monotonic_ns'] for e in stops)
            and context_complete)
        value.update(index=index+1,start_ns=lo,end_ns=hi-1,context_ids=sorted(c for c in contexts if c),
                     complete_reply=complete,transport_writes=len(writes),
                     playback_end_ns=max((e['monotonic_ns'] for e in stops),default=None))
        first=min((e['monotonic_ns'] for e in writes),default=end_ns+1)
        stop=max((e['monotonic_ns'] for e in stops),default=end_ns)
        value['interruptions']=len({e.get('frame_id',e['monotonic_ns']) for e in own
            if e.get('frame')=='InterruptionFrame' and first<=e['monotonic_ns']<=stop})
        value['passed'] &= complete and len(value['user_transcripts'])==1 and not value['interruptions']
        result.append(value)
    return result


def reconnect_witness(results,device_lines,before,after):
    prior={pc for turn in results[:-1] for pc in turn['pc_ids']}
    current=set(results[-1]['pc_ids']) if results else set()
    ready=[]
    for line in device_lines:
        m=re.search(r'\bSTATE Ready gen=(\d+).*\bt=(\d+)',line)
        if m:ready.append(dict(generation=int(m[1]),device_us=int(m[2])))
    old=[r for r in ready if r['device_us']<=before.get('device_us',-1)]
    new=[r for r in ready if before.get('device_us',-1)<r['device_us']<=after.get('device_us',-1)]
    generations=bool(old and len(new)==1 and old[-1]['generation']==before.get('generation')
        and new[0]['generation']==after.get('generation')
        and new[0]['generation']>max(r['generation'] for r in old))
    peers=bool(prior and len(current)==1 and not current.intersection(prior)
        and all(r.get('peer_attribution',{}).get('passed') for r in results)
        and before.get('pc_id') in prior and after.get('pc_id') in current)
    return dict(passed=peers and generations,peer_changed=peers,new_board_generation=generations,
                earlier_pc_ids=sorted(prior),reconnected_pc_ids=sorted(current),ready_states=ready,
                before_generation=before.get('generation'),after_generation=after.get('generation'))


def final_audit(log,device_lines,epochs):
    policy=log.count('DEMO_DUPLEX aec_full_duplex=False user_mute=True source=override-0')
    boot=[line for line in device_lines if 'elf_sha256=' in line]
    fatal=[line for line in device_lines if re.search(r'Guru Meditation|panic.ed|abort\(\)|CORRUPT HEAP|Stack canary',line)]
    checks=dict(half_duplex_policy=policy>=2,liveness_alarm_absent='LIVENESS alarm' not in log,
                one_boot=len(boot)==1,device_fatal_absent=not fatal,counters=bool(epochs) and all(e['passed'] for e in epochs))
    return dict(passed=all(checks.values()),checks=checks,policy_lines=policy,boot_lines=boot,fatal_lines=fatal,
                counters=dict(passed=checks['counters'],epochs=epochs))


def turn_witness(rows,lines,talk_over=False):
    user=[e for e in rows if e.get('kind')=='aggregator-user-turn-stopped' and e.get('text')]
    assistant=[e for e in rows if e.get('kind')=='aggregator-assistant-turn-stopped' and e.get('text')]
    writes=[e for e in rows if e.get('kind')=='tts-transport-write' and e.get('non_silent') and not e.get('drop')]
    peers=turn_peers(rows)
    stopped=[e for e in rows if e.get('kind')=='server-frame' and e.get('frame')=='BotStoppedSpeakingFrame' and e.get('pc_id') in peers['pc_ids']]
    first_write=min((e['monotonic_ns'] for e in writes),default=float('inf'))
    playback_end=min((e['monotonic_ns'] for e in stopped if e['monotonic_ns']>first_write),default=float('inf'))
    interrupted=[e for e in rows if e.get('frame')=='InterruptionFrame' and first_write<=e['monotonic_ns']<=playback_end]
    gate=[]
    for _,_,line in lines:
        m=re.search(r'MIC_GATE t=(\d+) closed=(\d+) pre=(\S+) post=(\S+)',line)
        if m:gate.append(dict(t=int(m[1]),closed=int(m[2]),post=float(m[4])))
    closed=[g for g in gate if g['closed']]
    reopened=bool(closed) and any(not g['closed'] and g['t']>closed[-1]['t'] for g in gate)
    # User notes establish audible overlap; diagnostic gate logs corroborate mute/reopen.
    passed=bool(user and assistant and writes and playback_end<float('inf') and closed and reopened)
    passed=passed and peers['passed'] and all(g['post']<=-119.9 for g in closed) and not interrupted
    passed=passed and not any(e.get('kind') in ('pipeline-error','gap') or e.get('frame') in ('ErrorFrame','FatalErrorFrame') for e in rows)
    if talk_over:passed=passed and len(user)==1
    return dict(passed=bool(passed),user_transcripts=[e['text'] for e in user],assistant_transcripts=[e['text'] for e in assistant],
        non_silent_writes=len(writes),completed_playback=playback_end<float('inf'),interruptions=len(interrupted),
        gate_closed_samples=len(closed),gate_reopened=reopened,pc_ids=peers['pc_ids'],peer_attribution=peers)


class Live:
    """Hardware methods are constructed only after runner authorization validation."""
    def __init__(self,p,out):self.p=p;self.out=out;self.board=None;self.bot=None;self.bot_log=None;self.lock=None;self.retry=False;self.touched=False
    def acquire(self):
        LOCK.parent.mkdir(parents=True,exist_ok=True);lock=LOCK.open('a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:lock.close();raise
        self.lock=lock;self.old_fd=os.environ.get('DG_BENCH_LOCK_FD');os.environ['DG_BENCH_LOCK_FD']=str(lock.fileno())
    def release(self):
        if self.old_fd is None:os.environ.pop('DG_BENCH_LOCK_FD',None)
        else:os.environ['DG_BENCH_LOCK_FD']=self.old_fd
        self.lock.close();self.lock=None
    def install(self,label):
        from echoear_aec_bench import verify_bundle,flash,boot
        verify_bundle(NORMAL,NORMAL_HASH);flash(NORMAL,NORMAL_HASH,self.out,label)
        self.board=boot(self.out,label,NORMAL_HASH)
    def setup(self):
        from step9a_echoear import verify
        verify(self.out);self.touched=True;self.install('rollback')
        for text in ('demo hold','audio 0','quiet 0','gate auto','playback-volume -6'):self.command(text)
        if not port_free():raise RuntimeError('Port 7861 occupied; never stop its owner')
        self.bot_log=(self.out/'bot.log').open('w')
        args=['uv','run','--frozen','--offline','python','demo_bot.py','--host','0.0.0.0','--port','7861']
        self.bot=subprocess.Popen(args,cwd=ROOT/'server',env=child_environment(self.p['public_env'],self.out),
            stdout=self.bot_log,stderr=subprocess.STDOUT,start_new_session=True)
        write(self.out/'owned-bot.json',dict(pid=self.bot.pid,pgid=self.bot.pid,argv=args,commit=self.p['bot_commit']))
        deadline=time.monotonic()+60
        while True:
            self.alive()
            if not port_free() and 'Uvicorn running on' in (self.out/'bot.log').read_text():break
            if time.monotonic()>deadline:raise TimeoutError('Owned bot startup')
            time.sleep(.2)
        from step9a_support import host_address
        self.command('server-url http://'+host_address()+':7861');self.connect()
        return dict(boot_elf_sha256=NORMAL_HASH,public_env=self.p['public_env'],bot_pid=self.bot.pid)
    def command(self,text,timeout=40):
        from echoear_aec_resource_trial import command
        return command(self.board,text,timeout)
    def snapshot(self,label):
        from echoear_aec_resource_trial import snapshot
        return snapshot(self.board,self.out,label)
    def connect(self):
        cursor,_=self.command('connect',90);self.board.wait_for('STATE Ready',20,since=cursor);self.command('audio 1')
        self.snapshot('connected-'+str(time.monotonic_ns()))
    def alive(self):
        if self.bot is None or self.bot.poll() is not None:raise RuntimeError('Owned bot exited')
    def smoke(self):
        results=[];before=self.snapshot('before-smoke');counter_epochs=[]
        for name,instruction in CHECKS:
            if name=='reconnected':
                start=time.monotonic_ns();cursor=self.board.cursor;time.sleep(POLICY['idle_s']);self.alive()
                idle=[line for _,_,line in self.board.lines_since(cursor)]
                if any(re.search(r'STATE (Idle|Disconnected|Failed)',line) for line in idle):raise RuntimeError('Session lost during idle')
                self.snapshot('after-idle');write(self.out/'idle.json',dict(start_ns=start,end_ns=time.monotonic_ns(),seconds=POLICY['idle_s'],passed=True))
                reconnect_before=self.snapshot('before-reconnect')
                counter_epochs.append(counter_check(before,reconnect_before))
                write(self.out/'counter-epochs.json',counter_epochs)
                if not counter_epochs[-1]['passed']:raise RuntimeError('Pre-reconnect legacy counters failed')
                self.command('audio 0');self.command('disconnect');self.connect()
                before=self.snapshot('after-reconnect');reconnect_after=before
            self.alive();start=time.monotonic_ns();cursor=self.board.cursor
            note=operator_note(instruction,POLICY['operator_timeout_s']);time.sleep(.3);end=time.monotonic_ns()
            rows=[e for e in events(self.out) if start<=e['monotonic_ns']<=end]
            lines=self.board.lines_since(cursor);w=turn_witness(rows,lines,name=='talk-over')
            individual=per_turn_witnesses(rows,lines,start,end)
            w['passed'] &= bool(individual) and all(t['passed'] for t in individual)
            w.update(id=name,operator_note=note,start_ns=start,end_ns=end,individual_turns=individual)
            results.append(w);write(self.out/'turns.json',results);write(self.out/(name+'-serial.json'),lines)
            if not w['passed']:raise RuntimeError('Incomplete/failed turn witness: '+name)
        after=self.snapshot('after-smoke')
        device_lines=[x for _,_,x in self.board.lines_since(0)]
        reconnect=reconnect_witness(results,device_lines,reconnect_before,reconnect_after)
        write(self.out/'reconnect-check.json',reconnect)
        if not reconnect['passed']:raise RuntimeError('Reconnect peer/generation witness failed')
        counter_epochs.append(counter_check(before,after))
        audit=final_audit((self.out/'bot.log').read_text(),device_lines,counter_epochs)
        write(self.out/'counter-check.json',audit['counters'])
        write(self.out/'final-audit.json',audit)
        if not audit['passed']:raise RuntimeError('Final policy/liveness/boot/counter audit failed')
        return dict(turns=results,reconnect=reconnect,**audit)

    def cleanup(self,attempt):
        errors=[]
        if self.board:
            for command in ('audio 0','disconnect'):
                try:self.command(command,10)
                except Exception:pass # Final reflash/boot is the required restoration proof.
        if self.bot:
            try:
                if self.bot.poll() is None:os.killpg(self.bot.pid,signal.SIGTERM)
                self.bot.wait(timeout=20)
                # Group may outlive uv; wait for every process in our owned group.
                until=time.monotonic()+10
                while True:
                    try:os.killpg(self.bot.pid,0)
                    except ProcessLookupError:break
                    if time.monotonic()>until:raise RuntimeError('Owned bot process group still exists')
                    time.sleep(.1)
                self.bot=None
                self.bot_log.close()
            except Exception as exc:errors.append('owned-bot: '+str(exc))
        if self.board:
            try:self.board.close();self.board=None
            except Exception as exc:errors.append('serial: '+str(exc))
        restored=None
        try:
            if self.board is not None:raise RuntimeError('Prior serial reader did not close')
            if self.touched:
                self.install('restore-'+str(attempt));self.board.close();self.board=None;restored=NORMAL_HASH
        except Exception as exc:errors.append('RESTORE: '+str(exc))
        if not port_free():errors.append('Port 7861 still occupied; never stop an unknown owner')
        return dict(errors=errors,restored_elf_sha256=restored,device_untouched=not self.touched,owned_bot_stopped=self.bot is None,mixer='untouched; operator speech only')
    def wait_recovery(self):
        self.retry=False
        def retry(*_):self.retry=True
        old=signal.signal(signal.SIGUSR1,retry)
        try:
            try:print('CLEANUP FAILED: retaining bench flock. Investigate; send SIGUSR1 to this runner to retry cleanup. Never force-unlock.',flush=True)
            except Exception:pass   # gap 3: a closed output never ends the retained-lock wait
            while not self.retry:time.sleep(1)
        finally:signal.signal(signal.SIGUSR1,old)


def counter_check(before,after):
    # Exact raw PERF schema of the retained 9bc2d42/9936da45 image. AUDIO
    # serial counters may be retained by snapshot(), but are not PERF fields.
    def get(p,path):
        for k in path.split('.'):
            if not isinstance(p,dict) or k not in p:return None
            p=p[k]
        return p
    values={k:[get(before,k),get(after,k)] for k in COUNTER_PATHS}
    return dict(passed=all(type(a) is int and type(b) is int and 0<=a==b for a,b in values.values()),values=values,
        scope='No increase in legacy faults; missing fields fail, no AEC acceptance thresholds imported')


def execute(driver,out):
    """One lock across setup, operator checks and all independent cleanup attempts."""
    result=dict(passed=False,qualification=False,lock_released=False,cleanup_attempts=[])
    driver.acquire()
    try:
        result['setup']=driver.setup();result['smoke']=driver.smoke();result['passed']=True
    except BaseException as exc:result['error']=type(exc).__name__+': '+str(exc)
    finally:
        signal.alarm(0)
        old={s:signal.signal(s,signal.SIG_IGN) for s in (signal.SIGINT,signal.SIGTERM)}
        while True:
            try:cleanup=driver.cleanup(len(result['cleanup_attempts'])+1)
            except BaseException as exc:cleanup=dict(errors=['cleanup exception: '+str(exc)],restored_elf_sha256=None,owned_bot_stopped=False)
            result['cleanup_attempts'].append(cleanup)
            safe=(not cleanup['errors'] and cleanup['owned_bot_stopped'] and
                  (cleanup['restored_elf_sha256']==NORMAL_HASH or cleanup.get('device_untouched') is True))
            result['passed']=result['passed'] and safe
            try:write(out/'result.json',result)
            except Exception as exc:
                result['passed']=False
                print('Result journal failed: '+str(exc),file=sys.stderr,flush=True)
            if safe:break
            # No exit path releases ownership while recovery remains unverified.
            while True:
                try:driver.wait_recovery();break
                except BaseException as exc:
                    print('Recovery wait interrupted; still retaining flock: '+str(exc),file=sys.stderr,flush=True)
                    time.sleep(1)
        driver.release();result['lock_released']=True
        for s,handler in old.items():signal.signal(s,handler)
        write(out/'result.json',result)
    return result


def check_start_window(hour,minute):
    start,end=POLICY['start_exclusion_minutes']
    if start<=hour*60+minute<end:
        raise ValueError('Rollback starts refused 02:30–04:09; cleanup margin before 03:00–04:10 exclusion')


def run(path,digest):
    refuse_pre_141()
    p=validate(path,digest,require_go=True)
    if not sys.stdin.isatty():raise ValueError('Attending operator terminal required')
    operator_note('Confirm campaign handoff (normal hash/mixer/lock released), quiet room, and this declared final normal image. No hold may cross 03:00-04:10.')
    from datetime import datetime
    now=datetime.now();check_start_window(now.hour,now.minute)
    from step9a_echoear import check_esptool
    check_esptool()
    if not port_free():raise RuntimeError('Port 7861 occupied; no hardware touched')
    out=Path(p['output']);out.mkdir();write(out/'declaration.json',p)
    def stop(sig,_):raise RuntimeError('Stop requested by signal '+str(sig))
    old={s:signal.signal(s,stop) for s in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM)}
    signal.alarm(POLICY['acquisition_deadline_s'])
    try:return execute(Live(p,out),out)
    finally:
        signal.alarm(0)
        for s,h in old.items():signal.signal(s,h)


# ---------------------------------------------------------------------------
# Profile mute-01: the merged AEC-off image with the current bot in half duplex
# (decision 109(e), hil/plans/echoear-mute-gate-v1.json). The rollback-01 code
# above is unchanged; this adapter reuses its lock/cleanup/execute machinery.
# ---------------------------------------------------------------------------
MUTE_PLAN_REL='hil/plans/echoear-mute-gate-v1.json'
MUTE_DOC='docs/echoear-mute-gate.md'


def mute_plan():
    import echoear_mute_gate as g
    plan,digest=g.load_plan(ROOT/MUTE_PLAN_REL)
    g.check_idle_budget(plan)
    return plan,digest


def mute_image_record(plan):
    spec=plan['image'];bundle=Path(spec['bundle'])
    manifest=json.loads((bundle/'manifest.json').read_text())
    if (manifest['elf_sha256']!=spec['elf_sha256'] or manifest.get('aec_enabled') is not False
            or manifest.get('autostart') is not False or manifest.get('firmware_commit')!=spec['firmware_commit']):
        raise ValueError('Wrong mute image identity/mode/autostart')
    cfg=manifest.get('public_config',{})
    if (cfg.get('CONFIG_DG_PLAYOUT_TARGET_FRAMES'),cfg.get('CONFIG_DG_PLAYOUT_MAX_TARGET_FRAMES'))!=(
            spec['playout_target_frames'],spec['playout_max_target_frames']) or 'CONFIG_DG_ECHOEAR_AEC' in cfg:
        raise ValueError('Mute image public config differs from the plan')
    files={}
    for name,h in manifest['sha256'].items():
        if sha(bundle/name)!=h:raise ValueError('Mute bundle changed: '+name)
        files[name]=h
    files['manifest.json']=sha(bundle/'manifest.json')
    return dict(path=str(bundle),elf_sha256=spec['elf_sha256'],firmware_commit=spec['firmware_commit'],files_sha256=files)


def mute_fixtures(plan):
    bank=ROOT/'hil/fixtures/agent_conversation';manifest=json.loads((bank/'manifest.json').read_text())
    rows={t['id']:t for t in manifest['turns']};out={}
    for prompt in plan['stimulus']['cycle']:
        row=rows[prompt]
        if sha(bank/row['file'])!=row['sha256']:raise ValueError('Prompt fixture changed: '+prompt)
        if plan['stimulus']['keywords'][prompt]!=row['stt']:raise ValueError('Prompt keywords differ from the bank: '+prompt)
        out[prompt]=dict(file=str(bank/row['file']),sha256=row['sha256'],duration_s=row['duration_s'])
    return out


def mute_code_manifest():
    files={str(f.relative_to(ROOT)):sha(f) for pat in ('hil/*.py','server/*.py') for f in ROOT.glob(pat)}
    for name in ('server/uv.lock','server/pyproject.toml',MUTE_DOC,MUTE_PLAN_REL,
                 'hil/fixtures/agent_conversation/manifest.json'):
        files[name]=sha(ROOT/name)
    return files


def mute_launch_script(declaration,digest):
    marker=Path(declaration).parent/'USER-GO'
    return '\n'.join(['#!/usr/bin/env bash','# DRAFT; only the coordinator creates USER-GO after the user go.',
        'set -euo pipefail','readonly marker='+shlex.quote(str(marker)),
        'if [[ ! -f "$marker" || -L "$marker" ]]; then',
        "  printf '%s\\n' 'Mute gate draft: USER-GO required' >&2",'  exit 2','fi',
        'cd '+shlex.quote(str(ROOT/'server')),
        'export ESPTOOL_PYTHON='+shlex.quote(ESPTOOL),'export PYTHONPATH=../hil:.','export PYTHONUNBUFFERED=1',
        'exec uv run --frozen --offline --with pyserial --with pyelftools --with idf-component-manager==3.1.2 python ../hil/echoear_rollback.py run-mute '+
        shlex.quote(str(declaration))+' --declaration-sha256 '+shlex.quote(digest)])+'\n'


def validate_mute(declaration,digest=None,require_go=False):
    path=Path(declaration).resolve();p=json.loads(path.read_text())
    if digest is not None and sha(path)!=digest:raise ValueError('Declaration hash mismatch')
    if require_go:
        marker=path.parent/'USER-GO'
        if not marker.is_file() or marker.is_symlink():raise ValueError('USER-GO required before any hardware or service access')
    plan,plan_sha=mute_plan()
    if p.get('schema')!='mute-01-draft-v1' or p['plan_sha256']!=plan_sha or p['public_env']!=plan['public_env']:
        raise ValueError('Mute gate contract changed')
    if p['image']!=mute_image_record(plan) or p['final_image']!=image_record():raise ValueError('Image/final-state contract changed')
    if p['fixtures']!=mute_fixtures(plan):raise ValueError('Prompt fixtures changed')
    if p['bot_commit']!=head() or p['runner_commit']!=head():raise ValueError('Pinned checkout mismatch')
    if p['code_sha256']!=mute_code_manifest():raise ValueError('Code changed since preparation')
    output=Path(p['output'])
    if output!=BASE/plan['id'] or output.exists() or output.is_symlink():raise ValueError('Fresh '+plan['id']+' output required')
    if (path.parent/('launch-'+plan['id']+'.sh')).read_text()!=mute_launch_script(path,sha(path)):raise ValueError('Launch script mismatch')
    return p,plan


def prepare_mute(destination):
    refuse_pre_141()
    plan,plan_sha=mute_plan();destination=Path(destination).absolute();output=BASE/plan['id']
    if destination.exists() or destination.is_symlink() or output.exists() or output.is_symlink():raise FileExistsError('Fresh draft and absent output required')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True):raise ValueError('Commit clean worktree before preparing')
    p=dict(schema='mute-01-draft-v1',status='draft-not-authorized',qualification=False,output=str(output),
        plan=MUTE_PLAN_REL,plan_sha256=plan_sha,image=mute_image_record(plan),final_image=image_record(),
        fixtures=mute_fixtures(plan),bot_commit=head(),runner_commit=head(),public_env=plan['public_env'],
        code_sha256=mute_code_manifest(),claim=plan['claim'])
    destination.mkdir(parents=True);declaration=destination/'declaration.json';write(declaration,p)
    launch=destination/('launch-'+plan['id']+'.sh');launch.write_text(mute_launch_script(declaration,sha(declaration)));launch.chmod(0o755)
    (destination/'STOP.txt').write_text('pgrep -af '+shlex.quote(stop_pattern(declaration,sha(declaration)).replace(' run ',' run-mute '))+'\nNOT EXECUTED. Same stop/cleanup rules as rollback-01 (docs/echoear-rollback-01.md).\n')
    validate_mute(declaration);subprocess.run(['bash','-n',str(launch)],check=True)
    write(destination/'validation.json',dict(passed=True,hardware_access=False,launch_executed=False,user_go_present=False,declaration_sha256=sha(declaration)))
    print(sha(declaration))


def mid_reply_trigger(delay,max_age_s=.12):
    """R1: fires `delay` s after the first non-silent write; active = still streaming, not stopped."""
    def check(rows,lines):
        w=[e for e in rows if e.get('kind')=='tts-transport-write' and e.get('non_silent')]
        if not w:return None
        first=min(e['monotonic_ns'] for e in w)/1e9
        now=time.monotonic()
        if now-first<delay:return None
        stopped=any(e.get('frame')=='BotStoppedSpeakingFrame' and e['monotonic_ns']>=first*1e9 for e in rows)
        age=now-max(e['monotonic_ns'] for e in w)/1e9
        return dict(kind='mid-reply',fired=True,fired_s=now,first_write_s=first,last_write_age_s=age,
                    active=bool(not stopped and age<=max_age_s))
    return check


def after_open_trigger(delay):
    """R2: fires `delay` s after the reply's device GATE open."""
    import echoear_mute_gate as g
    def check(rows,lines):
        w=[e for e in rows if e.get('kind')=='tts-transport-write' and e.get('non_silent')]
        o=g.gate_open_after(lines,max(e['monotonic_ns'] for e in w)/1e9) if w else None
        now=time.monotonic()
        return dict(kind='after-gate-open',fired=True,fired_s=now,gate_open_s=o) if o and now-o>=delay else None
    return check


IDLE_RESET_FALLBACK=('TranscriptionFrame','InterimTranscriptionFrame','UserStartedSpeakingFrame')


def pipecat_idle_reset_frames():
    """Frame class names that reset Pipecat's pipeline idle timer: the installed PipelineWorker idle_timeout_frames
    default (StartFrame is handled at connect). If pipecat cannot be imported, the conservative observed subset."""
    try:
        import inspect
        from pipecat.pipeline.worker import PipelineWorker
        return frozenset(c.__name__ for c in inspect.signature(PipelineWorker).parameters['idle_timeout_frames'].default)
    except Exception:
        return frozenset(IDLE_RESET_FALLBACK)


class RawRecording:
    """arecord raw S16 mono 16 kHz with a 50 ms ALSA buffer, owned from spawn.

    The process is registered with `owner` immediately after Popen, before anything that can raise, so
    cleanup can always stop and reap it. stop() is idempotent and records provenance.

    Sample times are bounded without any premise about ALSA timing (decision 111(b)). With D the written
    duration (frames/rate), written sample k satisfies
        t_spawn + k/r  <=  t(k)  <=  t_reaped - D + k/r
    because sample 0 cannot be captured before the process exists (t_spawn is the parent clock just before
    Popen), the last written sample is captured before the child is reaped (t_reaped is the parent clock
    after wait() returned), and lost samples - a silent overrun at abort, or anywhere - only stretch the
    true span between written samples. Two premises remain and are enforced here: the file holds only this
    capture (arecord 1.2.13 opens without O_TRUNC, so an existing target is unlinked before Popen), and the
    written samples are a subsequence of the device's 16 kHz samples (the hw rate in the -v dump must equal
    the requested rate: no plug resampling).
    DRIFT_PPM is a declared clock allowance for this fixed capture setup (Jabra crystal plus CLOCK_MONOTONIC
    slew), not a derived bound: DRIFT_PPM x D is added on each side. Realistic relative drift is far below
    it, and an actual drift beyond it is still contained by the measured startup+stop+reap slack (>=16 ms on
    this bench) up to about 500 ppm + slack/D (~700 ppm at 53 s); the worst mislocation at an implausible
    1000 ppm is under 25 ms, inside the 0.2 s post-prompt gap. Width = t_reaped - t_spawn - D +
    2 x DRIFT_PPM x D; above MAX_WIDTH_S the capture is unhealthy (INCOMPLETE). Measured widths: 0.016-0.033 s
    capture-only (coordinator, 20 Jabra captures, 2026-10-25), 0.08 s in smoke-01 part 2 under bot load.
    Readiness, buffer and period are diagnostic only; logged xruns and other ALSA errors remain unhealthy.
    """
    DRIFT_PPM = 500
    MAX_WIDTH_S = 0.15
    BUFFER_US = 50000
    def __init__(self,path,device,owner,rate=16000,popen=subprocess.Popen,clock=time.monotonic,sleep=time.sleep):
        self.path=Path(path);self.rate=rate;self.clock=clock;self.stopped_by=None;self.exited_early=False
        self.err=self.path.with_suffix('.alsa.log').open('w+')
        # arecord opens its target without O_TRUNC: a stale longer file would keep its tail and
        # overstate D. The capture file must hold this capture only.
        self.replaced_existing=self.path.exists()
        self.path.unlink(missing_ok=True)
        self.popen_s=clock()
        self.process=popen(['arecord','-v','-D',device,'-t','raw','-f','S16_LE','-r',str(rate),'-c','1',
                            '-B',str(self.BUFFER_US),str(path)],stderr=self.err)
        owner.append(self)
        deadline=clock()+5
        while 'buffer_size' not in self.path.with_suffix('.alsa.log').read_text():
            if self.process.poll() is not None or clock()>deadline:
                self.stop();raise RuntimeError('ALSA recording did not become ready')
            sleep(.02)
        self.ready_s=clock()
    def alive(self):return self.process.poll() is None
    def stop(self):
        if self.stopped_by is None:
            self.send_s=self.clock()
            if self.process.poll() is not None:self.exited_early=True;self.stopped_by='exited-early'
            else:
                self.process.send_signal(signal.SIGINT);self.stopped_by='sigint'
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait();self.stopped_by='kill'
            self.reaped_s=self.clock()
            try:self.err.close()
            except Exception:pass
        return self.health()
    @staticmethod
    def hw_seconds(log,field):
        values=[int(x) for x in re.findall(field+r'\s*:\s*(\d+)',log)]
        rates=[int(x) for x in re.findall(r'^\s*rate\s*:\s*(\d+)',log,re.M)]
        pairs=[(v,r) for v,r in zip(values,rates) if r>0]
        return max(v/r for v,r in pairs) if pairs else None
    def health(self):
        log=self.path.with_suffix('.alsa.log').read_text(errors='replace') if self.path.with_suffix('.alsa.log').exists() else ''
        size=self.path.stat().st_size if self.path.exists() else 0
        ready=getattr(self,'ready_s',None);send=getattr(self,'send_s',None);reaped=getattr(self,'reaped_s',None)
        code=self.process.returncode
        aborted='Aborted by signal Interrupt' in log
        errors=[line for line in log.splitlines() if re.search(r'overrun|underrun|xrun|error|suspend|fail',line,re.I)
                and 'Aborted by signal Interrupt' not in line]
        rates=[int(x) for x in re.findall(r'^\s*rate\s*:\s*(\d+)',log,re.M)]
        rate_ok=bool(rates) and all(x==self.rate for x in rates) and 'Rate conversion' not in log
        interval=width=None
        if reaped is not None and size>0:
            d=size/2/self.rate;drift=self.DRIFT_PPM*1e-6*d
            interval=[self.popen_s-drift,reaped-d+drift]
            width=interval[1]-interval[0]
        width_ok=width is not None and 0<=width<=self.MAX_WIDTH_S
        exit_ok=self.stopped_by=='sigint' and (code==0 or (code in (1,-signal.SIGINT) and aborted))
        healthy=bool(exit_ok and not self.exited_early and not errors and width_ok and rate_ok)
        return dict(healthy=healthy,stopped_by=self.stopped_by,returncode=code,aborted_by_sigint=aborted,
                    exited_early=self.exited_early,alsa_errors=errors[:10],bytes=size,
                    popen_s=self.popen_s,reaped_s=reaped,start_interval_s=interval,uncertainty_s=width,width_ok=width_ok,
                    max_width_s=self.MAX_WIDTH_S,drift_ppm=self.DRIFT_PPM,hw_rates=rates,rate_ok=rate_ok,
                    replaced_existing=getattr(self,'replaced_existing',False),
                    diagnostic=dict(ready_s=ready,send_s=send,buffer_s=self.hw_seconds(log,'buffer_size'),
                                    period_s=self.hw_seconds(log,'period_size'),
                                    startup_plus_tail_s=(reaped-self.popen_s-size/2/self.rate) if reaped is not None else None))
    def result(self):
        import numpy as np
        h=self.health()
        data=self.path.read_bytes() if self.path.exists() else b''
        write(self.path.with_suffix('.health.json'),h)
        return dict(samples=np.frombuffer(data[:len(data)//2*2],dtype='<i2'),rate=self.rate,
                    start_interval_s=h['start_interval_s'],healthy=h['healthy'],health=h)


class EventTail:
    """Incremental recorder reader: per-file offsets, complete lines only, audio rows skipped."""
    def __init__(self,out):self.out=out;self.offsets={};self.rows=[];self.partial={}
    def __call__(self):
        for f in sorted((self.out/'sessions').glob('session-*/events.jsonl')):
            with f.open('rb') as h:
                h.seek(self.offsets.get(f,0));data=self.partial.pop(f,b'')+h.read();self.offsets[f]=h.tell()
            *complete,rest=data.split(b'\n')
            if rest:self.partial[f]=rest
            for line in complete:
                if line.strip():
                    e=json.loads(line)
                    if e.get('kind')!='audio':self.rows.append(e)
        return list(self.rows)


class MuteLive(Live):
    """Hardware adapter for profile mute-01. Scoring is in echoear_mute_gate (pure)."""
    def __init__(self,p,plan,out):
        super().__init__(p,out);self.plan=plan;self.epochs=[];self.cells={};self.reconnects=[]
        self.extra=[];self.jabra=None;self.mixer_saved=False;self.last_snapshot=0;self.periodic=False;self.hold_periodic=False
        self.recorders=[];self.prompts_used=0
        self.last_bot_activity=None;self.idle_budget_stops=[]   # cumulative idle accounting (decision 120 should-fix)
        self.read_events=EventTail(out)
    def install_mute(self):
        # The qualified harness's flash/boot path, unchanged. Its declared_boot_gain() requires an
        # AEC gain for any non-normal image; the mute bundle has no MIC3 reference, so for this one
        # verified AEC-off bundle only, the declaration is None (no gain line is expected at boot).
        import echoear_aec_bench as bench
        from unittest import mock
        image=Path(self.plan['image']['bundle']);h=self.plan['image']['elf_sha256']
        manifest=json.loads((image/'manifest.json').read_text())
        if manifest.get('aec_enabled') is not False or 'boot_ref_gain_db' in manifest or manifest.get('elf_sha256')!=h:
            raise ValueError('Mute install requires the declared AEC-off bundle')
        def no_gain(img,expected,expected_gain_db=None):
            if expected!=h or expected_gain_db is not None:raise ValueError('AEC-off declaration applies only to the mute bundle')
            return None
        with mock.patch.object(bench,'declared_boot_gain',no_gain):
            bench.verify_bundle(image,h);bench.flash(image,h,self.out,'mute')
        self.board=bench.boot(self.out,'mute',h)
    install_label='mute'
    device_commands=('demo hold','audio 0','quiet 0','gate auto','playback-volume -6')
    # Candidate installation is install_candidate(); Live.install(label) is ONLY the normal restore-of-record (NORMAL_HASH)
    # installer, which Live.cleanup uses for every restore attempt. Never override install().
    def install_candidate(self):self.install_mute()
    def device_lines(self):
        """Text checks read the persisted serial log, never the 20000-line capture ring."""
        path=self.out/(self.install_label+'-serial.log')
        return path.read_bytes().decode(errors='replace').splitlines() if path.exists() else []
    def display_setup(self):
        """The qualified BenchHold display/character sequence (echoear_aec_campaign.BenchHold.__enter__):
        disp init, PSRAM arena from the init snapshot, preload and bind the demo assets, character on.
        Without it the display renders nothing and display.rendered cannot advance."""
        from step9a_support import request,terminal,read
        self.command('disp init',40);heap=self.snapshot('display-init')['heap']['psram']
        self.command(f"disp arena 491520 {heap['free']} 1287760 {heap['largest']}")
        for i,asset in enumerate(re.findall(r'"([0-9a-f]{64})"',(ROOT/'main/demo_assets.h').read_text())):
            rid=f'cat-{i}';request(self.board,'preload',{'hash':asset},rid)
            if terminal(self.board,rid)['error']!='none':raise RuntimeError('Character preload failed')
            self.command(f'disp asset {i} {asset}')
        self.command('disp character on',30)
        write(self.out/'character-start.json',read(self.board,'character-status','DISPLAY_CHARACTER'))
    def setup(self):
        from step9a_echoear import verify
        from alsa import resolve_jabra,set_volumes
        verify(self.out);self.touched=True;self.install_candidate()
        card,self.jabra=resolve_jabra()
        from echoear_aec_bench import jabra_card_zero
        jabra_card_zero()          # the mixer store/restore below addresses card 0, as BenchHold does
        subprocess.run(['alsactl','-f',str(self.out/'mixer-before.state'),'store','0'],check=True,capture_output=True)
        self.mixer_saved=True;set_volumes(card,self.out/'mixer-set.log',pcm_percent=self.plan['stimulus']['jabra_pcm_percent'])
        for text in self.device_commands:self.command(text)
        self.display_setup()
        if not port_free():raise RuntimeError('Port 7861 occupied; never stop its owner')
        self.bot_log=(self.out/'bot.log').open('w')
        args=['uv','run','--frozen','--offline','python','demo_bot.py','--host','0.0.0.0','--port','7861']
        self.bot=subprocess.Popen(args,cwd=ROOT/'server',env=child_environment(self.p['public_env'],self.out),
            stdout=self.bot_log,stderr=subprocess.STDOUT,start_new_session=True)
        write(self.out/'owned-bot.json',dict(pid=self.bot.pid,pgid=self.bot.pid,argv=args,commit=self.p['bot_commit']))
        deadline=time.monotonic()+60
        while True:
            self.alive()
            if not port_free() and 'Uvicorn running on' in (self.out/'bot.log').read_text():break
            if time.monotonic()>deadline:raise TimeoutError('Owned bot startup')
            time.sleep(.2)
        from step9a_support import host_address
        self.command('server-url http://'+host_address()+':7861')
        return dict(boot_elf_sha256=self.plan['image']['elf_sha256'],public_env=self.p['public_env'],bot_pid=self.bot.pid)
    # -- primitives ---------------------------------------------------------
    def connect_timed(self):
        start=time.monotonic();cursor,_=self.command('connect',90)
        self.board.wait_for('STATE Ready',self.plan['reconnect']['ready_timeout_s']+20,since=cursor)
        ready=[h for _,h,line in self.board.lines_since(cursor) if 'STATE Ready' in line]
        self.command('audio 1');return (ready[0]-start) if ready else None
    def snap(self,label):
        p=self.snapshot(label);self.last_snapshot=time.monotonic()
        if self.epochs:self.epochs[-1]['snapshots'].append(p)
        return p
    def boundary(self,label):
        from echoear_aec_resources import quiescent_admission_snapshot
        sample,samples=quiescent_admission_snapshot(lambda i:self.snapshot(label+'-'+str(i)))
        if self.epochs:self.epochs[-1]['snapshots'].extend(samples)
        self.last_snapshot=time.monotonic();return sample
    def begin_epoch(self,name,volume_db):
        # The observation interval opens BEFORE connect: new-peer rows are later selected by pc_id.
        # pre_connect_wall: the wall clock loguru stamps bot.log with (decision 112 liveness attribution).
        epoch=dict(id=name,volume_db=volume_db,snapshots=[],pre_connect_ns=time.monotonic_ns(),pre_connect_wall=wall_clock())
        self.epochs.append(epoch);epoch['ready_seconds']=self.connect_timed()
        self.last_bot_activity=epoch['pre_connect_ns']/1e9   # StartFrame restarts the idle timer; pre-connect precedes it
        epoch['first']=self.boundary('epoch-'+name+'-start')
    def end_boundary(self):
        e=self.epochs[-1];e['last']=self.boundary('epoch-'+e['id']+'-end')
    def disconnect(self,trigger=None,rows_reader=None):
        """disconnect FIRST (nothing interposed), timestamped at the actual console send; then audio 0.

        The retired session's single terminal PERF line (printed during disconnect) is the epoch's
        terminal counter record; a 'stats' snapshot right after it (no live session: retired transport
        stats + live tasks/heap/display + the lifetime AUDIO line) is its terminal device/resource record.
        For a trigger, lag is measured from the trigger to the send, and R1's activity is re-checked at
        the send from the recorder rows (last non-silent write within active_max_age_s, not stopped)."""
        from echoear_aec_resource_trial import FATAL
        cursor=self.board.cursor
        before_wall=wall_clock();before=time.monotonic();seq=self.board.send('disconnect');sent=time.monotonic();sent_wall=wall_clock()
        line=self.board.wait_for(rf'^(?:OK|ERR) #{seq}(?:\s|$)|{FATAL}',40,since=cursor)
        done=time.monotonic()
        finals=[]
        for _,_,text in self.board.lines_since(cursor):
            if text.startswith('PERF '):
                try:finals.append(json.loads(text[5:]))
                except ValueError:finals.append(None)
        e=self.epochs[-1]
        e['terminal']=finals[0] if len(finals)==1 else None
        e['terminal_count']=len(finals);e['end_ns']=time.monotonic_ns()
        e['disconnect']=dict(send_lower_s=before,sent_s=sent,done_s=done,ok=line.startswith('OK'),
                             send_lower_wall=before_wall,sent_wall=sent_wall)
        if trigger is not None:
            trigger.update(disconnect_sent_s=sent,disconnect_lag_s=sent-trigger['fired_s'])
            if trigger.get('kind')=='mid-reply':
                rows=(rows_reader or self.read_events)()
                start=trigger.get('first_write_s',0)*1e9
                w=[x['monotonic_ns'] for x in rows if x.get('kind')=='tts-transport-write' and x.get('non_silent')
                   and start<=x['monotonic_ns']<=sent*1e9]
                stopped=any(x.get('frame')=='BotStoppedSpeakingFrame' and start<=x['monotonic_ns']<=sent*1e9 for x in rows)
                age=(sent-max(w)/1e9) if w else None
                limit=self.plan['reconnect'].get('active_max_age_s',.12)
                trigger.update(active_at_send=bool(age is not None and age<=limit and not stopped),
                               last_write_age_at_send_s=age)
                trigger['active']=bool(trigger.get('active') and trigger['active_at_send'])
        if not line.startswith('OK'):e['disconnect']['error']=line
        if e['terminal'] is not None:write(self.out/('epoch-'+e['id']+'-terminal.json'),e['terminal'])
        try:e['post']=self.snapshot('epoch-'+e['id']+'-post-disconnect')
        except Exception as exc:e['post']=None;e['post_error']=str(exc)
        self.command('audio 0')
        return e['terminal']
    def tick(self):
        self.alive()
        if self.periodic and not self.hold_periodic and time.monotonic()-self.last_snapshot>=10:
            self.snap('periodic-'+str(time.monotonic_ns()))
    def idle_deadline(self):
        """Latest monotonic time the runner may stay without bot activity (decision 118 budget, cumulative)."""
        last=getattr(self,'last_bot_activity',None)
        if last is None:return None
        return last+self.plan['bot_idle']['max_runner_idle_s']
    def note_bot_activity(self,rows):
        """Decision 121: credit ONLY frames Pipecat's idle timer counts (pipecat_idle_reset_frames(): the installed
        PipelineWorker idle_timeout_frames default), observed on the CURRENT epoch's peer. The recorder logs
        TranscriptionFrame, InterimTranscriptionFrame and UserStartedSpeakingFrame; BotSpeakingFrame and
        UserSpeakingFrame are not logged, so bot speech is never credited from a proxy (writes, Bot started/stopped).
        Only the first observation of each frame (frame_id) is credited, as Pipecat's observer sees only the first
        push. Limitation (Astra r14): recorder timestamps are receipt times on an observer queue independent of
        Pipecat's IdleFrameObserver, so an observer backlog can still stamp a credit slightly later than Pipecat's
        reset; retained same-frame spreads are <1 ms against the 60 s planned margin, but this is not an absolute
        bound. The epoch's StartFrame is credited at pre-connect (before the real StartFrame)."""
        frames=pipecat_idle_reset_frames()
        pc=(self.epochs[-1].get('first') or {}).get('pc_id') if getattr(self,'epochs',None) else None
        # Astra r14: Pipecat's IdleFrameObserver sees only a frame's FIRST push (observe_every_push=False), while the
        # recorder logs the same frame at several taps (server-frame / direct). Credit the first observation per frame.
        first={}
        for e in rows:
            if pc is not None and e.get('pc_id')==pc and e.get('frame') in frames:
                key=(e.get('frame'),e.get('frame_id')) if e.get('frame_id') is not None else (e.get('frame'),'row',id(e))
                first[key]=min(first.get(key,float('inf')),e['monotonic_ns']/1e9)
        if first:self.last_bot_activity=max(getattr(self,'last_bot_activity',None) or 0,max(first.values()))
    def wait_until(self,deadline):
        cap=self.idle_deadline()
        if cap is not None and deadline>cap:
            self.__dict__.setdefault('idle_budget_stops',[]).append(dict(kind='wait-capped',requested_s=deadline,capped_s=cap))
            deadline=cap
        while time.monotonic()<deadline:self.tick();time.sleep(.2)
    def next_prompt(self):
        cycle=self.plan['stimulus']['cycle'];prompt=cycle[self.prompts_used%len(cycle)];return prompt
    def stop_recorders(self):
        errors=[]
        for rec in list(self.recorders):
            try:rec.stop()
            except Exception as exc:errors.append('RECORDER stop: '+str(exc))
            if rec.alive():errors.append('RECORDER: live capture after stop')
            else:self.recorders.remove(rec)
        return errors
    def cell(self,label,trigger_rule=None,prompt=None):
        """Prompt -> reply -> tail. trigger_rule(rows,lines) may fire a reconnect trigger; the
        disconnect is then issued HERE, before the recorder stops and before any snapshot."""
        import echoear_mute_gate as g
        from alsa import play_wav
        prompt=prompt or self.next_prompt();self.prompts_used+=1
        spec=self.plan['echo_cell'];cursor=self.board.cursor
        start_ns=time.monotonic_ns();rec=None;played=None;error=None;attempted=False;trigger=None
        prompt_start=prompt_end=time.monotonic()
        try:
            try:rec=RawRecording(self.out/(label+'-room.raw'),self.jabra,self.recorders)
            except Exception as exc:error='ALSA recording failed: '+str(exc)
            if error is None:
                time.sleep(.6);self.hold_periodic=True;prompt_start=time.monotonic();attempted=True
                try:played=play_wav(self.p['fixtures'][prompt]['file'],self.jabra)
                except Exception as exc:error='jabra playback duration invalid: '+str(exc)
                prompt_end=time.monotonic()
            while attempted:
                self.tick();now=time.monotonic()
                rows=[e for e in self.read_events() if e['monotonic_ns']>=start_ns];lines=self.board.lines_since(cursor)
                writes=[e for e in rows if e.get('kind')=='tts-transport-write' and e.get('non_silent')]
                if writes:self.hold_periodic=False
                if trigger_rule:
                    trigger=trigger_rule(rows,lines)
                    if trigger:
                        self.disconnect(trigger);break
                users=[e for e in rows if e.get('kind')=='aggregator-user-turn-stopped' and e.get('text')]
                if not users and now-prompt_end>spec['user_turn_timeout_s']:break
                if writes:
                    last=max(e['monotonic_ns'] for e in writes)
                    stops=[e['monotonic_ns']/1e9 for e in rows if e.get('frame')=='BotStoppedSpeakingFrame' and e['monotonic_ns']>=last]
                    # AEC mode has no mic gate: its tail runs from the reply's BotStoppedSpeakingFrame.
                    open_s=(min(stops) if stops else None) if g.aec_mode(self.plan) else g.gate_open_after(lines,last/1e9)
                    if open_s and stops and now>=open_s+spec['tail_after_gate_open_s']+.3:break
                if now-prompt_end>spec['reply_timeout_s']:break
                time.sleep(.2)
        finally:
            self.hold_periodic=False
            end_s=time.monotonic()
            if rec is not None:rec.stop()
            leftover=self.stop_recorders()
            if leftover:raise RuntimeError('; '.join(leftover))
        rows=[e for e in self.read_events() if e['monotonic_ns']>=start_ns];lines=self.board.lines_since(cursor)
        self.note_bot_activity(rows)
        write(self.out/(label+'-serial.json'),lines)
        recording=rec.result() if rec is not None else None
        if trigger is not None or trigger_rule is not None:
            result=dict(prompt=prompt,status='REPORT-ONLY',trigger=trigger,detail='reconnect cycle; reply deliberately cut',rows=len(rows))
        else:
            result=g.score_echo_cell(rows,lines,recording,self.plan,prompt,prompt_start,prompt_end,end_s,
                                     stimulus_error=error.split(':')[0] if error else None,retired=g.retired_peers(self.epochs,self.plan['teardown_bracket_max_s']))
        result.update(label=label,jabra_playback=played,recording_health=(recording or {}).get('health'))
        return result
    def block(self,name,required,spacing_s=0,min_epoch_s=0,cold_first=True):
        import echoear_mute_gate as g
        self.cells[name]=[];limit=self.plan['retakes']['per_echo_block'];allowed=set(self.plan['retakes']['allowed_causes'])
        t0=time.monotonic();k=0
        while True:
            cells=self.cells[name]
            if any(c['status']=='FAIL' or c.get('failures') for c in cells):break
            if sum(c['status']=='PASS' for c in cells)>=required:break
            if cold_first and cells and cells[0]['status']!='PASS':break   # cold coverage is not retakeable
            incomplete=[c for c in cells if c['status']=='INCOMPLETE']
            if any(c.get('cause') not in allowed for c in incomplete) or len(incomplete)>limit:break
            # Cumulative idle account (decision 120 should-fix): the next cell may produce no bot activity at all (no
            # user turn within user_turn_timeout_s), so it starts only if the wait plus that worst case stays inside
            # the idle budget counted from the LAST bot activity, not from the previous wait.
            self.note_bot_activity(self.read_events())
            cap=self.idle_deadline();start_at=max(time.monotonic(),t0+spacing_s*k) if spacing_s else time.monotonic()
            if cap is not None and start_at+self.plan['echo_cell']['user_turn_timeout_s']>cap:
                self.idle_budget_stops.append(dict(kind='block-stopped',block=name,cells=len(self.cells[name]),
                                                   idle_s=time.monotonic()-self.last_bot_activity));break
            if spacing_s:self.wait_until(t0+spacing_s*k)
            self.cells[name].append(self.cell(name+'-'+str(k+1)));k+=1
            write(self.out/'cells.json',self.cells)
        # Soak tail only for a complete block (decision 118): after an early stop (failure, cold INCOMPLETE) the
        # verdict is already non-PASS and waiting out min_epoch_s would idle the bot past its idle timeout. The
        # tail is capped by the declared idle budget in any case.
        self.note_bot_activity(self.read_events())
        if min_epoch_s and sum(c['status']=='PASS' for c in self.cells[name])>=required:
            self.wait_until(min(t0+min_epoch_s,time.monotonic()+self.plan['bot_idle']['max_runner_idle_s']))
        return g.block_verdict(self.cells[name],required,limit,self.plan,cold_first=cold_first)
    # -- sequence -------------------------------------------------------------
    def smoke(self):
        import echoear_mute_gate as g
        seq={s['id']:s for s in self.plan['sequence']}
        blocks={};triggers={}
        mid_reply,after_open=mid_reply_trigger,after_open_trigger
        self.begin_epoch('A',-6);self.periodic=True
        blocks['A']=self.block('A',seq['A']['replies'],seq['A']['spacing_s'],seq['A']['min_epoch_s'])
        self.periodic=False
        if blocks['A']['status']=='PASS':
            # R1: quiescent end boundary first, then a fresh prompt; the disconnect is the trigger.
            self.end_boundary()
            r1=self.cell('R1',mid_reply(seq['R1']['disconnect_after_first_write_s']),prompt=seq['R1']['prompt'])
            self.extra.append(r1);triggers['R1']=r1.get('trigger')
            if self.epochs[-1].get('terminal_count') is None:self.disconnect()   # no trigger: still retire, uncredited
            before=self.epochs[-1]
            self.begin_epoch('B',-6);self.reconnects.append(('R1',before))
            self.cells['B']=[self.cell('B-1')]
            self.end_boundary()
            r2=self.cell('R2',after_open(seq['R2']['disconnect_after_gate_open_s']),prompt=seq['R2']['prompt'])
            self.extra.append(r2);triggers['R2']=r2.get('trigger')
            if self.epochs[-1].get('terminal_count') is None:self.disconnect()
            before=self.epochs[-1];self.command('playback-volume 0')
            self.begin_epoch('C',0);self.reconnects.append(('R2',before))
            blocks['C']=self.block('C',seq['C']['replies'])
            if blocks['C']['status']=='PASS':
                self.wait_until(time.monotonic()+seq['R3']['idle_s'])
                self.end_boundary();self.disconnect()
                before=self.epochs[-1]
                self.begin_epoch('D',0);self.reconnects.append(('R3',before))
                self.cells['D']=[self.cell('D-1')]
        # Final generation: quiescent boundary, then retire it so its teardown is in the verdict.
        if 'last' not in self.epochs[-1]:self.end_boundary()
        if self.epochs[-1].get('terminal_count') is None:self.disconnect()
        self.sealed=self.seal_recorder()
        return self.judge(blocks,triggers)
    def seal_recorder(self,timeout_s=10):
        pc=self.epochs[-1].get('first',{}).get('pc_id');deadline=time.monotonic()+timeout_s
        while time.monotonic()<deadline:
            if any(e.get('kind')=='session-end' and e.get('pc_id')==pc for e in self.read_events()):return True
            time.sleep(.2)
        return False
    def judge(self,blocks,triggers):
        import echoear_mute_gate as g
        rows=self.read_events();device=self.device_lines();bot_log=(self.out/'bot.log').read_text()
        snaps=[p for e in self.epochs for p in e['snapshots']]
        parts=dict(mode=g.mode_check(bot_log,device,rows,snaps,self.plan))
        for name in ('A','C'):
            parts['block-'+name]=blocks.get(name,dict(status='INCOMPLETE',reason='not reached'))
        for name in ('B','D'):
            cells=self.cells.get(name)
            parts['reply-'+name]=dict(status=cells[0]['status']) if cells else dict(status='INCOMPLETE',reason='not reached')
        ready=[]
        for line in device:
            m=re.search(r'\bSTATE Ready gen=(\d+).*\bt=(\d+)',line)
            if m:ready.append(dict(generation=int(m[1]),device_us=int(m[2])))
        for index,(cycle,before) in enumerate(self.reconnects):
            epoch=self.epochs[index+1];after=epoch['first']
            prior={p.get('pc_id') for e in self.epochs[:index+1] for p in e['snapshots']}
            seen=[r for r in ready if r['device_us']<=after.get('device_us',-1)]
            new_rows=[e for e in rows if e.get('pc_id')==after.get('pc_id')]
            old_rows=[e for e in rows if e.get('pc_id')==before['first'].get('pc_id')]
            r=g.reconnect_rule(cycle,before['first'],after,seen,prior,rows,epoch['ready_seconds'],self.plan,trigger=triggers.get(cycle))
            # Decision 117: the old peer's session-end is post-disconnect teardown evidence: report-only.
            r['old_session_closed_report_only']=g.old_session_closed(old_rows,new_rows)
            parts['reconnect-'+cycle]=r
        for want in ('R1','R2','R3'):parts.setdefault('reconnect-'+want,dict(status='INCOMPLETE',reason='not reached'))
        for e in self.epochs:
            s=e['snapshots']
            parts['terminal-'+e['id']]=g.terminal_epoch(e['first'],e.get('terminal'),self.plan,e.get('disconnect'))
            parts['terminal-resources-'+e['id']]=g.terminal_resources(e.get('post'),self.plan)
            parts['liveness-'+e['id']]=g.liveness_epoch(e['first'],e.get('last',e['first']))
            parts['counters-'+e['id']]=g.counter_epoch(e['first'],e.get('last',e['first']),self.plan)
            parts['admission-'+e['id']]=g.admission_epoch(e['first'],e.get('last',e['first']))
            parts['playout-'+e['id']]=g.playout_epoch(s,self.plan)
            parts['volume-'+e['id']]=g.volume_epoch(s,e['volume_db'])
        parts['display-A']=g.display_epoch(self.epochs[0]['snapshots']) if self.epochs else dict(status='INCOMPLETE')
        parts['resources']=g.resources(snaps,self.plan)
        parts['recorder-integrity']=g.recorder_integrity(rows,g.retired_peers(self.epochs,self.plan['teardown_bracket_max_s']))
        parts['interrupts']=g.interrupt_absent(rows,device,[e['snapshots']+([e['terminal']] if e.get('terminal') else []) for e in self.epochs],
                                               g.retired_peers(self.epochs,self.plan['teardown_bracket_max_s']))
        parts['liveness-logs']=g.liveness_logs_attributed(bot_log,device,g.liveness_peers(self.epochs),self.plan)
        parts['fatal-boot']=g.fatal_and_boot(device)
        parts['bot-idle-timeout']=g.bot_idle_timeout(bot_log)
        parts['teardown-bracket']=g.teardown_brackets(self.epochs,self.plan['teardown_bracket_max_s'])
        parts['idle-budget']=(dict(status='INCOMPLETE',passed=False,reason='runner idle budget reached; block stopped or wait capped',stops=self.idle_budget_stops)
                              if any(x['kind']=='block-stopped' for x in self.idle_budget_stops) else dict(passed=True,stops=self.idle_budget_stops))
        parts['final-seal']=dict(passed=True) if getattr(self,'sealed',False) else dict(status='INCOMPLETE',reason='final recorder session-end not observed')
        verdict=g.gate_verdict(parts)
        write(self.out/'gate.json',dict(verdict=verdict,parts=parts,cells=self.cells,report_only_cells=self.extra,
              epochs=[{k:v for k,v in e.items() if k not in ('snapshots',)} for e in self.epochs],plan_sha256=self.p['plan_sha256']))
        if verdict['outcome']!='PASS':raise RuntimeError('Mute gate '+verdict['outcome']+': '+json.dumps(verdict['parts']))
        return verdict
    def cleanup(self,attempt):
        # Release is contingent on no live owned capture: a recorder that will not stop is an error.
        recorder_errors=self.stop_recorders()
        result=super().cleanup(attempt)
        result['errors']=recorder_errors+result['errors']
        if self.mixer_saved:
            try:
                subprocess.run(['alsactl','-f',str(self.out/'mixer-before.state'),'restore','0'],check=True,capture_output=True)
                subprocess.run(['alsactl','-f',str(self.out/'mixer-after.state'),'store','0'],check=True,capture_output=True)
                equal=(self.out/'mixer-before.state').read_bytes()==(self.out/'mixer-after.state').read_bytes()
                result['mixer']=dict(restored=True,byte_equal=equal)
                if not equal:result['errors'].append('MIXER: restored state differs')
            except Exception as exc:result['errors'].append('MIXER: '+str(exc))
        else:result['mixer']='not touched'
        return result


class AecReconnectLive(MuteLive):
    """AEC promotion smoke part 2 (decision 110 direction (b)): one reconnect on the AEC bundle, in its
    own hold, reusing the mute gate's reviewed reconnect machinery (trigger recorded mid-reply,
    disconnect sent at the trigger, new peer selected by id from all recorder rows, complete reply in
    the new session, no stale writes, per-generation terminal stats and resources). Mic-gate and
    mute-mode checks are off; acoustic consequences (false turns, extra output) are report-only.
    The bench lock, owned bot, cleanup and normal (NORMAL_HASH) restore are rollback-01's execute()/Live path."""
    install_label='aec'
    device_commands=('demo hold','audio 0','quiet 1','gate auto','aec-ref-gain 15','aec-guard-ms 400',
                     'playback-volume -6','peer-option arrival-trace 0')
    def install_candidate(self):
        import echoear_aec_bench as bench
        image=Path(self.plan['image']['bundle']);h=self.plan['image']['elf_sha256'];gain=self.plan['image']['boot_ref_gain_db']
        manifest=json.loads((image/'manifest.json').read_text())
        if manifest.get('aec_enabled') is not True or manifest.get('boot_ref_gain_db')!=gain or manifest.get('elf_sha256')!=h:
            raise ValueError('AEC reconnect check requires the declared AEC bundle')
        bench.verify_bundle(image,h,gain);bench.flash(image,h,self.out,'aec',gain)
        self.board=bench.boot(self.out,'aec',h)          # verifies the full boot hash and MIC3 gain readback
        self.board.wait_for(r'AEC_GUARD_CONFIG guard_ms=400 afe_delay_samples=1024 rearm_ms=2000',5,since=0)
    def smoke(self):
        seq={x['id']:x for x in self.plan['sequence']}
        triggers={}
        self.begin_epoch('A',seq['A']['volume_db'])
        self.cells['A']=[self.cell('A-1')]
        self.end_boundary()
        r1=self.cell('R1',mid_reply_trigger(seq['R1']['disconnect_after_first_write_s'],self.plan['reconnect']['active_max_age_s']),
                     prompt=seq['R1']['prompt'])
        self.extra.append(r1);triggers['R1']=r1.get('trigger')
        if self.epochs[-1].get('terminal_count') is None:self.disconnect()
        before=self.epochs[-1]
        self.begin_epoch('B',seq['B']['volume_db']);self.reconnects.append(('R1',before))
        self.cells['B']=[self.cell('B-1')]
        self.end_boundary();self.disconnect()
        self.sealed=self.seal_recorder()
        return self.judge_aec(triggers)
    def judge_aec(self,triggers):
        import echoear_mute_gate as g
        rows=self.read_events();device=self.device_lines();bot_log=(self.out/'bot.log').read_text()
        snaps=[p for e in self.epochs for p in e['snapshots']]
        parts=dict(mode=g.mode_check(bot_log,device,rows,snaps,self.plan))
        for name in ('A','B'):
            cells=self.cells.get(name)
            parts['reply-'+name]=dict(status=cells[0]['status']) if cells else dict(status='INCOMPLETE',reason='not reached')
        ready=[]
        for line in device:
            m=re.search(r'\bSTATE Ready gen=(\d+).*\bt=(\d+)',line)
            if m:ready.append(dict(generation=int(m[1]),device_us=int(m[2])))
        for index,(cycle,before) in enumerate(self.reconnects):
            epoch=self.epochs[index+1];after=epoch['first']
            prior={p.get('pc_id') for e in self.epochs[:index+1] for p in e['snapshots']}
            seen=[r for r in ready if r['device_us']<=after.get('device_us',-1)]
            new_rows=[e for e in rows if e.get('pc_id')==after.get('pc_id')]
            old_rows=[e for e in rows if e.get('pc_id')==before['first'].get('pc_id')]
            r=g.reconnect_rule(cycle,before['first'],after,seen,prior,rows,epoch['ready_seconds'],self.plan,trigger=triggers.get(cycle))
            # Decision 117: the old peer's session-end is post-disconnect teardown evidence: report-only.
            r['old_session_closed_report_only']=g.old_session_closed(old_rows,new_rows)
            parts['reconnect-'+cycle]=r
        if not self.reconnects:parts['reconnect-R1']=dict(status='INCOMPLETE',reason='not reached')
        for e in self.epochs:
            parts['terminal-'+e['id']]=g.terminal_epoch(e['first'],e.get('terminal'),self.plan,e.get('disconnect'))
            parts['terminal-resources-'+e['id']]=g.terminal_resources(e.get('post'),self.plan)
            parts['liveness-'+e['id']]=g.liveness_epoch(e['first'],e.get('last',e['first']))
            parts['counters-'+e['id']]=g.counter_epoch(e['first'],e.get('last',e['first']),self.plan)
            parts['admission-'+e['id']]=g.admission_epoch(e['first'],e.get('last',e['first']))
            parts['playout-'+e['id']]=g.playout_epoch(e['snapshots'],self.plan)
            parts['volume-'+e['id']]=g.volume_epoch(e['snapshots'],e['volume_db'])
        parts['display-A']=g.display_epoch(self.epochs[0]['snapshots']) if self.epochs else dict(status='INCOMPLETE')
        parts['resources']=g.resources(snaps,self.plan)
        parts['recorder-integrity']=g.recorder_integrity(rows,g.retired_peers(self.epochs,self.plan['teardown_bracket_max_s']))
        parts['liveness-logs']=g.liveness_logs_attributed(bot_log,device,g.liveness_peers(self.epochs),self.plan)
        parts['fatal-boot']=g.fatal_and_boot(device)
        parts['bot-idle-timeout']=g.bot_idle_timeout(bot_log)
        parts['teardown-bracket']=g.teardown_brackets(self.epochs,self.plan['teardown_bracket_max_s'])
        parts['idle-budget']=(dict(status='INCOMPLETE',passed=False,reason='runner idle budget reached; block stopped or wait capped',stops=self.idle_budget_stops)
                              if any(x['kind']=='block-stopped' for x in self.idle_budget_stops) else dict(passed=True,stops=self.idle_budget_stops))
        parts['final-seal']=dict(passed=True) if getattr(self,'sealed',False) else dict(status='INCOMPLETE',reason='final recorder session-end not observed')
        verdict=g.gate_verdict(parts)
        report=dict(audio_cancel_lines=sum('AUDIO_CANCEL' in line for line in device),
                    note='part 2 cells are echo-only; a cancel would be an acoustic false barge-in: report-only')
        write(self.out/'gate.json',dict(verdict=verdict,parts=parts,cells=self.cells,report_only_cells=self.extra,report_only=report,
              epochs=[{k:v for k,v in e.items() if k!='snapshots'} for e in self.epochs],plan_sha256=self.p['plan_sha256']))
        if verdict['outcome']!='PASS':raise RuntimeError('AEC reconnect check '+verdict['outcome']+': '+json.dumps(verdict['parts']))
        return verdict


def mute_start_allowed(plan,now=None):
    import echoear_mute_gate as g
    from datetime import datetime
    now=now or datetime.now()
    b=plan['budget']
    if g.exclusion_conflict(now,b['acquisition_deadline_s']+b['cleanup_reserve_s'],b['exclusion_local_minutes']):
        raise ValueError('Mute gate refused: acquisition plus protected cleanup would reach the 03:00-04:10 exclusion')


def run_mute(path,digest):
    refuse_pre_141()
    p,plan=validate_mute(path,digest,require_go=True)
    if not sys.stdin.isatty():raise ValueError('Attending operator terminal required')
    mute_start_allowed(plan)
    operator_note('Confirm campaign handoff (normal hash/mixer/lock released), quiet room, Jabra in place, and the declared final normal image.')
    mute_start_allowed(plan)
    from step9a_echoear import check_esptool
    check_esptool()
    if not port_free():raise RuntimeError('Port 7861 occupied; no hardware touched')
    out=Path(p['output']);out.mkdir();write(out/'declaration.json',p)
    return protected_execute(MuteLive(p,plan,out),out,plan['budget']['acquisition_deadline_s'])


def protected_execute(driver,out,deadline_s,*,execute_fn=None):
    """The one protected entry for mute-01 and the AEC smoke's part 2: SIGINT, SIGTERM and SIGALRM
    unwind into execute()'s protected cleanup (which ignores INT/TERM and retains the lock until a
    verified restore), and the acquisition deadline is armed. Previous handlers are restored after."""
    def stop(sig,_):raise RuntimeError('Stop requested by signal '+str(sig))
    old={sg:signal.signal(sg,stop) for sg in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM)}
    signal.alarm(int(deadline_s))
    try:return (execute_fn or execute)(driver,out)
    finally:
        signal.alarm(0)
        for sg,h in old.items():signal.signal(sg,h)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='mode',required=True)
    p=sub.add_parser('prepare');p.add_argument('destination',type=Path)
    p=sub.add_parser('validate');p.add_argument('declaration',type=Path)
    p=sub.add_parser('run');p.add_argument('declaration',type=Path);p.add_argument('--declaration-sha256',required=True)
    p=sub.add_parser('prepare-mute');p.add_argument('destination',type=Path)
    p=sub.add_parser('validate-mute');p.add_argument('declaration',type=Path)
    p=sub.add_parser('run-mute');p.add_argument('declaration',type=Path);p.add_argument('--declaration-sha256',required=True)
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args.destination)
    elif args.mode=='validate':validate(args.declaration);print('PASS; draft only; hardware not accessed')
    elif args.mode=='prepare-mute':prepare_mute(args.destination)
    elif args.mode=='validate-mute':validate_mute(args.declaration);print('PASS; draft only; hardware not accessed')
    elif args.mode=='run-mute':sys.exit(0 if run_mute(args.declaration,args.declaration_sha256)['passed'] else 1)
    else:sys.exit(0 if run(args.declaration,args.declaration_sha256)['passed'] else 1)


if __name__=='__main__':main()
