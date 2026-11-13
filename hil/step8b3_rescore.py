#!/usr/bin/env python3
"""Board-side scoring on retained captures; original files and verdicts retained."""
import argparse,copy,hashlib,json
from pathlib import Path
import numpy as np
from agent_conversation_test import (transcribe_acoustic,answer_matches,one_reply_matches,
    playback_counter_deltas,reply_gate_window,summarize,reference_audio)
from agent_prompts import TURNS,EXTRA
from bench_calibration import active_mask
from playback_quality import classify_turn,jabra_level_gate,decoded_match
from step7_support import pcm,write_pcm,transcribe
from speech_accuracy import speech_accuracy
from esp_serial import ROOT


def save(path,data):path.write_text(json.dumps(data,indent=2)+'\n')
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def rescore_agent(campaign,out):
    original=campaign/'result.json';result=json.loads(original.read_text())
    model=result['response_calibration'];definitions={r['id']:r for r in (*TURNS,*EXTRA)}
    result['rescoring']=dict(original=str(original),sha256=digest(original),
        basis='Board correlation >=0.6, raw board-mic STT answer, Jabra rise >=10 dB and frozen-FIR 1 kHz transferred board-mic SPL within +/-5 dB of the per-level 8b3 midpoint. Jabra correlation/STT informational. Historical decoded PCM absent means direct decode proof unavailable, not inferred from acoustic pass.')
    corrected=[]
    paths=sorted(p for p in campaign.rglob('result.json') if p!=original and 'warmup' not in p.parts)
    for path in paths:
        old=json.loads(path.read_text())
        if 'prompt' not in old:continue
        row=copy.deepcopy(old);relative=path.parent.relative_to(campaign);dest=out/relative;dest.mkdir(parents=True,exist_ok=True)
        row.update(original_checks=copy.deepcopy(old['checks']),original_passed=old['passed'],original_failures=copy.deepcopy(old['failures']),original_sha256=digest(path))
        classify_turn(row);definition=definitions[row['prompt']]
        try:
            board=pcm(path.parent/'turn-board.wav');jabra=pcm(path.parent/'turn.wav')
            segments=row.get('tts_segments',[])
            for r in segments:
                if r['cancelled']:continue
                ref=Path(r['source_path']);at=round(r['offset_s']*16000)
                r['jabra_level']=jabra_level_gate(jabra[at:at+len(pcm(ref))],r['rms_rise_db'],model,target_dbfs=result.get('playback_target_dbfs') if result.get('playback_target_dbfs') is not None else -25)
                r['verified']=r['complete']=r['board']['correlation']>=.6 and r['jabra_level']['passed']
            complete=[r for r in segments if not r['cancelled']]
            row['checks']['playback']=bool(complete) and all(r['verified'] for r in complete) and all(v>0 for v in playback_counter_deltas(row['before_perf'],row['after_perf']).values())
            row['checks']['one_reply']=one_reply_matches(row['events'],segments)
            closed,opened=reply_gate_window(row.get('gate_events',[]))
            row['checks']['gate']=bool(row['checks']['playback'] and closed and opened and closed<=min(r['onset'] for r in complete)+.1 and opened>=max(r['end'] for r in complete)-.1 and row.get('leakage'))
            responses=[]
            for response_id in (sorted({r['response_id'] for r in complete}) if row['acoustic_scoring_eligible'] else []):
                parts=[r for r in complete if r['response_id']==response_id]
                audio=reference_audio(board,parts,'board')
                wav=dest/f'reply-board-{response_id}.wav';write_pcm(wav,audio)
                text=transcribe_acoustic(wav);responses.append(dict(response_id=response_id,text=text,correct=answer_matches(text,definition)))
            row['board_responses']=responses;row['checks']['acoustic_content']=bool(responses) and all(r['correct'] for r in responses)
            row['jabra_transcript_informational']=old.get('jabra_transcript_informational',old.get('acoustic_text'))
            decoded_path=path.parent/'rx.wav'
            if decoded_path.exists():
                row['decoded_pcm_proof']=[decoded_match(pcm(decoded_path),pcm(Path(r['source_path']))) for r in complete]
                row['checks']['playback']=row['checks']['playback'] and all(x['passed'] for x in row['decoded_pcm_proof'])
                if row['acoustic_scoring_eligible'] and complete:
                    jabra_wav=dest/'reply-jabra-full-reference.wav'
                    write_pcm(jabra_wav,reference_audio(jabra,complete,'jabra'))
                    try:row['jabra_full_reference_text']=transcribe_acoustic(jabra_wav);row['jabra_full_reference_correct']=answer_matches(row['jabra_full_reference_text'],definition)
                    except Exception as exc:row['jabra_full_reference_text']=f'Unavailable: {exc}';row['jabra_full_reference_correct']=None
            else:row['decoded_pcm_proof']='unavailable in retained 8d3 turns'
        except Exception as exc:
            row['rescore_error']=f'{type(exc).__name__}: {exc}'
            row['checks']['acoustic_content']=False
        row['passed']=row['acoustic_scoring_eligible'] and all(row['checks'].values())
        row['failures']=[dict(check=k,cause='board-side rescore',detail='Retained original evidence') for k,v in row['checks'].items() if not v]
        if not row['acoustic_scoring_eligible']:row['failures'].append(dict(check='transport',cause=row['reply_transport_gate']['classification'],detail=row['reply_transport_gate']))
        save(dest/'result.json',row);corrected.append(row)
        result['turns']=corrected;result['summary']=summarize(corrected);save(out/'result.json',result)
        print(relative,'old',old['passed'],'board',row['passed'],row.get('board_responses'),flush=True)
    result['original_summary']=json.loads(original.read_text()).get('summary');result['completed']=True
    result['passed']=all(r['passed'] for r in corrected);save(out/'result.json',result)


def rescore_sweep(campaign,out,bank):
    original=campaign/'result.json';result=json.loads(original.read_text());assert result['completed']
    result['rescoring']=dict(original=str(original),sha256=digest(original),basis='Board content, decoded PCM correlation >=0.95, Jabra level only. SDR and level gain tolerances unchanged.')
    manifest=json.loads((ROOT/'server/fixtures/manifest.json').read_text());manifest.update(json.loads((bank/'manifest.json').read_text()))
    for row in result['utterances']:
        label='step8' if row['target_dbfs'] is None else str(row['target_dbfs']);name=row['name']
        directory=campaign/f"{row['temperature']}-{row['draw']}-{name}-{label}-attempt{row['attempt']}"
        source=(bank if name.startswith('tts_') else ROOT/'server/fixtures')/name
        dest=out/directory.name;dest.mkdir()
        row['original_checks']=copy.deepcopy(row['checks']);row['original_passed']=row['passed']
        at=round(row['chains']['board']['offset_s']*16000);audio=pcm(directory/'board.wav')[at:at+len(pcm(source))]
        wav=dest/'board-segment.wav';write_pcm(wav,audio);text=transcribe(wav)
        row['jabra_transcript_informational']=row['transcript'];row['transcript']=text
        # Fixture manifests use the shared text field.
        expected=manifest[name]['text']
        row['word_accuracy']=speech_accuracy(expected,text)
        row['decoded_content']=decoded_match(pcm(directory/'rx.wav'),pcm(source))
        at=round(row['chains']['jabra']['offset_s']*16000)
        level=jabra_level_gate(pcm(directory/'jabra.wav')[at:at+len(pcm(source))],row['chains']['jabra']['rise_db'],result['response'],target_dbfs=row['target_dbfs'])
        row['jabra_level']=level
        row['checks'].update(correlation=row['chains']['board']['correlation']>=.6,stt=row['word_accuracy']==1,decoded_content=row['decoded_content']['passed'],jabra_level=level['passed'])
        row['passed']=all(row['checks'].values());save(out/'result.json',result)
        print(directory.name,'old',row['original_passed'],'board',row['passed'],text,flush=True)
    result['targets']=[dict(target_dbfs=t,passed=all(r['passed'] for r in result['utterances'] if r['target_dbfs']==t and r['acoustic_scoring_eligible'])) for t in (None,-25,-22,-19) if any(r['target_dbfs']==t for r in result['utterances'])]
    save(out/'result.json',result)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--kind',choices=('agent','sweep'),required=True);p.add_argument('--campaign',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--bank',type=Path)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    if a.kind=='agent':rescore_agent(a.campaign.resolve(),a.out.resolve())
    else:rescore_sweep(a.campaign.resolve(),a.out.resolve(),a.bank.resolve())
if __name__=='__main__':main()
