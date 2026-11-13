import numpy as np
import pytest
from step8e_analysis import cyclic_fixture_windows


def fixture():
    return np.random.default_rng(734).normal(0,1000,16000)


@pytest.mark.parametrize('phase',[0,913,7700,15555])
def test_cyclic_fixture_scores_every_block_at_arbitrary_initial_phase(phase):
    source=fixture()
    decoded=np.tile(np.roll(source,phase),6)
    result=cyclic_fixture_windows(decoded,source)
    assert len(result['windows'])==6
    assert result['clean_fraction']==1
    assert result['p50']>.99


def test_cyclic_fixture_does_not_project_later_phase_onto_earlier_blocks():
    source=fixture()
    decoded=np.concatenate([np.tile(np.roll(source,713),3),
                            np.tile(np.roll(source,8700),3)])
    result=cyclic_fixture_windows(decoded,source)
    assert len(result['windows'])==6
    assert result['clean_fraction']==1
    assert result['minimum']>.95


def test_cyclic_fixture_keeps_whole_missing_blocks():
    source=fixture()
    decoded=np.concatenate([np.tile(source,2),np.zeros(2*len(source)),np.tile(source,2)])
    result=cyclic_fixture_windows(decoded,source)
    assert len(result['windows'])==6
    assert result['windows'][2]['correlation']<.1
    assert result['windows'][3]['correlation']<.1
    assert result['clean_fraction']==4/6


def test_cyclic_fixture_keeps_damage_within_a_block():
    source=fixture()
    damaged=source.copy()
    damaged[4000:12000]=0
    result=cyclic_fixture_windows(damaged,source)
    assert len(result['windows'])==1
    assert result['clean_fraction']==0
    assert result['p50']<.95


def test_missing_returned_pcm_is_unknown_content_instead_of_a_filter_crash():
    result=cyclic_fixture_windows(np.array([]),fixture())
    assert result['windows']==[] and result['trailing_samples']==0
    assert result['p50'] is None and result['minimum'] is None
    assert result['clean_fraction'] is None
    assert 'Missing returned PCM' in result['basis']


def test_partial_returned_pcm_cannot_supply_a_complete_fixture_score():
    result=cyclic_fixture_windows(fixture()[:500],fixture())
    assert result['windows']==[] and result['trailing_samples']==500
    assert result['p50'] is None and result['clean_fraction'] is None
