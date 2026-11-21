from .source import record
from .store import history
def decide(db,batch):
    weights=db.execute('SELECT * FROM weigh_in WHERE batch=? ORDER BY at DESC,id',(batch,)).fetchall()
    if not weights:return None
    w=weights[0]
    t=db.execute('SELECT * FROM transport WHERE batch=?',(batch,)).fetchone()
    m=db.execute('SELECT * FROM manifest WHERE id=?',(t['manifest_id'],)).fetchone() if t else None
    c=db.execute('SELECT * FROM classification WHERE batch=?',(batch,)).fetchone()
    profile=db.execute('SELECT * FROM waste_profile WHERE id=?',(c['profile_id'],)).fetchone() if c else None
    permit=db.execute('SELECT * FROM receiver_permit WHERE id=?',(w['permit_id'],)).fetchone()
    delta=round(m['out_kg']-w['net_kg'],3) if m else None
    if not t or t['state']!='carrier_responsible':state,reason,liable='transport_hold','no lawful transfer chain',t['liable_party'] if t else 'producer'
    elif not permit or permit['receiver']!=w['receiver_id'] or permit['code']!=profile['code'] or not(permit['start']<=w['at']<=permit['end']):state,reason,liable='receiver_unlicensed','receiver lacks waste-code permit at arrival','carrier'
    elif w['at']<m['arrive_at'] or not w['scale_witness'] or w['scale_witness']==m['carrier_sign']:state,reason,liable='weigh_invalid','receiving scale must be later and independent','carrier'
    elif w['net_kg']<=0:state,reason,liable='weigh_invalid','receiving net weight must be positive','receiver'
    elif abs(delta)>5:
        arrival=db.execute('SELECT * FROM handoff WHERE manifest_id=? AND kind="arrival" ORDER BY at DESC LIMIT 1',(m['id'],)).fetchone()
        liable='receiver' if arrival and arrival['signer']=='receiver' else 'carrier'
        state,reason='difference_investigation','weight difference exceeds 5 kg tolerance at last trusted transfer'
    else:state,reason,liable='closed','permitted receiver accepted weight within tolerance','receiver'
    old=db.execute('SELECT * FROM closure WHERE batch=?',(batch,)).fetchone()
    if not old or (old['state'],old['reason'],old['weight_delta'],old['liable_party'])!=(state,reason,delta,liable):
        ver=old['version']+1 if old else 1
        db.execute('INSERT INTO closure VALUES(?,?,?,?,?,?) ON CONFLICT(batch) DO UPDATE SET state=excluded.state,reason=excluded.reason,weight_delta=excluded.weight_delta,liable_party=excluded.liable_party,version=excluded.version',(batch,state,reason,delta,liable,ver))
        history(db,'closure',batch,state,reason,w['id'])
        if state=='difference_investigation':db.execute('INSERT OR IGNORE INTO investigation VALUES(?,?,?,?,?)',('INV-'+w['id'],batch,w['at'],'receiving-scale','unexplained weight difference'))
        if state=='difference_investigation':db.execute('INSERT INTO future_dispatch_hold VALUES(?,?,?,?) ON CONFLICT(batch) DO UPDATE SET state=excluded.state,reason=excluded.reason,closure_version=excluded.closure_version',(batch,'blocked','unresolved receiving difference',ver))
        elif state=='closed' and db.execute('SELECT 1 FROM future_dispatch_hold WHERE batch=?',(batch,)).fetchone():db.execute('UPDATE future_dispatch_hold SET state="cleared",reason="independent reweigh within tolerance",closure_version=? WHERE batch=?',(ver,batch))
    from .capture_desk import reconcile
    reconcile(db,batch)
    return dict(db.execute('SELECT * FROM closure WHERE batch=?',(batch,)).fetchone())
def import_data(db,data):
    touched=set()
    with db:
        for p in data.get('receiver_permits',[]):
            if record(db,'receiver-permit',p):db.execute('INSERT INTO receiver_permit VALUES(?,?,?,?,?,?)',(p['id'],p['receiver'],p['code'],p['method'],p['start'],p['end']))
            touched.update(x['batch'] for x in db.execute('SELECT batch FROM weigh_in WHERE permit_id=?',(p['id'],)))
        for w in data.get('weights',[]):
            if record(db,'weigh-in',w):db.execute('INSERT INTO weigh_in VALUES(?,?,?,?,?,?,?,?)',(w['id'],w['batch'],w['receiver_id'],w['permit_id'],w['at'],w['net_kg'],w['scale_witness'],w.get('kind','first')))
            touched.add(w['batch'])
        return [decide(db,b) for b in sorted(touched) if db.execute('SELECT 1 FROM weigh_in WHERE batch=?',(b,)).fetchone()]

