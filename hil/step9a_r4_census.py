#!/usr/bin/env python3
"""R4 fixed-allocation census; complete raw heap windows through cleanup."""
import hashlib,json,os,time,subprocess
from pathlib import Path
from esp_serial import EspSerial,ROOT
from step9a_assets import asset_server
from step9a_support import host_address,FIXTURES,read
from step7_support import BotServer
from transport_test import perf,READY_LOG
from alsa import record_room_baseline
from step8e2b_analysis import lcd_memory_gate
from step9a_r2_checks import audit
from step9a_r2_diagnose import save
OUT=Path('/home/khkramer/src/evidence/step9a/r4')
ELF=Path(os.environ.get('STEP9A_R4_ELF',str(OUT/'image/dg-esp32.elf')))
def main():
    os.environ['OFFLINE_SKIP_GREETING']='1'
    path=OUT/os.environ.get('STEP9A_R4_CENSUS_NAME','census-01');path.mkdir(exist_ok=False);r={'completed':False,'elf_sha256':hashlib.sha256(ELF.read_bytes()).hexdigest()}
    r['flash_baseline']=record_room_baseline(path/'flash-room','r4 app flash')
    if r['flash_baseline']['noise_contaminated']:save(path/'result.json',r);raise RuntimeError('ACOUSTIC HOLD')
    with (path/'flash.log').open('x') as f:
        subprocess.run(['/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python','-m','esptool','--chip','esp32s3','-p','/dev/ttyACM0','-b','460800','--before','default-reset','--after','hard-reset','write-flash','0x10000',str(ELF.with_suffix('.bin'))],stdout=f,stderr=subprocess.STDOUT,check=True,cwd=ROOT)
    r['reset_baseline']=record_room_baseline(path/'reset-room','r4 census reset')
    if r['reset_baseline']['noise_contaminated']:save(path/'result.json',r);raise RuntimeError('ACOUSTIC HOLD')
    with asset_server(),BotServer(path/'bot',port=7861),EspSerial('/dev/ttyACM0',log_path=path/'serial.log',display_checks=True) as b:
        try:
            at=b.cursor;b.reset();b.wait_for('READY transport-console',60,since=at);assert r['elf_sha256'] in b.wait_for('elf_sha256=',3,since=at)
            r['boot_crypto']=read(b,'r4-crypto 0','DISPLAY_R4_CRYPTO')
            if os.environ.get('STEP9A_R4_GUARD')=='1':assert r['boot_crypto']['guard']==1,'Selected guard must be production default'
            for cmd in ('quiet 1','audio 0','disp clock 40000000','disp r4-guard '+os.environ.get('STEP9A_R4_GUARD','0'),'server-url http://'+host_address()+':7861','disp init','disp origin http://'+host_address()+':7874'):b.command(cmd,30)
            fixture=json.loads((FIXTURES/'fetch-fixture.json').read_text());at=b.cursor;b.command('disp probe http://'+host_address()+':7874/'+fixture['hash']+'.dgi');b.wait_for('probe_complete ok=1 bytes=306917',45,since=at)
            perf(b)
            for cycle in range(2):
                at=b.cursor;b.command('connect',90);b.wait_for(READY_LOG,45,since=at);perf(b);time.sleep(10);perf(b);time.sleep(.35);b.command('disconnect',45);time.sleep(.35);perf(b)
            r['status']=read(b,'status','DISPLAY');r['hash']=read(b,'r4-hash-status','DISPLAY_R4_HASH');r['crypto']=read(b,'r4-crypto 0','DISPLAY_R4_CRYPTO');r['completed']=True
        except Exception as e:r['error']=repr(e)
        finally:
            b.display_cleanup=True
            for cmd in ('audio 0','disconnect','disp bench off'):
                try:b.command(cmd,45)
                except Exception as e:r.setdefault('cleanup_errors',[]).append(repr(e))
            time.sleep(.35)
            try:perf(b)
            except Exception as e:r.setdefault('cleanup_errors',[]).append(repr(e))
            save(path/'before-close.json',r)
    r['snapshots']=[json.loads(l[5:]) for l in (path/'serial.log').read_text().splitlines() if l.startswith('PERF ')]
    r['memory']=lcd_memory_gate(r['snapshots'],reconnects=1)
    r['internal_cost']=113624-min(s['heap']['internal']['minimum_free'] for s in r['snapshots'])
    r['F']=min(s['heap']['psram']['window_session_minimum_free'] for s in r['snapshots'] if s['heap_window']['session_samples'])
    r['largest_psram']=min(s['heap']['psram']['sampled_largest_minimum'] for s in r['snapshots'] if s['heap_window']['session_samples'])
    r['monitor']=audit((path/'serial.log').read_bytes());r['passed']=r['completed'] and r['memory']['passed'] and r['internal_cost']<=27108 and r['monitor']['passed'] and not r.get('cleanup_errors')
    save(path/'result.json',r)
    if r['passed']:
        W=1287760;upper=min(r['F']-W-589824,r['largest_psram']);budget=min(491520,int(upper)//4096*4096)
        assert budget>=106895+306917+2048,'Fresh arena cannot fit cat and300KB fixture'
        save(OUT/'arena-budget.json',{'F':r['F'],'W':W,'largest':r['largest_psram'],'budget':budget,'maximum':upper,'reserve_fixed':65536,'reserve_heap':524288,'basis':'fresh R4 no-arena full-window census; unchanged lazymic/arrival/parser/arena overhead W'})
    print(json.dumps({k:r[k] for k in ('passed','internal_cost','memory','F','largest_psram')}),flush=True)
    return 0 if r['passed'] else 2
if __name__=='__main__':raise SystemExit(main())
