"""Close-out identity replay against retained ERLE layout; no device IO."""
import copy
from pathlib import Path
import shutil

import pytest
import p4_parity as core
import p4_parity_campaign as ledger
import p4_parity_followup as follow
import p4_parity_followup_campaign as campaign
import p4_step16_close as close
from test_p4_parity_followup import plans
from test_p4_parity_fix1 import copy_restore,put

ROOT=Path.home()/'src/evidence/echoear/p4/step-16/product-follow-up-run-2'
PART=ROOT/'aec-demo-report/gain-30/part-0/attempt-01'


@pytest.fixture(autouse=True)
def decision_runtime():
    with close.decision_runtime():yield


def test_real_retained_erle_part_binds_result_identity_without_creating_identity_file():
    path=PART/'identity.json';assert not path.exists()
    before=(ROOT/'attempts.jsonl').read_bytes()
    with follow.adapters():assert core.bound_receipt(ROOT/'plan.json','erle','p4xc','aec-demo',PART)['status']=='fail'
    with close.replay_runtime():
        row=core.bound_receipt(ROOT/'plan.json','erle','p4xc','aec-demo',PART)
        assert row['status']=='report-only',row.get('reason')
        assert row['collection_policy']['identity_source']==str(PART/'result.json')
        assert str(PART/'result.json') in row['sources'] and str(PART/'scored-original.json') in row['sources']
    assert not path.exists() and before==(ROOT/'attempts.jsonl').read_bytes()


@pytest.mark.parametrize('change',['none','identity','identity-file','marker','score','restore'])
def test_retained_erle_identity_and_existing_gates_are_strict(plans,change):
    _,plan,root=plans;run=root/'follow/a0/part-0/attempt-01';image=root/'follow/image-plan.json'
    shutil.copyfile(ROOT/'aec-demo-report/plan.json',image)
    token=campaign.INPUT.set(dict(file=str(image),sha256=core.digest(image)))
    try:
        with follow.adapters(),ledger.attempt(plan,'erle','p4xc','aec-demo',run,0):
            restored=copy_restore(run,'p4xc','aec-demo');data=copy.deepcopy(core.load(PART/'result.json'))
            data.update(restore=restored['restore'],identity=core.load(plan)['product_identity'])
            if change=='identity':data['identity']={}
            if change=='identity-file':put(run/'identity.json',{})
            if change=='score':data['score_produced']=False
            if change=='restore':data['restore']['clean']=False;put(run/'restore.json',data['restore'])
            put(run/'result.json',data);put(run/'scored-original.json',data if change!='marker' else {})
        with close.replay_runtime():
            row=core.bound_receipt(plan,'erle','p4xc','aec-demo',run)
            assert row['status']==('report-only' if change=='none' else 'fail'),row.get('reason')
    finally:campaign.INPUT.reset(token)


def test_close_notes_explain_real_pair_f11_and_erle_regeneration():
    main=ROOT.parent/'coordinator-run-1/plan.json'
    left=[core.load(p) for p in (main.parent/'receipts').glob('*.json')]
    right=[core.load(p) for p in (ROOT/'receipts').glob('*.json')]
    with close.replay_runtime():right.append(core.bound_receipt(ROOT/'plan.json','erle','p4xc','aec-demo',PART))
    from p4_step16_paths import report_fixtures
    with report_fixtures(ROOT/'aec-demo-report/plan.json'):
        text=close.close_notes(main,ROOT/'plan.json',left,right)
    assert 'pair **FAIL**' in text and 'S3 47 ms vs P4 34 ms' in text and 'two-sided 6 ms' in text
    assert 'F11 echoear **unverified**' in text and 'F11 p4xc **unverified**' in text
    assert 'interrupts=0, reads=0' in text and 'reads=2978' in text and 'taps=1' in text
    assert '22.07 dB, 0 clips' in text and 'receipts-2' in text and 'host-only' in text


def test_only_the_exact_approved_decision_append_is_accepted(tmp_path,monkeypatch):
    bad=tmp_path/'decision.md';bad.write_bytes(close.DECISION.read_bytes()+b'Unapproved change\n')
    monkeypatch.setattr(close,'DECISION',bad)
    with pytest.raises(ValueError,match='decision'):close.approved_decision()



def test_captured_measurement_fixture_paths_survive_worktree_move():
    import p4_aec_close
    from p4_aec_image_measure import validate_plan
    from p4_step16_paths import report_fixtures
    path=ROOT/'aec-demo-report/plan.json';original=core.load(path)
    with report_fixtures(path),p4_aec_close.adapters():assert validate_plan(original)==original


def test_fixture_path_adapter_refuses_changed_definition(tmp_path):
    from p4_step16_paths import report_fixtures
    data=core.load(ROOT/'aec-demo-report/plan.json');data['fixtures'][0]['sha256']='f'*64
    path=tmp_path/'bad-plan.json';put(path,data)
    with pytest.raises(ValueError,match='fixture definitions'):
        with report_fixtures(path):pass
