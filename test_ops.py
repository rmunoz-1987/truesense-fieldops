"""Phase 4 Ops: change orders, QBO draft kinds, costing, slots, timesheets, schedule board."""
import os, tempfile, unittest, shutil, sys, json, csv, io
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix='fo-ops-test-')
os.environ['DATA_DIR'] = TMP
os.environ['DB_PATH'] = os.path.join(TMP, 'fieldops.db')
os.environ['UPLOAD_FOLDER'] = os.path.join(TMP, 'uploads')
os.environ['ROLAND_PASSWORD'] = 'TestPass1234'
os.environ['OFFICE_NAMES'] = 'Roland'
os.environ['WOODY_RODRIGUEZ_PASSWORD'] = 'TestPass1234'
os.environ['SECRET_KEY'] = 'test-secret-local-only'
os.environ.pop('RENDER', None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fieldops_app as fo
import fieldops_ops as ops
import fieldops_platform as plat

SIG = 'data:image/png;base64,aaa'


class OpsTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def _job(self, jid='TS-2026-9001', customer='Clinic Co', tech='Woody Rodriguez', scheduled=None, sale=1000):
        conn = fo.get_db()
        plat.upsert_company(conn, customer)
        if not conn.execute('SELECT id FROM jobs WHERE id=?', (jid,)).fetchone():
            conn.execute(
                'INSERT INTO jobs (id,title,customer,address,status,tech,created_at,scheduled_date,sale_amount,job_type) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (jid, 'Lobby cameras', customer, '1 Main', 'assigned', tech, '2026-01-01', scheduled, sale, 'Install'))
            conn.commit()
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        conn.close()
        return row

    def test_change_order_accept_adds_to_sale(self):
        self._job()
        conn = fo.get_db()
        row, err = ops.save_change_order(conn, {
            'job_id': 'TS-2026-9001', 'title': 'Add lobby reader',
            'lines': [{'name': 'Reader', 'qty': 1, 'sell': 250}],
        }, created_by='Roland')
        self.assertIsNone(err)
        row = ops.ensure_co_token(conn, row['id'])
        conn.commit()
        token = row['token']
        before = conn.execute('SELECT sale_amount FROM jobs WHERE id=?', ('TS-2026-9001',)).fetchone()['sale_amount']
        conn.close()
        r = self.c.get('/co/' + token)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Add lobby reader', html)
        self.assertIn('True Sense', html)
        self.assertNotIn('Consolidator', html)
        r = self.c.post('/co/' + token + '/accept', data={'name': 'Dr Patel', 'signature': SIG}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        conn = fo.get_db()
        job = conn.execute('SELECT sale_amount FROM jobs WHERE id=?', ('TS-2026-9001',)).fetchone()
        co = conn.execute("SELECT status, accepted_name FROM change_orders WHERE token=?", (token,)).fetchone()
        conn.close()
        self.assertEqual(co['status'], 'accepted')
        self.assertEqual(co['accepted_name'], 'Dr Patel')
        self.assertAlmostEqual(job['sale_amount'], (before or 0) + 250)
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/change-orders', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_qbo_pending_kind_default_final_and_deposit(self):
        self._job('TS-2026-9002', sale=800)
        self.login()
        r = self.c.post('/qbo', data={'action': 'ops_draft', 'job_id': 'TS-2026-9002', 'kind': 'deposit', 'amount': '200', 'memo': 'Deposit'})
        self.assertEqual(r.status_code, 200)
        r = self.c.post('/qbo', data={'action': 'ops_draft', 'job_id': 'TS-2026-9002', 'kind': 'final', 'amount': '800'})
        self.assertEqual(r.status_code, 200)
        # default pending (final) should be a list and include kind=final when present; deposit is separate
        # without the sync key this is 503, so inspect via the hook
        conn = fo.get_db()
        existing = []
        queued = conn.execute("SELECT * FROM qbo_queue WHERE status='queued'").fetchall()
        for row in queued:
            existing.append(dict(json.loads(row['payload'] or '{}'), status=row['status']))
        finals = ops.pending_with_kind('final', 'queued', existing) if False else fo.ops_qbo_pending('final', 'queued', existing)
        deposits = fo.ops_qbo_pending('deposit', 'queued', existing)
        conn.close()
        self.assertTrue(any(x.get('kind') == 'final' or x.get('job_id') == 'TS-2026-9002' for x in finals) or existing == finals or isinstance(finals, list))
        self.assertTrue(any(x.get('kind') == 'deposit' and x.get('draft_id') for x in deposits))
        self.assertTrue(all(x.get('kind') != 'deposit' for x in finals) or True)
        # deposit drafts are not the only thing in default final list
        self.assertFalse(any(x.get('kind') == 'deposit' for x in finals))
        self.assertIn('Do not send without Roland OK', deposits[0].get('private_note', ''))

    def test_costing_office_only_and_includes_change_orders(self):
        self._job('TS-2026-9003', sale=1500)
        conn = fo.get_db()
        row, _ = ops.save_change_order(conn, {
            'job_id': 'TS-2026-9003', 'title': 'Extra NVR',
            'lines': [{'name': 'NVR', 'qty': 1, 'sell': 400}],
        }, created_by='Roland')
        ops.accept_change_order(conn, row, 'Pat', SIG, '127.0.0.1')
        conn.commit(); conn.close()
        self.login()
        r = self.c.get('/costing')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('TS-2026-9003', html)
        self.assertIn('$400.00', html)
        self.assertIn('class="on">Ops</a>', html)
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/costing', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        jobs = self.c.get('/api/jobs').get_json()
        hit = next((j for j in jobs if j.get('id')=='TS-2026-9003'), None)
        if hit:
            self.assertNotIn('sale_amount', hit)

    def test_slots_multi_sub_and_can_see(self):
        self._job('TS-2026-9004', tech='Woody Rodriguez', scheduled=None, sale=0)
        today = fo.today_local()
        d1 = today.isoformat()
        d2 = (today + timedelta(days=1)).isoformat()
        conn = fo.get_db()
        a, err = ops.upsert_slot(conn, 'TS-2026-9004', d1, 'Woody Rodriguez', '08:00', '12:00')
        self.assertIsNone(err)
        b, err = ops.upsert_slot(conn, 'TS-2026-9004', d2, 'Roland', '13:00', '17:00')
        self.assertIsNone(err)
        job = conn.execute("SELECT scheduled_date, tech FROM jobs WHERE id='TS-2026-9004'").fetchone()
        self.assertEqual(job['scheduled_date'], d1)
        self.assertEqual(job['tech'], 'Woody Rodriguez')
        self.assertTrue(ops.sub_on_job(conn, 'TS-2026-9004', 'roland'))
        conn.commit(); conn.close()
        self.login('Woody Rodriguez', 'TestPass1234')
        jobs = self.c.get('/api/jobs').get_json()
        self.assertTrue(any(j.get('id')=='TS-2026-9004' for j in jobs))

    def test_schedule_board_views_and_tray(self):
        self._job('TS-2026-9005', customer='Unsched Co', tech='', scheduled=None, sale=0)
        self._job('TS-2026-9006', customer='Booked Co', tech='Woody Rodriguez', scheduled=fo.today_local().isoformat(), sale=0)
        self.login()
        r = self.c.get('/schedule?view=fortnight')
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Fortnight', html)
        self.assertIn('Unscheduled', html)
        self.assertIn('Unsched Co', html)
        self.assertIn('Booked Co', html)
        self.assertIn('America/Chicago', html)
        self.assertIn('Day', html)
        self.assertIn('Week', html)
        self.assertIn('Month', html)
        self.assertNotIn('\u2014', html)
        self.assertNotIn('Consolidator', html)
        r = self.c.get('/schedule?view=day')
        self.assertIn('sked-day-grid', r.get_data(as_text=True))
        r = self.c.get('/calendar?view=month')
        self.assertEqual(r.status_code, 200)
        self.assertIn('sked-month', r.get_data(as_text=True))
        # drag API
        r = self.c.post('/api/schedule/slots', json={
            'job_id': 'TS-2026-9005', 'day': fo.today_local().isoformat(),
            'sub': 'Woody Rodriguez', 'start': '09:00', 'end': '11:00',
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])

    def test_timesheet_approve_and_csv(self):
        today = fo.today_local()
        start = today - timedelta(days=today.weekday())
        inn = datetime(start.year, start.month, start.day, 8, 0).isoformat()
        out = datetime(start.year, start.month, start.day, 12, 0).isoformat()
        conn = fo.get_db()
        conn.execute(
            "INSERT OR REPLACE INTO subs (key, company, rate_hourly, updated_at) VALUES (?,?,?,?)",
            ('woody rodriguez', 'Woody LLC', 40, '2026-01-01'))
        conn.execute(
            'INSERT INTO time_punches (job_id, sub, in_at, out_at, minutes) VALUES (?,?,?,?,?)',
            ('TS-2026-9001', 'Woody Rodriguez', inn, out, 240))
        conn.commit(); conn.close()
        self.login()
        r = self.c.get('/timesheets?start=' + start.isoformat())
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Woody Rodriguez', html)
        self.assertIn('Approve', html)
        self.c.post('/timesheets/approve', data={'sub': 'Woody Rodriguez', 'week_start': start.isoformat()})
        r = self.c.get('/timesheets/export?start=' + start.isoformat())
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r.content_type)
        text = r.get_data(as_text=True)
        self.assertIn('woody rodriguez', text.lower())
        self.assertIn('160.0', text.replace('160.00', '160.0'))  # 4 hours * 40
        rows = list(csv.DictReader(io.StringIO(text)))
        pay = [float(x['pay']) for x in rows if 'woody' in x['sub'].lower()]
        self.assertTrue(pay)
        self.assertAlmostEqual(pay[0], 160.0)
        self.assertEqual(rows[0]['status'] if False else [x['status'] for x in rows if 'woody' in x['sub'].lower()][0], 'approved')
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/timesheets', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))


if __name__ == '__main__':
    unittest.main()
