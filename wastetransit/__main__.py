import argparse,json
from pathlib import Path
from .store import connect
from .source import read,file_record
from . import classification,transport,closure,report,assay
HANDLERS={'waste-import':classification.import_data,'manifest-import':transport.import_data,'receipt-import':closure.import_data,'assay-import':assay.import_data}
def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=[*HANDLERS,'demo','report','audit','assay-plan']);p.add_argument('--db',default='state.sqlite');p.add_argument('--input');p.add_argument('--batch');p.add_argument('--budget',type=int,default=2);p.add_argument('--window',default='W1');a=p.parse_args();db=connect(a.db)
    if a.command=='report':print(json.dumps(report.build(db),ensure_ascii=False,indent=2));return
    if a.command=='audit':print(json.dumps(report.audit(db),ensure_ascii=False,indent=2));return
    if a.command=='assay-plan':
        if not a.batch:p.error('--batch required')
        print(json.dumps(assay.plan(db,a.batch,a.budget,a.window),ensure_ascii=False,indent=2));return
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
