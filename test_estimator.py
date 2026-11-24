"""Estimator role: allowed workspace, 403 on office money and admin routes."""
import os, tempfile, unittest, shutil, sys, re

TMP = tempfile.mkdtemp(prefix='fo-est-test-')
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
import fieldops_users as users
import fieldops_quote as quote
import fieldops_platform as plat
import fieldops_estimator as est


class EstimatorTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), name)

    def add_estimator(self, name='Alex Estimator', pw='Estimator9!x'):
        self.login()
        r = self.c.post('/users', data={'name': name, 'role': 'estimator', 'password': pw}, follow_redirects=True)
        self.assertIn('User added', r.get_data(as_text=True))
        self.c.get('/logout')
        return name, pw

    def test_users_page_can_create_estimator(self):
        self.login()
        html = self.c.get('/users').get_data(as_text=True)
        self.assertIn('option value="estimator"', html)
        self.assertIn('Estimator', html)
        self.c.get('/logout')
        name, pw = self.add_estimator()
        self.login(name, pw)
        home = self.c.get('/').get_data(as_text=True)
        self.assertIn('fo-estimator', home)
        self.assertIn('Quotes to finish', home)
        self.assertIn('Surveys pending', home)
        self.assertIn('Follow-ups', home)
        # switcher is in the markup but hidden; tabs are the estimator set
        block = re.search(r'<nav class="fo-tabs[^"]*" aria-label="Main">(.*?)</nav>', home, re.S)
        self.assertIsNotNone(block)
        labels = re.findall(r'<span>([^<]+)</span>', block.group(1))
        self.assertEqual(labels, ['Today', 'Quotes', 'Customers', 'More'])
        self.assertIn('href="/quote"', home)
        # desktop trimmed nav
        self.assertIn('>Catalog</a>', home)
        self.assertIn('>Floor Plans</a>', home)
        more = self.c.get('/more').get_data(as_text=True)
        self.assertEqual(self.c.get('/more').status_code, 200)
        self.assertIn('Floor Plans', more)
        self.assertIn('Sales pipeline', more)
        self.assertIn('Catalog', more)
        self.assertNotIn('Revenue', more)
        self.assertNotIn('QuickBooks', more)
        self.assertNotIn('>Users</', more)

    def test_blocked_routes_are_403(self):
        name, pw = self.add_estimator('Blair Estimator', 'Estimator9!y')
        self.login(name, pw)
        for path in est.BLOCKED_FOR_TESTS:
            r = self.c.get(path, follow_redirects=False)
            self.assertEqual(r.status_code, 403, path)
            body = r.get_data(as_text=True)
            self.assertTrue('office area' in body.lower() or 'Office only' in body, path)
        # APIs and write actions
        r = self.c.get('/api/invoice-drafts')
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/api/jobs/TS-1/sale', json={'sale_amount': 9})
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/api/jobs/TS-1/to-job', json={'id': ''})
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/catalog', data={'action': 'add', 'name': 'Nope', 'cost': '1'})
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/catalog/import', data={})
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/revenue/qbo-import', data={'text': '2026-01,1,1'})
        self.assertEqual(r.status_code, 403)
        r = self.c.get('/users')
        self.assertEqual(r.status_code, 403)
        r = self.c.get('/settings')
        self.assertEqual(r.status_code, 403)

    def test_allowed_routes_and_quote_margin(self):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Est Co')
        row, err = quote.save_quote(conn, {
            'title': 'Lobby estimate', 'company_id': co['id'],
            'lines': [{'name': 'Dome', 'qty': 2, 'cost': 80, 'sell': 100}],
        }, created_by='Roland')
        self.assertIsNone(err)
        qid = row['id']
        conn.execute(
            "INSERT INTO jobs (id,title,customer,address,status,tech,created_at,job_type) VALUES (?,?,?,?,?,?,?,?)",
            ('TS-EST-1', 'Front survey', 'Est Co', '1 Main', 'assigned', 'Alex Estimator', '2026-11-24', 'Survey'))
        conn.commit(); conn.close()
        name, pw = self.add_estimator('Casey Estimator', 'Estimator9!z')
        self.login(name, pw)
        for path in ('/', '/quote', '/quotes/new', '/quotes/%d' % qid, '/catalog', '/customers',
                     '/sales', '/floor-plans', '/more', '/me', '/new-job?t=Survey'):
            r = self.c.get(path, follow_redirects=True)
            self.assertEqual(r.status_code, 200, path)
        qhtml = self.c.get('/quotes/%d' % qid).get_data(as_text=True)
        self.assertIn('Cost, markup and margin', qhtml)
        self.assertIn('id="qm-margin"', qhtml)
        self.assertIn('$160.00', qhtml)  # cost 2*80
        self.assertIn('$200.00', qhtml)  # sell 2*100
        self.assertIn('$40.00', qhtml)   # margin
        self.assertIn('Ready for Roland', qhtml)
        self.assertNotIn('Make client link', qhtml)
        self.assertIn('Terms', qhtml)
        # catalog is readable, including cost, and has no add form
        cat = self.c.get('/catalog').get_data(as_text=True)
        self.assertNotIn('Add item', cat)
        self.assertNotIn('/catalog/import', cat)
        # cannot send
        r = self.c.post('/quotes/%d' % qid, data={'action': 'link'}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        # ready status
        r = self.c.post('/quotes/%d' % qid, data={'action': 'ready_for_roland'}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        conn = fo.get_db()
        st = conn.execute('SELECT status, token FROM quotes WHERE id=?', (qid,)).fetchone()
        self.assertEqual(st['status'], 'ready')
        self.assertFalse(st['token'])
        conn.close()
        # office Needs you
        self.c.get('/logout')
        self.login()
        raw = self.c.get('/api/today').get_data(as_text=True)
        self.assertIn('Ready for Roland', raw)
        self.assertIn('Lobby estimate', raw)

    def test_estimator_cannot_create_install_job_but_can_create_survey(self):
        name, pw = self.add_estimator('Drew Estimator', 'Estimator9!a')
        self.login(name, pw)
        r = self.c.post('/api/jobs', json={
            'id': 'TS-EST-INST', 'title': 'Install', 'job_type': 'Install', 'customer': 'Est Co'
        })
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/api/jobs', json={
            'id': 'TS-EST-SV', 'title': 'Site survey', 'job_type': 'Survey', 'customer': 'Est Co'
        })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json().get('id'), 'TS-EST-SV')
        # convert to job blocked
        # mark complete first not required for 403
        r = self.c.post('/api/jobs/TS-EST-SV/to-job', json={'id': ''})
        self.assertEqual(r.status_code, 403)

    def test_estimator_today_payload(self):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Pipe Co')
        quote.save_quote(conn, {
            'title': 'Unfinished quote', 'company_id': co['id'],
            'lines': [{'name': 'Cam', 'qty': 1, 'cost': 10, 'sell': 20}],
        }, created_by='Roland')
        conn.execute(
            "INSERT INTO jobs (id,title,customer,status,tech,created_at,job_type) VALUES (?,?,?,?,?,?,?)",
            ('TS-EST-P', 'Pending survey', 'Pipe Co', 'assigned', 'X', '2026-11-24', 'Survey'))
        conn.commit(); conn.close()
        name, pw = self.add_estimator('Eden Estimator', 'Estimator9!b')
        self.login(name, pw)
        d = self.c.get('/api/today').get_json()
        self.assertIn('quotes', d)
        self.assertTrue(any(q['title']=='Unfinished quote' for q in d['quotes']))
        self.assertTrue(any(s['id']=='TS-EST-P' for s in d['surveys']))
        self.assertIn('followups', d)


if __name__ == '__main__':
    unittest.main()
