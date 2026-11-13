"""Versioned echo-acceptance transport contract, separate from playback quality."""

RULE='echo-transport-v8.3'
LIMITS=dict(rule=RULE,loss_fraction_max=.02,max_consecutive_playout_dropped=None,
            burst_report_only=True,histogram_required=True,ge6_ended_delta_max=0,
            post_plc_zeros_delta_max=0,starvation_delta_max=0)


def validate_policy(gate):
    if gate.get('rule') is None:return # historical v8.2 and earlier
    if any(type(gate.get(k)) is not type(v) or gate[k]!=v for k,v in LIMITS.items()):
        raise ValueError('Unknown or altered echo transport policy')


def burst_summary(holds):
    """Keep every window; overlapping/pair/epoch windows must never be summed."""
    windows=[]
    def add(hold,scope,identity,value):
        windows.append(dict(hold=hold.get('id'),run_root=hold.get('run_root'),scope=scope,
                            **identity,transport=value))
    for h in holds:
        for c in h.get('cases',[]):
            if 'transport' in c:add(h,'cell',dict(id=c.get('id'),clean=c.get('clean')),c['transport'])
        for f in h.get('harness_failures',[]):
            # Failed acquisitions have no scored case, but retain their transport record.
            if 'transport' in f:add(h,'recovery',dict(id=f.get('id'),attempt=f.get('attempt')),f['transport'])
            original=f.get('witnesses',{}).get('raw',{}).get('checks',{})
            if 'transport' in original:add(h,'failed_acquisition',dict(id=f.get('id'),attempt=f.get('attempt')),original['transport'])
        hard=h.get('cleanup',{}).get('hard_failure',{})
        if 'transport' in hard.get('checks',{}):
            add(h,'hard_failure',dict(id=hard.get('capture_dir')),hard['checks']['transport'])
        for epoch in h.get('epoch_checks',[]):
            value=epoch.get('checks',{}).get('transport',{})
            identity=dict(pc_id=epoch.get('pc_id'),generation=epoch.get('generation'))
            if 'intervals' in value:
                for interval in value['intervals']:add(h,'epoch_interval',identity,interval)
                add(h,'epoch_raw',identity,value['raw_first_to_last'])
            else:add(h,'epoch',identity,value)
    return dict(report_only=True,aggregation='Per-window records; overlapping windows are not summed.',
                windows=windows)
