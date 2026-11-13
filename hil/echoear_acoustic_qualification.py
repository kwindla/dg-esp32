#!/usr/bin/env python3
"""Fresh quiet-room5/5 offline and5/5 live; separate coordinated bench holds."""
import argparse,hashlib,json,os,subprocess
from pathlib import Path
from types import SimpleNamespace
from esp_serial import ROOT
from playback_quality import transport_gate

PROFILE=dict(schema=1,knee=8192,ceiling=16384)


def gate(result,peers):
    runs=result.get('runs',[])
    checks=dict(five_originals=len(runs)==5 and all(r.get('passed') is True for r in runs),
        no_retakes=result.get('reruns')==[],one_peer=len(peers)==1,
        uplink=result.get('link_tail',{}).get('passed') is True,
        display=result.get('display_monitor',{}).get('passed') is True)
    for i,r in enumerate(runs,1):
        checks[f'run{i}_quiet']=r.get('room_baseline',{}).get('noise_contaminated') is False
        head=r.get('mic_headroom',{})
        checks[f'run{i}_protection']=r.get('gain_readback',{}).get('protection')==PROFILE and head.get('passed') is True and head.get('unprotected_headroom_db',-1)>=3
        gain=r.get('gain_readback',{})
        checks[f'run{i}_gain']=gain.get('analog_db')==30 and gain.get('digital_percent')==125 and gain.get('register')==0x1a
        checks[f'run{i}_clips']=r.get('mic_clip_gate',{}).get('passed') is True
        checks[f'run{i}_volume']=r.get('qualification_volume_relative_db')==0
        checks[f'run{i}_quiet_queries']=r.get('perf_acquisition_gate',{}).get('passed') is True
        transport=transport_gate(r.get('before_perf',{}),r.get('after_perf',{}))
        checks[f'run{i}_whole_transport']=transport['passed'] is True and all(v==0 for v in transport['deltas'].values())
    return dict(passed=all(checks.values()),checks=checks)


def prepare(plan,budget,elf=None,references=()):
    if plan.exists():raise FileExistsError('Do not replace a frozen qualification plan')
    elf_path=(elf or ROOT/'build/dg-esp32.elf').resolve()
    image=elf_path.read_bytes()
    if elf_path==(ROOT/'build/dg-esp32.elf').resolve():
        config=json.loads((ROOT/'build/config/sdkconfig.json').read_text())
        if not config.get('DG_BOARD_ECHOEAR') or config.get('DG_MIC_GAIN_PERCENT')!=125:
            raise ValueError('Build the protected EchoEar 125% image before freezing a plan')
    if b'MIC_PROTECTION schema=' not in image:
        raise ValueError('Build the protected EchoEar 125% image before freezing a plan')
    identity=hashlib.sha256(image).hexdigest()
    refs=[ROOT/'hil/fixtures/prompt_speech.wav',*(ROOT/'server/fixtures'/n for n in ('greeting.wav','reply_pineapple.wav','prompt_france.wav'))]
    refs.append(ROOT/'server/fixtures/manifest.json')
    for ref in references:
        refs.extend(sorted(p for p in ref.rglob('*') if p.is_file()) if ref.is_dir() else [ref])
    value=dict(schema=2,elf_path=str(elf_path),elf_sha256=identity,budget=str(budget.resolve()),budget_sha256=hashlib.sha256(budget.read_bytes()).hexdigest(),
        reference_hashes={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in refs},
        modes=['offline','live'],runs_each=5,warm=True,no_retakes=True,volume_relative_db=0,
        gain=dict(analog_db=30,digital_percent=125,protection=PROFILE),
        noise_bypass=False,prior_acoustic_runs_counted=False,soak=False,
        headroom_gate_basis='unprotected high-pass plus digital gain peak',minimum_headroom_db=3,
        a6_order=['B strict voice+cat 3 turns','A echo stress','A hot STT -39/-27 pair','A analog saturation probe','fresh quiet offline5/live5'],
        instructions='Run each mode under bench.py after coordinating with B. Room is checked before every turn; a noisy turn stops the campaign. Rebuild/new ELF requires a new plan.')
    plan.parent.mkdir(parents=True,exist_ok=True);plan.write_text(json.dumps(value,indent=2)+'\n')
    return value


def verify_plan(plan):
    frozen=json.loads(plan.read_text())
    assert frozen.get('schema')==2 and frozen.get('headroom_gate_basis')=='unprotected high-pass plus digital gain peak' and frozen.get('minimum_headroom_db')==3,'Prepare a new final-image A6 plan; earlier acoustic plans are superseded'
    elf=Path(frozen.get('elf_path',str(ROOT/'build/dg-esp32.elf')))
    assert hashlib.sha256(elf.read_bytes()).hexdigest()==frozen['elf_sha256'],'ELF changed; prepare a new plan'
    assert hashlib.sha256(Path(frozen['budget']).read_bytes()).hexdigest()==frozen['budget_sha256'],'Budget changed'
    for path,expected in frozen['reference_hashes'].items():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==expected,'Reference changed'
    return frozen,elf


def execute(plan,out,mode,flash=False):
    if not os.getenv('DG_BENCH_LOCK_FD'):raise RuntimeError('Run under bench.py after coordinating with B')
    frozen,elf=verify_plan(plan)
    if out.exists():raise FileExistsError('Fresh acquisition directory required')
    os.environ['DG_BOARD']='echoear'
    if flash:
        if elf.resolve()!=(ROOT/'build/dg-esp32.elf').resolve():
            raise ValueError('Shared external ELF: install its frozen binary with the coordinator; do not rebuild or flash this worktree')
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.with_name(out.name+'-flash.log').open('w') as log:
            subprocess.run([str(ROOT/'scripts/idf.sh'),'flash'],check=True,stdout=log,stderr=subprocess.STDOUT,
                cwd=ROOT,pass_fds=(int(os.environ['DG_BENCH_LOCK_FD']),))
        verify_plan(plan)
    # The acoustic campaign is distinct from B's longer strict display protocol campaign.
    os.environ.pop('ECHOEAR_STRICT_MERGE',None)
    os.environ.pop('OFFLINE_BENCH_FIXTURE_DIR',None)
    os.environ.pop('DISPLAY_HIL_COMMANDS',None)
    os.environ.update(DG_BOARD='echoear',ECHOEAR_ACOUSTIC_QUALIFICATION='1',
        STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),STEP9A_BUDGET_PATH=frozen['budget'])
    from e2e_common import run
    from step9a_assets import asset_server
    from step9a_support import host_address
    with asset_server():
        code=run(SimpleNamespace(out=out,port=None,runs=5,spl=65,warm=True,no_retakes=True,elf=elf,
            server_url='http://'+host_address()+':7861',ice_servers=[],option=[],display='cat',diagnostic_noisy=False),mode=='live')
    path=out/'result.json'
    if not path.exists():raise RuntimeError('No acquisition result; a credential skip cannot qualify')
    result=json.loads(path.read_text());events=out/'bot/events.jsonl'
    peers=sorted({e['pc_id'] for line in events.read_text().splitlines() if (e:=json.loads(line)).get('pc_id')}) if events.exists() else []
    result['acoustic_qualification']=gate(result,peers)
    result['qualification_plan']=frozen;result['bot_peer_ids']=peers
    result['passed']=result.get('passed',False) and result['acoustic_qualification']['passed']
    path.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(mode=mode,passed=result['passed'],pass_count=result.get('pass_count'),acoustic_hold=result.get('acoustic_hold'))))
    return code or (0 if result['passed'] else 1)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--prepare',action='store_true');p.add_argument('--budget',type=Path)
    p.add_argument('--elf',type=Path,help='Coordinator-frozen final ELF, also used by B')
    p.add_argument('--reference',type=Path,action='append',default=[],help='Freeze additional fixture file or directory; repeat for stress and analog banks')
    p.add_argument('--mode',choices=('offline','live'));p.add_argument('--out',type=Path)
    p.add_argument('--flash',action='store_true')
    a=p.parse_args()
    if a.prepare:
        if not a.budget:p.error('--budget required for preparation')
        print(json.dumps(prepare(a.plan,a.budget,a.elf,a.reference),indent=2))
    else:
        if not a.mode or not a.out:p.error('--mode and fresh --out required')
        raise SystemExit(execute(a.plan,a.out,a.mode,a.flash))
