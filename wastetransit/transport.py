from .source import record
from .store import history
def decide(db,batch):
    m=db.execute('SELECT * FROM manifest WHERE batch=? ORDER BY depart_at DESC,id',(batch,)).fetchone()
    if not m:return None
    c=db.execute('SELECT * FROM classification WHERE batch=?',(batch,)).fetchone()
    p=db.execute('SELECT * FROM vehicle_permit WHERE id=?',(m['permit_id'],)).fetchone()
    profile=db.execute('SELECT * FROM waste_profile WHERE id=?',(c['profile_id'],)).fetchone() if c else None
    handoffs=db.execute('SELECT * FROM handoff WHERE manifest_id=? ORDER BY at',(m['id'],)).fetchall()
    if not c or c['state']!='classified':state,reason,liable='classification_hold','classification does not permit dispatch','producer'
    elif not p or p['vehicle']!=m['vehicle'] or p['code']!=profile['code'] or not(p['start']<=m['depart_at']<=p['end']):state,reason,liable='unlicensed','vehicle permit not valid for waste code at departure','producer'
    elif not m['producer_sign'] or not m['carrier_sign'] or m['producer_sign']==m['carrier_sign']:state,reason,liable='unsigned_handoff','producer and carrier must sign independently','producer'
    elif profile['storage_until']<m['depart_at']:state,reason,liable='storage_expired','waste was dispatched after permitted storage period','producer'
    elif m['arrive_at']<m['depart_at'] or len(handoffs)<2 or handoffs[0]['kind']!='departure' or handoffs[-1]['kind']!='arrival' or handoffs[0]['at']<m['depart_at'] or handoffs[-1]['at']>m['arrive_at']:state,reason,liable='sequence_conflict','transport evidence chronology invalid','carrier'
    elif m['route']!=p['route']:state,reason,liable='route_hold','route deviates from permit','carrier'
    elif m['out_kg']<=0:state,reason,liable='weight_hold','outgoing weight must be positive','producer'
    else:state,reason,liable='carrier_responsible','signed transfer makes carrier responsible pending receiving weigh-in','carrier'
    old=db.execute('SELECT * FROM transport WHERE batch=?',(batch,)).fetchone()
    if not old or (old['state'],old['reason'],old['manifest_id'],old['liable_party'])!=(state,reason,m['id'],liable):
        ver=old['version']+1 if old else 1
        db.execute('INSERT INTO transport VALUES(?,?,?,?,?,?) ON CONFLICT(batch) DO UPDATE SET state=excluded.state,reason=excluded.reason,manifest_id=excluded.manifest_id,liable_party=excluded.liable_party,version=excluded.version',(batch,state,reason,m['id'],liable,ver))
        history(db,'transport',batch,state,reason,m['id'])
    from .closure import decide as close
    close(db,batch)
    from .incident_notice import refresh
    refresh(db,batch)
    return dict(db.execute('SELECT * FROM transport WHERE batch=?',(batch,)).fetchone())
def import_data(db,data):
    touched=set()
    with db:
        for p in data.get('permits',[]):
            if record(db,'vehicle-permit',p):db.execute('INSERT INTO vehicle_permit VALUES(?,?,?,?,?,?,?)',(p['id'],p['vehicle'],p['driver'],p['code'],p['start'],p['end'],p['route']))
            touched.update(x['batch'] for x in db.execute('SELECT batch FROM manifest WHERE permit_id=?',(p['id'],)))
        for m in data.get('manifests',[]):
            if record(db,'manifest',m):db.execute('INSERT INTO manifest VALUES(?,?,?,?,?,?,?,?,?,?)',(m['id'],m['batch'],m['vehicle'],m['permit_id'],m['route'],m['depart_at'],m['arrive_at'],m['out_kg'],m.get('producer_sign'),m.get('carrier_sign')))
            touched.add(m['batch'])
        for h in data.get('handoffs',[]):
            if record(db,'handoff',h):db.execute('INSERT INTO handoff VALUES(?,?,?,?,?)',(h['id'],h['manifest_id'],h['kind'],h['at'],h['signer']))
            touched.update(x['batch'] for x in db.execute('SELECT batch FROM manifest WHERE id=?',(h['manifest_id'],)))
        return [decide(db,b) for b in sorted(touched) if db.execute('SELECT 1 FROM manifest WHERE batch=?',(b,)).fetchone()]

