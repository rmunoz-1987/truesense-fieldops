"""Phase 3 CRM: activities, tasks on Today, contact timeline, sales dashboard, catalog polish."""
import os, tempfile, unittest, shutil, sys

TMP = tempfile.mkdtemp(prefix='fo-crm-test-')
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
import fieldops_platform as plat
import fieldops_crm as crm
import fieldops_quote as quote


class CrmTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def _seed(self):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'CRM Co')
        conn.execute(
            'INSERT INTO contacts (company_id,name,role,phone,created_at) VALUES (?,?,?,?,?)',
            (co['id'], 'Pat Buyer', 'decision', '713-555-0101', '2026-01-01'))
        tid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute(
            'INSERT INTO opportunities (company_id,contact_id,title,stage,amount,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
            (co['id'], tid, 'Lobby cameras', 'qualified', 0, 'Roland', '2026-01-01', '2026-01-01'))
        oid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit(); conn.close()
        return co['id'], tid, oid

    def test_activity_and_task_on_contact_timeline(self):
        cid, tid, oid = self._seed()
        conn = fo.get_db()
        aid, err = crm.add_activity(conn, 'call', 'Left voicemail about the lobby cameras.', cid, tid, oid, created_by='Roland')
        self.assertIsNone(err)
        self.assertTrue(aid)
        _, err = crm.add_activity(conn, 'note', '   ', cid, tid)
        self.assertIsNotNone(err)
        due = fo.today_local().isoformat()
        kid, err = crm.add_task(conn, 'Call Pat back', due, notes='After 2pm', company_id=cid, contact_id=tid, opportunity_id=oid, created_by='Roland')
        self.assertIsNone(err)
        conn.commit(); conn.close()
        self.login()
        r = self.c.get('/contacts/%d' % tid)
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Timeline', html)
        self.assertIn('Left voicemail about the lobby cameras.', html)
        self.assertIn('Call Pat back', html)
        self.assertIn('Log activity', html)
        r = self.c.get('/opportunities/%d' % oid)
        html = r.get_data(as_text=True)
        self.assertIn('Left voicemail', html)
        self.assertIn('Call Pat back', html)
        r = self.c.get('/customers/%d' % cid)
        self.assertIn('Left voicemail', r.get_data(as_text=True))

    def test_due_tasks_appear_on_today(self):
        cid, tid, oid = self._seed()
        today = fo.today_local().isoformat()
        conn = fo.get_db()
        crm.add_task(conn, 'Follow up CRM Co', today, company_id=cid, created_by='Roland')
        conn.commit(); conn.close()
        self.login()
        r = self.c.get('/api/today')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        titles = [a['title'] for a in data['actions']]
        self.assertIn('CRM tasks due', titles)
        act = next(a for a in data['actions'] if a['title'] == 'CRM tasks due')
        self.assertIn('Follow up CRM Co', act['detail'])
        self.assertEqual(act['href'], '/tasks')

    def test_tasks_page_and_complete(self):
        cid, tid, oid = self._seed()
        self.login()
        r = self.c.post('/tasks', data={'title': 'Send site walk notes', 'due': fo.today_local().isoformat(),
                                       'company_id': str(cid)}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        r = self.c.get('/tasks')
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Send site walk notes', html)
        self.assertIn('class="on">Sales</a>', html)
        conn = fo.get_db()
        kid = conn.execute("SELECT id FROM crm_tasks WHERE title='Send site walk notes'").fetchone()[0]
        conn.close()
        self.c.post('/tasks/%d/done' % kid, follow_redirects=False)
        conn = fo.get_db()
        row = conn.execute('SELECT done, done_by FROM crm_tasks WHERE id=?', (kid,)).fetchone()
        conn.close()
        self.assertEqual(row['done'], 1)
        self.assertEqual(row['done_by'], 'Roland')
        r = self.c.get('/tasks?show=done')
        self.assertIn('Send site walk notes', r.get_data(as_text=True))

    def test_sales_dashboard_and_sub_blocked(self):
        cid, tid, oid = self._seed()
        conn = fo.get_db()
        crm.add_activity(conn, 'meeting', 'Walked the warehouse with Pat.', cid, tid, oid, created_by='Roland')
        crm.add_task(conn, 'Write proposal', fo.today_local().isoformat(), company_id=cid)
        conn.commit(); conn.close()
        self.login()
        r = self.c.get('/sales')
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Open tasks', html)
        self.assertIn('Walked the warehouse with Pat.', html)
        self.assertIn('Quotes this month', html)
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/tasks', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        r = self.c.get('/sales', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_catalog_forms_are_compact_and_client_uses_addon_tag(self):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Addon Co')
        row, err = quote.save_quote(conn, {
            'title': 'Addon quote', 'company_id': co['id'],
            'lines': [
                {'name': 'Dome', 'qty': 1, 'cost': 80, 'sell': 100},
                {'name': 'Extra monitor', 'qty': 1, 'cost': 120, 'sell': 150, 'optional': True},
            ],
        }, created_by='Roland')
        self.assertIsNone(err)
        row = quote.ensure_token(conn, row['id'])
        conn.commit(); conn.close()
        self.login()
        html = self.c.get('/catalog').get_data(as_text=True)
        self.assertIn('cat-row', html)
        self.assertIn('>Add</button>', html)
        self.assertNotIn('Add to catalog', html)
        tok = row['token']
        self.c.get('/logout')
        page = self.c.get('/q/%s' % tok).get_data(as_text=True)
        self.assertIn('addon-tag', page)
        self.assertIn('Add-on', page)
        self.assertNotIn('(optional)', page)
        self.assertIn('white-space:nowrap', page)


if __name__ == '__main__':
    unittest.main()
