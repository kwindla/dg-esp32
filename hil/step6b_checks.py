"""Strict playout acceptance shared by greeting, reply and long-run HIL."""
def playout_checks(stats):
    for key in ('i2s_underflows', 'rebuffers'):
        assert key in stats and stats[key] == 0, f'{key}: {stats.get(key, "missing")}'
    assert stats['decode_errors'] == 0
    assert stats['queue_dropped'] == 0
    assert stats['decode_stack'] >= stats['playback_stack_bytes']*.25, 'Playback decode stack margin below 25%'
