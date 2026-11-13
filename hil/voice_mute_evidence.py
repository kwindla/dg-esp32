"""Compare server speaking/mute intervals with physical playout gate edges."""
def mute_evidence(events, gates):
    def transitions(begin,end):
        selected=[];active=False
        for event in sorted(events,key=lambda e:e['t']):
            if event['event'] not in {begin,end}:continue
            new=event['event']==begin
            if new!=active:selected.append(event)
            active=new
        return selected
    # Pipecat broadcasts separate upstream/downstream frames with distinct
    # IDs. Keep both raw events, but count a speaking-state transition once.
    boundaries=transitions('bot-audio-start','bot-audio-end')
    starts=[e for e in boundaries if e['event']=='bot-audio-start']
    stops=[e for e in boundaries if e['event']=='bot-audio-end']
    intervals=[]
    for start in starts:
        end=next((e for e in stops if e['t']>=start['t']),None)
        intervals.append((start['t'],end['t'] if end else None))
    def speaking(t):
        return any(begin<=t and (end is None or t<end) for begin,end in intervals)
    tails=[]
    for end in stops:
        opening=next((t for t,state in gates if state=='open' and t>=end['t']),None)
        if opening is not None:
            tails.append(dict(server_stop=end['t'],board_open=opening,
                              tail_ms=1000*(opening-end['t'])))
    return dict(bot_intervals=intervals,
        mute_edges=transitions('user-mute-start','user-mute-stop'),
        board_tail_after_server_stop=tails,
        user_turns_during_bot_speech=[e for e in events if e['event']=='user-turn-started'
            and (e.get('during_bot_speech',False) or speaking(e['t']))],
        interruptions_during_bot_speech=[e for e in events if e['event']=='interruption'
            and (e.get('during_bot_speech',False) or speaking(e['t']))],
        gate_opens_during_bot_speech=[t for t,state in gates if state=='open' and speaking(t)],
        raw_transcripts_during_bot_speech=[e for e in events if e['event'] in {'final-transcript','interim-transcript'} and speaking(e['t'])],
        raw_transcripts_in_board_tail=[e for e in events if e['event'] in {'final-transcript','interim-transcript'}
            and any(t['server_stop']<=e['t']<t['board_open'] for t in tails)],
        tail_user_turns=[e for e in events if e['event']=='user-turn-started'
            and any(t['server_stop']<=e['t']<t['board_open'] for t in tails)])
