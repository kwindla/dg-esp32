"""Host-only decision regressions using recorder events and real gate validators."""
import copy
import json

import pytest

import board_smoke as smoke
import p4_aec_product as product
import p4_smoke_rescore as rescore
import p4_step14 as step14
from make_bundle import digest
from test_board_smoke import events, FakeIO, image, options, perf
from test_p4_aec_product import pair_fixture
from test_p4_audio_fix4 import cost_fixture
from wire_contract import ContractError


def changed_events(kind='france', stt=None, answer="I don't know, but Paris might be the answer."):
    rows = events(kind, True)
    users = [e for e in rows if e['kind'] == 'aggregator-user-turn-stopped']
    assistants = [e for e in rows if e['kind'] == 'aggregator-assistant-turn-stopped']
    if stt is not None:
        users[-1]['text'] = stt
    assistants[-1]['text'] = answer
    return rows


@pytest.mark.parametrize('kind,stt,answer', [
    ('france', None, 'No idea.'),
    ('france', None, "I don't know, but Paris is the capital of France."),
    ('long', None, 'I prefer not to count.'),
    ('talkover', 'What about Italy?', 'Florence?'),
    ('talkover', 'Italy', "I don't know, maybe Rome."),
])
def test_content_only_is_report_only_with_expected_answer_and_text(kind, stt, answer):
    result = rescore.turn_checks(changed_events(kind, stt, answer), kind, True)
    assert result['passed'] and result['stt_passed'] and result['turn_completed']
    assert result['llm_content'] == 'LLM content: report-only'
    assert result['assistants'][-1] == answer and result['expected_answer']


@pytest.mark.parametrize('kind,stt', [('france', 'What is the capital of Spain?'),
                                    ('long', 'Please tell a story.'),
                                    ('talkover', 'What about illness?')])
def test_stt_miss_never_softened(kind, stt):
    with pytest.raises(ContractError):
        rescore.turn_checks(changed_events(kind, stt), kind, True)


@pytest.mark.parametrize('mutation', ['self-interrupt', 'no-stop', 'early-stop', 'pipeline-error', 'ended', 'interrupted-reply'])
def test_completion_and_other_turn_gates_stay_strict(mutation):
    rows = changed_events()
    if mutation == 'self-interrupt':
        rows += [dict(kind='frame', frame='InterruptionFrame', direction='DOWNSTREAM', bot_active=True, monotonic_ns=100)]
    elif mutation == 'no-stop':
        rows = [e for e in rows if e.get('frame') != 'BotStoppedSpeakingFrame']
    elif mutation == 'early-stop':
        for e in rows:
            if e.get('frame') == 'BotStoppedSpeakingFrame': e['monotonic_ns'] = 1
    elif mutation == 'pipeline-error':
        rows += [dict(kind='frame', frame='ErrorFrame', direction='DOWNSTREAM', monotonic_ns=100)]
    elif mutation == 'ended':
        rows += [dict(kind='session-end')]
    else:
        rows[-1]['interrupted'] = True
    with pytest.raises(ContractError):
        rescore.turn_checks(rows, 'france', True)


def test_mode_off_retains_strict_step14_and_context_restores_on_error():
    previous = smoke.turn_checks
    with step14.adapters():
        strict = smoke.turn_checks
        with rescore.content_mode('off'):
            with pytest.raises(ContractError): smoke.turn_checks(changed_events(), 'france', True)
        with pytest.raises(RuntimeError):
            with rescore.content_mode('report-only'):
                assert smoke.turn_checks(changed_events(), 'france', True)['passed']
                raise RuntimeError('abort')
        assert smoke.turn_checks is strict
    assert smoke.turn_checks is previous


@pytest.mark.parametrize('mutation', [None, 'stt', 'illness', 'unmeasured'])
def test_frozen_executor_continues_all_later_steps_only_for_content(tmp_path, mutation):
    root, manifest = image(tmp_path, aec=True, demo=True)
    args = options(tmp_path, root, calibration=False)
    class IO(FakeIO):
        def turn(self, board, kind, index):
            self.calls.append(kind)
            self.now += 1
            rows = changed_events(kind, 'What about illness?' if mutation == 'illness' and kind == 'talkover'
                                  else 'Spain?' if mutation == 'stt' and index == 1 else None,
                                  'No idea.' if kind in ('france', 'talkover') else 'One through twenty')
            return dict(smoke.turn_checks(rows, kind, True, calibration=self.args.calibration), kind=kind, events=rows)
    io = IO(args, manifest)
    with step14.adapters(), rescore.content_mode('report-only'):
        row = smoke.execute(args, backend=io)
    if mutation == 'unmeasured': row['sessions'].pop()
    rescore.finalize_live(row)
    assert row['passed'] is (mutation is None)
    if mutation is None:
        assert len(row['turns']) == 14 and [s['label'] for s in row['sessions']] == ['main', 'resume', 'cold']
        assert row['llm_content_findings'] and 'restored-assets' in io.calls
    elif mutation == 'unmeasured':
        assert 'cold:wire/uplink' in row['unmeasured_steps']
    else:
        assert row['error'] and row['unmeasured_steps']


def retained_fixture(tmp_path, variant='aec-normal'):
    _, directory = pair_fixture(tmp_path, variant)
    path = directory/'result.json'
    row = json.loads(path.read_text())
    row.update(passed=True, calibration=False, reference_only=False, headless=False, cleanup_errors=[],
               room=dict(noise_contaminated=False), turns=[], task_stack_reports=[])
    manifest = json.loads((directory/'image.json').read_text())
    erle_source = tmp_path/'erle-source.json'
    erle_source.write_text('{}')
    row['erle_report'] = dict(available=True, report_only=True, profile=variant, gain_db=15,
        elf_sha256=manifest['elf_sha256'], sources={str(erle_source): digest(erle_source)},
        plan_file=str(erle_source), plan_sha256=digest(erle_source),
        delay_file=str(erle_source), delay_sha256=digest(erle_source))
    sample = cost_fixture(tmp_path/'cost')
    sample.update(perf(), generation=1)
    # Use real product stack floor metadata from the cost fixture.
    stacks = cost_fixture(tmp_path/'cost2')
    sample['tasks'] = copy.deepcopy(stacks['tasks'])
    sample.update({k: stacks[k] for k in ('tx_stack', 'decode_stack', 'owner_stack', 'rx_stack', 'rx_stack_bytes')})
    for session in row['sessions']:
        label = session['label']
        serial = directory/(label+'-serial.log')
        serial.write_text(serial.read_text()+('PERF '+json.dumps(sample)+'\n')*2)
        lines = serial.read_text().splitlines()
        bot = (directory/(label+'-bot.log')).read_text().splitlines()
        capture, wire, timing = smoke.check_capture(lines, bot, manifest=manifest, board='p4xc',
            bounds=smoke.bounds_for(smoke.BOUNDS_FILE, variant))
        wire_path = directory/(label+'-wire.log')
        wire_path.write_text('\n'.join(wire)+'\n')
        session.update(resources=dict(passed=True), wire_file=wire_path.name, wire_sha256=digest(wire_path),
                       intervals=capture.intervals[0], timing=timing)
        row['task_stack_reports'].append(dict(label=label, stack_checks_passed=True))
    for index, (_, kind) in enumerate(rescore.script(variant), 1):
        rows = changed_events(kind, answer='No idea.' if index == 1 else 'Paris' if kind == 'france'
                              else 'Rome' if kind == 'talkover' else 'One through twenty')
        turn = dict(kind=kind, events=rows)
        row['turns'].append(turn)
        (directory/f'turn-{index:02d}.json').write_text(json.dumps(turn))
    if variant == 'aec-demo': row.update(hold=dict(held=True), cold_assets=dict(passed=True))
    (directory/'restore.json').write_text(json.dumps(row['restore']))
    path.write_text(json.dumps(row))
    return directory, row


@pytest.mark.parametrize('variant', ['aec-normal', 'aec-demo'])
def test_rescore_complete_content_only_failure_can_pass_without_rewriting_original(tmp_path, variant):
    directory, row = retained_fixture(tmp_path, variant)
    row.update(passed=False, acquisition_passed=False, error='ContractError: France/Paris turn failed')
    (directory/'result.json').write_text(json.dumps(row))
    original = digest(directory/'result.json')
    result = rescore.rescore(directory)
    assert result['passed'], result['failures']
    assert result['turns'][0]['verdict'] == 'LLM content: report-only'
    assert result['source_result_sha256'] == original
    assert rescore.main([str(directory)]) == 0
    assert digest(directory/'result.json') == original
    with pytest.raises(ContractError, match='overwrite'): rescore.main([str(directory)])


@pytest.mark.parametrize('mutation', ['missing-session', 'stt', 'illness', 'wire', 'heap', 'stack', 'cleanup',
                                     'restore', 'other-error', 'turn-file', 'cold-assets', 'missing-turn',
                                     'erle-missing', 'erle-image', 'erle-source'])
def test_rescore_cannot_promote_other_failures_or_unmeasured_evidence(tmp_path, mutation):
    directory, row = retained_fixture(tmp_path, 'aec-demo')
    if mutation == 'missing-session': row['sessions'].pop()
    elif mutation in ('stt', 'illness'):
        index = 1 if mutation == 'stt' else 2
        turn = row['turns'][index-1]
        users = [e for e in turn['events'] if e['kind'] == 'aggregator-user-turn-stopped']
        users[-1]['text'] = 'Spain' if mutation == 'stt' else 'What about illness?'
        (directory/f'turn-{index:02d}.json').write_text(json.dumps(turn))
    elif mutation in ('wire', 'heap', 'stack'):
        path = directory/'main-serial.log'
        text = path.read_text()
        if mutation == 'wire': text = text.replace('"aec":true', '"aec":false')
        else:
            lines = []
            for line in text.splitlines():
                if line.startswith('PERF '):
                    sample = json.loads(line[5:])
                    if mutation == 'heap': sample['failed_allocations']['internal'] = 1
                    else: sample['tx_stack'] = 4095
                    line = 'PERF '+json.dumps(sample)
                lines.append(line)
            text = '\n'.join(lines)+'\n'
        path.write_text(text)
    elif mutation == 'cleanup': row['cleanup_errors'] = ['failed']
    elif mutation == 'restore': row['restore']['clean'] = False
    elif mutation == 'other-error': row['error'] = 'TimeoutError: turn did not complete'
    elif mutation == 'turn-file': (directory/'turn-01.json').unlink()
    elif mutation == 'cold-assets': row.pop('cold_assets')
    elif mutation == 'missing-turn': row['turns'].pop()
    elif mutation == 'erle-missing': row.pop('erle_report')
    elif mutation == 'erle-image': row['erle_report']['elf_sha256'] = 'b'*64
    elif mutation == 'erle-source': row['erle_report']['plan_sha256'] = 'b'*64
    (directory/'result.json').write_text(json.dumps(row))
    result = rescore.rescore(directory)
    assert not result['passed']
    assert result['failures'] or result['unmeasured_steps']


def test_failed_recorder_classifies_reply_but_never_supplies_final_session_gates(tmp_path):
    directory, row = retained_fixture(tmp_path, 'aec-demo')
    row['turns'] = []
    row['sessions'] = []
    row.update(passed=False, acquisition_passed=False, error='ContractError: France/Paris turn failed')
    rows = changed_events(answer='No idea.')
    rows += [dict(kind='rtvi-output', message=dict(type='disconnect-bot')),
             dict(kind='frame', frame='EndFrame', direction='DOWNSTREAM')]
    (directory/'failed-bot-events.json').write_text(json.dumps(rows))
    (directory/'result.json').write_text(json.dumps(row))
    result = rescore.rescore(directory)
    assert not result['passed'] and result['turns'][0]['verdict'] == 'LLM content: report-only'
    assert 'main:turn-01:france' not in result['unmeasured_steps']
    assert 'main:turn-02:talkover' in result['unmeasured_steps']
    assert 'cold:wire/uplink' in result['unmeasured_steps']


def test_frozen_calibration_does_not_gate_or_relax_non_content_requirements():
    """The additive adapter relies on this frozen checker's limited relaxation.

    Check execution guards and predicates, allowing calibration-specific error
    wording. The behavioral matrix below also exercises the actual failures.
    """
    import ast
    import inspect
    source=ast.parse(inspect.getsource(step14._turn_checks))
    class Guard(ast.NodeVisitor):
        requirements=0
        def visit_FunctionDef(self,node):
            if node.name!='content':self.generic_visit(node)
        def visit_If(self,node):
            if any(isinstance(n,ast.Name) and n.id=='calibration' for n in ast.walk(node.test)):
                assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='require'
                    for branch in node.body+node.orelse for n in ast.walk(branch)), 'calibration gates a non-content requirement'
            self.generic_visit(node)
        def visit_Call(self,node):
            if isinstance(node.func,ast.Name) and node.func.id=='require':
                self.requirements+=1
                assert not any(isinstance(n,ast.Name) and n.id=='calibration' for n in ast.walk(node.args[0])), \
                    'calibration relaxes a non-content predicate'
            self.generic_visit(node)
    guard=Guard();guard.visit(source)
    assert guard.requirements>=8


@pytest.mark.parametrize('mutation',[
    'no-bot-start','no-bot-stop','missing-active','no-user-text','no-assistant-text','no-user-start',
    'france-stt','count-stt','italy-stt','no-talkover-interruption','extra-start','aec-off-italy',
    'pipeline-error','missing-direction','session-end'])
def test_frozen_calibration_keeps_every_non_content_failure_hard(mutation):
    kind='talkover' if mutation in ('italy-stt','no-talkover-interruption','aec-off-italy') else 'long' if mutation=='count-stt' else 'france'
    aec=mutation!='aec-off-italy';rows=events(kind,aec)
    if mutation in ('no-bot-start','no-bot-stop','no-user-start'):
        name={'no-bot-start':'BotStartedSpeakingFrame','no-bot-stop':'BotStoppedSpeakingFrame','no-user-start':'UserStartedSpeakingFrame'}[mutation]
        rows=[e for e in rows if e.get('frame')!=name]
    elif mutation=='missing-active':
        for e in rows:
            if e.get('frame')=='UserStartedSpeakingFrame':e.pop('bot_active',None)
    elif mutation in ('no-user-text','no-assistant-text'):
        target='aggregator-user-turn-stopped' if mutation=='no-user-text' else 'aggregator-assistant-turn-stopped'
        rows=[e for e in rows if e['kind']!=target]
    elif mutation in ('france-stt','count-stt','italy-stt'):
        users=[e for e in rows if e['kind']=='aggregator-user-turn-stopped']
        users[-1]['text']='What about illness?' if mutation=='italy-stt' else 'What is the capital of Spain?'
    elif mutation=='no-talkover-interruption':rows=[e for e in rows if e.get('frame')!='InterruptionFrame']
    elif mutation=='extra-start':rows.append(dict(kind='frame',frame='UserStartedSpeakingFrame',direction='DOWNSTREAM',bot_active=False,monotonic_ns=100))
    elif mutation=='aec-off-italy':
        next(e for e in rows if e['kind']=='aggregator-user-turn-stopped')['text']='Count to twenty, Italy'
    elif mutation=='pipeline-error':rows.append(dict(kind='frame',frame='ErrorFrame',direction='DOWNSTREAM',monotonic_ns=100))
    elif mutation=='missing-direction':
        next(e for e in rows if e['kind']=='frame').pop('direction')
    elif mutation=='session-end':rows.append(dict(kind='session-end'))
    for calibration in (False,True):
        with pytest.raises(ContractError):step14._turn_checks(rows,kind,aec,calibration=calibration)
