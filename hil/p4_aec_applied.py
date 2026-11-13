"""Replay the frozen calibration offline, then validate its rebuilt image/readback."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
REPLAY_COMMIT='b8496ae620bef92602f199f897b2434b283e77e5'
RECOMMENDATION_SHA256='66e49b3b7b89c7d3efb450ff0254d42c3be3f4831c1a2939310d3a743b91eacf'
HISTORICAL_SOURCE='/home/khkramer/src/dg-esp32-p4-fix4/hil/p4_aec_guard_rescore.py'
CALIBRATION_STATUS=dict(gain='selected-single-trial',guard='provisional',delay='decision-backed')
GAIN_SELECTION=dict(method='pre-declared-sweep-rule',prompt_trials_per_gain=1)
GUARD_VALIDATION=dict(basis='event-detection-only',physically_validated_in_window=False,validation_owed='15d')


def calibration_labels(calibration,delay):
    require(calibration.get('calibration_status')==CALIBRATION_STATUS and
        calibration.get('gain_selection')==GAIN_SELECTION and calibration.get('guard_validation')==GUARD_VALIDATION,
        'explicit single-trial gain, provisional guard and decision-backed delay labels required')
    require(type(calibration['gain_selection']['prompt_trials_per_gain']) is int and
        calibration['guard_validation']['physically_validated_in_window'] is False,'typed trial count/physical validation status required')
    require(calibration.get('measured') is False and not any(key in calibration for key in
        ('gain_guard_measured','gain_measured','guard_measured','delay_measured')),
        'calibration must not claim measured gain/guard/delay validation')
    require(delay.get('kind')=='decision-backed' and delay.get('measured_pass') is False and
        calibration.get('delay_provenance')==delay,'decision-backed unmeasured delay provenance required')


def require(value,message):
    if not value:raise ValueError(message)


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replay_child(snapshot,path):
    """Only the historical file-location label is aliased; every code byte is pinned."""
    sys.path[:0]=[str(snapshot/'hil'),str(snapshot/'scripts'),str(snapshot/'server')]
    import p4_aec_guard_rescore as guard
    require(Path(guard.__file__).resolve().is_relative_to(snapshot),'historical replay import escaped snapshot')
    row=json.loads(path.read_text());current=guard.context();historical=row['scoring_context']
    require(historical['adapter_source']['path']==HISTORICAL_SOURCE,'unexpected historical source label')
    current['adapter_source']['path']=HISTORICAL_SOURCE
    require(current==historical,'historical replay code/decision hashes differ')
    # The old worktree no longer exists. Keep its authenticated provenance
    # label in comparisons, while executing the exact Git-archived source.
    guard.context=lambda:historical
    expected=guard.recommendation(Path(row['plan_file']),Path(row['gain_file']),[Path(p) for p in row['guard_files']])
    require(row==expected and row['passed'] and row['applied'] is False,'recommendation replay differs')
    print(json.dumps(dict(passed=True,recommendation_sha256=digest(path),selected=row['selected'])))


def checked_recommendation(path):
    path=Path(path).resolve()
    require(digest(path)==RECOMMENDATION_SHA256,'approved event recommendation hash differs')
    row=json.loads(path.read_text())
    require(row.get('kind')=='p4-aec-event-guard-recommendation','event recommendation required')
    temporary=Path(os.environ.get('TMPDIR',str(Path.home()/'.cache/gap3-tmp')));temporary.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='p4-aec-replay-',dir=temporary) as name:
        snapshot=Path(name)
        archive=subprocess.check_output(['git','archive',REPLAY_COMMIT,'hil','scripts','server/fixtures','server/tests/audio_analysis.py'],cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:stream.extractall(snapshot,filter='data')
        git_dir=subprocess.check_output(['git','rev-parse','--absolute-git-dir'],cwd=ROOT,text=True).strip()
        env=dict(os.environ,GIT_DIR=git_dir,GIT_WORK_TREE=str(snapshot),PYTHONDONTWRITEBYTECODE='1',OPENBLAS_NUM_THREADS='1')
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--replay-child',str(snapshot),str(path)],
            cwd=snapshot,env=env,capture_output=True,text=True,timeout=900)
        require(result.returncode==0,'frozen calibration replay failed: '+result.stderr[-2000:])
        proof=json.loads(result.stdout)
        require(proof==dict(passed=True,recommendation_sha256=digest(path),selected=row['selected']),
            'frozen replay attestation differs')
    return row


def readback(lines,manifest,selected):
    """Boot hash, hardware reference-gain readback and actual gate configuration."""
    from board_smoke import boot_check
    boot_check(lines,manifest,'p4xc')
    gain=selected['ref_gain_db']
    gains=[tuple(map(lambda v:int(v,16) if v.startswith('0x') else int(v),m.groups())) for line in lines
        for m in [re.search(r'MIC3_GAIN=(0x[0-9a-fA-F]+) analog_db=(\d+)',line)] if m]
    require(gains and all(reg==0x10+gain//3 and db==gain for reg,db in gains),'applied reference register readback differs')
    guards=[tuple(map(int,m.groups())) for line in lines for m in
        [re.search(r'AEC_GUARD_CONFIG guard_ms=(\d+) afe_delay_samples=(\d+) rearm_ms=(\d+)',line)] if m]
    wanted=(selected['guard_ms'],selected['afe_delay_samples'],selected['rearm_ms'])
    require(guards and all(value==wanted for value in guards),'applied guard/re-arm/delay readback differs')
    return dict(passed=True,elf_sha256=manifest['elf_sha256'],ref_gain_db=gain,
        guard_ms=wanted[0],afe_delay_samples=wanted[1],rearm_ms=wanted[2])


if __name__=='__main__':
    require(len(sys.argv)==4 and sys.argv[1]=='--replay-child','internal offline replay entry only')
    replay_child(Path(sys.argv[2]),Path(sys.argv[3]))
