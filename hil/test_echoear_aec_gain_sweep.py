from copy import deepcopy
import pytest
from echoear_aec_gain_sweep import validate


def screen():
    return dict(stage='screen',trials=[dict(run_id=f'g{g}-v{v}-r{r}',gain_db=g,
        volume_db=v,state='cold',mode='speech') for g in (0,15,24,30) for v in (0,-6) for r in (1,2)])


def test_complete_grid():
    validate(screen())


def test_missing_attempt_cannot_be_dropped():
    p=screen();p['trials'].pop()
    with pytest.raises(ValueError):validate(p)


def test_duplicate_identity_rejected():
    p=screen();p['trials'][1]['run_id']=p['trials'][0]['run_id']
    with pytest.raises(ValueError):validate(p)


@pytest.mark.parametrize('value',[-3,1,34,True,'15'])
def test_bad_gain(value):
    p=screen();p['trials'][0]['gain_db']=value
    with pytest.raises(ValueError):validate(p)


def test_finalists_need_all_cold_warm_repetitions():
    p=dict(stage='finalists',trials=[dict(run_id=f'g{g}-v{v}-{s}-r{r}',gain_db=g,
        volume_db=v,state=s,mode='speech') for g in (24,30) for v in (0,-6)
        for s in ('cold','warm') for r in (1,2,3)])
    validate(p);p['trials'].pop()
    with pytest.raises(ValueError):validate(p)


def test_resume_checkpoint_cannot_reset_or_drop_pending(tmp_path):
    import hashlib
    import json
    p=screen();p['stage']='controls';p['trials']=p['trials'][:2]
    p['trials'][1]['start_attempt']=2
    checkpoint=tmp_path/'checkpoint.json'
    checkpoint.write_text(json.dumps({'pending_cells':[
        {'cell_id':t['run_id'],'attempt':t.get('start_attempt',1)} for t in p['trials']]}))
    p['resume_checkpoint']={'path':str(checkpoint),'sha256':hashlib.sha256(checkpoint.read_bytes()).hexdigest()}
    validate(p)
    reset=deepcopy(p);reset['trials'][1]['start_attempt']=1
    with pytest.raises(ValueError,match='preserve'):validate(reset)
    dropped=deepcopy(p);dropped['trials'].pop()
    with pytest.raises(ValueError,match='preserve'):validate(dropped)
    checkpoint.write_text('{}')
    with pytest.raises(ValueError,match='hash mismatch'):validate(p)


def test_later_attempt_needs_checkpoint():
    p=screen();p['trials'][0]['start_attempt']=2
    with pytest.raises(ValueError,match='checkpoint'):validate(p)
