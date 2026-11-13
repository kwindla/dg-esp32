"""The production warm-sequence renderer must preserve its level history."""
import json
import subprocess
from pathlib import Path
import numpy as np


def test_sequence_renders_each_file_and_retains_history(tmp_path):
    root=Path(__file__).resolve().parents[1]
    # Host-only config; these are inputs to the actual renderer and DSP source.
    definitions=dict(DG_PLAYBACK_SEED_DBFS_X10=-289,DG_PLAYBACK_MAX_GAIN_DB=26,
        DG_PLAYBACK_ACTIVITY_DBFS=-60,DG_PLAYBACK_LEVEL_MS=400,
        DG_PLAYBACK_ATTACK_MS=20,DG_PLAYBACK_RELEASE_MS=800,
        DG_PLAYBACK_LOOKAHEAD_MS=2,DG_PLAYBACK_LIMITER_RELEASE_MS=30,
        DG_PLAYBACK_COMPRESSION_RATIO=2,DG_PLAYBACK_COMPRESSION_THRESHOLD_DBFS=-26,
        DG_PLAYBACK_HIGHPASS_HZ=100)
    (tmp_path/'sdkconfig.h').write_text('\n'.join(f'#define CONFIG_{k} {v}' for k,v in definitions.items()))
    exe=tmp_path/'render'
    subprocess.run(['g++','-std=c++17','-O2','-I'+str(tmp_path),
        '-I'+str(root/'components/dg_audio/include'),str(root/'hil/playback_dsp_render.cpp'),
        str(root/'components/dg_audio/src/playback_dsp.cpp'),'-o',str(exe)],check=True,capture_output=True)
    t=np.arange(16000*4)/16000
    loud=(np.sin(2*np.pi*1000*t)*14000).astype('<i2')
    quiet=(np.sin(2*np.pi*1000*t)*500).astype('<i2')
    first=tmp_path/'first.pcm';second=tmp_path/'second.pcm'
    loud.tofile(first);quiet.tofile(second)
    one=tmp_path/'one.pcm';warm=tmp_path/'warm.pcm';cold=tmp_path/'cold.pcm'
    stats=json.loads(subprocess.check_output([str(exe),'--sequence','-25','26',str(first),str(one),str(second),str(warm)]))
    subprocess.run([str(exe),'-25','26',str(second),str(cold)],check=True,capture_output=True)
    assert one.stat().st_size==first.stat().st_size
    assert warm.stat().st_size==second.stat().st_size
    assert stats['resets']==1
    assert stats['active_samples']>0
    assert warm.read_bytes()!=cold.read_bytes(), 'Warm sequence lost the normalizer history'
    invalid=subprocess.run([str(exe),'--sequence','-25','26',str(first)],capture_output=True)
    assert invalid.returncode==2
