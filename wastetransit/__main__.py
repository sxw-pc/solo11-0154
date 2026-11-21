import argparse,json
from pathlib import Path
from .store import connect
from .source import read,file_record
from . import classification,transport,closure,report,residue_trace
HANDLERS={
 'waste-import':classification.import_data,
 'manifest-import':transport.import_data,
 'receipt-import':closure.import_data,
 'residue-import':residue_trace.import_data,
 'interlock-import':residue_trace.import_interlock,
 'station-import':residue_trace.import_station,
}
def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=[*HANDLERS,'demo','report','audit','cycle-query'])
    p.add_argument('--db',default='state.sqlite');p.add_argument('--input');p.add_argument('--batch')
    a=p.parse_args();db=connect(a.db)
    if a.command=='report':print(json.dumps(report.build(db),ensure_ascii=False,indent=2));return
    if a.command=='audit':print(json.dumps(report.audit(db),ensure_ascii=False,indent=2));return
    if a.command=='cycle-query':
        print(json.dumps(residue_trace.query(db,a.batch),ensure_ascii=False,indent=2));return
    if a.command=='demo':
        for cmd,name in [('waste-import','waste_ok.json'),('manifest-import','manifest_ok.json'),('receipt-import','receipt_ok.json')]:
            doc,raw=read(Path(__file__).resolve().parents[1]/'fixtures'/name);HANDLERS[cmd](db,doc)
            with db:file_record(db,cmd,raw)
        print(json.dumps(report.build(db),ensure_ascii=False,indent=2));return
    if not a.input:p.error('--input required')
    doc,raw=read(a.input);result=HANDLERS[a.command](db,doc)
    with db:file_record(db,a.command,raw)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
