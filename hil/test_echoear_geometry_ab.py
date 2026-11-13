from echoear_geometry_ab import analyze


def plan():
    return dict(old_elf_sha256='old',new_elf_sha256='new',reference=dict(sha256='frozen'),analysis=dict(old_repeatability_max_db=1,new_minus_old_mean_abs_max_db=1,native_new_vs_old_mean_abs_max_db=1.5,
        native_old_vs_historical_max_db=1.5,geometry_shift_db_range=[-10,-4],
        historical_replayed=dict(jabra=dict(rms_dbfs=-23.4),native=dict(peak=14777))))


def arms(levels):
    import math
    peak=20*math.log10(14777/32768)
    return [dict(role=role,elf_sha256='new' if role=='B-new' else 'old',reference_sha256='frozen',diagnostic_valid=True,measurements=dict(jabra=dict(rms_dbfs=level),native=dict(peak_dbfs=peak)))
        for role,level in zip(('A-old','B-new','A-old-repeat'),levels)]


def test_common_shift_proposes_old_only_band_without_applying_it():
    result=analyze(plan(),arms([-30.3,-30.4,-30.5]))
    assert result['classification']=='changed-measurement-path-consistent'
    assert result['new_band_candidate']['target_dbfs']==-29
    assert result['new_band_candidate']['tolerance_db']==3 and not result['new_band_candidate']['applied']
    changed=analyze(plan(),arms([-30.3,-30.9,-30.5]))
    assert changed['new_band_candidate']==result['new_band_candidate']
    assert result['unique_geometry_cause_proven'] is False


def test_new_only_loss_is_a_regression_not_a_rebaseline():
    result=analyze(plan(),arms([-23.3,-30.4,-23.5]))
    assert result['classification']=='new-firmware-level-regression'
    assert result['new_band_candidate'] is None
    assert analyze(plan(),arms([-23,-26.5,-24]))['classification']=='inconclusive'


def test_unstable_old_native_change_and_invalid_arms_stay_inconclusive():
    assert analyze(plan(),arms([-30,-30,-33]))['classification']=='inconclusive'
    values=arms([-30.3,-30.4,-30.5]);values[1]['measurements']['native']['peak_dbfs']-=3
    assert analyze(plan(),values)['classification']=='inconclusive'
    values=arms([-30.3,-30.4,-30.5]);values[0]['diagnostic_valid']=False
    assert analyze(plan(),values)['classification']=='inconclusive'
    assert analyze(plan(),values[:2])['classification']=='inconclusive'
    values=arms([-30.3,-30.4,-30.5]);values[0]['elf_sha256']='substituted'
    assert analyze(plan(),values)['classification']=='inconclusive'


def test_measure_uses_complete_aligned_reference_without_active_only_trimming(tmp_path):
    import math
    import wave
    import numpy as np
    from echoear_geometry_ab import measure
    rng=np.random.default_rng(12)
    source=np.concatenate([np.zeros(1600),rng.integers(-3000,3001,16000),np.zeros(1600)])
    captured=np.concatenate([np.zeros(8000),source,np.zeros(3200)])
    def wav(name,data):
        path=tmp_path/name
        with wave.open(str(path),'wb') as stream:
            stream.setparams((1,2,16000,0,'NONE','not compressed'))
            stream.writeframes(data.astype('<i2').tobytes())
        return path
    ref=wav('reference.wav',source);recording=wav('capture.wav',captured)
    result=measure(recording,recording,ref,jabra_search_s=.3,baseline_end_s=.3)
    assert result['passed']
    assert result['jabra']['alignment']['offset_s']==.5
    assert result['jabra']['samples']==len(source)
    assert abs(result['jabra']['rms_dbfs']-20*math.log10(np.sqrt(np.mean(source**2))/32768))<1e-9
    assert result['native']['peak']==3000
    assert result['native_headroom_full_capture']['available']
