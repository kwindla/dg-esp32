from unittest.mock import MagicMock,patch
import pytest
from step7_support import BotServer
from step8b2_speech_sweep import quality_checks,SDR_FLOORS
from speech_accuracy import speech_accuracy

@pytest.mark.parametrize('detach',[False,True])
def test_owned_bot_detaches_only_when_requested(tmp_path,detach):
    socket=MagicMock();socket.__enter__.return_value.connect_ex.return_value=1
    proc=MagicMock();proc.poll.return_value=None
    with patch('step7_support.socket.socket',return_value=socket), \
         patch('step7_support.subprocess.Popen',return_value=proc) as spawn, \
         patch('step7_support.urllib.request.urlopen',return_value=MagicMock(status=200)):
        bot=BotServer(tmp_path,detach=detach);bot.start()
        assert spawn.call_args.kwargs['start_new_session'] is detach
        bot.stop();proc.terminate.assert_called_once()

def test_quality_gate_requires_board_and_reshaping_and_every_band():
    good={c:dict(correlation=.95,distortion_sdr_db={b:f+1 for b,f in SDR_FLOORS.items()}) for c in ['board','jabra']}
    good['jabra']['correlation']=.1  # Low external-mic correlation is informational.
    assert all(quality_checks(good,10000,100).values())
    good['board']['correlation']=.5
    assert not quality_checks(good,10000,100)['correlation']
    good['board']['correlation']=.95
    for band,floor in SDR_FLOORS.items():
        good['board']['distortion_sdr_db'][band]=floor-.1
        assert not quality_checks(good,10000,100)['band_sdr']
        good['board']['distortion_sdr_db'][band]=floor+1
    assert not quality_checks(good,10000,101)['reshaped_fraction']
    assert not quality_checks(good,0,0)['reshaped_fraction']

def test_france_substring_is_not_full_transcript_accuracy():
    prompt='Please tell me the capital of France.'
    assert speech_accuracy(prompt,'Please me the cap of France.')<1
    assert speech_accuracy(prompt,prompt)==1

def test_rescore_preserves_original_and_offset_fallback(tmp_path,monkeypatch):
    import hashlib,json
    import numpy as np
    import step8b2_speech_sweep as sweep
    from step7_support import write_pcm,acoustic_match
    from acoustic_response import predict,TAPS
    source=tmp_path/'server/fixtures/greeting.wav';source.parent.mkdir(parents=True)
    rng=np.random.default_rng(52);clean=rng.normal(0,3000,16000)
    write_pcm(source,clean);clean=sweep.pcm(source)
    h=np.zeros(TAPS);h[TAPS//2]=1
    model=dict(chains={c:dict(fir=h.tolist()) for c in ('board','jabra')})
    predicted=predict(clean,model,'board');recording=np.r_[np.zeros(8000),predicted,np.zeros(8000)]
    campaign=tmp_path/'campaign';capture=campaign/'greeting.wav--25';capture.mkdir(parents=True)
    chains={}
    for c in ('board','jabra'):
        path=capture/f'{c}.wav';write_pcm(path,recording)
        match=acoustic_match(path,source,.1,0,.3,reference_pcm=predicted)
        chains[c]=dict(correlation=match['correlation'],aligned_sdr_db={b:-100 for b in SDR_FLOORS})
    old=dict(completed=True,response=model,thresholds=dict(board_band_sdr_db={b:-200 for b in SDR_FLOORS}),
        utterances=[dict(name='greeting.wav',target_dbfs=-25,chains=chains,checks=dict(band_sdr=False),passed=False,
            active_samples=1000,before=dict(peaks=0),after=dict(peaks=0))])
    original=campaign/'result.json';original.write_text(json.dumps(old));original_bytes=original.read_bytes()
    monkeypatch.setattr(sweep,'ROOT',tmp_path)
    out=tmp_path/'rescore';assert sweep.rescore_campaign(campaign,out,tmp_path)==0
    result=json.loads((out/'result.json').read_text());row=result['utterances'][0]
    assert original.read_bytes()==original_bytes
    assert result['analysis_correction']['original_result_sha256']==hashlib.sha256(original_bytes).hexdigest()
    assert row['original_checks']==dict(band_sdr=False) and row['original_passed'] is False
    assert row['source_sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
    for c in chains:
        assert row['chains'][c]['correlation']==chains[c]['correlation']
        assert row['chains'][c]['integer_aligned_sdr_db']==chains[c]['aligned_sdr_db']
        assert row['chains'][c]['recording_sha256']==hashlib.sha256((capture/f'{c}.wav').read_bytes()).hexdigest()

def test_agent_comparison_summary_excludes_transport_damage(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    import step8b2_agent_compare as compare
    from playback_quality import TRANSPORT_COUNTERS
    image=tmp_path/'image.bin';elf=tmp_path/'image.elf';image.write_bytes(b'bin');elf.write_bytes(b'elf')
    clean={k:0 for k in TRANSPORT_COUNTERS};clean['playout_schema']=2;damaged=dict(clean,lost=10,concealed=10,plc_frames=10)
    def child(cmd,**kwargs):
        out=Path(cmd[cmd.index('--out')+1]);out.mkdir()
        turns=[dict(before_perf=clean,after_perf=clean,passed=True,checks=dict(llm_content=True,acoustic_content=True)),
               dict(before_perf=clean,after_perf=damaged,passed=False,checks=dict(llm_content=True,acoustic_content=False))]
        (out/'result.json').write_text(json.dumps(dict(turns=turns)))
        return SimpleNamespace(returncode=1)
    from pathlib import Path
    monkeypatch.setattr(compare,'flash_image',lambda *a:None)
    monkeypatch.setattr(compare.subprocess,'run',child)
    a=SimpleNamespace(out=tmp_path/'compare',compare_old_bin=image,compare_old_elf=elf,elf=elf,runs=3,spl=65)
    assert compare.run_comparison(a)==0
    result=json.loads((a.out/'result.json').read_text())
    assert result['order']==['pair1-old','pair1-new','pair2-new','pair2-old','pair3-old','pair3-new']
    for row in result['summary'].values():
        assert row['turns']==6 and row['acoustic_scored_turns']==3 and row['transport_damaged_turns']==3
        assert row['correct_llm_turns']==3 and row['acoustic_content_passed']==3

def test_stats_retries_missing_serial_summary(monkeypatch):
    from types import SimpleNamespace
    import step8b2_speech_sweep as sweep
    calls=[]
    def perf(board):calls.append(1);return {'lost':0}
    board=SimpleNamespace(cursor=0,lines_since=lambda cursor:[] if len(calls)==1 else
        [(0,0,'LOUDNESS target_dbfs=-25 active_samples=10 limiter_active_samples=1 peak_exceedances=0 lookahead_ms=4 resets=0')])
    monkeypatch.setattr(sweep,'perf',perf);monkeypatch.setattr(sweep.time,'sleep',lambda s:None)
    assert sweep.stats(board)['active']==10 and len(calls)==2

def test_warm_retries_never_reset_between_programs():
    from pathlib import Path
    source=(Path(__file__).parent/'step8b2_speech_sweep.py').read_text()
    # Execute the production configuration predicate for retries, not a copy.
    import re
    expression=re.search(r'^\s+configure=(.*)$',source,re.M)[1]
    for attempt in (0,1,2):
        assert not eval(expression,{},dict(temperature='warm',index=2,attempt=attempt))
        assert eval(expression,{},dict(temperature='cold',index=2,attempt=attempt))

def test_distortion_sdr_finds_its_own_integer_anchor(tmp_path):
    import numpy as np
    from scipy import fft
    from step8b2_speech_sweep import distortion_metrics
    from step7_support import write_pcm
    rng=np.random.default_rng(54);reference=rng.normal(0,5000,16000)
    size=fft.next_fast_len(len(reference)+256);spectrum=fft.rfft(np.pad(reference,(128,size-len(reference)-128)))
    observed=fft.irfft(spectrum*np.exp(-2j*np.pi*fft.rfftfreq(size)*.37),size)[128:128+len(reference)]
    source=tmp_path/'source.wav';capture=tmp_path/'capture.wav'
    # This source is deliberately spectrally different; physical timing must
    # be established with the supplied rendered prediction.
    write_pcm(source,reference*.5);write_pcm(capture,np.r_[np.zeros(8000),observed,np.zeros(8000)])
    result=distortion_metrics(capture,source,reference,.1,0,.3)
    assert abs(result['integer_offset_samples']-8000)<=1
    assert abs(result['fractional_delay_samples'])<=1
    assert min(result['sdr_db'].values())>50
