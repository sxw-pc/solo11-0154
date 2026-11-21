from .source import record
from .store import history
def decide(db,batch):
    profiles=db.execute('SELECT * FROM waste_profile WHERE batch=? ORDER BY version DESC,id',(batch,)).fetchall()
    if not profiles:return None
    top=profiles[0];same=[x for x in profiles if x['version']==top['version']]
    b=db.execute('SELECT * FROM waste_batch WHERE id=?',(batch,)).fetchone()
    hold=db.execute('SELECT * FROM future_dispatch_hold WHERE batch=?',(batch,)).fetchone()
    if len({(x['code'],x['hazard'],x['container_class'],x['withdrawn']) for x in same})>1:state,reason='conflict','parallel classifications disagree'
    elif top['withdrawn']:state,reason='withdrawn','classification withdrawn before transport'
    elif top['version']>1 and hold and hold['state']=='blocked':state,reason='investigation_hold','unresolved weight difference blocks future dispatch classification'
    elif not b:state,reason='missing_batch','declared waste batch missing'
    elif top['issued_by']!='producer_officer' or not top['code'].startswith('HW') or top['code']!=top['hazard']:state,reason='unqualified_code','waste code and hazard not jointly supported'
    elif b['container_class']!=top['container_class'] or top['created_at']>top['storage_until']:state,reason='storage_invalid','container incompatible or storage expired'
    elif b['declared_kg']<=0:state,reason='invalid_weight','batch net weight must be positive'
    else:state,reason='classified','authorized code, container and storage accepted'
    if state=='classified' and db.execute('SELECT 1 FROM assay_requirement WHERE profile_id=?',(top['id'],)).fetchone():
        con=db.execute('SELECT state FROM assay_conclusion WHERE batch=?',(batch,)).fetchone()
        if con and con['state']=='verified':reason='authorized code, container, storage and retained-assay evidence accepted'
        elif con and con['state']=='review':state,reason='assay_review','retained-sample reading above sign-off threshold assigned to review class'
        else:state,reason='assay_hold','retained-sample assay evidence incomplete for declared requirement'
    old=db.execute('SELECT * FROM classification WHERE batch=?',(batch,)).fetchone()
    if not old or (old['state'],old['reason'],old['profile_id'])!=(state,reason,top['id']):
        ver=old['version']+1 if old else 1
        db.execute('INSERT INTO classification VALUES(?,?,?,?,?) ON CONFLICT(batch) DO UPDATE SET state=excluded.state,reason=excluded.reason,profile_id=excluded.profile_id,version=excluded.version',(batch,state,reason,top['id'],ver))
        history(db,'classification',batch,state,reason,top['id'])
    from .transport import decide as next_decide
    next_decide(db,batch)
    return dict(db.execute('SELECT * FROM classification WHERE batch=?',(batch,)).fetchone())
def import_data(db,data):
    touched=set()
    with db:
        for p in data.get('profiles',[]):
            if record(db,'profile',p):
                db.execute('INSERT INTO waste_profile VALUES(?,?,?,?,?,?,?,?,?,?)',(p['id'],p['batch'],p['version'],p['code'],p['hazard'],p['container_class'],p['created_at'],p['storage_until'],p['issued_by'],int(p.get('withdrawn',False))))
                if p.get('assay_required'):db.execute('INSERT OR IGNORE INTO assay_requirement VALUES(?,?)',(p['id'],p['batch']))
            touched.add(p['batch'])
        for b in data.get('batches',[]):
            if record(db,'batch',b):db.execute('INSERT INTO waste_batch VALUES(?,?,?,?,?)',(b['id'],b['owner'],b['declared_kg'],b['container_class'],b['classification_id']))
            touched.add(b['id'])
        return [decide(db,b) for b in sorted(touched) if db.execute('SELECT 1 FROM waste_profile WHERE batch=?',(b,)).fetchone()]
