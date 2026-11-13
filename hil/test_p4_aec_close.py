"""Closing labels reject historical claims and retain frozen gate semantics."""
import copy
import pytest
import p4_aec_close as close


def calibration():
    historical=dict(kind='decision-backed',measured_pass=False,delay_samples=1024)
    return dict(measured=False,**close.labels(historical)),historical


@pytest.mark.parametrize('label',['decision-backed','measured','UNMEASURED'])
def test_old_delay_labels_refused_for_new_calibration(label):
    row,original=calibration();row['calibration_status']['delay']=label
    with pytest.raises(ValueError,match='measured-diagnostic'):close.calibration_labels(row,original)


@pytest.mark.parametrize('key',['decision_file','decision_sha256','evidence_file','evidence_sha256','nearend_locator_gate'])
def test_diagnostic_references_required(key):
    row,original=calibration();del row['delay_provenance'][key]
    with pytest.raises(ValueError):close.calibration_labels(row,original)


@pytest.mark.parametrize('key',['gain','guard','double_talk','nearend_level'])
def test_provisional_notes_required(key):
    row,original=calibration();del row['provisional_notes'][key]
    with pytest.raises(ValueError):close.calibration_labels(row,original)


def test_new_labels_are_isolated_and_authenticated(monkeypatch):
    row,original=calibration();close.calibration_labels(row,original)
    row['delay_provenance']['repetitions']=1
    assert close.DELAY_PROVENANCE['repetitions']==3
    monkeypatch.setattr(close,'digest',lambda path:'0'*64)
    with pytest.raises(ValueError,match='evidence changed'):close.labels(original)


def test_frozen_applied_gate_accepts_new_metadata_only_in_adapter(monkeypatch,tmp_path):
    import json
    import p4_aec_applied as applied
    import p4_aec_product as product
    recommendation=tmp_path/'recommendation.json'
    selected=dict(ref_gain_db=30,guard_ms=400,rearm_ms=2000,afe_delay_samples=1024)
    row,original=calibration()
    proof=dict(kind='p4-aec-event-guard-recommendation',selected=selected,delay_provenance=original,
        passed=True,source_elf_sha256='a'*64)
    recommendation.write_text(json.dumps(proof))
    monkeypatch.setattr(applied,'checked_recommendation',lambda path:copy.deepcopy(proof))
    monkeypatch.setattr(close,'checked_recommendation',applied.checked_recommendation)
    manifest=dict(elf_sha256='b'*64,p4_aec_calibration=dict(row,**selected,status='APPLIED',
        recommendation_sha256=close.digest(recommendation)))
    frozen=applied.calibration_labels
    with pytest.raises(ValueError):product.applied_gate(manifest,recommendation)
    with close.adapters():
        result=product.applied_gate(manifest,recommendation)
        assert result['selected']==selected and result['delay_provenance']==close.DELAY_PROVENANCE
        old=copy.deepcopy(manifest);old['p4_aec_calibration']['calibration_status']['delay']='decision-backed'
        with pytest.raises(ValueError):product.applied_gate(old,recommendation)
        wrong=copy.deepcopy(manifest);wrong['p4_aec_calibration']['ref_gain_db']=27
        with pytest.raises(ValueError,match='selected values'):product.applied_gate(wrong,recommendation)
    assert applied.calibration_labels is frozen
