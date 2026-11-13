"""No devices: authenticate the new band and dispatch through the frozen runner."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import board_smoke as smoke
import p4_audio as base
import p4_audio_fix4 as fix4
import p4_audio_measurable as measurable
import p4_audio_measurable_voice as voice
from test_p4_audio import declared_audio
from test_p4_audio_measurable import calibration, sweep
from wire_contract import ContractError


@pytest.fixture
def band(tmp_path,monkeypatch):
    directory=tmp_path/'original';directory.mkdir()
    saved=sweep();saved['calibration']=[]
    (directory/'acoustic.json').write_text(json.dumps(saved))
    (directory/'result.json').write_text(json.dumps(dict(board='p4xc',acquisition_passed=True,
        elf_sha256='a'*64,restore=dict(clean=True,action='p4-product-of-record'))))
    wav=directory/'calibration.wav';wav.write_bytes(b'test calibration identity')
    def evidence(*args):
        result={c:calibration() for c in measurable.CHAINS}
        for chain in result.values():
            for entry in chain.values():entry.update(wav=str(wav),wav_sha256=measurable.digest(wav))
        return result
    monkeypatch.setattr(measurable,'calibration_evidence',evidence)
    path=tmp_path/'band.json';result=measurable.reanalyze(directory,path)
    return directory,path,result


def test_gate_accepts_nonempty_current_decisions_and_same_elf(band):
    directory,path,row=band
    result=measurable.reanalysis_gate(path,dict(elf_sha256='a'*64))
    assert result['decision_id']==measurable.DECISION_ID
    assert result['linearity_decision_id']==measurable.LINEARITY_DECISION_ID
    assert result['clean_amplitudes_percent']==[6,8,10,15]
    assert result['reanalysis_sha256']==measurable.digest(path)


@pytest.mark.parametrize('mutation', ['elf','decision','linearity-decision','clean-decision','applied','post-hoc',
    'qualification','report-only','threshold','exclusions','band','point','calibration','linearity','plan','restore'])
def test_gate_refuses_altered_provenance_rules_or_metrics(band,mutation):
    directory,path,row=band;manifest=dict(elf_sha256='a'*64)
    if mutation=='elf':manifest['elf_sha256']='b'*64
    elif mutation=='decision':row['decision_id']='unapproved'
    elif mutation=='linearity-decision':row.pop('linearity_decision_id')
    elif mutation=='clean-decision':row['clean_band_decision_id']='unapproved'
    elif mutation=='applied':row['decision_applied']=False
    elif mutation=='post-hoc':row['post_hoc']=False
    elif mutation=='qualification':row['qualification']=True
    elif mutation=='report-only':row['report_only']=True
    elif mutation=='threshold':row['rule']['min_chain_snr_db']=9
    elif mutation=='exclusions':row['excluded_hz']=[500,1000]
    elif mutation=='band':row['clean_amplitudes_percent']=[2,4,6,8,10,15]
    elif mutation=='point':row['points'][0]['chains']['board']['snr_db']=100
    elif mutation=='calibration':row['calibration']['board']['7500']['snr_db']=100
    elif mutation=='linearity':row['linearity_valid']=False
    elif mutation=='plan':row['source_plan_sha256']='b'*64
    else:row['restore']['clean']=False
    path.write_text(json.dumps(row))
    with pytest.raises(ContractError):measurable.reanalysis_gate(path,manifest)


@pytest.mark.parametrize('source',['acoustic.json','result.json'])
def test_gate_binds_source_bytes(band,source):
    directory,path,row=band
    original=directory/source;original.write_text(original.read_text()+'\n')
    with pytest.raises(ContractError,match='hash mismatch'):
        measurable.reanalysis_gate(path,dict(elf_sha256='a'*64))


def test_gate_binds_every_calibration_wav(band):
    directory,path,row=band
    (directory/'calibration.wav').write_bytes(b'changed calibration')
    with pytest.raises(ContractError,match='calibration WAV hash mismatch'):
        measurable.reanalysis_gate(path,dict(elf_sha256='a'*64))


def test_gate_rejects_legitimate_empty_band(band):
    directory,path,row=band
    source=directory/'acoustic.json';saved=json.loads(source.read_text())
    for point in saved['points']:
        point['chains']['board']['harmonics_dbc']['H2']=-10
    source.write_text(json.dumps(saved));path.unlink()
    assert not measurable.reanalyze(directory,path)['clean_amplitudes_percent']
    with pytest.raises(ContractError,match='non-empty'):
        measurable.reanalysis_gate(path,dict(elf_sha256='a'*64))


def test_old_kinds_delegate_and_scoped_gate_is_restored(tmp_path,monkeypatch):
    legacy=Mock(return_value={'old':'unchanged'});monkeypatch.setattr(base,'reanalysis_gate',legacy)
    path=tmp_path/'old.json';path.write_text(json.dumps(dict(kind='offline-acoustic-frequency-exclusion')))
    manifest=dict(elf_sha256='a'*64)
    with voice.gates():
        assert base.reanalysis_gate(path,manifest)=={'old':'unchanged'}
    assert base.reanalysis_gate is legacy
    legacy.assert_called_once_with(path,manifest)
    with pytest.raises(RuntimeError):
        with voice.gates():raise RuntimeError('test failure')
    assert base.reanalysis_gate is legacy


def test_main_keeps_existing_step14_runner_and_arguments(band,monkeypatch):
    directory,path,row=band;original=base.reanalysis_gate
    args=['run','--restore-record','pin.json','--restore-record-sha256','f'*64,
          'hil/p4_audio_fix4.py','voice-smoke','--acoustic-reanalysis',str(path)]
    def run(received):
        assert received==args
        assert base.reanalysis_gate(path,dict(elf_sha256='a'*64))['clean_amplitudes_percent']
        return 42
    monkeypatch.setattr(voice.p4_step14,'main',run)
    assert voice.main(args)==42 and base.reanalysis_gate is original


def test_real_voice_executor_receives_authenticated_sweep_before_backend_creation(band,tmp_path,monkeypatch):
    directory,path,row=band
    manifest=dict(profile='normal',aec_enabled=False,elf_sha256='a'*64,**declared_audio())
    monkeypatch.setattr(base,'verify_bundle',lambda *args:(manifest,{}))
    io=SimpleNamespace(product_evidence_verified=True,display_initialized=True)
    factory=Mock(return_value=io);monkeypatch.setattr(fix4,'VoiceIO',factory)
    def execute(args,*,backend):
        assert backend is io
        args.out.mkdir();return dict(acquisition_passed=True,restore=dict(clean=True))
    monkeypatch.setattr(smoke,'execute',execute)
    identity=tmp_path/'identity.json';identity.write_text('{}')
    args=SimpleNamespace(board='p4xc',bundle=tmp_path/'bundle',headless=True,acoustic_run=None,
        acoustic_reanalysis=path,bot_identity=identity,out=tmp_path/'voice')
    with voice.gates():assert fix4.voice_smoke(args)==0
    result=json.loads((args.out/'result.json').read_text())
    assert result['acoustic_sweep']['linearity_decision_id']==measurable.LINEARITY_DECISION_ID
    assert result['product_evidence_verified'] and result['display_initialized']
    factory.reset_mock();manifest['elf_sha256']='b'*64
    with voice.gates(),pytest.raises(ContractError,match='ELF'):fix4.voice_smoke(args)
    factory.assert_not_called()


def test_s3_cannot_use_the_new_decision(band,tmp_path,monkeypatch):
    directory,path,row=band
    monkeypatch.setattr(base,'verify_bundle',lambda *args:(dict(profile='normal',aec_enabled=False),{}))
    factory=Mock();monkeypatch.setattr(fix4,'VoiceIO',factory)
    args=SimpleNamespace(board='echoear',bundle=tmp_path,headless=True,acoustic_reanalysis=path)
    with voice.gates(),pytest.raises(ContractError,match='P4 only'):fix4.voice_smoke(args)
    factory.assert_not_called()
