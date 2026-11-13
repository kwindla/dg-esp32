"""Host-only attenuation of frozen PCM fixtures; no normalization or playback."""
import math
from pathlib import Path
import wave
import numpy as np
from echoear_aec_bench import sha


def pcm(path):
    with wave.open(str(path)) as w:
        params=w.getparams()
        if params.sampwidth!=2 or params.nchannels!=1 or params.comptype!='NONE':
            raise ValueError('Expected mono uncompressed signed16 PCM')
        samples=np.frombuffer(w.readframes(params.nframes),dtype='<i2').copy()
    return params,samples


def attenuate(source,output,gain_db):
    if not math.isfinite(gain_db) or gain_db>=0:raise ValueError('Scale-down only')
    source,output=Path(source),Path(output)
    params,x=pcm(source);scale=10**(gain_db/20)
    y=np.rint(x.astype(float)*scale).astype('<i2')
    with output.open('xb') as f:
        with wave.open(f,'wb') as w:w.setparams(params);w.writeframes(y.tobytes())
    return dict(path=str(output),sha256=sha(output),source_path=str(source),source_sha256=sha(source),
                gain_db=gain_db,scale=scale,rounding='nearest-even signed16; no dither/normalization',
                samples=len(x),rate_hz=params.framerate,channels=1,
                output_peak_lsb=int(np.max(np.abs(y.astype(np.int32)))),output_clipped_samples=int(np.sum((y==-32768)|(y==32767))))


def verify_scaled(row):
    if row['gain_db']>=0 or row['scale']!=10**(row['gain_db']/20):raise ValueError('Invalid scale-down contract')
    if sha(row['source_path'])!=row['source_sha256'] or sha(row['path'])!=row['sha256']:raise ValueError('Scaled fixture hash mismatch')
    a,x=pcm(row['source_path']);b,y=pcm(row['path'])
    if a!=b or not np.array_equal(y,np.rint(x.astype(float)*row['scale']).astype('<i2')):
        raise ValueError('Fixture is not pure declared digital scaling')
    if np.any((y==-32768)|(y==32767)):raise ValueError('Scaled fixture clips')
    return True
