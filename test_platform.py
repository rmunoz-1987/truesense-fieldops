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


    def test_archive_company_keeps_jobs(self):
        self.login('Roland', 'TestPass1234')
        conn=fo.get_db()
        before=conn.execute("SELECT COUNT(*) FROM jobs WHERE LOWER(TRIM(customer))='abc warehouse'").fetchone()[0]
        self.assertTrue(before)
        job_ids=[r['id'] for r in conn.execute("SELECT id FROM jobs WHERE LOWER(TRIM(customer))='abc warehouse'")]
        conn.close()
        r=self.c.get('/clients/remove?name=ABC Warehouse')
        html=r.get_data(as_text=True)
        self.assertIn('Jobs stay', html)
        self.assertNotIn('ALL of its jobs', html)
        r=self.c.post('/clients/remove', data={'name':'ABC Warehouse','confirm':'yes'}, follow_redirects=False)
        self.assertIn(r.status_code,(302,303))
        conn=fo.get_db()
        after=conn.execute("SELECT id FROM jobs WHERE id IN (%s)"%(','.join('?'*len(job_ids))), job_ids).fetchall()
        self.assertEqual(len(after), before)
        co=plat.get_company_by_key(conn,'abc warehouse')
        self.assertTrue(co['archived'])
        conn.execute('UPDATE companies SET archived=0 WHERE id=?',(co['id'],))
        conn.commit(); conn.close()

    def test_link_job_keeps_company_id(self):
        """A typo in the customer string must not split off a second company."""
        conn=fo.get_db()
        co=plat.upsert_company(conn,'Link Co LLC')
        conn.execute(
            "INSERT INTO jobs (id,title,customer,address,status,created_at,company_id) VALUES (?,?,?,?,?,?,?)",
            ('TS-TEST-LNK','Typo job','Link Co L.L.C. typo','9 Main', 'assigned','2026-01-01',co['id']))
        plat.link_job(conn, conn.execute("SELECT * FROM jobs WHERE id='TS-TEST-LNK'").fetchone())
        job=conn.execute("SELECT customer,company_id FROM jobs WHERE id='TS-TEST-LNK'").fetchone()
        self.assertEqual(job['company_id'], co['id'])
        self.assertEqual(job['customer'], 'Link Co LLC')
        self.assertIsNone(plat.get_company_by_key(conn,'link co l.l.c. typo'))
        conn.execute("DELETE FROM jobs WHERE id='TS-TEST-LNK'")
        conn.execute('DELETE FROM companies WHERE id=?',(co['id'],))
        conn.commit(); conn.close()

    def test_create_job_uses_company_id(self):
        self.login('Roland','TestPass1234')
        conn=fo.get_db()
        co=plat.upsert_company(conn,'Picker Co')
        conn.commit(); cid=co['id']; conn.close()
        r=self.c.post('/api/jobs', json={
            'id':'TS-TEST-PICK','title':'Install cameras','customer':'Picker Company typo',
            'company_id':cid,'address':'10 Main','tech':'Woody Rodriguez','job_type':'Install'
        })
        self.assertEqual(r.status_code,201)
        conn=fo.get_db()
        job=conn.execute("SELECT customer,company_id FROM jobs WHERE id='TS-TEST-PICK'").fetchone()
        self.assertEqual(job['company_id'], cid)
        self.assertEqual(job['customer'], 'Picker Co')
        self.assertIsNone(plat.get_company_by_key(conn,'picker company typo'))
        conn.execute("DELETE FROM jobs WHERE id='TS-TEST-PICK'")
        conn.execute('DELETE FROM companies WHERE id=?',(cid,))
        conn.commit(); conn.close()

    def test_merge_companies_moves_jobs(self):
        conn=fo.get_db()
        keep=plat.upsert_company(conn,'Keep Firm')
        drop=plat.upsert_company(conn,'Drop Firm')
        conn.execute("INSERT INTO jobs (id,title,customer,status,created_at,company_id) VALUES (?,?,?,?,?,?)",
                     ('TS-TEST-MRG','Merge job','Drop Firm','assigned','2026-01-01',drop['id']))
        plat.ensure_contact(conn, drop['id'], 'Pat Lee', role='pm')
        conn.commit()
        row,err=plat.merge_companies(conn, keep['id'], drop['id'])
        self.assertIsNone(err)
        job=conn.execute("SELECT customer,company_id FROM jobs WHERE id='TS-TEST-MRG'").fetchone()
        self.assertEqual(job['company_id'], keep['id'])
        self.assertEqual(job['customer'], 'Keep Firm')
        self.assertIsNone(conn.execute('SELECT 1 FROM companies WHERE id=?',(drop['id'],)).fetchone())
        self.assertTrue(conn.execute('SELECT 1 FROM contacts WHERE company_id=? AND name=?',(keep['id'],'Pat Lee')).fetchone())
        conn.execute("DELETE FROM jobs WHERE id='TS-TEST-MRG'")
        conn.execute('DELETE FROM contacts WHERE company_id=?',(keep['id'],))
        conn.execute('DELETE FROM companies WHERE id=?',(keep['id'],))
        conn.commit(); conn.close()

    def test_opportunity_stage_api(self):
        self.login('Roland','TestPass1234')
        conn=fo.get_db()
        co=plat.upsert_company(conn,'Stage Co')
        conn.execute(
            'INSERT INTO opportunities (company_id,title,stage,amount,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
            (co['id'],'Move me','lead',1000,'Roland','2026-01-01','2026-01-01'))
        oid=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit(); conn.close()
        r=self.c.post('/api/opportunities/%d/stage'%oid, json={'stage':'proposal'})
        self.assertEqual(r.status_code,200)
        conn=fo.get_db()
        row=conn.execute('SELECT stage FROM opportunities WHERE id=?',(oid,)).fetchone()
        self.assertEqual(row['stage'],'proposal')
        html=self.c.get('/opportunities').get_data(as_text=True)
        self.assertIn('foOppStage', html)
        self.assertIn('Move to', html)
        conn.execute('DELETE FROM opportunities WHERE id=?',(oid,))
        conn.execute('DELETE FROM companies WHERE id=?',(co['id'],))
        conn.commit(); conn.close()

    def test_merge_page_renders(self):
        self.login('Roland','TestPass1234')
        r=self.c.get('/customers/merge')
        self.assertEqual(r.status_code,200)
        html=r.get_data(as_text=True)
        self.assertIn('Merge this duplicate', html)
        self.assertIn('Into this company', html)

    def test_users_page_and_db_login(self):
        import fieldops_users as users
        self.login('Roland','TestPass1234')
        html=self.c.get('/users').get_data(as_text=True)
        self.assertIn('Render logins', html)
        self.assertIn('Roland', html)
        self.assertIn('Woody Rodriguez', html)
        self.assertIn('Set in Render', html)
        # weak password rejected
        r=self.c.post('/users', data={'name':'Pat Field','role':'field','password':'short'}, follow_redirects=True)
        self.assertIn('at least 10', r.get_data(as_text=True).lower())
        r=self.c.post('/users', data={'name':'Pat Field','role':'field','password':'onlylettersxx'}, follow_redirects=True)
        self.assertIn('mix letters', r.get_data(as_text=True).lower())
        # cannot shadow an env login
        r=self.c.post('/users', data={'name':'Roland','role':'office','password':'GoodPass1234'}, follow_redirects=True)
        self.assertIn('already has a Render login', r.get_data(as_text=True))
        r=self.c.post('/users', data={'name':'Pat Field','role':'field','password':'GoodPass1234'}, follow_redirects=True)
        self.assertIn('User added', r.get_data(as_text=True))
        self.c.get('/logout')
        r=self.c.post('/login', data={'name':'Pat Field','password':'GoodPass1234'}, follow_redirects=False)
        self.assertIn(r.status_code,(302,303))
        # field cannot open Users
        r=self.c.get('/users', follow_redirects=False)
        self.assertIn(r.status_code,(302,303))
        self.c.get('/logout')
        # env login still works
        r=self.c.post('/login', data={'name':'Roland','password':'TestPass1234'}, follow_redirects=False)
        self.assertIn(r.status_code,(302,303))
        # add an office user and confirm is_office
        r=self.c.post('/users', data={'name':'Casey Office','role':'office','password':'OfficePass1234'}, follow_redirects=True)
        self.assertIn('User added', r.get_data(as_text=True))
        self.c.get('/logout')
        r=self.c.post('/login', data={'name':'Casey Office','password':'OfficePass1234'}, follow_redirects=False)
        self.assertIn(r.status_code,(302,303))
        r=self.c.get('/users')
        self.assertEqual(r.status_code,200)
        # deactivate Pat, then login fails
        conn=fo.get_db()
        pat=conn.execute("SELECT id FROM app_users WHERE name_key='pat field'").fetchone()
        self.assertIsNotNone(pat)
        conn.close()
        self.c.post('/users/%d/deactivate'%pat['id'], follow_redirects=True)
        self.c.get('/logout')
        r=self.c.post('/login', data={'name':'Pat Field','password':'GoodPass1234'}, follow_redirects=True)
        self.assertIn('Wrong name or password', r.get_data(as_text=True))
        # cleanup
        self.login('Roland','TestPass1234')
        conn=fo.get_db()
        conn.execute("DELETE FROM app_users WHERE name_key IN ('pat field','casey office')")
        conn.commit(); conn.close()
        users._bust()

if __name__ == '__main__':
    unittest.main(verbosity=2)
