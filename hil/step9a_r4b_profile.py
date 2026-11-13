#!/usr/bin/env python3
"""Five ordered fresh diagnostic boots; all failures retained, never retaken."""
import hashlib,json,os,socket,subprocess,time,urllib.request
from pathlib import Path
from esp_serial import EspSerial,ROOT,port_lock
from step7_support import BotServer
from step9a_assets import asset_server
from step9a_support import read,host_address,pack
from step9a_r2_diagnose import save
from step9a_r2_checks import audit
from step9a_r3b_natural import Observer,settle
from step9a_r4_inventory import crypto
from step9a_r4b import baseline,perf_records,DRIVER_ERROR
from step8e2b_analysis import lcd_memory_gate

OUT=Path('/home/khkramer/src/evidence/step9a/r4b')
IMAGE=OUT/'image-profile-03/dg-esp32.elf'
REPORT=ROOT/'proj-2026-10-08-0949/codex-jobs/step-9a-r4b-report.md'
RATES=['0','25','50','100','saturating']
SITES={1:'runtime lock semaphore timeout',2:'dead_display generic',3:'io_watchdog io_started >2s',4:'io_watchdog color_inflight >2s',5:'await_dma notification timeout',6:'drain_dma tx_param error',7:'display gate timeout',8:'bench engine admission failure',9:'qualification send/await failure',10:'qualification drain/generation failure',11:'DMA bounce detected',12:'bringup get_trans_result timeout (old73)',13:'bringup queue_trans timeout (old79)',14:'bringup polling_end timeout (old91)',15:'runtime_dma_dead unspecified'}


class ProfileBot(BotServer):
    def __init__(self,out,rate):super().__init__(out,port=7861);self.rate=rate
    def start(self):
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1',7861))==0:raise RuntimeError('Owned7861occupied')
        self.out.mkdir(parents=True,exist_ok=True)
        env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
        env.update(BOT_MODE='offline-test',SERVER_PORT='7861',EVENTS_PATH=str(self.out/'events.jsonl'),
            RECORD_INPUT='1',RECORD_INPUT_DIR=str(self.out/'input'),TTS_REFERENCE_DIR=str(self.out/'tts'),
            PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',OFFLINE_SKIP_GREETING='1',OFFLINE_IDLE_TIMEOUT_SECS='7200',
            OFFLINE_R4B_RATE=self.rate,OFFLINE_R4_FLOOD_SECONDS='0',OFFLINE_R4_CIPHER_AUDIT='1')
        self.starts+=1;self.log=(self.out/f'server-{self.starts}.log').open('x')
        self.proc=subprocess.Popen([str(ROOT/'server/.venv/bin/python'),'r4b_profile_bot.py','--host','0.0.0.0','--port','7861'],cwd=ROOT/'server',env=env,stdout=self.log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            if self.proc.poll() is not None:raise RuntimeError('Profile bot exited; retained server log')
            try:
                if urllib.request.urlopen('http://127.0.0.1:7861/',timeout=.5).status<500:return
            except Exception:pass
            time.sleep(.1)
        raise TimeoutError('Profile bot startup')


def command_rows(b,cmd,prefix,timeout=45):
    at=b.cursor;b.command(cmd,timeout)
    return [json.loads(line.split(prefix+' ',1)[1]) for _,_,line in b.lines_since(at) if prefix+' ' in line]


def diag_census(obs,path,r):
    """One unmeasured 0-rate Ready call for fresh diagnostic cost/arena, same boot."""
    obs.stats()
    with ProfileBot(path/'census-bot','0'):
        at=obs.board.cursor;obs.command('connect',90);obs.board.wait_for(READY_LOG,45,since=at)
        obs.stats();settle(obs,10);obs.stats();settle(obs,.35);obs.command('disconnect',45);settle(obs,.35);obs.stats()
    snapshots,malformed,_=perf_records((path/'serial.log').read_bytes())
    memory=lcd_memory_gate(snapshots,reconnects=1)
    r['diagnostic_census']={'memory':memory,'malformed':malformed,'cost_vs113624':113624-min(s['heap']['internal']['minimum_free'] for s in snapshots),'scope':'diagnostic-only instrumentation cost, not production qualification'}
    active=[s for s in snapshots if s['heap_window']['session_samples']]
    F=min(s['heap']['psram']['window_session_minimum_free'] for s in active)
    largest=min(s['heap']['psram']['sampled_largest_minimum'] for s in active)
    W=1287760;maximum=min(F-W-589824,largest);budget=min(114688,maximum//4096*4096)
    if budget<110592:raise RuntimeError(f'Diagnostic arena cannot fit cat under unchangedPSRAMreserve: {budget}')
    save(OUT/'profile-arena-budget.json',{'F':F,'W':W,'largest':largest,'maximum':maximum,'budget':budget,'basis':'fresh diagnostic no-arena Ready+teardown; profilerPSRAM pool alreadyallocated; onlycat needed'})


def setup_cat(obs):
    budget=json.loads((OUT/'profile-arena-budget.json').read_text())
    obs.command('disp arena {budget} {F} {W} {largest}'.format(**budget))
    with asset_server(port=7861):
        for i,image in enumerate(pack()[:16]):
            obs.durable('load',{'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host_address()+':7861/'+image['hash']+'.dgi'},'profile-fixture-'+str(i))
            obs.command('disp asset '+str(i)+' '+image['hash'])
    return {'budget':budget,'status':read(obs.board,'status','DISPLAY')}


def parse_profile(path):
    values={};bad=[]
    for number,line in enumerate((path/'serial.log').read_text().splitlines(),1):
        if not line.startswith('R4B_'):continue
        prefix,_,payload=line.partition(' ')
        try:
            value=json.loads(payload);assert isinstance(value,dict)
            values.setdefault(prefix,[]).append(value)
        except Exception as e:bad.append({'line':number,'prefix':prefix,'reason':str(e)})
    save(path/'profile-records.json',{'records':values,'malformed':bad})
    return values,bad


def run(rate):
    name='profile-rate-'+(rate.zfill(3) if rate!='saturating' else 'saturating');path=OUT/name;path.mkdir(exist_ok=False)
    r={'rate':rate,'name':name,'diagnostic':True,'fresh_boot':True,'acoustic_waived':True,'completed':False,'elf_sha256':hashlib.sha256(IMAGE.read_bytes()).hexdigest(),'overridden_stops':'CPU/memory/dead/drain findings; continue to next higher freshboot, no retake'}
    r['room_baseline']=baseline(path/'room-baseline',name)
    with EspSerial('/dev/ttyACM0',log_path=path/'serial.log',display_checks=True,display_diagnostic=True) as b:
        obs=Observer(b,path);b.display_terminal_events=True
        try:
            at=b.cursor;b.reset();b.wait_for('READY transport-console',90,since=at);assert r['elf_sha256'] in b.wait_for('elf_sha256=',3,since=at)
            for cmd in ('quiet 1','audio 0','mic-fixture 1','disp clock 40000000','server-url http://'+host_address()+':7861','disp init','disp origin http://'+host_address()+':7861'):obs.command(cmd)
            assert crypto(obs)['guard']==1
            if rate=='0':diag_census(obs,path,r)
            r['setup']=setup_cat(obs)
            for cmd in ('disp r2 diagnostic 1','peer-option call-timing 1','peer-option arrival-trace 1','mic-fixture 1','gate open','audio 1','disp bench cat 30'):obs.command(cmd)
            settle(obs,2)
            if rate=='0':r['profile_arm_baseline']=baseline(path/'profile-arm-room',name+' actualbaseline afterdiagnosticcensus')
            with ProfileBot(path/'bot',rate) as bot:
                at=b.cursor;obs.command('connect',90);b.wait_for(READY_LOG,45,since=at);obs.scan()
                deadline=time.monotonic()+30
                while not any(e.get('event')=='r4-flood-start' for e in bot.events()):
                    if time.monotonic()>deadline:raise RuntimeError('No source start event')
                    settle(obs,.25)
                settle(obs,10);r['before']=obs.info();r['before_crypto']=crypto(obs);obs.stats()
                at=b.cursor;obs.command('profile start',90);b.wait_for('R4B_STARTED ',10,since=at)
                print(json.dumps({'rate':rate,'profile_started':True,'before_dead':r['before']['dead']}),flush=True)
                deadline=time.monotonic()+180;next_progress=time.monotonic()+15
                while not any('R4B_DUMP_DONE ' in l for _,_,l in b.lines_since(at)):
                    if time.monotonic()>deadline:raise RuntimeError('Automatic profile export deadline; no retake')
                    settle(obs,.5)
                    if time.monotonic()>next_progress:
                        print(json.dumps({'rate':rate,'waiting_for_auto_dump':True}),flush=True);next_progress+=15
                r['dump_done']=True
                deadline=time.monotonic()+90
                while not any(e.get('event') in ('r4-flood-complete','r4-flood-failed') for e in bot.events()):
                    if time.monotonic()>deadline:break
                    settle(obs,.5)
                r['bot_events']=[e for e in bot.events() if str(e.get('event','')).startswith('r4-flood-')]
                try:
                    r['after']=obs.info();r['after_crypto']=crypto(obs);obs.stats()
                    r['dead_final']=command_rows(b,'profile dead','R4B_DEAD')
                except Exception as e:r.setdefault('endpoint_errors',[]).append(repr(e))
                r['completed']=True
        except Exception as e:r['error']=repr(e)
        finally:
            b.display_cleanup=True
            for cmd in ('audio 0','disconnect','disp bench off','mic-fixture 0','gate auto','disp defer-persistence 0','disp call-persistence 0'):
                try:obs.command(cmd,45)
                except Exception as e:r.setdefault('cleanup_errors',[]).append(repr(e))
            settle(obs,.35)
            try:r['cleanup_info']=obs.info();r['cleanup_status']=read(b,'status','DISPLAY');obs.scan();r['cleanup_crypto']=crypto(obs)
            except Exception as e:r.setdefault('cleanup_errors',[]).append(repr(e))
            save(path/'before-close.json',r)
    raw=(path/'serial.log').read_bytes();records,bad=parse_profile(path);r['profile_record_counts']={k:len(v) for k,v in records.items()};r['malformed_profile']=bad
    r['monitor_findings']=audit(raw,diagnostic=True);r['driver_matches']=[{'offset':m.start(),'match':m.group().decode(errors='replace')} for m in DRIVER_ERROR.finditer(raw)]
    r['watchdog_episodes']=raw.count(b'task_wdt: Task watchdog got triggered');r['watchdog_fragments']=raw.count(b'task_wdt:')
    snaps,malformed,partial=perf_records(raw,allow_partial=True);r['memory']=lcd_memory_gate(snaps,reconnects=1);r['malformed_PERF']=malformed;r['partial_memory_fields']=partial
    r['message_drops']=max((s.get('drop_reasons',{}).get('message_queue',0) for s in snaps),default=0);r['log_drops']=max((s.get('diagnostic_log_drops',0) for s in snaps),default=0)
    save(path/'result.json',r)
    with REPORT.open('a') as f:f.write(f"\nPart2 orderedfreshboot `{name}`: complete={r['completed']}, error={r.get('error')}; baseline={r['room_baseline']['baseline_dbfs']:.6f}dBFS/acoustic_waived=true; profile_records={r['profile_record_counts']}; watchdogepisodes={r['watchdog_episodes']}; heap={r['memory']['measured']['internal']}; cleanupdead={r.get('cleanup_info',{}).get('dead')}. All raw/counters/errors retained; continuehigher rate underexplicitoverride, neverretake.\n")
    print(json.dumps({'rate':rate,'completed':r['completed'],'error':r.get('error'),'profile_records':r['profile_record_counts'],'dead':r.get('cleanup_info',{}).get('dead')}),flush=True)
    return r


def main():
    freeze=json.loads((OUT/'image-profile-03-freeze.json').read_text())
    assert all(hashlib.sha256((OUT/'image-profile-03'/n).read_bytes()).hexdigest()==want for n,want in freeze['image'].items())
    sources={}
    for name in ('hil/step9a_r4b_profile.py','hil/step9a_r4b_build_profile.py','server/r4b_profile_bot.py'):
        dst=OUT/'profile-harness-freeze'/name;dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes((ROOT/name).read_bytes());sources[name]=hashlib.sha256(dst.read_bytes()).hexdigest()
    save(OUT/'part2-predeclared.json',{'rates':RATES,'each_fresh_boot':True,'window_us':60000000,'source_seconds':100,'warmup_s':10,'image':freeze['image'],'harness_sources':sources,'production_source_changes':False,'no_fixes_no_retakes':True,'one_diagnostic_census':'0rateboot, noarena, unmeasuredReadyconnection forcost/budget, thennewbaselineprofile session','dead_codes':SITES,'PC':'raw TG1T0level2 1kHz hardwareEPC2, core0only; per-sampleMMUregion+stage+sharedmiss-change','core1_PC':'omitted toboundbuffer/memory/interruptoverhead; CPUtasks/cyclestagesbothcores','timing_quantiles':'whole-population4sub-buckets perpoweroftwo; p50/p99 lower/upperµs; cyclesinclusiveofpreemption/blocking','heap':'actualinternal-pointercaps-taggedrequest counters andcallertriggeredfree minima; notalllive-blocktrace; boot/windowcountersseparate','overhead':'ISRbodycyclesandobserverbodycycles reported; interruptentry/exit andunbracketedwrapperpartsnotincluded','source_PC_dictionary':'8192hashslots,32probebound; allloss counters retained','port':7861,'waiver':True})
    path=OUT/'profile-flash';path.mkdir(exist_ok=False);room=baseline(path/'room-baseline','R4b diagnostic image flash');save(path/'metadata.json',{'acoustic_waived':True,'room_baseline':room,'image':freeze['image']})
    with port_lock(),(path/'flash.log').open('x') as log:
        subprocess.run(['/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python','-m','esptool','--chip','esp32s3','-p','/dev/ttyACM0','-b','460800','--before','default-reset','--after','hard-reset','write-flash','0x10000',str(IMAGE.with_suffix('.bin'))],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,check=True)
    rows=[]
    for rate in RATES:
        rows.append(run(rate));save(OUT/'profile-campaign.json',{'ordered':RATES,'rows':rows,'completed_rates':[r['rate'] for r in rows],'retakes':False})
    return 0


from transport_test import READY_LOG
if __name__=='__main__':raise SystemExit(main())
