"""Migration and office-only access tests for Phase 1 platform records."""
import os, tempfile, unittest, shutil, sys

TMP = tempfile.mkdtemp(prefix='fo-plat-test-')
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

class PlatformTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name, pw):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_seed_jobs_migrated_to_companies(self):
        conn = fo.get_db()
        companies = {r['name_key']: r['name'] for r in conn.execute('SELECT name,name_key FROM companies')}
        self.assertIn('abc warehouse', companies)
        self.assertIn('xyz retail', companies)
        jobs = list(conn.execute('SELECT id,customer,company_id,site_id FROM jobs'))
        self.assertTrue(jobs)
        for j in jobs:
            self.assertTrue(j['customer'], 'customer string must stay')
            self.assertIsNotNone(j['company_id'])
            self.assertIsNotNone(j['site_id'])
            co = conn.execute('SELECT name FROM companies WHERE id=?', (j['company_id'],)).fetchone()
            self.assertEqual(co['name'], j['customer'])
        conn.close()

    def test_migrate_is_idempotent(self):
        conn = fo.get_db()
        a = plat.migrate_existing(conn)
        b = plat.migrate_existing(conn)
        conn.commit(); conn.close()
        self.assertEqual(a['companies'], b['companies'])
        self.assertEqual(a['sites'], b['sites'])
        self.assertEqual(a['contacts'], b['contacts'])
        self.assertEqual(a['jobs_linked'], b['jobs_linked'])

    def test_customer_strings_unchanged_after_second_migrate(self):
        conn = fo.get_db()
        before = {r['id']: r['customer'] for r in conn.execute('SELECT id,customer FROM jobs')}
        plat.migrate_existing(conn)
        after = {r['id']: r['customer'] for r in conn.execute('SELECT id,customer FROM jobs')}
        conn.close()
        self.assertEqual(before, after)

    def test_rename_company_keeps_jobs_in_sync(self):
        conn = fo.get_db()
        plat.upsert_company(conn, 'Rename Me LLC', phone='713-555-0100', billing_address='1 Main St, Houston, TX')
        conn.execute(
            "INSERT INTO jobs (id,title,customer,address,status,created_at) VALUES (?,?,?,?,?,?)",
            ('TS-TEST-REN', 'Test job', 'Rename Me LLC', '1 Main St, Houston, TX', 'assigned', '2026-01-01'))
        plat.link_job(conn, conn.execute("SELECT * FROM jobs WHERE id='TS-TEST-REN'").fetchone())
        co = plat.get_company_by_key(conn, 'rename me llc')
        row, err = plat.rename_company(conn, co['id'], 'Renamed Company LLC')
        self.assertIsNone(err)
        self.assertEqual(row['name'], 'Renamed Company LLC')
        job = conn.execute("SELECT customer FROM jobs WHERE id='TS-TEST-REN'").fetchone()
        self.assertEqual(job['customer'], 'Renamed Company LLC')
        # old key should not leave a stray company
        self.assertIsNone(plat.get_company_by_key(conn, 'rename me llc'))
        conn.execute("DELETE FROM jobs WHERE id='TS-TEST-REN'")
        conn.execute('DELETE FROM companies WHERE id=?', (co['id'],))
        conn.commit(); conn.close()

    def test_office_pages_and_field_blocked(self):
        self.login('Roland', 'TestPass1234')
        for path in ('/customers', '/sales', '/quote', '/opportunities'):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)
            html = r.get_data(as_text=True)
            self.assertIn('fo-switch', html)
            self.assertIn('Ops', html)
            self.assertIn('Sales', html)
            self.assertIn('Quote', html)
            self.assertNotIn('\u2014', html)
        # Add a company through the form
        r = self.c.post('/customers/new', data={
            'name': 'Bayou Camera Shop', 'phone': '713-555-2000',
            'email': 'shop@example.com', 'billing_address': '100 Westheimer Rd, Houston, TX',
            'notes': 'Walk-in retail'
        }, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        loc = r.headers.get('Location', '')
        self.assertTrue(loc.startswith('/customers/'), loc)
        r = self.c.get(loc)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Bayou Camera Shop', r.get_data(as_text=True))
        # Field user cannot open these pages
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        for path in ('/customers', '/sales', '/quote', '/opportunities', loc):
            r = self.c.get(path, follow_redirects=False)
            self.assertIn(r.status_code, (302, 303), path)
            self.assertEqual(r.headers.get('Location'), '/')
        # Field home has no product switcher and no prices
        r = self.c.get('/')
        html = r.get_data(as_text=True)
        self.assertNotIn('fo-switch', html)
        self.assertNotIn('Bayou Camera Shop', html)  # may or may not, skip strict
        # money should not appear as sell prices on field home; switcher must be absent
        self.assertNotIn('Open pipeline', html)

    def test_pre_migrate_backup_exists(self):
        backup = os.path.join(TMP, 'backups', plat.PRE_MIGRATE_BACKUP)
        self.assertTrue(os.path.exists(backup), 'pre-migration backup should be written once')

    def test_opportunity_crud(self):
        self.login('Roland', 'TestPass1234')
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Opp Test Co', billing_address='2 Texas Ave, Houston, TX')
        conn.commit(); cid = co['id']; conn.close()
        r = self.c.post('/opportunities/new', data={
            'title': 'Front door cameras', 'company_id': str(cid),
            'stage': 'lead', 'amount': '4200'
        }, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        loc = r.headers.get('Location', '')
        self.assertTrue(loc.startswith('/opportunities/'), loc)
        r = self.c.get('/opportunities')
        html = r.get_data(as_text=True)
        self.assertIn('Front door cameras', html)
        self.assertIn('$4,200.00', html)


    def _switcher(self, html):
        self.assertIn('fo-switch', html)
        for area, href, lab in (('ops','/?area=ops','Ops'),('sales','/sales','Sales'),('quote','/quote','Quote')):
            if 'href="%s" class="on">%s</a>'%(href,lab) in html:
                return area
        self.fail('no selected product in switcher')

    def test_shared_customers_keep_area(self):
        """Customers is shared. Default Ops. Visiting Jobs after Quote must not leave Quote stuck."""
        self.login('Roland', 'TestPass1234')
        html=self.c.get('/customers').get_data(as_text=True)
        self.assertEqual(self._switcher(html), 'ops')
        self.assertNotIn('fo-in', html)
        self.assertNotIn('animation:fo-in', html)
        conn=fo.get_db()
        co=conn.execute('SELECT id FROM companies ORDER BY id LIMIT 1').fetchone()
        self.assertIsNotNone(co)
        cid=co['id']; conn.close()
        html=self.c.get('/customers/%d'%cid).get_data(as_text=True)
        self.assertEqual(self._switcher(html), 'ops')
        self.assertNotIn('class="on">Quote</a>', html)

        self.c.get('/sales')
        self.assertEqual(self._switcher(self.c.get('/customers').get_data(as_text=True)), 'sales')
        self.assertEqual(self._switcher(self.c.get('/customers/%d'%cid).get_data(as_text=True)), 'sales')

        self.c.get('/quote')
        self.assertEqual(self._switcher(self.c.get('/customers').get_data(as_text=True)), 'quote')
        # An Ops page must clear Quote so Customers does not jump back to it
        self.c.get('/jobs')
        self.assertEqual(self._switcher(self.c.get('/jobs').get_data(as_text=True)), 'ops')
        self.assertEqual(self._switcher(self.c.get('/customers').get_data(as_text=True)), 'ops')
        self.assertEqual(self._switcher(self.c.get('/customers/%d'%cid).get_data(as_text=True)), 'ops')
        html=self.c.get('/customers/%d'%cid).get_data(as_text=True)
        self.assertNotIn('class="on">Quote</a>', html)
        self.assertNotIn('animation:fo-in', html)

if __name__ == '__main__':
    unittest.main(verbosity=2)
