import copy,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from wastetransit import __file__ as imported_file
from wastetransit.store import connect
from wastetransit import classification,transport,closure
ROOT=Path(__file__).resolve().parents[1]
def fx(name):return json.loads((ROOT/'fixtures'/(name+'.json')).read_text(encoding='utf8'))
class WasteTests(unittest.TestCase):
    def setUp(self):self.db=connect(':memory:')
    def tearDown(self):self.db.close()
    def state(self,table):return self.db.execute('SELECT state FROM '+table+' WHERE batch="WASTE-1"').fetchone()[0]
    def first(self):classification.import_data(self.db,fx('waste_ok'))
    def second(self):self.first();transport.import_data(self.db,fx('manifest_ok'))
    def good(self):self.second();closure.import_data(self.db,fx('receipt_ok'))
    def test_module_local(self):
        print('imported module',imported_file)
        self.assertTrue(str(Path(imported_file).resolve()).startswith(str(ROOT.resolve())))
    def test_three_independent_business_results(self):
        self.good();self.assertEqual(self.state('classification'),'classified')
        self.assertEqual(self.state('transport'),'carrier_responsible')
        self.assertEqual(self.state('closure'),'closed')
        self.assertEqual(self.db.execute('SELECT liable_party FROM closure').fetchone()[0],'receiver')
    def test_waste_code_and_container(self):
        self.assertEqual(classification.import_data(self.db,fx('waste_bad'))[0]['state'],'unqualified_code')
        self.db.close();self.db=connect(':memory:')
        x=fx('waste_ok');x['batches'][0]['container_class']='open-bin'
        self.assertEqual(classification.import_data(self.db,x)[0]['state'],'storage_invalid')
    def test_parallel_reclassification_and_withdrawal(self):
        self.good();p=fx('waste_ok')['profiles'][0].copy();p.update(id='PROFILE-CONFLICT',hazard='HW49')
        classification.import_data(self.db,{'profiles':[p]})
        self.assertEqual(self.state('classification'),'conflict')
        self.assertEqual(self.state('closure'),'transport_hold')
        revised=fx('waste_ok')['profiles'][0].copy();revised.update(id='PROFILE-WITHDRAW',version=2,withdrawn=True)
        classification.import_data(self.db,{'profiles':[revised]})
        self.assertEqual(self.state('classification'),'withdrawn')
    def test_vehicle_permit_dual_sign_and_route(self):
        self.first();transport.import_data(self.db,fx('manifest_bad'))
        self.assertEqual(self.state('transport'),'route_hold')
        self.db.close();self.db=connect(':memory:');self.first()
        x=fx('manifest_ok');x['manifests'][0]['carrier_sign']=None
        self.assertEqual(transport.import_data(self.db,x)[0]['state'],'unsigned_handoff')
        self.db.close();self.db=connect(':memory:');self.first()
        x=fx('manifest_ok');x['permits'][0]['code']='HW49'
        self.assertEqual(transport.import_data(self.db,x)[0]['state'],'unlicensed')
    def test_ordered_handoffs_and_storage_limit(self):
        self.first();x=fx('manifest_ok');x['handoffs']=[]
        self.assertEqual(transport.import_data(self.db,x)[0]['state'],'sequence_conflict')
        self.db.close();self.db=connect(':memory:')
        x=fx('waste_ok');x['profiles'][0]['storage_until']='2026-01-05'
        classification.import_data(self.db,x)
        self.assertEqual(transport.import_data(self.db,fx('manifest_ok'))[0]['state'],'storage_expired')
    def test_receiver_license_and_independent_scale(self):
        self.second();x=fx('receipt_ok');x['receiver_permits'][0]['code']='HW49'
        self.assertEqual(closure.import_data(self.db,x)[0]['state'],'receiver_unlicensed')
        self.db.close();self.db=connect(':memory:');self.second()
        x=fx('receipt_ok');x['weights'][0]['scale_witness']='carrier'
        self.assertEqual(closure.import_data(self.db,x)[0]['state'],'weigh_invalid')
    def test_large_difference_and_reweigh(self):
        self.second();closure.import_data(self.db,fx('receipt_bad'))
        self.assertEqual(self.state('closure'),'difference_investigation')
        self.assertEqual(self.db.execute('SELECT liable_party FROM closure').fetchone()[0],'carrier')
        new=fx('receipt_bad')['weights'][0].copy();new.update(id='WEIGH-RECHECK',at='2026-01-13',net_kg=998,kind='reweigh')
        closure.import_data(self.db,{'weights':[new]})
        self.assertEqual(self.state('closure'),'closed')
        self.assertEqual(self.db.execute('SELECT count(*) FROM investigation').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT state FROM future_dispatch_hold').fetchone()[0],'cleared')
    def test_unexplained_receipt_blocks_future_classification(self):
        self.second();closure.import_data(self.db,fx('receipt_bad'))
        revised=fx('waste_ok')['profiles'][0].copy();revised.update(id='PROFILE-REV2',version=2)
        classification.import_data(self.db,{'profiles':[revised]})
        self.assertEqual(self.state('classification'),'investigation_hold')
        self.assertEqual(self.state('transport'),'classification_hold')
    def test_identity_conflict_rolls_back(self):
        self.good();before=self.db.execute('SELECT count(*) FROM evidence').fetchone()[0]
        x=fx('waste_ok');x['profiles'][0]['hazard']='HW49'
        with self.assertRaises(ValueError):classification.import_data(self.db,x)
        self.assertEqual(self.db.execute('SELECT count(*) FROM evidence').fetchone()[0],before)
    def test_cli_demo_and_replay(self):
        with tempfile.TemporaryDirectory() as d:
            db=str(Path(d)/'demo.sqlite')
            for cmd in ('demo','audit'):
                r=subprocess.run([sys.executable,'-m','wastetransit',cmd,'--db',db],cwd=ROOT,capture_output=True,text=True)
                self.assertEqual(r.returncode,0,r.stderr)
                if cmd=='audit':self.assertTrue(json.loads(r.stdout)['ok'])
if __name__=='__main__':unittest.main()
