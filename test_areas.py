"""Plan areas: sidebar actions, count into quote, notes and photos, walk and present."""
import io, os, tempfile, unittest, shutil, sys, json
from datetime import datetime

TMP = tempfile.mkdtemp(prefix='fo-areas-test-')
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
import fieldops_quote as quote
import fieldops_platform as plat


class AreaTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()
        self.jid = 'TS-AREA-1'
        conn = fo.get_db()
        if not conn.execute('SELECT id FROM jobs WHERE id=?', (self.jid,)).fetchone():
            conn.execute(
                '''INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,job_type)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                (self.jid, 'Lobby cameras', 'Memorial Family Clinic', '500 Memorial Dr, Houston, TX',
                 'Cameras and access', 'assigned', 'Woody Rodriguez', datetime.utcnow().isoformat(), 'Survey'))
            conn.commit()
        conn.close()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def _add(self, name):
        r = self.c.post('/api/jobs/%s/areas' % self.jid, json={'name': name})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _place(self, fn, typ='Dome Cam', n=2):
        conn = fo.get_db()
        row = conn.execute('SELECT survey_devices FROM jobs WHERE id=?', (self.jid,)).fetchone()
        devs = fo.parse_devices(row['survey_devices'] if row else '[]')
        for i in range(n):
            devs.append({'id': 'd%s%s' % (typ[:2], i + len(devs)), 'type': typ, 'plan': fn,
                         'x': 10 + i * 10, 'y': 20, 'label': ''})
        conn.execute('UPDATE jobs SET survey_devices=? WHERE id=?', (json.dumps(devs), self.jid))
        conn.commit(); conn.close()

    def test_add_rename_reorder_and_duplicate(self):
        self.login()
        a = self._add('Dining room + bar')
        fn1 = a['filename']
        self.assertTrue(fn1)
        self.assertTrue(any(p['fn']==fn1 and p['name']=='Dining room + bar' for p in a['plan_info']))
        b = self._add('Kitchen')
        fn2 = b['filename']
        self._place(fn1, 'Dome Cam', 2)
        self._place(fn2, 'Card Reader', 1)
        # rename via existing meta
        r = self.c.post('/api/jobs/%s/plans/meta' % self.jid, json={'filename': fn2, 'name': 'Kitchen walk-in'})
        self.assertEqual(r.status_code, 200)
        names = [p['name'] for p in r.get_json()['plan_info']]
        self.assertEqual(names[0], 'Dining room + bar')
        self.assertIn('Kitchen walk-in', names)
        # reorder
        r = self.c.post('/api/jobs/%s/areas/reorder' % self.jid, json={'order': [fn2, fn1]})
        self.assertEqual(r.status_code, 200)
        names = [p['name'] for p in r.get_json()['plan_info']]
        self.assertEqual(names, ['Kitchen walk-in', 'Dining room + bar'])
        # duplicate dining
        r = self.c.post('/api/jobs/%s/areas/duplicate' % self.jid, json={'filename': fn1})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(len(d['plan_info']), 3)
        copy = next(p for p in d['plan_info'] if p['fn']==d['filename'])
        self.assertIn('copy', copy['name'].lower())
        conn = fo.get_db()
        devs = fo.parse_devices(conn.execute('SELECT survey_devices FROM jobs WHERE id=?', (self.jid,)).fetchone()['survey_devices'])
        conn.close()
        self.assertEqual(sum(1 for x in devs if x['plan']==d['filename'] and x['type']=='Dome Cam'), 2)
        self.assertEqual(sum(1 for x in devs if x['plan']==fn1 and x['type']=='Dome Cam'), 2)

    def test_notes_photos_and_asbuilt_groups_by_area(self):
        self.login()
        fn = self._add('Lobby')['filename']
        r = self.c.post('/api/jobs/%s/areas/notes' % self.jid, json={
            'filename': fn, 'notes': 'Pendants over the bar are customer-supplied.'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['area_notes'][fn], 'Pendants over the bar are customer-supplied.')
        png = areas.blank_png(40, 30)
        r = self.c.post('/api/jobs/%s/areas/photos' % self.jid,
                        data={'filename': fn, 'photo': (io.BytesIO(png), 'wall.jpg')},
                        content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        photos = r.get_json()['area_photos'][fn]
        self.assertEqual(len(photos), 1)
        conn = fo.get_db()
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (self.jid,)).fetchone()
        data = fo.asbuilt_pdf_bytes(conn, job, False, 'Roland')
        conn.close()
        import pymupdf
        doc = pymupdf.open(stream=data, filetype='pdf')
        text = '\n'.join(page.get_text() for page in doc)
        doc.close()
        self.assertIn('Area: Lobby', text)
        self.assertIn('Pendants over the bar are customer-supplied.', text)

    def test_count_into_quote_updates_quantities_and_is_office_only(self):
        self.login()
        fn = self._add('Dining room + bar')['filename']
        conn = fo.get_db()
        conn.execute(
            'INSERT INTO catalog_items (name,sku,unit,category,cost,sell,active,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?)',
            ('Dome Cam', 'CAM-DOME', 'ea', 'Cameras', 100, 999, '2026-01-01', '2026-01-01'))
        conn.commit(); conn.close()
        self._place(fn, 'Dome Cam', 2)
        r = self.c.post('/api/jobs/%s/areas/count-into-quote' % self.jid,
                        json={'scope': 'area', 'filename': fn})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        lines = d['lines']
        dome = next(ln for ln in lines if ln['name']=='Dome Cam')
        self.assertEqual(dome['qty'], 2)
        self.assertEqual(dome['cost'], 100)
        self.assertEqual(dome['sell'], 125)  # cost + 25%
        qid = d['quote_id']
        # re-run with 5 devices updates qty, does not duplicate the line
        self._place(fn, 'Dome Cam', 3)
        r = self.c.post('/api/jobs/%s/areas/count-into-quote' % self.jid,
                        json={'scope': 'area', 'filename': fn})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['quote_id'], qid)
        domes = [ln for ln in d['lines'] if ln.get('source')=='plan-count' and ln['name']=='Dome Cam']
        self.assertEqual(len(domes), 1)
        self.assertEqual(domes[0]['qty'], 5)
        # sub cannot count
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.post('/api/jobs/%s/areas/count-into-quote' % self.jid,
                        json={'scope': 'area', 'filename': fn})
        self.assertEqual(r.status_code, 403)
        html = r.get_data(as_text=True)
        self.assertNotIn('125', html)

    def test_walk_and_present_pages_and_share_present(self):
        self.login()
        fn = self._add('Dining room + bar')['filename']
        self._place(fn, 'Dome Cam', 2)
        page = self.c.get('/design/%s' % self.jid)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Start walk', html)
        self.assertIn('Present', html)
        self.assertIn('Count into quote', html)
        self.assertIn('No scale, set it', html)
        self.assertIn('Area notes', html)
        self.assertIn('pd-leg-edit', html)
        self.assertIn('paintLegend', html)
        self.assertIn('width:auto!important', html)
        self.assertIn('+ Add area', html)
        self.assertIn('AREAS', html)
        self.assertNotIn('Consolidator', html)
        walk = self.c.get('/design/%s?walk=1' % self.jid).get_data(as_text=True)
        self.assertIn('class="pd-walk"', walk)
        self.assertIn('Next area', walk)
        present = self.c.get('/design/%s?present=1' % self.jid)
        self.assertEqual(present.status_code, 200)
        ph = present.get_data(as_text=True)
        self.assertIn('class="pd-present"', ph)
        self.assertIn('Legend', ph)
        self.assertNotIn('$100', ph)
        self.assertNotIn('Cost $', ph)
        # share present
        r = self.c.post('/job/%s/share' % self.jid, data={'action': 'create', 'kind': 'plan', 'days': '30'},
                        follow_redirects=True)
        conn = fo.get_db()
        link = conn.execute('SELECT token FROM share_links WHERE job_id=? ORDER BY created_at DESC', (self.jid,)).fetchone()
        conn.close()
        self.assertTrue(link)
        token = link['token']
        self.c.get('/logout')
        page = self.c.get('/s/%s/present' % token)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('class="pd-present"', html)
        self.assertIn('Dining room + bar', html)
        self.assertNotIn('$125', html)
        self.assertNotIn('Cost $', html)
