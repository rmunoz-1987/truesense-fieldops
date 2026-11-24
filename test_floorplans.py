"""Floor Plans list and per-user favorites for jobs, devices, and catalog lines."""
import os, tempfile, unittest, shutil, sys, json, re
from datetime import datetime

TMP = tempfile.mkdtemp(prefix='fo-floor-test-')
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
import fieldops_areas as areas
import fieldops_favorites as favs
import fieldops_floorplans as fp
import fieldops_quote as quote
import fieldops_platform as plat


class FloorPlanFavoritesTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()
        conn = fo.get_db()
        favs.init_schema(conn)
        if not conn.execute("SELECT id FROM jobs WHERE id=?", ('TS-FP-1',)).fetchone():
            conn.execute(
                '''INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,job_type)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                ('TS-FP-1', 'Lobby cameras', 'Memorial Family Clinic', '500 Memorial Dr, Houston, TX',
                 'Cameras and access', 'assigned', 'Woody Rodriguez', datetime.utcnow().isoformat(), 'Survey'))
        if not conn.execute("SELECT id FROM jobs WHERE id=?", ('TS-FP-2',)).fetchone():
            conn.execute(
                '''INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,job_type)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                ('TS-FP-2', 'Warehouse access', 'Other Co', '100 Industrial, Houston, TX',
                 'Access control', 'assigned', 'Roland', datetime.utcnow().isoformat(), 'Install'))
        if not conn.execute("SELECT id FROM jobs WHERE id=?", ('TS-FP-NONE',)).fetchone():
            conn.execute(
                '''INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,job_type)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                ('TS-FP-NONE', 'No plan job', 'Memorial Family Clinic', '500 Memorial',
                 'Service only', 'assigned', 'Woody Rodriguez', datetime.utcnow().isoformat(), 'Service'))
        # plans on 1 and 2
        for jid, name in (('TS-FP-1', 'Dining'), ('TS-FP-2', 'Warehouse')):
            if not fo.plan_files(conn, jid):
                areas.add_blank_area(conn, jid, name, fo, created_by='Roland')
        # devices on job 1
        row = conn.execute('SELECT survey_devices FROM jobs WHERE id=?', ('TS-FP-1',)).fetchone()
        devs = fo.parse_devices(row['survey_devices'] if row else '[]')
        if not devs:
            files = fo.plan_files(conn, 'TS-FP-1')
            fn = files[0] if files else ''
            devs = [
                {'id': 'd1', 'type': 'Dome Cam', 'plan': fn, 'x': 10, 'y': 10, 'label': ''},
                {'id': 'd2', 'type': 'Dome Cam', 'plan': fn, 'x': 20, 'y': 10, 'label': ''},
                {'id': 'd3', 'type': 'Card Reader', 'plan': fn, 'x': 30, 'y': 10, 'label': ''},
            ]
            conn.execute('UPDATE jobs SET survey_devices=? WHERE id=?', (json.dumps(devs), 'TS-FP-1'))
        if not conn.execute("SELECT 1 FROM catalog_items WHERE name=?", ('4K dome camera',)).fetchone():
            conn.execute(
                'INSERT INTO catalog_items (name,sku,unit,category,cost,sell,active,created_at,updated_at) '
                'VALUES (?,?,?,?,?,?,1,?,?)',
                ('4K dome camera', 'CAM-4K', 'ea', 'Cameras', 180, 320, '2026-01-01', '2026-01-01'))
        conn.commit(); conn.close()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_home_grid_has_floor_plans_not_as_service_plans(self):
        self.login()
        today = self.c.get('/').get_data(as_text=True)
        self.assertIn('Floor Plans', today)
        self.assertIn('href="/floor-plans"', today)
        jobs = self.c.get('/jobs').get_data(as_text=True)
        self.assertIn('Floor Plans', jobs)
        self.assertIn('href="/floor-plans"', jobs)
        # Service plans stay in More, distinct from Floor Plans
        more = self.c.get('/more').get_data(as_text=True)
        self.assertIn('Service Plans', more)
        self.assertIn('href="/plans"', more)

    def test_list_shows_jobs_with_plans_and_hides_jobs_without(self):
        self.login()
        page = self.c.get('/floor-plans').get_data(as_text=True)
        self.assertIn('Memorial Family Clinic', page)
        self.assertIn('TS-FP-1', page)
        self.assertIn('500 Memorial Dr', page)
        self.assertIn('3 devices', page)
        self.assertIn('Updated', page)
        self.assertIn('/design/TS-FP-1', page)
        self.assertIn('/?job=TS-FP-1', page)
        self.assertIn('Open plan', page)
        self.assertIn('Open job', page)
        self.assertIn('New plan', page)
        self.assertIn('Search client, site, or job ID', page)
        self.assertNotIn('TS-FP-NONE', page)
        self.assertNotIn('\u2014', page)
        found = self.c.get('/floor-plans?q=Memorial').get_data(as_text=True)
        self.assertIn('TS-FP-1', found)
        self.assertNotIn('TS-FP-2', found)
        miss = self.c.get('/floor-plans?q=no-such-client').get_data(as_text=True)
        self.assertIn('No floor plans match', miss)

    def test_new_plan_creates_survey_and_opens_designer(self):
        self.login()
        r = self.c.post('/floor-plans/new', data={
            'title': 'Front door survey',
            'customer': 'New Client Co',
            'address': '12 Main St',
        }, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        loc = r.headers.get('Location') or ''
        self.assertTrue(loc.startswith('/design/'), loc)
        jid = loc.split('/design/')[1]
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        self.assertEqual(row['job_type'], 'Survey')
        self.assertEqual(row['customer'], 'New Client Co')
        self.assertTrue(fo.plan_files(conn, jid))
        conn.close()
        design = self.c.get(loc)
        self.assertEqual(design.status_code, 200)

    def test_sub_sees_only_assigned_jobs_and_no_prices_or_new_plan(self):
        self.login('Woody Rodriguez', 'TestPass1234')
        page = self.c.get('/floor-plans').get_data(as_text=True)
        self.assertIn('TS-FP-1', page)
        self.assertNotIn('TS-FP-2', page)
        self.assertNotIn('New plan', page)
        self.assertNotIn('$320', page)
        self.assertNotIn('Cost $', page)
        self.assertNotIn('sale', page.lower() if 'Sale $' in page else 'ok')
        r = self.c.get('/floor-plans/new', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        home = self.c.get('/').get_data(as_text=True)
        self.assertIn('href="/floor-plans"', home)

    def test_job_favorites_are_per_user_and_pin_to_top(self):
        self.login()
        r = self.c.post('/api/favorites', json={'kind': 'job', 'item_key': 'TS-FP-2', 'on': True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertIn('TS-FP-2', data['jobs'])
        page = self.c.get('/floor-plans').get_data(as_text=True)
        i2 = page.find('TS-FP-2')
        i1 = page.find('TS-FP-1')
        self.assertGreater(i2, 0)
        self.assertGreater(i1, 0)
        self.assertLess(i2, i1)  # favorited warehouse job is first
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        mine = self.c.get('/api/favorites').get_json()
        self.assertNotIn('TS-FP-2', mine.get('jobs') or [])
        # Woody can favorite his own assigned job
        r = self.c.post('/api/favorites', json={'kind': 'job', 'item_key': 'TS-FP-1', 'on': True})
        self.assertEqual(r.status_code, 200)
        self.assertIn('TS-FP-1', r.get_json()['jobs'])

    def test_device_and_catalog_favorites(self):
        self.login()
        r = self.c.post('/api/favorites', json={'kind': 'device', 'item_key': 'Dome Cam', 'on': True})
        self.assertEqual(r.status_code, 200)
        self.assertIn('Dome Cam', r.get_json()['devices'])
        design = self.c.get('/design/TS-FP-1').get_data(as_text=True)
        self.assertIn('favorites_devices', design)
        self.assertIn('Dome Cam', design)
        self.assertIn('pd-fav', design)
        # catalog is office only
        conn = fo.get_db()
        cat = conn.execute("SELECT id FROM catalog_items WHERE name=?", ('4K dome camera',)).fetchone()
        self.assertTrue(cat)
        cid = cat['id']
        conn.close()
        r = self.c.post('/api/favorites', json={'kind': 'catalog', 'item_key': str(cid), 'on': True})
        self.assertEqual(r.status_code, 200)
        # quote editor shows the favorite chip with the office price (office only)
        co_id = None
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Fav Co')
        row, err = quote.save_quote(conn, {
            'title': 'Fav quote', 'company_id': co['id'],
            'lines': [{'name': 'Labor', 'qty': 1, 'cost': 80, 'sell': 150}],
        }, created_by='Roland')
        conn.commit(); conn.close()
        self.assertIsNone(err)
        ed = self.c.get('/quotes/%d' % row['id']).get_data(as_text=True)
        self.assertIn('Favorite parts and labor', ed)
        self.assertIn('4K dome camera', ed)
        self.assertIn('Star selected item', ed)
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.post('/api/favorites', json={'kind': 'catalog', 'item_key': str(cid), 'on': True})
        self.assertEqual(r.status_code, 403)

    def test_updated_time_is_short_chicago_local(self):
        self.assertEqual(fp.format_local_time('2026-11-24T13:50:52'), 'Nov 24, 7:50 AM CT')
        self.assertEqual(fp.format_local_time('2026-11-24 13:50:52'), 'Nov 24, 7:50 AM CT')
        self.assertEqual(fp.format_local_time('2026-07-04T18:05:00'), 'Jul 4, 1:05 PM CT')
        self.assertEqual(fp.format_local_time(''), '')
        conn = fo.get_db()
        conn.execute("UPDATE photos SET uploaded_at=? WHERE job_id=? AND photo_type='plan'",
                     ('2026-11-24T13:50:52', 'TS-FP-1'))
        conn.execute("UPDATE plan_meta SET created_at=? WHERE job_id=?",
                     ('2026-11-24T13:50:52', 'TS-FP-1'))
        conn.execute("UPDATE jobs SET created_at=? WHERE id=?",
                     ('2026-11-24T13:50:52', 'TS-FP-1'))
        conn.commit(); conn.close()
        self.login()
        page = self.c.get('/floor-plans').get_data(as_text=True)
        self.assertIn('Updated Nov 24, 7:50 AM CT', page)
        self.assertIn('TS-FP-1', page)
        self.assertNotIn('13:50:52', page)
        self.assertNotIn('—', page)


if __name__ == '__main__':
    unittest.main()
