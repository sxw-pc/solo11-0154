import json
from .store import connect
from . import classification,transport,closure,incident_notice
def build(db):
    batches=[r[0] for r in db.execute('SELECT DISTINCT batch FROM waste_profile UNION SELECT DISTINCT batch FROM manifest UNION SELECT DISTINCT batch FROM weigh_in ORDER BY batch')]
    return [{'batch':b,'classification':dict(x) if (x:=db.execute('SELECT * FROM classification WHERE batch=?',(b,)).fetchone()) else None,'transport':dict(x) if (x:=db.execute('SELECT * FROM transport WHERE batch=?',(b,)).fetchone()) else None,'closure':dict(x) if (x:=db.execute('SELECT * FROM closure WHERE batch=?',(b,)).fetchone()) else None,'investigations':[dict(x) for x in db.execute('SELECT * FROM investigation WHERE batch=?',(b,))],'history':[dict(x) for x in db.execute('SELECT subsystem,state,reason,source_id FROM decision_history WHERE batch=? ORDER BY id',(b,))]} for b in batches]
def audit(db):
    replay=connect(':memory:');handlers={'waste-import':classification.import_data,'manifest-import':transport.import_data,'receipt-import':closure.import_data,'reception-import':incident_notice.import_data,'authorize-correction':incident_notice.register_authorization,'issue-correction':incident_notice.issue_correction,'record-readback':incident_notice.record_readback,'record-occupation':incident_notice.record_occupation}
    for r in db.execute('SELECT command,raw FROM file_journal ORDER BY id'):handlers[r['command']](replay,json.loads(r['raw']))
    equal=build(db)==build(replay);replay.close();return {'ok':equal,'files':db.execute('SELECT count(*) FROM file_journal').fetchone()[0]}
