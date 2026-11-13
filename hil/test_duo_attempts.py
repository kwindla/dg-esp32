"""Offline ledger tests; no bench or live process."""
import json
import pytest
from duo_attempts import register,finish
from duo_identity import write_new


def completed(root,number=1,*,retryable=False,passed=False):
    out=root/'echoear/generating'/f'attempt-{number:02d}'
    row=register(out,'echoear','generating')
    result=dict(passed=passed,retryable=retryable,tap_latencies=[dict(milliseconds=12.5)])
    write_new(out/'result.json',result);finish(out,'echoear','generating',result)
    return out,row


def test_only_one_retry_and_both_results_remain_reported(tmp_path):
    first,_=completed(tmp_path,retryable=True);second,row=completed(tmp_path,2)
    assert row['attempt']==2 and len(row['prior_attempts'])==1
    summary=json.loads((second.parent/'attempts-summary.json').read_text())
    assert len(summary['attempts'])==2 and summary['attempts'][0]['tap_latencies'][0]['milliseconds']==12.5
    assert first.exists() and second.exists()
    with pytest.raises(ValueError):register(second.parent/'attempt-03','echoear','generating')


@pytest.mark.parametrize('passed,retryable',[(True,False),(False,False)])
def test_success_and_final_failure_cannot_be_retried(tmp_path,passed,retryable):
    out,_=completed(tmp_path,passed=passed,retryable=retryable)
    with pytest.raises(ValueError):register(out.parent/'attempt-02','echoear','generating')


def test_unfinished_tampered_and_unregistered_attempts_are_refused(tmp_path):
    out=tmp_path/'echoear/generating/attempt-01';register(out,'echoear','generating')
    with pytest.raises(ValueError):register(out.parent/'attempt-02','echoear','generating')
    result=dict(passed=False,retryable=True);write_new(out/'result.json',result);finish(out,'echoear','generating',result)
    (out/'result.json').write_text('{}')
    with pytest.raises(ValueError,match='changed'):register(out.parent/'attempt-02','echoear','generating')


def test_existing_unregistered_output_and_other_numbering_are_refused(tmp_path):
    out=tmp_path/'echoear/generating/attempt-01';out.mkdir(parents=True)
    with pytest.raises(ValueError,match='unregistered'):register(out,'echoear','generating')
    with pytest.raises(ValueError):register(tmp_path/'wrong/attempt-01','echoear','generating')
