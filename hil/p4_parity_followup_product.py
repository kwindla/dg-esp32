"""AEC-off content-only adaptation, room gate and strict retained replay."""
import argparse
from contextlib import contextmanager
from pathlib import Path

import board_smoke as smoke
import p4_parity as core
import p4_step14 as step14
from p4_parity import load,digest,require

ORIGINAL_SMOKE=core.smoke_result


def turn_checks(events,kind,aec,*,calibration=False):
    from p4_smoke_rescore import turn_checks as aec_checks
    if aec:return aec_checks(events,kind,aec,calibration=calibration)
    require(calibration is False,'content-only mode cannot enable acquisition calibration')
    # This is the same pure checker called by the AEC content adapter. Only
    # LLM findings soften; its AEC-off STT, mic gate and interruption rules stay.
    result=step14._turn_checks(events,kind,False,calibration=True)
    require(smoke.turn_finished(events,1),'reply to latest user did not complete')
    assistants=[e for e in events if e.get('kind')=='aggregator-assistant-turn-stopped']
    require(assistants[-1].get('interrupted') is not True,'final reply self-interrupted')
    findings=result.pop('bot_content_findings')
    result.update(stt_passed=True,turn_completed=True,expected_answer='Paris' if kind=='france' else 'one through twenty',
        llm_content='LLM content: report-only' if findings else 'pass',llm_content_findings=findings,
        decision='decision-2026-11-03-step16-prereq-retry')
    return result


def findings(row):
    result=[]
    for i,turn in enumerate(row['turns'],1):
        checked=turn_checks(turn['events'],turn['kind'],False)
        result.extend(dict(f,turn=i,session='main' if i<=12 else 'resume' if i==13 else 'cold',
            expected_answer=checked['expected_answer']) for f in checked['llm_content_findings'])
    return result


def verify_off(directory):
    from p4_parity_followup import DECISION,DECISION_SHA
    directory=Path(directory);row=load(directory/'result.json')
    require(row.get('llm_content_mode')=='report-only' and row.get('content_decision_sha256')==DECISION_SHA and
        row.get('content_decision_file')==str(DECISION) and digest(DECISION)==DECISION_SHA,'content mode/decision differs')
    require(row.get('room',{}).get('noise_contaminated') is False,'clean room preflight required')
    room_file=directory.with_name(directory.name+'-room')/'result.json'
    require(load(room_file)==row['room'],'raw room result/summary differs')
    require(row.get('llm_content_findings')==findings(row),'retained LLM findings differ from event replay')
    for session in row['sessions']:
        lines=(directory/(session['label']+'-serial.log')).read_text().splitlines()
        from wire_contract import parse_capture
        capture=parse_capture(lines)
        require(len(capture.intervals)==1,'one measured product generation required')
        gen=capture.intervals[0]['gen']
        import json
        raw=[json.loads(line[5:]) for line in lines if line.startswith('PERF {')]
        samples=[load(p) for p in sorted(directory.glob('perf-*.json'))]
        # The session file is LiveIO.finish()'s own begin/close byte window.
        # Generations restart on cold boot: resolve raw session membership
        # BEFORE selecting the final before/after pair from saved snapshots.
        samples=[s for s in samples if s.get('generation')==gen and s in raw]
        require(len(samples)>=2,'before/after resource snapshots unavailable')
        samples=samples[-2:]  # begin() also saves an initial identity snapshot.
        require(all(s in raw for s in samples),'before/after resource snapshots detached from measured serial')
        require(smoke.resource_check(samples,row['board'])==session['resources'],'retained resource summary differs')
    return row


@contextmanager
def replay_mode():
    previous=core.smoke_result,step14.turn_checks
    def result(directory,board,variant):
        if variant!='aec-demo':
            row=load(Path(directory)/'result.json')
            if row.get('acquisition_passed') is True:verify_off(directory)
        return ORIGINAL_SMOKE(directory,board,variant)
    core.smoke_result=result;step14.turn_checks=turn_checks
    try:yield
    finally:core.smoke_result,step14.turn_checks=previous


@contextmanager
def content_mode():
    previous=smoke.turn_checks;smoke.turn_checks=turn_checks
    try:yield
    finally:smoke.turn_checks=previous


def smoke_main(argv):
    from p4_parity_followup import checked_plan,scope,DECISION,DECISION_SHA
    import p4_parity_smoke as entry
    import p4_aec_product
    from p4_product import candidate_storage
    import board_smoke_bounds
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board',choices=core.BOARDS,required=True)
    parser.add_argument('--variant',choices=core.VARIANTS,required=True)
    for name in ('plan','out','bot-dir','bot-identity'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    parser.add_argument('--erle-report',type=Path)
    parser.add_argument('--llm-content',choices=('off','report-only'),default='report-only')
    args=parser.parse_args(argv);plan=checked_plan(args.plan);scope('smoke',args.board,args.variant)
    require(args.llm_content==plan['llm_content_mode'],'content mode differs from frozen follow-up plan')
    args.out=core.output_path(plan,args.out)
    require(load(args.bot_identity)==plan['product_identity'],'one planned product bot required')
    entry.preflight(args,plan)
    with __import__('p4_parity_campaign').attempt(args.plan,'smoke',args.board,args.variant,args.out):
        if args.variant=='aec-demo':return entry.execute(args,plan)
        args.bundle=Path(plan['images'][args.board+'/'+args.variant]['bundle'])
        args.bounds=smoke.BOUNDS_FILE;args.pair_id='step16-followup-'+args.variant
        args.reference_only=args.calibration=args.headless=False
        args.p4_restore_bundle=args.p4_restore_elf=None;args.p4_factory_backup=smoke.FACTORY_BACKUP
        @p4_aec_product.record_untouched('result.json')
        def execute(a):
            from alsa import record_room_baseline
            room=record_room_baseline(a.out.with_name(a.out.name+'-room'),'AEC-off product smoke')
            a._preflight_room=room
            require(room['noise_contaminated'] is False,'room preflight failed; retain untouched noisy original')
            manifest=core.pinned_bundle(a.bundle,a.board,a.variant);identity=load(a.bot_identity)
            class OffIO(smoke.LiveIO):
                def flash(self,bundle,manifest,label):
                    if label=='candidate':a._device_touched=True
                    return super().flash(bundle,manifest,label)
                def boot(self,board):
                    at=super().boot(board)
                    if not (self.out/'boot.log').exists():
                        (self.out/'boot.log').write_text('\n'.join(line for _,_,line in board.lines_since(at))+'\n')
                    return at
            with step14.restoring(plan['restore_record'],plan['restore_record_sha256']),step14.adapters(),\
                    candidate_storage(a.board,a.bundle,a.out),content_mode():
                io=OffIO(a,a.out,manifest,identity)
                result=board_smoke_bounds.execute(a,backend=io)
            result.update(room=room,llm_content_mode='report-only',content_decision_file=str(DECISION),
                content_decision_sha256=DECISION_SHA,helper_sha256=digest(Path(__file__)))
            result['llm_content_findings']=findings(result)
            from p4_parity_serial import candidate_serial
            try:candidate_serial(a.out)
            except ValueError as exc:result.update(acquisition_passed=False,passed=False,error=str(exc))
            smoke.write(a.out/'result.json',result)
            import shutil
            shutil.copyfile(a.bundle/'manifest.json',a.out/'image.json')
            return 0 if result['acquisition_passed'] and not result.get('error') else 1
        return execute(args)


TOUCH_INSTRUCTIONS=('Two short taps on the same spot, each under 0.25 s; '
    'the second ends 0.08–0.4 s after the first. Relay TAP NOW immediately.')


@contextmanager
def touch_prompt():
    """Keep the frozen acquisition; replace its prompt before its timer starts."""
    import builtins
    import p4_parity_touch as entry
    previous=entry.__dict__.get('print')
    def relay(*args,**kwargs):
        if args and str(args[0]).startswith('USER ACTION:'):
            return builtins.print('TAP NOW: '+TOUCH_INSTRUCTIONS+' Window: 45 seconds.',flush=True)
        return builtins.print(*args,**kwargs)
    entry.print=relay
    try:yield
    finally:
        if previous is None:del entry.print
        else:entry.print=previous


def touch_main(argv):
    from p4_parity_followup import checked_plan
    import p4_parity_touch as entry
    parser=argparse.ArgumentParser(add_help=False);parser.add_argument('--plan',type=Path,required=True)
    known,_=parser.parse_known_args(argv);checked_plan(known.plan)
    print('F11 instructions BEFORE the window opens: '+TOUCH_INSTRUCTIONS,flush=True)
    previous=entry.checked_plan;entry.checked_plan=checked_plan
    try:
        with touch_prompt():return entry.main(argv)
    finally:entry.checked_plan=previous
