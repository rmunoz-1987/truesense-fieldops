"""Org isolation for True Sense and Datacom: 404s, search, switch, subs."""
import os, tempfile, unittest, shutil, sys, json

TMP = tempfile.mkdtemp(prefix='fo-orgs-test-')
os.environ['DATA_DIR'] = TMP
os.environ['DB_PATH'] = os.path.join(TMP, 'fieldops.db')
os.environ['UPLOAD_FOLDER'] = os.path.join(TMP, 'uploads')
os.environ['BACKUP_DIR'] = os.path.join(TMP, 'backups')
os.environ['ROLAND_PASSWORD'] = 'TestPass1234'
os.environ['OFFICE_NAMES'] = 'Roland,Tony La'
os.environ['WOODY_RODRIGUEZ_PASSWORD'] = 'TestPass1234'
os.environ['TONY_LA_PASSWORD'] = 'TestPass1234'
os.environ['SECRET_KEY'] = 'test-secret-local-only'
os.environ.pop('RENDER', None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fieldops_app as fo
import fieldops_orgs as orgs
import fieldops_users as users
import fieldops_quote as quote
import fieldops_platform as plat


def login(c, name='Roland', pw='TestPass1234'):
    r = c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
    assert r.status_code in (302, 303), name


class OrgTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def switch(self, org_id, next_path='/'):
        return self.c.post('/org/switch', data={'org_id': org_id, 'next': next_path}, follow_redirects=False)

    def add_db_user(self, name, role, org_ids, pw='OrgUser9!xx'):
        login(self.c)
        data = {'name': name, 'role': role, 'password': pw}
        # add_user then set orgs from checkboxes
        for oid in org_ids:
            data.setdefault('org_id', [])
        # werkzeug wants repeated fields: use list
        r = self.c.post('/users', data={'name': name, 'role': role, 'password': pw,
                                        'org_id': [str(i) for i in org_ids]}, follow_redirects=True)
        self.assertIn('User added', r.get_data(as_text=True), r.get_data(as_text=True)[:400])
        self.c.get('/logout')
        return name, pw

    def seed_jobs(self):
        """Roland creates one True Sense job and one Datacom job."""
        conn = fo.get_db()
        has = conn.execute("SELECT id,org_id FROM jobs WHERE id IN ('TS-2026-9001','DC-2026-9001')").fetchall()
        conn.close()
        if len(has) == 2:
            return
        login(self.c)
        self.switch(1)
        r = self.c.post('/api/jobs', json={
            'id': 'TS-2026-9001', 'title': 'TS Camera Job', 'customer': 'Alpha TS Co',
            'address': '100 True Sense St', 'scope': 'Cameras', 'tech': 'Woody Rodriguez',
        })
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.switch(2)
        # Datacom has no env field subs; Roland can be assigned as office
        r = self.c.post('/api/jobs', json={
            'id': 'DC-2026-9001', 'title': 'DC Access Job', 'customer': 'Beta DC Co',
            'address': '200 Datacom Ave', 'scope': 'Access', 'tech': 'Roland',
        })
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.switch(1)
        self.c.get('/logout')

    def test_migrate_seeds_orgs_and_backup(self):
        snap = os.path.join(os.environ['BACKUP_DIR'], 'fieldops-pre-orgs.db')
        self.assertTrue(os.path.exists(snap), 'pre-org backup should exist')
        conn = fo.get_db()
        rows = {r['id']: r['slug'] for r in conn.execute('SELECT id,slug FROM orgs')}
        self.assertEqual(rows[1], 'true-sense')
        self.assertEqual(rows[2], 'datacom')
        # seed jobs from init_db belong to True Sense
        n = conn.execute('SELECT COUNT(*) FROM jobs WHERE org_id=1').fetchone()[0]
        self.assertGreaterEqual(n, 1)
        # Init seed jobs stay on True Sense
        for jid in ('TS-2026-0142', 'TS-2026-0143'):
            row = conn.execute('SELECT org_id FROM jobs WHERE id=?', (jid,)).fetchone()
            if row:
                self.assertEqual(row['org_id'], 1, jid)
        # unique key is per org
        plat.upsert_company(conn, 'Same Name')
        # switch context not available; stamp org 2 directly
        conn.execute(
            'INSERT INTO companies (name,name_key,archived,created_at,updated_at,org_id) VALUES (?,?,0,?,?,?)',
            ('Same Name', plat.nkey('Same Name'), 't', 't', 2))
        conn.commit()
        a = conn.execute('SELECT COUNT(*) FROM companies WHERE name_key=?', (plat.nkey('Same Name'),)).fetchone()[0]
        self.assertEqual(a, 2)
        conn.close()
        login(self.c)
        # Roland default org is True Sense; switcher is present
        html = self.c.get('/').get_data(as_text=True)
        self.assertIn('action="/org/switch"', html)
        self.assertIn('True Sense', html)
        self.assertIn('Datacom', html)
        self.c.get('/logout')

    def test_env_assignments_and_users_page(self):
        login(self.c)
        html = self.c.get('/users').get_data(as_text=True)
        self.assertIn('Companies: True Sense and Datacom', html)
        self.assertIn('Companies: True Sense.', html)  # Tony or Woody
        self.assertIn('name="org_id"', html)
        self.assertIn('value="1"', html)
        self.assertIn('value="2"', html)
        self.assertIn('Add user', html)
        # Save companies shows after a database user exists
        self.c.get('/logout')
        self.add_db_user('Org Assign Check', 'field', [1])
        login(self.c)
        html = self.c.get('/users').get_data(as_text=True)
        self.assertIn('Save companies', html)
        self.c.get('/logout')
        # Tony cannot switch to Datacom
        login(self.c, 'Tony La')
        r = self.switch(2)
        self.assertEqual(r.status_code, 404)
        home = self.c.get('/').get_data(as_text=True)
        self.assertNotIn('action="/org/switch"', home)
        self.c.get('/logout')

    def test_office_leakage_404_and_search(self):
        self.seed_jobs()
        self.add_db_user('Dana Datacom', 'office', [orgs.ORG_DATACOM])
        self.add_db_user('Tess Truesense', 'office', [orgs.ORG_TRUE_SENSE])
        # Datacom-only: 404 on TS job, no TS in search or list
        login(self.c, 'Dana Datacom', 'OrgUser9!xx')
        r = self.c.get('/api/jobs/TS-2026-9001')
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))
        r = self.c.get('/api/jobs')
        ids = [j['id'] for j in r.get_json()]
        self.assertNotIn('TS-2026-9001', ids)
        self.assertIn('DC-2026-9001', ids)
        search = self.c.get('/search?q=Alpha').get_data(as_text=True)
        self.assertNotIn('TS-2026-9001', search)
        self.assertNotIn('Alpha TS Co', search)
        search = self.c.get('/search?q=Beta').get_data(as_text=True)
        self.assertIn('DC-2026-9001', search)
        self.c.get('/logout')
        # True Sense-only: 404 on DC job
        login(self.c, 'Tess Truesense', 'OrgUser9!xx')
        r = self.c.get('/api/jobs/DC-2026-9001')
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))
        r = self.c.get('/api/jobs')
        ids = [j['id'] for j in r.get_json()]
        self.assertIn('TS-2026-9001', ids)
        self.assertNotIn('DC-2026-9001', ids)
        search = self.c.get('/search?q=Datacom').get_data(as_text=True)
        self.assertNotIn('DC-2026-9001', search)
        self.assertNotIn('Beta DC Co', search)
        self.c.get('/logout')

    def test_both_org_switch_sees_one_at_a_time(self):
        self.seed_jobs()
        login(self.c)
        self.switch(1)
        ids = [j['id'] for j in self.c.get('/api/jobs').get_json()]
        self.assertIn('TS-2026-9001', ids)
        self.assertNotIn('DC-2026-9001', ids)
        search = self.c.get('/search?q=Beta').get_data(as_text=True)
        self.assertNotIn('DC-2026-9001', search)
        r = self.c.get('/api/jobs/DC-2026-9001')
        self.assertEqual(r.status_code, 404)
        self.switch(2)
        ids = [j['id'] for j in self.c.get('/api/jobs').get_json()]
        self.assertIn('DC-2026-9001', ids)
        self.assertNotIn('TS-2026-9001', ids)
        search = self.c.get('/search?q=Alpha').get_data(as_text=True)
        self.assertNotIn('TS-2026-9001', search)
        r = self.c.get('/api/jobs/TS-2026-9001')
        self.assertEqual(r.status_code, 404)
        # prefix
        q = self.c.get('/api/quick').get_json()
        self.assertTrue(q['next_id'].startswith('DC-2026-'), q)
        self.switch(1)
        q = self.c.get('/api/quick').get_json()
        self.assertTrue(q['next_id'].startswith('TS-2026-'), q)
        self.c.get('/logout')

    def test_sub_leakage(self):
        self.seed_jobs()
        self.add_db_user('Sam Subdc', 'field', [orgs.ORG_DATACOM])
        # Assign the Datacom job to Sam
        login(self.c)
        self.switch(2)
        r = self.c.post('/api/jobs/DC-2026-9001/reassign', json={'tech': 'Sam Subdc'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # Woody is True Sense only and cannot be assigned a Datacom job
        r = self.c.post('/api/jobs/DC-2026-9001/reassign', json={'tech': 'Woody Rodriguez'})
        self.assertIn(r.status_code, (400, 403, 404))
        self.c.get('/logout')
        # Sam cannot see the True Sense job
        login(self.c, 'Sam Subdc', 'OrgUser9!xx')
        r = self.c.get('/api/jobs/TS-2026-9001')
        self.assertEqual(r.status_code, 404)
        ids = [j['id'] for j in self.c.get('/api/jobs').get_json()]
        self.assertIn('DC-2026-9001', ids)
        self.assertNotIn('TS-2026-9001', ids)
        search = self.c.get('/search?q=Alpha').get_data(as_text=True)
        self.assertNotIn('TS-2026-9001', search)
        self.c.get('/logout')
        # Woody cannot see the Datacom job
        login(self.c, 'Woody Rodriguez')
        r = self.c.get('/api/jobs/DC-2026-9001')
        self.assertEqual(r.status_code, 404)
        ids = [j['id'] for j in self.c.get('/api/jobs').get_json()]
        self.assertNotIn('DC-2026-9001', ids)
        self.c.get('/logout')

    def test_datacom_quote_branding_and_terms(self):
        login(self.c)
        self.switch(2)
        conn = fo.get_db()
        terms = quote.get_saved_terms(conn, org_id=2)
        self.assertIn('Datacom Security', terms)
        self.assertIn('B19441', terms)
        self.assertNotIn('B31163401', terms)
        self.assertNotIn('True Sense Security LLC', terms)
        with fo.app.test_request_context('/quote'):
            from flask import g, session
            g.org_id = 2
            session['org_id'] = 2
            session['name'] = 'Roland'
            co = plat.upsert_company(conn, 'Quote DC Client')
            row, err = quote.save_quote(conn, {
                'title': 'Datacom Estimate',
                'company_id': co['id'],
                'lines': [{'name': 'Camera', 'qty': 1, 'sell': 100, 'cost': 40}],
            }, created_by='Roland')
            self.assertIsNone(err, err)
            token_row = quote.ensure_token(conn, row['id'])
            conn.commit()
            tok = token_row['token'] if hasattr(token_row, 'keys') else token_row
        conn.close()
        html = self.c.get('/q/' + tok).get_data(as_text=True)
        self.assertIn('Datacom Security', html)
        self.assertIn('B19441', html)
        self.assertIn('/static/datacom-logo.png', html)
        self.assertNotIn('True Sense Security', html)
        self.assertNotIn('B31163401', html)
        self.switch(1)
        conn = fo.get_db()
        ts = quote.get_saved_terms(conn, org_id=1)
        conn.close()
        self.assertIn('True Sense Security', ts)
        self.assertIn('B31163401', ts)
        self.c.get('/logout')

    def test_qbo_books_do_not_mix(self):
        self.seed_jobs()
        login(self.c)
        # pending with no org_id is True Sense only, and Datacom sync is off
        os.environ['QBO_SYNC_KEY'] = 'test-qbo-key-xyz'
        try:
            # queue a TS invoice as Roland in org 1 would; just insert a queue row
            conn = fo.get_db()
            conn.execute(
                "INSERT OR REPLACE INTO qbo_queue (job_id,status,payload,queued_at,queued_by,updated_at,org_id) VALUES (?,?,?,?,?,?,?)",
                ('TS-2026-9001', 'queued', '{"job_id":"TS-2026-9001"}', 't', 'Roland', 't', 1))
            conn.execute(
                "INSERT OR REPLACE INTO qbo_queue (job_id,status,payload,queued_at,queued_by,updated_at,org_id) VALUES (?,?,?,?,?,?,?)",
                ('DC-2026-9001', 'queued', '{"job_id":"DC-2026-9001"}', 't', 'Roland', 't', 2))
            conn.commit(); conn.close()
            h = {'X-Sync-Key': 'test-qbo-key-xyz'}
            r = self.c.get('/api/qbo/pending', headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            jobs = [x.get('job_id') for x in r.get_json()]
            self.assertIn('TS-2026-9001', jobs)
            self.assertNotIn('DC-2026-9001', jobs)
            r = self.c.get('/api/qbo/pending?org_id=2', headers=h)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json(), [])
            r = self.c.post('/api/qbo/result', json={'job_id': 'DC-2026-9001', 'status': 'created', 'org_id': 2}, headers=h)
            self.assertEqual(r.status_code, 403)
        finally:
            os.environ.pop('QBO_SYNC_KEY', None)
        # Datacom QBO page says sync is off
        self.switch(2)
        html = self.c.get('/qbo').get_data(as_text=True)
        self.assertIn('QuickBooks sync is off', html)
        self.c.get('/logout')

    def test_shared_catalog_and_blank_datacom_supplier(self):
        login(self.c)
        conn = fo.get_db()
        conn.execute(
            "INSERT INTO catalog_items (name,sku,cost,sell,active,created_at,updated_at) VALUES ('Shared Cam','SC-1',10,25,1,'t','t')")
        conn.commit(); conn.close()
        self.switch(1)
        html = self.c.get('/catalog').get_data(as_text=True)
        self.assertIn('Shared Cam', html)
        self.switch(2)
        html = self.c.get('/catalog').get_data(as_text=True)
        self.assertIn('Shared Cam', html)
        import re
        orders = self.c.get('/orders').get_data(as_text=True)
        m = re.search(r'name="adi_account" value="([^"]*)"', orders)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), '')  # Datacom supplier left blank
        self.switch(1)
        orders = self.c.get('/orders').get_data(as_text=True)
        m = re.search(r'name="adi_account" value="([^"]*)"', orders)
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1) or '463208' in orders)
        self.c.get('/logout')

    def test_datacom_agreement_is_separate(self):
        login(self.c)
        self.switch(2)
        conn = fo.get_db()
        a = conn.execute('SELECT * FROM agreements WHERE org_id=? ORDER BY id DESC LIMIT 1', (2,)).fetchone()
        self.assertIsNotNone(a)
        body = a['body'] or ''
        self.assertTrue('Datacom' in body or 'DC & S' in body, body[:200])
        self.assertNotIn('TRUE SENSE SECURITY', body)
        a1 = conn.execute('SELECT * FROM agreements WHERE org_id=? ORDER BY id DESC LIMIT 1', (1,)).fetchone()
        conn.close()
        self.assertIn('TRUE SENSE', (a1['body'] or '').upper())
        self.c.get('/logout')


if __name__ == '__main__':
    unittest.main()
