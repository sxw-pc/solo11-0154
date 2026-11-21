import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wastetransit import assay, classification, transport
from wastetransit.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def screening(batch):
    doc = read("assay_mountings.json")
    doc["batch"] = batch
    return doc


def declaration(batch, profile_id, **profile):
    doc = read("waste_ok.json")
    doc["profiles"][0].update(id=profile_id, batch=batch, assay_required=True, **profile)
    doc["batches"][0].update(id=batch, classification_id=profile_id)
    return doc


def manifest(batch, manifest_id, permit_id):
    doc = read("manifest_ok.json")
    doc["permits"][0]["id"] = permit_id
    doc["manifests"][0].update(id=manifest_id, batch=batch, permit_id=permit_id)
    doc["handoffs"] = [dict(h, id="%s-%s" % (h["id"], manifest_id), manifest_id=manifest_id) for h in doc["handoffs"]]
    return doc


class RetainedAssayTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    def conclusion(self, batch):
        row = self.db.execute("SELECT * FROM assay_conclusion WHERE batch=?", (batch,)).fetchone()
        return dict(row) if row else None

    def state(self, table, batch):
        return self.db.execute("SELECT state FROM %s WHERE batch=?" % table, (batch,)).fetchone()[0]

    def version(self, table, batch):
        return self.db.execute("SELECT version FROM %s WHERE batch=?" % table, (batch,)).fetchone()[0]

    def test_common_orientation_display_without_false_certainty(self):
        assay.import_data(self.db, {"references": [{"id": "REF-1"}], "screenings": [screening("WASTE-1")]})
        plan = assay.plan(self.db, "WASTE-1", budget=2)
        self.assertIsNone(plan["orientation"])
        # 交换朝向不改变 A/B 一高一低的已知事实；低读数不得永远归 A
        self.assertEqual([2, 6], plan["cells"]["A"]["signals"])
        self.assertEqual([2, 6], plan["cells"]["B"]["signals"])
        self.assertFalse(plan["cells"]["A"]["certain"])
        self.assertFalse(plan["cells"]["B"]["certain"])
        self.assertEqual([3, 4, 5], plan["cells"]["C"]["signals"])
        self.assertEqual({}, plan["signable_now"])
        self.assertTrue(any("common orientation" in s for s in plan["invariants"]))
        self.assertTrue(any("one-high(6)/one-low(2)" in s for s in plan["invariants"]))
        self.assertEqual(2, len(plan["dependencies"]))
        # 两次预算：两条路线都可兑现并保证三格全定；一次预算：连续委托不足以全定
        by_route = {p["route"]: p for p in plan["plans"]}
        self.assertEqual("all_three", by_route["continuous"]["guarantee"])
        self.assertTrue(by_route["continuous"]["keeps_sealed_sample"])
        self.assertEqual("all_three", by_route["joint_unfold"]["guarantee"])
        self.assertFalse(by_route["joint_unfold"]["keeps_sealed_sample"])
        self.assertEqual(0, by_route["joint_unfold"]["reserve_after"])
        solo = {p["route"]: p for p in assay.plan(self.db, "WASTE-1", budget=1)["plans"]}
        self.assertEqual("A+B", solo["continuous"]["guarantee"])
        self.assertEqual("all_three", solo["joint_unfold"]["guarantee"])
        # 连续委托的读数分支：3/4 可签，5 进复核类，且都能完成三格
        outcomes = {b["reading"]["c_signal"]: b["outcome"] for b in by_route["continuous"]["branches"]}
        self.assertEqual({3: "verified", 4: "verified", 5: "review"}, outcomes)

    def test_reading_without_fixed_commission_is_inert(self):
        assay.import_data(self.db, {
            "screenings": [screening("WASTE-1")],
            "actions": [{"id": "ACT-LABEL", "batch": "WASTE-1", "window": "W1", "kind": "end_labels", "orientation": "normal"}],
        })
        self.assertEqual("pending", self.conclusion("WASTE-1")["state"])
        self.assertEqual(0, self.db.execute("SELECT effective FROM assay_action WHERE id='ACT-LABEL'").fetchone()[0])

    def test_continuous_commission_full_chain_to_carrier(self):
        classification.import_data(self.db, declaration("WASTE-1", "PROFILE-1"))
        transport.import_data(self.db, manifest("WASTE-1", "MAN-1", "VP-1"))
        # 申报要求保留样凭据但尚无读数：证据门禁先挂起，下游为准运挂起
        self.assertEqual("assay_hold", self.state("classification", "WASTE-1"))
        self.assertEqual("classification_hold", self.state("transport", "WASTE-1"))
        assay.import_data(self.db, {
            "screenings": [screening("WASTE-1")],
            "commissions": [{"id": "COM-1", "batch": "WASTE-1", "window": "W1", "route": "continuous"}],
            "actions": [
                {"id": "ACT-LABEL", "batch": "WASTE-1", "window": "W1", "kind": "end_labels", "orientation": "normal"},
                {"id": "ACT-READ", "batch": "WASTE-1", "window": "W1", "kind": "reader", "c_signal": 4},
            ],
        })
        con = self.conclusion("WASTE-1")
        self.assertEqual("verified", con["state"])
        cells = json.loads(con["cells"])
        self.assertEqual([2], cells["A"]["signals"])
        self.assertEqual([6], cells["B"]["signals"])
        self.assertEqual([4], cells["C"]["signals"])
        self.assertEqual("normal", con["orientation"])
        self.assertEqual(["ACT-LABEL", "ACT-READ"], json.loads(con["evidence"]))
        self.assertEqual(1, con["reserve_untouched"])  # 连续留样委托保留最后一份原封样
        self.assertEqual("classified", self.state("classification", "WASTE-1"))
        self.assertEqual("carrier_responsible", self.state("transport", "WASTE-1"))

    def test_continuous_commission_reader_outage_reports_only_ab(self):
        classification.import_data(self.db, declaration("WASTE-1", "PROFILE-1"))
        assay.import_data(self.db, {
            "screenings": [screening("WASTE-1")],
            "resources": [{"id": "RES-1", "window": "W1", "reader_available": False}],
            "commissions": [{"id": "COM-1", "batch": "WASTE-1", "window": "W1", "route": "continuous"}],
            "actions": [
                {"id": "ACT-LABEL", "batch": "WASTE-1", "window": "W1", "kind": "end_labels", "orientation": "mirror"},
                {"id": "ACT-READ", "batch": "WASTE-1", "window": "W1", "kind": "reader", "c_signal": 3},
            ],
        })
        con = self.conclusion("WASTE-1")
        self.assertEqual("partial", con["state"])
        self.assertIn("A/B", con["reason"])
        cells = json.loads(con["cells"])
        self.assertEqual([6], cells["A"]["signals"])  # 镜像朝向：A 在 R 位
        self.assertEqual([2], cells["B"]["signals"])
        self.assertEqual([3, 4, 5], cells["C"]["signals"])  # 读头不可用，C 仍只有初筛范围
        self.assertEqual(0, self.db.execute("SELECT effective FROM assay_action WHERE id='ACT-READ'").fetchone()[0])
        self.assertEqual("assay_hold", self.state("classification", "WASTE-1"))
        reader_action = [a for a in assay.plan(self.db, "WASTE-1")["actions"] if a["kind"] == "reader"][0]
        self.assertFalse(reader_action["redeemable"])
        self.assertIn("unavailable", reader_action["reason"])

    def test_joint_unfold_completes_when_reader_fails_and_consumes_sample(self):
        classification.import_data(self.db, declaration("WASTE-1", "PROFILE-1"))
        transport.import_data(self.db, manifest("WASTE-1", "MAN-1", "VP-1"))
        assay.import_data(self.db, {
            "references": [{"id": "REF-1"}],
            "screenings": [screening("WASTE-1")],
            "resources": [{"id": "RES-1", "window": "W1", "reader_available": False}],
            "commissions": [{"id": "COM-1", "batch": "WASTE-1", "window": "W1", "route": "joint_unfold"}],
            "actions": [
                {"id": "ACT-REQ", "batch": "WASTE-1", "window": "W1", "kind": "joint_unfold_request",
                 "reference_id": "REF-1", "predicted": {"orientation": "normal", "c_signal": 3}},
            ],
        })
        # 没有独立执行凭据：预计读数不得采用
        self.assertEqual("pending", self.conclusion("WASTE-1")["state"])
        self.assertEqual("assay_hold", self.state("classification", "WASTE-1"))
        self.assertEqual("checked_out", self.db.execute("SELECT state FROM reference_sample WHERE id='REF-1'").fetchone()[0])
        # 执行封条到达：成对观测成套引用，实际读数覆盖预计读数
        assay.import_data(self.db, {"actions": [
            {"id": "ACT-EXE", "batch": "WASTE-1", "window": "W1", "kind": "joint_unfold_execution",
             "request_id": "ACT-REQ", "orientation": "mirror", "c_signal": 4},
        ]})
        con = self.conclusion("WASTE-1")
        self.assertEqual("verified", con["state"])
        cells = json.loads(con["cells"])
        self.assertEqual([6], cells["A"]["signals"])
        self.assertEqual([2], cells["B"]["signals"])
        self.assertEqual([4], cells["C"]["signals"])
        self.assertEqual(["ACT-EXE"], json.loads(con["evidence"]))
        self.assertEqual(0, con["reserve_untouched"])  # 保留样已消耗，不能再供异议复核
        self.assertEqual("consumed", self.db.execute("SELECT state FROM reference_sample WHERE id='REF-1'").fetchone()[0])
        self.assertEqual("classified", self.state("classification", "WASTE-1"))
        self.assertEqual("carrier_responsible", self.state("transport", "WASTE-1"))
        self.assertEqual("consumed", assay.plan(self.db, "WASTE-1")["reserve"]["objection_review"])

    def test_reading_above_threshold_goes_to_review_class(self):
        classification.import_data(self.db, declaration("WASTE-1", "PROFILE-1"))
        transport.import_data(self.db, manifest("WASTE-1", "MAN-1", "VP-1"))
        assay.import_data(self.db, {
            "screenings": [screening("WASTE-1")],
            "commissions": [{"id": "COM-1", "batch": "WASTE-1", "window": "W1", "route": "continuous"}],
            "actions": [
                {"id": "ACT-LABEL", "batch": "WASTE-1", "window": "W1", "kind": "end_labels", "orientation": "normal"},
                {"id": "ACT-READ", "batch": "WASTE-1", "window": "W1", "kind": "reader", "c_signal": 5},
            ],
        })
        self.assertEqual("review", self.conclusion("WASTE-1")["state"])
        self.assertEqual("assay_review", self.state("classification", "WASTE-1"))
        self.assertEqual("classification_hold", self.state("transport", "WASTE-1"))

    def test_shared_reference_power_loss_and_late_execution(self):
        for batch, pid, mid, vp in [("WASTE-1", "PROFILE-1", "MAN-1", "VP-1"),
                                    ("WASTE-2", "PROFILE-2", "MAN-2", "VP-2")]:
            classification.import_data(self.db, declaration(batch, pid))
            transport.import_data(self.db, manifest(batch, mid, vp))
        # 两个批次共借一份同编号参考保留样；第一次联合展开申请送达后中途掉电
        assay.import_data(self.db, {
            "references": [{"id": "REF-1"}],
            "screenings": [screening("WASTE-1"), screening("WASTE-2")],
            "commissions": [
                {"id": "COM-1", "batch": "WASTE-1", "window": "W1", "route": "joint_unfold"},
                {"id": "COM-2", "batch": "WASTE-2", "window": "W1", "route": "continuous"},
                {"id": "COM-3", "batch": "WASTE-2", "window": "W2", "route": "joint_unfold"},
            ],
            "actions": [
                {"id": "ACT-REQ", "batch": "WASTE-1", "window": "W1", "kind": "joint_unfold_request", "reference_id": "REF-1"},
            ],
        })
        # 值班记事只证明领取，执行封条尚未传回
        ref = dict(self.db.execute("SELECT * FROM reference_sample WHERE id='REF-1'").fetchone())
        self.assertEqual("checked_out", ref["state"])
        self.assertEqual("WASTE-1", ref["holder"])
        self.assertEqual("pending", self.conclusion("WASTE-1")["state"])
        self.assertEqual("assay_hold", self.state("classification", "WASTE-1"))
        self.assertEqual("classification_hold", self.state("transport", "WASTE-1"))
        # 已展开与未展开都相容；本批委托路线仍可在执行封条到达后续办
        plan = assay.plan(self.db, "WASTE-1", budget=2)
        mine = [r for r in plan["references"] if r["id"] == "REF-1"][0]
        self.assertEqual({"executed_consumed", "not_executed_intact"}, set(mine["compatible"]))
        joint_plan = [p for p in plan["plans"] if p["route"] == "joint_unfold"][0]
        self.assertTrue(joint_plan["redeemable"])
        # 新批不能借还未证实归还的参考样
        assay.import_data(self.db, {"actions": [
            {"id": "ACT-REQ2", "batch": "WASTE-2", "window": "W2", "kind": "joint_unfold_request", "reference_id": "REF-1"},
        ]})
        ref2 = dict(self.db.execute("SELECT * FROM reference_sample WHERE id='REF-1'").fetchone())
        self.assertEqual(("checked_out", "WASTE-1", ref["version"]), (ref2["state"], ref2["holder"], ref2["version"]))
        self.assertEqual(0, self.db.execute("SELECT effective FROM assay_action WHERE id='ACT-REQ2'").fetchone()[0])
        denied = self.db.execute("SELECT state FROM decision_history WHERE batch='WASTE-2' AND subsystem='reference'").fetchall()
        self.assertEqual([("borrow_denied",)], [tuple(r) for r in denied])
        # 系统仍可在未使用参考样的读头路线继续可证部分
        assay.import_data(self.db, {"actions": [
            {"id": "ACT-LABEL2", "batch": "WASTE-2", "window": "W1", "kind": "end_labels", "orientation": "normal"},
            {"id": "ACT-READ2", "batch": "WASTE-2", "window": "W1", "kind": "reader", "c_signal": 3},
        ]})
        self.assertEqual("verified", self.conclusion("WASTE-2")["state"])
        self.assertEqual("classified", self.state("classification", "WASTE-2"))
        self.assertEqual("carrier_responsible", self.state("transport", "WASTE-2"))
        before = (self.version("classification", "WASTE-2"), self.version("transport", "WASTE-2"))
        # 后到完整执行证据：锁定两次操作归属，只重裁实际依赖该证据的分类与下游准运
        assay.import_data(self.db, {"actions": [
            {"id": "ACT-EXE", "batch": "WASTE-1", "window": "W1", "kind": "joint_unfold_execution",
             "request_id": "ACT-REQ", "orientation": "normal", "c_signal": 4},
        ]})
        self.assertEqual("verified", self.conclusion("WASTE-1")["state"])
        self.assertEqual("classified", self.state("classification", "WASTE-1"))
        self.assertEqual("carrier_responsible", self.state("transport", "WASTE-1"))
        self.assertEqual("consumed", self.db.execute("SELECT state FROM reference_sample WHERE id='REF-1'").fetchone()[0])
        after = (self.version("classification", "WASTE-2"), self.version("transport", "WASTE-2"))
        self.assertEqual(before, after)
        # 已出单与样品实际消耗可回看
        events = [tuple(r) for r in self.db.execute(
            "SELECT subsystem,state FROM decision_history WHERE batch='WASTE-1' AND subsystem='reference' ORDER BY id")]
        self.assertEqual([("reference", "checked_out"), ("reference", "consumed")], events)
        # 重复导入同一动作不得重领
        marker = (self.db.execute("SELECT count(*) FROM evidence").fetchone()[0],
                  self.db.execute("SELECT version FROM reference_sample WHERE id='REF-1'").fetchone()[0],
                  self.conclusion("WASTE-1")["version"],
                  self.version("classification", "WASTE-1"))
        assay.import_data(self.db, {"actions": [
            {"id": "ACT-REQ", "batch": "WASTE-1", "window": "W1", "kind": "joint_unfold_request", "reference_id": "REF-1"},
            {"id": "ACT-EXE", "batch": "WASTE-1", "window": "W1", "kind": "joint_unfold_execution",
             "request_id": "ACT-REQ", "orientation": "normal", "c_signal": 4},
        ]})
        self.assertEqual(marker, (self.db.execute("SELECT count(*) FROM evidence").fetchone()[0],
                                  self.db.execute("SELECT version FROM reference_sample WHERE id='REF-1'").fetchone()[0],
                                  self.conclusion("WASTE-1")["version"],
                                  self.version("classification", "WASTE-1")))

    def test_withdrawal_still_uses_existing_path(self):
        classification.import_data(self.db, declaration("WASTE-1", "PROFILE-1"))
        transport.import_data(self.db, manifest("WASTE-1", "MAN-1", "VP-1"))
        assay.import_data(self.db, {
            "screenings": [screening("WASTE-1")],
            "commissions": [{"id": "COM-1", "batch": "WASTE-1", "window": "W1", "route": "continuous"}],
            "actions": [
                {"id": "ACT-LABEL", "batch": "WASTE-1", "window": "W1", "kind": "end_labels", "orientation": "normal"},
                {"id": "ACT-READ", "batch": "WASTE-1", "window": "W1", "kind": "reader", "c_signal": 4},
            ],
        })
        self.assertEqual("classified", self.state("classification", "WASTE-1"))
        withdrawn = declaration("WASTE-1", "PROFILE-W", version=2, withdrawn=True)
        classification.import_data(self.db, {"profiles": withdrawn["profiles"]})
        self.assertEqual("withdrawn", self.state("classification", "WASTE-1"))
        self.assertEqual("classification_hold", self.state("transport", "WASTE-1"))

    def test_cli_assay_import_report_and_replay(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "state.sqlite")
            docs = {
                "waste": declaration("WASTE-1", "PROFILE-1"),
                "manifest": manifest("WASTE-1", "MAN-1", "VP-1"),
                "assay": {
                    "references": [{"id": "REF-1"}],
                    "screenings": [screening("WASTE-1")],
                    "commissions": [{"id": "COM-1", "batch": "WASTE-1", "window": "W1", "route": "continuous"}],
                    "actions": [
                        {"id": "ACT-LABEL", "batch": "WASTE-1", "window": "W1", "kind": "end_labels", "orientation": "normal"},
                        {"id": "ACT-READ", "batch": "WASTE-1", "window": "W1", "kind": "reader", "c_signal": 4},
                    ],
                },
            }
            for name, doc in docs.items():
                (Path(d) / (name + ".json")).write_text(json.dumps(doc), encoding="utf-8")
            for cmd, name in [("waste-import", "waste"), ("manifest-import", "manifest"), ("assay-import", "assay")]:
                r = subprocess.run([sys.executable, "-B", "-m", "wastetransit", cmd, "--db", db,
                                    "--input", str(Path(d) / (name + ".json"))],
                                   cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run([sys.executable, "-B", "-m", "wastetransit", "assay-plan", "--db", db,
                                "--batch", "WASTE-1", "--budget", "2"], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            plan = json.loads(r.stdout)
            self.assertEqual("normal", plan["orientation"])
            self.assertEqual({"signal": 2, "class": "separated"}, plan["signable_now"]["A"])
            r = subprocess.run([sys.executable, "-B", "-m", "wastetransit", "report", "--db", db],
                               cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            row = [b for b in json.loads(r.stdout) if b["batch"] == "WASTE-1"][0]
            self.assertEqual("verified", row["assay"]["state"])
            self.assertEqual("classified", row["classification"]["state"])
            self.assertEqual("carrier_responsible", row["transport"]["state"])
            r = subprocess.run([sys.executable, "-B", "-m", "wastetransit", "audit", "--db", db],
                               cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(json.loads(r.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
