#!/usr/bin/env python3
"""Step 13b coordinator-only live contention acquisition and offline gates.

No bot lifecycle. Import/score/pair never open a device. Limits are fixed here
and in p4_contention.md before acquisition; frozen HIL modules stay unchanged.
"""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from display_session import display_session, display_sent, display_result, stale_bridge_sends

ROOT = Path(__file__).resolve().parents[1]
SECONDS = 300
RESTART_AT = 150
PERIOD = 5
REQUEST_ID = 'p4-contention-load'
CAPTURE_DECISION = 'decision-2026-11-01-capture-counter-liveness'
LIMITS = dict(seconds=SECONDS, restart_at_s=RESTART_AT, stall_us=68411, p99_delta_us=7000,
              coverage=.95, minimum_internal=81920, largest_internal=32768, failed_allocations=0,
              mic_capture_overflows=0, playback_underruns=0)


def require(value, message):
    if not value: raise ValueError(message)


def write_new(path, value):
    with Path(path).open('x') as stream: stream.write(json.dumps(value, indent=2, allow_nan=False)+'\n')


def normal_references(bounds_path):
    """Use the three immutable captures that fixed normal's bounds, not new fits."""
    from board_smoke import bounds_for
    from make_bundle import digest
    from wire_contract import measured, parse_capture
    bounds = bounds_for(bounds_path, 'normal')
    require((bounds.stall_us, bounds.p99_delta_us, bounds.coverage) == (68411, 7000, .95), 'fixed normal bounds differ')
    references = []
    for source in json.loads(Path(bounds_path).read_text())['inputs']:
        path = Path(source['path'])
        require(digest(path) == source['sha256'], 'S3 reference result hash changed')
        result = json.loads(path.read_text())
        if result['variant'] != 'normal': continue
        require(result['board'] == 'echoear' and result['acquisition_passed'] and not result.get('error'), 'invalid S3 normal reference')
        wire = path.parent/'main-wire.log'
        require(digest(wire) == source['wire_sha256']['main'], 'S3 normal wire hash changed')
        rows = parse_capture(wire.read_text().splitlines()).intervals
        require(len(rows) == 1, 'S3 reference generation count differs')
        references.append(dict(path=str(wire), sha256=digest(wire), **measured(rows[0], bounds)))
    require(len(references) == 3, 'three fixed S3 normal captures required')
    return bounds, references


def integer(value, name):
    require(type(value) is int and value >= 0, 'missing/invalid '+name)
    return value


def capture_counters(row):
    capture=row.get('capture')
    require(isinstance(capture,dict) and all(key in capture for key in
        ('rx_overflows','rx_completions','read_errors','read_timeouts','read_frames')),
        'missing board-neutral PERF.capture counters; rebuild the AEC-off candidate')
    return {key:integer(capture[key], 'PERF.capture.'+key) for key in
        ('rx_overflows','rx_completions','read_errors','read_timeouts','read_frames')}


def resources(samples, *, board='p4xc', require_cpu=True, require_live_capture=True):
    try: return _resources(samples, board=board, require_cpu=require_cpu, require_live_capture=require_live_capture)
    except (KeyError,TypeError) as exc:
        raise ValueError('missing/invalid required PERF resource evidence: '+str(exc)) from exc


def _resources(samples, *, board, require_cpu, require_live_capture):
    require(len(samples) >= 2, 'missing resource samples')
    minima, largest, cpu = [], [], []
    overflows, underruns, dtls = [], [], []
    for row in samples:
        heap = row['heap']['internal']
        minima.append(integer(heap['minimum_free'], 'internal minimum'))
        largest.append(integer(heap['largest'], 'internal largest'))
        # Current blocks alone can miss transient fragmentation between queries.
        if heap.get('sampled_largest_minimum') is not None:
            largest.append(integer(heap['sampled_largest_minimum'], 'sampled largest'))
        for key in ('total', 'internal'):
            require(integer(row['failed_allocations'][key], 'failed allocations') == 0, 'failed allocation')
        require(integer(row['diagnostic_log_drops'], 'diagnostic log drops') == 0, 'lost diagnostics')
        overflows.append(capture_counters(row)['rx_overflows'])
        underruns.append(integer(row['i2s_underflows'], 'playback underruns'))
        dtls.append(integer(row['dtls_duration_us'], 'DTLS duration'))
        idle = row['per_core_idle_percent']
        require(len(idle) == 2, 'missing per-core CPU')
        if all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 100 for v in idle):
            cpu.append(dict(device_us=row['device_us'], busy_percent=[100-v for v in idle]))
        display = row['display']
        require(display.get('lcd_initialized') is True and not display.get('failed') and not display.get('dead'), 'display unavailable')
    if require_cpu: require(cpu, 'no measured per-core CPU windows')
    counters=[capture_counters(row) for row in samples]
    completions_delta=counters[-1]['rx_completions']-counters[0]['rx_completions']
    counter_live=completions_delta>0 if require_live_capture else None
    if require_live_capture and not counter_live:
        require(board=='echoear', 'mic capture counter not live: PERF.capture.rx_completions did not increase within measured generation ('+board+')')
        overflow_result='unmeasured (report-only)'
        measured_overflows=None
    else:
        # Startup still rejects an already-observed fault. A single snapshot
        # cannot claim liveness or a measured zero; final scoring must prove it.
        require(max(overflows) == 0, 'mic capture overflow')
        overflow_result='measured' if require_live_capture else 'pending measured-generation liveness'
        measured_overflows=max(overflows) if require_live_capture else None
    require(max(underruns) == 0, 'playback underrun')
    require(min(minima) >= 81920 and min(largest) >= 32768, 'internal heap floor')
    return dict(minimum_internal=min(minima), largest_internal=min(largest), capture_overflows=measured_overflows,
                capture_overflows_result=overflow_result, capture_counter_live=counter_live,
                capture_completions_delta=completions_delta, capture_decision=CAPTURE_DECISION,
                playback_underruns=max(underruns), per_core_cpu=cpu, dtls_duration_us=dtls,
                capture_counters=counters, failed_allocations=0)


def f11(before, after, observed=False):
    """Only an explicit user observation plus real driver/delivery deltas proves F11."""
    if not observed: return 'unverified'
    require(before.get('initialized') is True and before.get('enabled') is True, 'touch not initialized/enabled')
    require(before['driver']['injected'] == after['driver']['injected'], 'synthetic touch cannot prove F11')
    require(after['driver']['double_taps'] == before['driver']['double_taps']+1 and
            after['requests'] == before['requests']+1 and after['generation'] > before['generation'], 'real double-tap/reconnect evidence missing')
    return 'observed'


def score(directory, bounds_path=None, *, acquisition=None):
    from board_smoke import BOUNDS_FILE
    from make_bundle import digest
    from wire_contract import measured, parse_capture, validate_capture
    directory = Path(directory)
    if acquisition is None: acquisition = json.loads((directory/'acquisition.json').read_text())
    require(acquisition.get('acquisition_passed') is True and acquisition.get('restore', {}).get('clean') is True, 'unclean acquisition/restore')
    require(acquisition['limits'] == LIMITS, 'predeclared limits differ')
    bounds, references = normal_references(bounds_path or BOUNDS_FILE)
    require(acquisition['elapsed_s'] >= SECONDS and len(acquisition['sessions']) == 2, 'five minutes and exactly two intentional generations required')
    require(len(acquisition['restart']) == 1, 'exactly one intentional restart required')
    restart = acquisition['restart'][0]
    require(RESTART_AT <= restart['at_s'] < RESTART_AT+PERIOD*2 and
            restart['rendered_after'] > restart['rendered_before'], 'restart lacks continuing scan-out')
    reports, gens = [], []
    for index, session in enumerate(acquisition['sessions']):
        path = directory/session['wire_file']
        require(digest(path) == session['wire_sha256'], 'wire capture changed')
        capture = parse_capture(path.read_text().splitlines())
        require(len(capture.intervals) == 1, 'each restart generation must be frozen exactly once')
        validate_capture(capture, profile='bench-display', intended_aec=False,
                         target='esp32p4' if acquisition['board'] == 'p4xc' else 'esp32s3', request_id=REQUEST_ID)
        row = capture.intervals[0]; timing = measured(row, bounds); gens.append(row['gen'])
        deltas = [abs(timing['p99_us']-r['p99_us']) for r in references]
        require(max(deltas) <= 7000, 'p99 versus fixed S3 normal captures exceeds pair bound')
        samples = session['samples']; report = resources(samples,board=acquisition['board'])
        require(all(s['generation'] == row['gen'] for s in samples), 'resource generation differs')
        require(samples[-1]['device_us']-samples[0]['device_us'] >= 130000000, 'short contention generation')
        require(samples[-1]['dtls_duration_us'] > 0, 'missing DTLS handshake witness')
        if acquisition['board']=='p4xc':
            touch=session['touch']; require(len(touch)==2 and all(t.get('initialized') is True and t.get('enabled') is False for t in touch), 'touch poller unavailable or reconnect enabled')
            require(touch[-1]['driver']['reads']>touch[0]['driver']['reads'] and
                    all(t['driver']['interrupts']==0 for t in touch), 'missing polled touch progress')
            report['touch']=dict(before=touch[0]['driver'],after=touch[-1]['driver'])
        pc_id=samples[0]['pc_id']
        require(isinstance(pc_id,str) and pc_id and all(s['pc_id']==pc_id for s in samples), 'resource bot session differs')
        events = [e for e in session['bot_events'] if e.get('pc_id')==pc_id]
        require(events and all(e.get('mode')=='live-test' for e in events), 'observed live bot profile differs')
        require(any(e.get('event') == 'stt-settings' for e in events) and
                any(e.get('event') == 'final-transcript' for e in events) and
                any(e.get('event') == 'first-tts-byte' for e in events), 'live STT/reply evidence missing')
        cycles = session['cycles']
        require(len(cycles) >= 20 and cycles[0]['at_s'] < session['started_s']+PERIOD*2 and
                cycles[-1]['at_s'] > session['ended_s']-PERIOD*3, 'fetch/write workload coverage missing')
        require(all(b['at_s']-a['at_s'] < PERIOD*3 for a,b in zip(cycles,cycles[1:])), 'fetch/write workload stalled')
        require(all(c['http_gets'] >= 1 and c['persisted'] is True and c['deleted'] is True for c in cycles), 'missing real fetch/LittleFS completion')
        display = session['display_status']
        require(len(display) >= 2 and display[-1]['updates'][2] > display[0]['updates'][2], 'animation did not render')
        for status in display:
            require(not status['failed'] and not status['dead'], 'failed render')
            require(status['bench'][:2]==[1,30], 'continuous scene workload missing')
        require(all(b['updates'][2] > a['updates'][2] for a,b in zip(display,display[1:])), 'animation stalled between samples')
        misses = [integer(s['deadline_misses'], 'render deadline misses') for s in display]
        reports.append(dict(label='before intentional restart' if index == 0 else 'after intentional restart',
            generation=row['gen'], intervals=row, timing=timing, p99_deltas_vs_s3_us=deltas,
            resources=report, render_deadline_misses=dict(before=misses[0], after=misses[-1], delta=misses[-1]-misses[0]), cycles=len(cycles)))
    require(gens[1] > gens[0], 'restart did not create a new generation')
    text = (directory/'serial.log').read_text()
    require(not re.search(r'Guru Meditation|panic.ed|Stack canary|SESSION FAIL', text, re.I), 'fatal/session failure')
    return dict(checks_passed=True, passed=False, qualification=False, board=acquisition['board'], limits=LIMITS,
        generations=reports, s3_normal_references=references, bounds_sha256=digest(bounds_path or BOUNDS_FILE),
        display_static_ram=acquisition.get('display_static_ram'),
        display_underrun_lines=text.count('underrun happens'), f11='unverified',
        note='F11 is separate; two generations retained without stitching or restart-gap exemption inside either window')


def repo_bot_processes(repo, recorded_pid, proc_root=Path('/proc')):
    """Only Python children executing these exact repo scripts count as bots.

    uv/shell wrappers may name the same script. They are not extra Python
    instances. Other repositories and their ports are outside this check.
    """
    scripts={str((repo/'server'/name).resolve()):name for name in ('bot.py','demo_bot.py')}
    found={}
    for proc in proc_root.iterdir():
        if not proc.name.isdecimal(): continue
        try:
            command=[os.fsdecode(arg) for arg in (proc/'cmdline').read_bytes().split(b'\0') if arg]
            if not command: continue
            executable=Path(command[0]).name
            if not executable.startswith('python'):
                if not (proc/'exe').resolve().name.startswith('python'): continue
            cwd=(proc/'cwd').resolve()
            for arg in command[1:]:
                if Path(arg).name not in ('bot.py','demo_bot.py'): continue
                target=Path(arg) if Path(arg).is_absolute() else cwd/arg
                kind=scripts.get(str(target.resolve()))
                if kind: found[int(proc.name)]=kind
        except (FileNotFoundError,PermissionError,ProcessLookupError,OSError): continue
    require(found.get(recorded_pid)=='bot.py', 'bot.pid must name the Python child for '+str((repo/'server/bot.py').resolve())+' (--bot-repo '+str(repo.resolve())+')')
    require(found=={recorded_pid:'bot.py'}, 'another bot.py/demo_bot.py from --bot-repo '+str(repo.resolve())+' is running')
    return recorded_pid


def display_static_ram(elf):
    """Account selected display objects by their actual ELF memory section.

    Legacy images keep history/table internally. The 13a boot recovery uses
    external BSS plus a small internal pointer/guard; neither size is inferred
    from a historical constant or from another board's build.
    """
    from elftools.elf.elffile import ELFFile
    from make_bundle import digest
    candidates={}
    with Path(elf).open('rb') as stream:
        binary=ELFFile(stream); symbols=binary.get_section_by_name('.symtab')
        require(symbols is not None,'P4 ELF lacks symbol table')
        for symbol in symbols.iter_symbols():
            name=symbol.name
            kind=('panel_storage' if 'dg_display' in name and name.endswith('panel_storageE') else
                  'construction_guard' if name.startswith('_ZGV') and 'panel_backend' in name and name.endswith('E5panel') else
                  'panel' if name.startswith('_ZZ') and 'panel_backend' in name and name.endswith('E5panel') else
                  'init_commands' if name.startswith('_ZZ') and 'Jd9365' in name and name.endswith('E8commands') else None)
            if kind is None: continue
            section=binary.get_section(symbol['st_shndx'])
            require(symbol['st_size']>0,'empty P4 display ELF object')
            require(kind not in candidates,'ambiguous P4 display ELF object')
            candidates[kind]=dict(symbol=name,bytes=int(symbol['st_size']),section=section.name,type=section['sh_type'])
    require('panel' in candidates,'P4 display ELF objects missing')
    objects={}
    if 'panel_storage' in candidates:
        require('init_commands' not in candidates,'unexpected separate P4 init table')
        storage=candidates['panel_storage']
        require(storage['section'].startswith('.ext_ram.') and storage['type']=='SHT_NOBITS',
                'P4 display storage is not external BSS')
        objects['panel_storage']=storage;objects['panel_pointer']=candidates['panel'];layout='external-bss'
    else:
        require('init_commands' in candidates,'P4 display ELF objects missing')
        objects.update(panel_history=candidates['panel'],init_commands=candidates['init_commands']);layout='legacy-internal'
    if 'construction_guard' in candidates:objects['construction_guard']=candidates['construction_guard']
    require(all(obj['section'].startswith('.dram') for key,obj in objects.items() if key!='panel_storage'),
            'P4 display bookkeeping is not internal DRAM')
    return dict(bytes=sum(obj['bytes'] for obj in objects.values() if obj['section'].startswith('.dram')),
                external_bss_bytes=sum(obj['bytes'] for obj in objects.values() if obj['section'].startswith('.ext_ram.')),
                objects=objects,layout=layout,elf_sha256=digest(elf),
                basis='bundle ELF .symtab selected display objects, grouped by memory section')


def bot_identity(args, identity):
    from board_smoke import process_token
    from wire_contract import identity_schema
    identity_schema(identity)
    pid = int((args.bot_dir/'bot.pid').read_text())
    require(identity['bot_instance'] == process_token(pid), 'bot process changed')
    repo = args.bot_repo.resolve()
    repo_bot_processes(repo,pid)
    for field, name in (('bot_source_sha', 'bot.py'), ('policy_source_sha', 'demo_policy.py')):
        expected = subprocess.check_output(['git', 'rev-parse', 'HEAD:server/'+name], cwd=repo, text=True).strip()
        actual = subprocess.check_output(['git', 'hash-object', 'server/'+name], cwd=repo, text=True).strip()
        require(expected == actual == identity[field], 'bot source differs')
    profile = identity['profile_manifest']
    require(profile.get('entrypoint') == 'server/bot.py' and profile.get('BOT_MODE') == 'live-test' and
            profile.get('SERVER_PORT') == '7861' and profile.get('DISPLAY_RETRY_BUDGET') == '0', 'live STT/display bench profile required')
    require(Path(profile.get('DISPLAY_HIL_COMMANDS', '')).resolve() == args.commands.resolve(), 'bot command path differs')
    return pid


@contextmanager
def assets(directory):
    """Serve immutable animation fixtures plus one run-owned 18 KB noise DGI."""
    import random
    spec = importlib.util.spec_from_file_location('contention_dgi', ROOT/'tools/dgi.py')
    dgi = importlib.util.module_from_spec(spec); spec.loader.exec_module(dgi)
    directory.mkdir()
    for source in (ROOT/'hil/fixtures/echoear360').glob('*'):
        if source.is_file(): (directory/source.name).symlink_to(source)
    rng = random.Random(str(directory)); pixels = [(*[rng.randrange(256) for _ in range(3)],255) for _ in range(96*96)]
    data = dgi.encode(96,96,pixels,0,1); hash_value = hashlib.sha256(data).hexdigest()
    (directory/(hash_value+'.dgi')).write_bytes(data)
    gets = []
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            if self.path == '/'+hash_value+'.dgi': gets.append(time.monotonic())
            super().do_GET()
    server = ThreadingHTTPServer(('0.0.0.0',7874),partial(Handler,directory=str(directory)))
    worker = threading.Thread(target=server.serve_forever,daemon=True); worker.start()
    try: yield dict(hash=hash_value,bytes=len(data)), gets
    finally: server.shutdown(); server.server_close(); worker.join(timeout=3)


def acquire(args, io, manifest, identity):
    import boards
    from board_serial import open_board
    from board_smoke import boot_check, process_token
    from demo_session import prepare_board
    from make_bundle import digest
    from step9a_support import read, terminal, host_address
    from step9a_merge_checks import publish_command
    from transport_test import wait_until
    from alsa import play_wav, resolve_jabra, set_volumes
    pid = bot_identity(args, identity)
    fixture = ROOT/'hil/fixtures/agent_conversation'
    spec = next(t for t in json.loads((fixture/'manifest.json').read_text())['turns'] if t['id'] == 'long')
    require(digest(fixture/spec['file']) == spec['sha256'], 'voice fixture changed')
    card, device = resolve_jabra(); set_volumes(card,args.out/'mixer.log')
    io.flash(args.bundle,manifest,'candidate')
    from board_smoke import verify_flash_mac
    from make_bundle import verify_bundle, local_file
    _, flash=verify_bundle(args.bundle,args.board)
    port=verify_flash_mac(args.board)
    verify=[io.python,'-m','esptool','--chip',boards.BOARDS[args.board]['chip'],'--port',port,'--after','no-reset','verify-flash']
    for address,name in flash['flash_files'].items(): verify += [address,str(local_file(args.bundle,name))]
    with (args.out/'candidate-verify.log').open('w') as log:
        subprocess.run(verify,check=True,timeout=180,pass_fds=(boards.require_bench(),),stdout=log,stderr=subprocess.STDOUT)
    sessions, restarts = [], []
    with open_board(args.board,log_path=args.out/'serial.log') as board, assets(args.out/'assets') as (image,gets):
        begin = 0 if args.board == 'p4xc' else board.cursor
        if args.board == 'echoear': board.reset()
        board.wait_for('READY transport-console',90,since=begin)
        boot_check([line for _,_,line in board.lines_since(begin)],manifest,args.board)
        board.command('demo hold',45); board.command('touch-enable 0')
        prepare_board(board,args.server_url,args.out/'arena-budget.json',volume_db=-6)
        board.command('disp demo off'); board.command('disp bench cat 30')
        board.command('disp defer-persistence 1'); board.command('disp call-persistence 1')
        origin = 'http://'+host_address()+':7874'
        # Never delete a pre-existing object; run-owned noise hash must be absent.
        inventory=[]; page=0
        while True:
            row=read(board,'inventory '+str(page),'DISPLAY_INVENTORY'); inventory+=row['items']; page=row['next']
            if not page or page>=256: break
        require(all(r[0] != image['hash'] for r in inventory), 'run-owned hash already exists')
        event_path=args.bot_dir/'events.jsonl'
        def events(offset):
            with event_path.open('rb') as stream: stream.seek(offset); value=stream.read().decode()
            return [json.loads(line) for line in value.splitlines() if line.endswith('}')]
        def alive(): require(process_token(pid)==identity['bot_instance'],'bot exited/restarted')
        def snapshot():
            at=board.cursor; board.command('stats'); return json.loads(board.wait_for(r'^PERF \{',10,since=at)[5:])
        def touch_status():
            at=board.cursor; board.command('touch-status')
            return json.loads(board.wait_for(r'^DEMO_TOUCH_STATUS \{',10,since=at).split(' ',1)[1])
        def wire(operation,body,rid,offset,since=None):
            cursor=board.cursor if since is None else since
            alive(); publish_command(args.commands,operation,body,rid)
            sent=wait_until(lambda: display_sent(events(offset),session,rid),10)
            require(sent is not None,'missing display-sent '+rid)
            packet=sent['packet']['d']
            ack=wait_until(lambda: display_result(events(offset),session,packet),10)
            require(ack is not None and ack['result'].get('accepted') is True and not ack['result'].get('parse_failed'),'wire rejected '+rid)
            # At 30 scene updates/s the result ring can evict a job quickly.
            # Retained worker records are the authoritative durable witness.
            line=board.wait_for(r'DISPLAY_WORK_RESULT .*"id":"'+re.escape(rid)+'"',45,since=cursor)
            done=json.loads(line.split('DISPLAY_WORK_RESULT ',1)[1])
            require(done['epoch']==session['epoch'],'display worker epoch differs from active session '+rid)
            require(done['error']=='none','display worker failure '+rid)
            return done
        total_start=None; last_end=None; last_render=None
        try:
            for index in range(2):
                offset=event_path.stat().st_size; bot_at=(args.bot_dir/'bot.log').stat().st_size; at=board.cursor
                # Clear stale command before the new display-ready.
                base=json.loads((ROOT/'hil/fixtures/echoear360/codec-benchmark.json').read_text())['rows'][1]
                body=dict(hash=base['hash'],bytes=base['bytes'],url=origin+'/'+base['hash']+'.dgi')
                publish_command(args.commands,'load',body,REQUEST_ID)
                connect_at=time.monotonic(); board.command('connect',100)
                board.wait_for(r'\bSTATE Ready gen=\d+',30,since=at)
                ready_status=read(board,'status','DISPLAY')
                session=wait_until(lambda: display_session(events(offset),ready_status.get('epoch')),10)
                require(session is not None,'missing current-session bot display-ready')
                first=snapshot()
                require(first['pc_id']==session['pc_id'],'device PERF pc_id differs from bot display-ready')
                # Fail before starting the timed contention window, not five
                # minutes later with an AEC-only field lookup.
                try:
                    capture_counters(first); resources([first,first],board=args.board,require_cpu=False,require_live_capture=False)
                except ValueError as exc:raise ValueError('pre-window: '+str(exc)) from exc
                board.command('audio 1')
                wire('load',body,REQUEST_ID,offset,since=at)
                if total_start is None: total_start=time.monotonic()
                else:
                    status=ready_status
                    restarts.append(dict(at_s=last_end-total_start, handshake_wall_s=time.monotonic()-connect_at,
                        rendered_before=last_render,rendered_after=status['updates'][2],
                        basis='render progress from immediately before disconnect to immediately after Ready; not a timestamped scan-during-DTLS witness'))
                started=time.monotonic()-total_start
                samples=[first]; display=[read(board,'status','DISPLAY')]; touches=[touch_status()]; cycles=[]; next_prompt=0
                end=total_start+(RESTART_AT if index==0 else SECONDS)
                while time.monotonic()<end:
                    tick=time.monotonic(); alive()
                    lines=[line for _,_,line in board.lines_since(at)]
                    require(not any(re.search(r'SESSION FAIL|Peer disconnected|RTCP_BYE|Guru Meditation|panic.ed|Stack canary',l,re.I) for l in lines),'unintentional session loss')
                    if tick>=next_prompt:
                        play_wav(fixture/spec['file'],device); next_prompt=tick+30
                    number=len(cycles); rid=f'c13b-{index}-{number}'
                    before=len(gets); load=dict(image,url=origin+'/'+image['hash']+'.dgi')
                    wire('load',load,rid+'l',offset)
                    persisted=wire('persist',dict(hash=image['hash']),rid+'p',offset)
                    deleted=wire('delete',dict(hash=image['hash']),rid+'d',offset)
                    cycles.append(dict(at_s=tick-total_start,http_gets=len(gets)-before,
                        persisted=persisted.get('persisted') is True,deleted=deleted['error']=='none'))
                    samples.append(snapshot()); display.append(read(board,'status','DISPLAY'))
                    until=min(tick+PERIOD,end)
                    while time.monotonic()<until: alive(); time.sleep(min(.1,max(0,until-time.monotonic())))
                samples.append(snapshot()); touches.append(touch_status())
                # Freeze once while still Ready, before our intentional stop.
                board.command('uplink-intervals')
                # Bracket restart tightly; the last workload-cycle status can be seconds old.
                before_stop=read(board,'status','DISPLAY'); last_render=before_stop['updates'][2]
                last_end=time.monotonic()
                stop_at=board.cursor; board.command('disconnect',45)
                board.wait_for(r'\bPHASE closed\b',15,since=stop_at)
                text='\n'.join(line for _,_,line in board.lines_since(at))+'\n'
                path=args.out/f'generation-{index}-wire.log'; path.write_text(text)
                with (args.bot_dir/'bot.log').open('rb') as stream:
                    stream.seek(bot_at); bot_text=stream.read()
                require(b'\x1b[' not in bot_text,'bot log must be uncoloured')
                (args.out/f'generation-{index}-bot.log').write_bytes(bot_text)
                sessions.append(dict(wire_file=path.name,wire_sha256=digest(path),samples=samples,display_status=display,
                    cycles=cycles,touch=touches,bot_events=events(offset),display_session=session,
                    stale_bridge_sends=stale_bridge_sends(events(offset),session),started_s=started,ended_s=last_end-total_start))
            alive()
            return dict(acquisition_passed=True,board=args.board,limits=LIMITS,elapsed_s=time.monotonic()-total_start,
                        sessions=sessions,restart=restarts,f11='unverified')
        finally:
            board.command('audio 0'); board.command('disconnect',45)
            board.command('disp bench off'); board.command('disp character off')
            # Only this run's hash; no fixture/inventory-wide cleanup.
            from step9a_support import request
            request(board,'delete',dict(hash=image['hash']),'c13b-cleanup')
            require(terminal(board,'c13b-cleanup')['error']=='none','owned object cleanup failed')
            board.command('disp call-persistence 0'); board.command('disp defer-persistence 0')


def live(args):
    import boards
    from p4_audio import HardwareIO
    from make_bundle import verify_bundle, digest
    boards.require_bench()
    manifest,_=verify_bundle(args.bundle,args.board)
    require(manifest['profile']=='normal' and not manifest['aec_enabled'],'normal AEC-off bundle required')
    normal_references(args.bounds)  # Missing/tampered references fail BEFORE flash.
    identity=json.loads(args.bot_identity.read_text()); bot_identity(args,identity)
    static_ram=display_static_ram(args.bundle/'dg-esp32.elf') if args.board=='p4xc' else None
    os.environ['STEP9A_FIXTURES']=str(ROOT/'hil/fixtures/echoear360')
    args.out.mkdir(parents=True,exist_ok=False)
    write_new(args.out/'identity.json',identity); write_new(args.out/'image.json',manifest)
    write_new(args.out/'limits.json',dict(limits=LIMITS,helper_sha256=digest(Path(__file__))))
    io=HardwareIO(args,manifest); io.preflight_tools(); io.preflight_restore(); guard=io.hold()
    result=dict(acquisition_passed=False,passed=False,qualification=False,limits=LIMITS,board=args.board,display_static_ram=static_ram)
    try:
        with guard:
            guard.touch(); result.update(acquire(args,io,manifest,identity))
            # Gate failure is a failed P4 run under R7: keep restore armed.
            assessment=score(args.out,args.bounds,acquisition=dict(result,restore=dict(clean=True)))
            guard.success=True
        result['restore']=guard.record
        require(guard.record.get('clean') is True,'unclean restore')
    except BaseException as exc:
        result.update(acquisition_passed=False,error=type(exc).__name__+': '+str(exc),restore=guard.record)
    finally:
        guard.release_signals(); write_new(args.out/'acquisition.json',result)
    if not result['acquisition_passed']: return 1
    try: write_new(args.out/'score.json',assessment)
    except Exception as exc:
        write_new(args.out/'score-error.json',dict(error=type(exc).__name__+': '+str(exc),qualification=False)); return 2
    return 0


def pair(left,right,bounds_path):
    from wire_contract import parse_capture,compare
    bounds,_=normal_references(bounds_path)
    reports=[score(left,bounds_path),score(right,bounds_path)]
    require([r['board'] for r in reports]==['echoear','p4xc'],'pair needs S3 then P4')
    acquisitions=[json.loads((p/'acquisition.json').read_text()) for p in (left,right)]
    identities=[json.loads((p/'identity.json').read_text()) for p in (left,right)]
    measured=[]
    for index in range(2):
        captures=[parse_capture((p/a['sessions'][index]['wire_file']).read_text().splitlines()) for p,a in zip((left,right),acquisitions)]
        measured.append(compare(*captures,profile='bench-display',left_aec=False,right_aec=False,bounds=bounds,
            left_identity=identities[0],right_identity=identities[1],request_id=REQUEST_ID))
    capture_evidence=[dict(board=report['board'],generations=[dict(generation=g['generation'],
        **{key:g['resources'][key] for key in ('capture_overflows','capture_overflows_result','capture_counter_live',
                                              'capture_completions_delta','capture_decision')}) for g in report['generations']]) for report in reports]
    return dict(exact_wire_passed=True,checks_passed=True,passed=False,qualification=False,generations=measured,
                capture_overflow_evidence=capture_evidence,f11='unverified')


def main(argv=None):
    from board_smoke import BOUNDS_FILE, FACTORY_BACKUP
    parser=argparse.ArgumentParser(description=__doc__); sub=parser.add_subparsers(dest='action',required=True)
    analysis=sub.add_parser('score'); analysis.add_argument('directory',type=Path); analysis.add_argument('--out',type=Path,required=True)
    paired=sub.add_parser('pair'); paired.add_argument('left',type=Path); paired.add_argument('right',type=Path); paired.add_argument('--out',type=Path,required=True)
    touch=sub.add_parser('f11'); touch.add_argument('before',type=Path); touch.add_argument('after',type=Path)
    touch.add_argument('--user-observed',action='store_true'); touch.add_argument('--out',type=Path,required=True)
    run=sub.add_parser('live'); run.add_argument('--board',choices=('echoear','p4xc'),required=True)
    for name in ('bundle','out','bot-dir','bot-identity','commands'): run.add_argument('--'+name,type=Path,required=True)
    run.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    run.add_argument('--server-url',default='http://192.168.7.222:7861')
    run.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=FACTORY_BACKUP)
    run.set_defaults(reference_only=False,p4_restore_bundle=None,p4_restore_elf=None)
    for child in (analysis,paired,run): child.add_argument('--bounds',type=Path,default=BOUNDS_FILE)
    args=parser.parse_args(argv)
    if args.action=='live':
        require(args.server_url.startswith('http://') and args.server_url.endswith(':7861'),'bench bot uses 7861')
        return live(args)
    if args.action=='score': result=score(args.directory,args.bounds)
    elif args.action=='pair': result=pair(args.left,args.right,args.bounds)
    else: result=dict(f11=f11(json.loads(args.before.read_text()),json.loads(args.after.read_text()),args.user_observed),qualification=False)
    write_new(args.out,result); return 0


if __name__=='__main__': raise SystemExit(main())
