#!/usr/bin/env python3
"""Duo physical witness: user-paced GO, fixed 600 s window, shared R7 lifecycle."""
import json
from pathlib import Path

from duo_identity import require,write_new,digest
from duo_smoke import delta,switch_witness,status_matches,record_physical_latency
# Reuse the frozen timing and contact policy. Its reconnect witness is deliberately
# not used: duo must preserve the connection throughout the physical switch.
from p4_touch_verify import WAIT,WINDOW,POLL,validate_status as driver_status,contact


def witness(before,after,prior,proof,events,wire,driver_before,driver_after):
    from p4_touch_verify import delta as driver_delta
    d=driver_delta(driver_before,driver_after)
    require(d['double_taps']==1 and not any(d[k] for k in ('injected','injected_ignored','read_errors')),
            'physical driver witness differs')
    return dict(switch_witness(before,after,prior,proof,events,wire,physical=True),driver_delta=d)


def observe(out,io,*,emit=print):
    """Fake-IO compatible. Early contacts retained but never scored; no reconnect."""
    out=Path(out);go=out/'GO';require(not go.exists(),'GO must be created only after ready-for-go.json')
    io.command('touch-enable 1')
    def sample(phase,timeline):
        started=io.clock();state,proof=io.snapshot();driver=driver_status(io.json_command('touch-status','DEMO_TOUCH_STATUS'))
        row=dict(started=started,at=io.clock(),phase=phase,status=state,proof=proof,driver=driver)
        timeline.write(json.dumps(row)+'\n');timeline.flush();return row
    try:
        with (out/'status-timeline.jsonl').open('x') as timeline:
            sample('waiting-for-go',timeline)
            write_new(out/'ready-for-go.json',dict(go_file=str(go),window_seconds=WINDOW,wait_seconds=WAIT,
                instructions='Wait for WINDOW OPEN. One short single tap; wait for CONTACT PRE-CHECK and at least 1 s before one physical double tap at the same spot; stop touching.'))
            emit('READY FOR GO '+str(out/'ready-for-go.json'),flush=True)
            deadline=io.clock()+WAIT
            while not go.exists():
                if io.clock()>=deadline:return dict(go_observed=False,touch_verified=False,status='go-timeout')
                io.pause(min(POLL,deadline-io.clock()));sample('waiting-for-go',timeline)
            require(go.is_file() and not go.is_symlink(),'GO must be a regular file')
            before=sample('go-baseline',timeline);status_matches(before['status'],before['proof'])
            require(not before['status']['pending'] and before['driver']['initialized'],
                    'GO baseline not ready')
            mark=io.mark();opened=io.clock();deadline=opened+WINDOW
            io.touch_mark=mark
            write_new(out/'before.json',before)
            write_new(out/'window-opened.json',dict(opened_at=opened,deadline=deadline,go_sha256=digest(go),mark=mark))
            emit('WINDOW OPEN — 600 s; one short single tap, wait for CONTACT PRE-CHECK and at least 1 s, then one physical double tap; stop touching.',flush=True)
            found=None;after=before;precheck=False;last_zero=before;tap_interval=None
            while io.clock()<deadline:
                io.pause(min(POLL,deadline-io.clock()));after=sample('window',timeline)
                d=delta(before['status'],after['status'])
                if not d['taps_physical']:last_zero=after
                elif tap_interval is None:
                    tap_interval=(last_zero['started'],after['at']);io.touch_tap_interval=tap_interval
                if tap_interval is not None:record_physical_latency(io,io.evidence(mark)[1])
                from p4_touch_verify import delta as driver_delta
                dd=driver_delta(before['driver'],after['driver'])
                require(not d['taps_injected'] and not any(dd[k] for k in ('injected','injected_ignored','read_errors')),
                        'injection or touch read error during window')
                require(after['proof']['session']==before['proof']['session'] and after['proof']['conn']==before['proof']['conn'],
                        'physical window reconnected')
                if not precheck and contact(dd,io.args.board)['detected']:
                    precheck=True;emit('CONTACT PRE-CHECK '+json.dumps(dd)+' — wait at least 1 s before the double tap.',flush=True)
                if d['taps_physical']==1 and d['answered']==1 and not after['status']['pending']:
                    events,wire,_=io.evidence(mark)
                    try:
                        found=witness(before['status'],after['status'],before['proof'],after['proof'],events,wire,before['driver'],after['driver'])
                    except ValueError:pass
                    if found:
                        status_matches(after['status'],after['proof'])
                        found['tap_latency']=record_physical_latency(io,wire)
                        break
            closed=io.clock();write_new(out/'after.json',after)
            events,wire,voices=io.evidence(mark)
            write_new(out/'touch-bot-evidence.json',dict(events=events,wire=wire,voices=voices))
            return dict(go_observed=True,touch_verified=bool(found),status='passed' if found else 'unverified',
                        opened_at=opened,closed_at=closed,window_seconds=WINDOW,window_complete=closed>=deadline,
                        witness=found,before=before,after=after,contact_precheck=precheck,retryable=False)
    finally:io.command('touch-enable 0')


def main(argv=None):
    import sys
    from duo_smoke import main as smoke_main
    return smoke_main(['run','--part','touch',*(sys.argv[1:] if argv is None else argv)])


if __name__=='__main__':raise SystemExit(main())
