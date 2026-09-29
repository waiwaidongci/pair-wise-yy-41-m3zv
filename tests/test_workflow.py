import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_complete_workflow_and_audit(self):
        item=self.service.create_item({"title":"workflow item","description":"complete business flow","severity":'warning',"quantity":12,"threshold":6,"external_ref":"WF-1"},"creator",'sensor_operator')
        self.assertEqual(item["status"],STATES[0])
        self.service.add_record(item["id"],{"kind":"evidence","detail":"evidence registered","status":"closed","external_ref":"EV-1"},"recorder",'sensor_operator')
        # 封闭前必须存在有效（未解除）交通通告
        self.service.add_traffic_notice(item["id"],{"detail":"封闭绕行通告","external_ref":"TN-1"},"ta",'traffic_authority')
        current=item
        for target in STATES[1:]:
            if target=='restored':
                # 恢复前归档所有未关闭记录（含交通通告）
                for rec in self.repo.list_records(current["id"]):
                    if rec["status"]=='open': self.repo.close_record(rec["id"],"reviewer")
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"],STATES[-1])
        records=self.service.list_records(current["id"],"viewer")
        self.assertTrue(any(r["kind"]=="evidence" for r in records))
        self.assertTrue(any(r["kind"]=="traffic_notice" for r in records))
        events=self.service.audit("viewer",current["id"]); self.assertGreaterEqual(len(events),len(STATES)+1); self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
