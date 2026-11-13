"""Pure selectors for overlapping per-connection display command bridges."""


def require(value,message):
    if not value:raise ValueError(message)


def display_session(events,device_epoch):
    require(type(device_epoch) is int and device_epoch>0,'missing device display epoch')
    ready=next((e for e in events if e.get('event')=='display-ready' and e.get('epoch')==device_epoch),None)
    if ready is None:return None
    require(isinstance(ready.get('pc_id'),str) and ready['pc_id'],'missing bot display-ready pc_id')
    return dict(pc_id=ready['pc_id'],epoch=ready['epoch'],ready=ready)


def display_sent(events,session,rid):
    return next((e for e in events if e.get('event')=='display-sent' and
        e.get('pc_id')==session['pc_id'] and e.get('packet',{}).get('d',{}).get('epoch')==session['epoch'] and
        e.get('packet',{}).get('d',{}).get('id')==rid),None)


def display_result(events,session,packet):
    return next((e for e in events if e.get('event')=='display-result' and
        e.get('pc_id')==session['pc_id'] and e.get('result',{}).get('epoch')==packet['epoch']==session['epoch'] and
        e.get('result',{}).get('seq')==packet['seq']),None)


def stale_bridge_sends(events,session):
    return [e for e in events if e.get('event')=='display-sent' and
        (e.get('pc_id')!=session['pc_id'] or e.get('packet',{}).get('d',{}).get('epoch')!=session['epoch'])]
