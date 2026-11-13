"""Source-only playback high-pass for the versioned onset template witness."""
import math
import re
import numpy as np
from scipy.signal import lfilter
from echoear_aec_bench import ROOT,sha

KEY='CONFIG_DG_PLAYBACK_HIGHPASS_HZ'
RULE='candidate-playback-highpass-v1'


def candidate_config(elf_sha256):
    """Export only the public DSP field from the matching retained build."""
    build=ROOT/'build/accept'
    if sha(build/'dg-esp32.elf')!=elf_sha256:raise ValueError('Candidate config build ELF mismatch')
    header=build/'config/sdkconfig.h'
    values=re.findall(r'^#define '+KEY+r' (\d+)$',header.read_text(),re.M)
    if len(values)!=1:raise ValueError('Missing/ambiguous playback high-pass config')
    return dict(elf_sha256=elf_sha256,config={KEY:int(values[0])},
                sdkconfig_header_sha256=sha(header),
                implementation='components/dg_audio/src/playback_dsp.cpp',
                implementation_sha256=sha(ROOT/'components/dg_audio/src/playback_dsp.cpp'),
                filter='causal second-order Butterworth high-pass, zero initial state per source context',
                sample_rate_hz=16000)


def declared_corner(plan):
    rule=plan.get('gates',{}).get('missing_playback',{}).get('source_template_rule')
    if rule is None:return None # byte-for-byte historical scoring path
    if rule!=RULE:raise ValueError('Unknown playback template rule')
    c=plan.get('candidate',{}).get('playback_config',{})
    hz=c.get('config',{}).get(KEY)
    if (c.get('elf_sha256')!=plan.get('elf_sha256') or c.get('sample_rate_hz')!=16000 or
        type(hz) is not int or not 0<=hz<8000):raise ValueError('Invalid frozen candidate playback config')
    return hz


def filter_source(source,hz):
    """Same biquad/order as PlaybackDsp, in source LSB units; never filter MIC3.

    Double precision host analysis; no gain/normalizer/limiter or delay simulation.
    This is causal, not filtfilt: future source does not move an onset backwards.
    """
    if type(hz) not in (int,float) or not math.isfinite(hz) or not 0<=hz<8000:
        raise ValueError('Invalid playback corner')
    x=np.asarray(source,dtype=float)
    if not hz:return x.copy()
    w=2*math.pi*hz/16000;c=math.cos(w);alpha=math.sin(w)/math.sqrt(2);norm=1/(1+alpha)
    b=[(1+c)*.5*norm,-(1+c)*norm,(1+c)*.5*norm]
    a=[1,-2*c*norm,(1-alpha)*norm]
    return lfilter(b,a,x)
