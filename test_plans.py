"""Recurring service plans: tray tickets, QBO drafts only, RMR, idempotent reruns."""
import os, tempfile, unittest, shutil, sys, json
from datetime import date, timedelta

TMP = tempfile.mkdtemp(prefix='fo-plans-test-')
os.environ['DATA_DIR'] = TMP
os.environ['DB_PATH'] = os.path.join(TMP, 'fieldops.db')
os.environ['UPLOAD_FOLDER'] = os.path.join(TMP, 'uploads')
os.environ['BACKUP_DIR'] = os.path.join(TMP, 'backups')
os.environ['ROLAND_PASSWORD'] = 'TestPass1234'
os.environ['OFFICE_NAMES'] = 'Roland'
os.environ['WOODY_RODRIGUEZ_PASSWORD'] = 'TestPass1234'
os.environ['SECRET_KEY'] = 'test-secret-local-only'
os.environ.pop('RENDER', None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fieldops_app as fo
import fieldops_plans as plans
import fieldops_tickets as tix


class PlanTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def _add_plan(self, every=12, price=1200, kind='inspection', next_date=None):
        nd = next_date or date.today().isoformat()
        conn = fo.get_db()
        cur = conn.execute(
            '''INSERT INTO service_plans (client,site,phone,title,every_months,next_date,tech,price,checklist,active,created_at,kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('Memorial Family Clinic', '1450 Memorial Dr', '2815550100', 'Camera inspection',
             every, nd, 'Woody Rodriguez', price, '[]', 1, '2026-01-01', kind))
        pid = cur.lastrowid
        conn.commit(); conn.close()
        return pid, nd

    def test_due_visit_makes_ticket_and_draft(self):
        pid, nd = self._add_plan(every=12, price=1200)
        conn = fo.get_db()
        n = fo.run_plans(conn)
        self.assertGreaterEqual(n, 1)
        tickets = list(conn.execute('SELECT * FROM service_tickets WHERE plan_id=?', (pid,)))
        self.assertEqual(len(tickets), 1)
        t = tickets[0]
        self.assertEqual(t['source'], 'plan')
        self.assertEqual(t['status'], 'new')
        self.assertFalse(t['scheduled_date'])  # unscheduled, tray
        self.assertFalse(t['job_id'])
        self.assertIn('inspection', (t['issue'] or '').lower())
        self.assertNotIn('1200', t['issue'] or '')  # no price on the ticket
        drafts = list(conn.execute('SELECT * FROM qbo_drafts WHERE plan_id=?', (pid,)))
        self.assertEqual(len(drafts), 1)
        d = drafts[0]
        self.assertEqual(d['status'], 'queued')
        self.assertEqual(d['amount'], 1200)
        payload = json.loads(d['payload'] or '{}')
        self.assertIn('Do not send without Roland OK', payload.get('private_note') or '')
        # no SP job created for this visit
        jobs = list(conn.execute("SELECT id FROM jobs WHERE id LIKE 'SP%d-%%'" % pid))
        self.assertEqual(jobs, [])
        # next date advanced 12 months
        plan = conn.execute('SELECT * FROM service_plans WHERE id=?', (pid,)).fetchone()
        self.assertNotEqual(plan['next_date'], nd)
        conn.close()
        # appears in tray
        self.login()
        html = self.c.get('/schedule').get_data(as_text=True)
        self.assertIn('Camera inspection', html)
        self.assertNotIn('\u2014', html)
        # office page shows RMR: 1200/12 = 100
        page = self.c.get('/plans').get_data(as_text=True)
        self.assertIn('RMR', page)
        self.assertIn('$100.00', page)
        self.assertIn('unscheduled tickets', page.lower())

    def test_rerun_is_idempotent(self):
        pid, nd = self._add_plan(price=600, every=6)
        conn = fo.get_db()
        fo.run_plans(conn)
        fo.run_plans(conn)
        tickets = list(conn.execute('SELECT * FROM service_tickets WHERE plan_id=?', (pid,)))
        drafts = list(conn.execute('SELECT * FROM qbo_drafts WHERE plan_id=?', (pid,)))
        self.assertEqual(len(tickets), 1)
        self.assertEqual(len(drafts), 1)
        conn.close()

    def test_rmr_on_today_and_sub_blocked(self):
        self._add_plan(every=3, price=300)  # $100 / mo
        self._add_plan(every=12, price=1200)  # $100 / mo
        self.login()
        conn = fo.get_db()
        expected = fo.money(plans.rmr_monthly(conn))
        conn.close()
        data = self.c.get('/api/today').get_json()
        self.assertEqual(data['kpi']['rmr'], expected)
        self.assertNotEqual(expected, fo.money(0))
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/plans', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))


if __name__ == '__main__':
    unittest.main()
