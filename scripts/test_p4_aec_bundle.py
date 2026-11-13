"""AEC bundle values are explicitly inherited/unmeasured; off images unchanged."""
import json
import copy
import pytest

from test_p4_audio import bundle_metadata_case


def test_aec_bundle_labels_every_placeholder_without_exposing_other_values(bundle_metadata_case,monkeypatch):
    module,build,profile,config=bundle_metadata_case
    before=module.metadata(build,profile)
    assert 'p4_aec_calibration' not in before
    monkeypatch.setattr(module,'resolve_audio_defaults',lambda project:before['p4_kconfig_defaults'])
    with config.open('a') as stream:
        stream.write('CONFIG_DG_ECHOEAR_AEC=y\nCONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=0\n'
                     'CONFIG_DG_ECHOEAR_AEC_GUARD_MS=400\nCONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS=2000\n')
    result=module.metadata(build,profile)
    assert result['p4_aec_calibration']==dict(status='UNMEASURED',measured=False,measurement_step='15b',
        ref_gain_db=0,guard_ms=400,rearm_ms=2000,afe_delay_samples=1024,
        scope='Inherited implementation/config placeholders; no P4 calibration claimed')
    assert result['p4_audio']==before['p4_audio']
    assert 'private-test' not in json.dumps(result) and 'CONFIG_PASSWORD' not in json.dumps(result)


def test_applied_bundle_stamps_authenticated_recommendation_with_diagnostic_delay(bundle_metadata_case,monkeypatch,tmp_path):
    import p4_aec_close
    module,build,profile,config=bundle_metadata_case
    before=module.metadata(build,profile)
    monkeypatch.setattr(module,'resolve_audio_defaults',lambda project:before['p4_kconfig_defaults'])
    with config.open('a') as stream:
        stream.write('CONFIG_DG_ECHOEAR_AEC=y\nCONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=30\n'
                     'CONFIG_DG_ECHOEAR_AEC_GUARD_MS=400\nCONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS=2000\n')
    recommendation=tmp_path/'recommendation.json';recommendation.write_text('{}')
    (build/'dg-esp32.elf').write_bytes(b'fresh test image')
    selected=dict(ref_gain_db=30,guard_ms=400,rearm_ms=2000,afe_delay_samples=1024)
    proof=dict(kind='decision-backed',measured_pass=False,delay_samples=1024)
    monkeypatch.setattr(p4_aec_close,'checked_recommendation',lambda path:dict(selected=selected,delay_provenance=proof,source_elf_sha256='a'*64))
    result=module.metadata(build,profile,recommendation)['p4_aec_calibration']
    assert result['status']=='APPLIED' and not result['measured'] and 'gain_guard_measured' not in result
    assert result['calibration_status']==dict(gain='selected-single-trial',guard='provisional',delay='measured-diagnostic')
    assert result['gain_selection']==dict(method='pre-declared-sweep-rule',prompt_trials_per_gain=1)
    assert result['guard_validation']==p4_aec_close.GUARD_VALIDATION
    assert result['selection_delay_provenance']==proof and result['ref_gain_db']==30
    assert result['delay_provenance']==p4_aec_close.DELAY_PROVENANCE
    assert result['provisional_notes']=={key:p4_aec_close.PROVISIONAL_NOTE for key in ('gain','guard','double_talk','nearend_level')}
    assert result['recommendation_sha256']==module.digest(recommendation)


@pytest.mark.parametrize('change',[None,'image','config','audio','target-lock','existing'])
def test_metadata_refreeze_preserves_image_provenance_and_rejects_changed_inputs(tmp_path,monkeypatch,change):
    import p4_bundle as module
    root=tmp_path/'repo';root.mkdir();monkeypatch.setattr(module,'ROOT',root)
    lock=root/'dependencies.p4xc.lock';lock.write_text('pinned target lock')
    tool=root/'scripts/p4_bundle.py';tool.parent.mkdir();tool.write_text('metadata writer fixture')
    source=root/'source';source.mkdir();build=root/'build';build.mkdir();out=root/'new-bundle'
    hashes={}
    for name in ('dg-esp32.elf','dg-esp32.bin','flasher_args.json'):
        (source/name).write_bytes(name.encode());(build/name).write_bytes(name.encode());hashes[name]=module.digest(source/name)
    original=dict(profile='aec-normal',aec_enabled=True,firmware_commit='d'*40,project_version='old-version',
        source_files_sha256={'historical-source':'e'*64},sha256=hashes,elf_sha256=hashes['dg-esp32.elf'],
        resolved_config_sha256='a'*64,target_dependencies_lock_sha256=module.digest(lock),
        p4_audio=dict(analog_db=30),p4_kconfig_defaults={},p4_overlay_values={},p4_overlay_keydiff={},
        p4_aec_calibration=dict(ref_gain_db=30,guard_ms=400,rearm_ms=2000,afe_delay_samples=1024))
    (source/'manifest.json').write_text(json.dumps(original))
    public={key:copy.deepcopy(original[key]) for key in ('resolved_config_sha256','p4_audio','p4_kconfig_defaults',
        'p4_overlay_values','p4_overlay_keydiff','p4_aec_calibration')}
    public['p4_aec_calibration']['calibration_status']=dict(gain='selected-single-trial',guard='provisional',delay='measured-diagnostic')
    monkeypatch.setattr(module,'metadata',lambda *args:public)
    monkeypatch.setattr(module,'verify_bundle',lambda path,*args:(copy.deepcopy(original) if path==source else json.loads((path/'manifest.json').read_text()),{}))
    def git(command,**kwargs):
        if 'diff' in command:return 'scripts/p4_bundle.py\n'
        if 'rev-parse' in command:return 'c'*40+'\n'
        return ''
    monkeypatch.setattr(module.subprocess,'check_output',git)
    if change=='image':(build/'dg-esp32.bin').write_bytes(b'changed')
    elif change=='config':public['resolved_config_sha256']='b'*64
    elif change=='audio':public['p4_audio']['analog_db']=24
    elif change=='target-lock':lock.write_text('changed lock')
    elif change=='existing':out.mkdir()
    if change:
        with pytest.raises(ValueError):module.refreeze(source,build,out,root/'profile',root/'recommendation')
        assert not (out/'manifest.json').exists()
    else:
        row=module.refreeze(source,build,out,root/'profile',root/'recommendation')
        assert row['firmware_commit']==original['firmware_commit'] and row['project_version']==original['project_version']
        assert row['source_files_sha256']==original['source_files_sha256'] and row['sha256']==hashes
        assert row['metadata_refreeze']['firmware_rebuilt'] is False
        assert row['metadata_refreeze']['source_manifest_sha256']==module.digest(source/'manifest.json')
        assert all(module.digest(out/name)==sha for name,sha in hashes.items())
