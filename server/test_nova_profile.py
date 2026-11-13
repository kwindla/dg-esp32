import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import numpy as np
import pytest
from pipecat.frames.frames import TranscriptionFrame, InterimTranscriptionFrame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from demo_policy import (profile_name,turn_policy,make_stt,profile_manifest,transcript_boundary,NOVA_TTFS_P99_S)


def test_demo_default_and_invalid_profiles():
    assert profile_name()=='demo'
    for value in ('', 'DEV', 'nova', True):
        with pytest.raises(ValueError):turn_policy(profile=value)
    assert transcript_boundary('demo') is None
    assert turn_policy().vad_analyzer is None
    stt=make_stt('test')
    assert type(stt).__name__=='DeepgramFluxSTTService'
    assert stt._should_interrupt is True


def test_dev_defaults():
    params=turn_policy(profile='dev')
    assert params.user_mute_strategies==[]
    assert params.vad_analyzer.params.model_dump()==dict(confidence=.7,start_secs=.2,stop_secs=.2,min_volume=.6)
    starts=params.user_turn_strategies.start
    assert [type(s).__name__ for s in starts]==['VADUserTurnStartStrategy','TranscriptionUserTurnStartStrategy']
    assert starts[1]._use_interim is True
    assert all(s._enable_interruptions is True for s in starts)
    assert type(params.user_turn_strategies.stop[0]).__name__=='TurnAnalyzerUserTurnStopStrategy'


def test_provenance_is_stable_secret_free_and_content_hashed():
    m=profile_manifest('dev')
    assert m==profile_manifest('dev')
    assert m['versions']['pipecat-ai']=='1.15.0'
    assert len(m['config_sha256'])==64 and m['local_model_sha256']
    assert m['config']['ttfs_p99_latency']==1.0
    assert m['config_sha256']!=profile_manifest('demo')['config_sha256']
    assert 'api_key' not in json.dumps(m)


async def test_segment_final_flag_only_text_and_interim_unchanged():
    events=[]
    processor=transcript_boundary('dev',lambda kind,**data:events.append((kind,data)))
    processor.push_frame=AsyncMock()
    final=TranscriptionFrame('Please change the subject to','u','now',finalized=True)
    interim=InterimTranscriptionFrame('cooking','u','now')
    audio=InputAudioRawFrame(b'\0'*640,16000,1)
    with patch.object(FrameProcessor,'process_frame',new=AsyncMock()):
        for f in (final,interim,audio):await processor.process_frame(f,FrameDirection.DOWNSTREAM)
    assert final.finalized is False and final.text=='Please change the subject to'
    assert [c.args[0] for c in processor.push_frame.await_args_list]==[final,interim,audio]
    assert events[0][1]['provider_finalized'] is True and len(events)==1


async def test_retained_sentence04_real_aggregator_old_split_new_one_turn():
    from nova_retained_replay import replay
    with np.load(Path(__file__).parent/'tests/data/nova-sentence04.npz',allow_pickle=False) as data:
        pcm=data['pcm'].tobytes();meta=json.loads(str(data['metadata']))
    assert any(e.get('finalized') for e in meta['frames'])
    old=await replay(pcm,meta['timeline'],meta['frames'],budget=.35,boundary=False)
    new=await replay(pcm,meta['timeline'],meta['frames'])
    assert [r['text'] for r in old['turns']]==['Please change the subject to','cooking.']
    assert [r['text'] for r in new['turns']]==['Please change the subject to cooking.']
    assert new['predictions'][0]['state'].endswith('COMPLETE')
    assert new['predictions'][0]['t'] < new['turns'][0]['t']


async def test_streaming_vendor_uuid_retained_and_changes_are_observed():
    from deepgram.listen.v1.types.listen_v1results_metadata import ListenV1ResultsMetadata
    events=[]
    class FakeNova:
        def __init__(self,*args,**kwargs):self.forwarded=[]
        async def _on_message(self,message):self.forwarded.append(message)
    stt=make_stt('dummy',profile='dev',nova_class=FakeNova,
                 event=lambda kind,**data:events.append(dict(event=kind,**data)))
    for uid in ('40bd3654-e622-47c4-a111-63a61b23bfe8','40bd3654-e622-47c4-a111-63a61b23bfe8','changed'):
        metadata=ListenV1ResultsMetadata.model_validate(dict(request_id='test',model_uuid=uid,
            model_info=dict(name='general-nova-3',version='2025-04-17.21547',arch='nova-3')))
        await stt._on_message(SimpleNamespace(metadata=metadata))
    assert len(stt.forwarded)==3 and len(events)==2
    assert events[0]['model_uuid']=='40bd3654-e622-47c4-a111-63a61b23bfe8'
    assert events[1]['model_uuid']=='changed'
    assert events[0]['model_info']['version']=='2025-04-17.21547'
