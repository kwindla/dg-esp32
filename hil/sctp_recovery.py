"""Evaluate retained post-SACK flight recovery; timer reset alone is not proof."""

def check_recovery(rows):
    timers = [r for r in rows if r['event'] == 't3-expired']
    recoveries = []
    for timer in timers:
        settled = next((r for r in rows if r['pc_id'] == timer['pc_id']
                        and r['monotonic_ns'] > timer['monotonic_ns']
                        and r['event'] == 'receive-sack-complete'
                        and r['flight_size'] == r['sent_queue'] == r['outbound_queue'] == 0), None)
        recoveries.append(dict(pc_id=timer['pc_id'], t3_ns=timer['monotonic_ns'],
                               recovered_ns=settled['monotonic_ns'] if settled else None,
                               passed=settled is not None))
    return dict(passed=all(r['passed'] for r in recoveries), t3_events=len(timers),
                naturally_exercised=bool(timers), recoveries=recoveries,
                send_failures=sum(r['event'] == 'send-failed' for r in rows))


def check_active_recovery(rows, active_end_ns):
    """Freeze the recovery obligation before intentional peer teardown.

    Keep teardown timers visible, but never require a disconnected receiver to
    acknowledge them. An unresolved active timer still fails, even if teardown
    later provides an ACK.
    """
    active = [r for r in rows if r['monotonic_ns'] < active_end_ns]
    result = check_recovery(active)
    result.update(active_end_ns=active_end_ns,
        teardown_t3_events=sum(r['event']=='t3-expired' and r['monotonic_ns']>=active_end_ns for r in rows))
    return result
