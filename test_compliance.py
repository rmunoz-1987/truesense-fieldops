"""Sub compliance: COI, W-9, signed agreement, Today warnings, soft schedule warning, 1099 report."""
import os, tempfile, unittest, shutil, sys, json, io
from datetime import date, timedelta
from pathlib import Path

TMP = tempfile.mkdtemp(prefix='fo-comp-test-')
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
import fieldops_compliance as comp


class ComplianceTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def _seed_docs(self, sub='Woody Rodriguez', coi_exp=None, w9=False, agreement=False):
        conn = fo.get_db()
        key = fo.ckey(sub)
        if coi_exp is not None:
            conn.execute(
                'INSERT OR REPLACE INTO sub_docs (sub,kind,filename,orig_name,expires,uploaded_at) VALUES (?,?,?,?,?,?)',
                (key, 'insurance', 'coi.pdf', 'coi.pdf', coi_exp, '2026-01-01T00:00:00'))
        if w9:
            conn.execute(
                'INSERT OR REPLACE INTO sub_docs (sub,kind,filename,orig_name,expires,uploaded_at) VALUES (?,?,?,?,?,?)',
                (key, 'w9', 'w9.pdf', 'w9.pdf', None, '2026-01-01T00:00:00'))
        if agreement:
            agr = conn.execute('SELECT id, version FROM agreements ORDER BY id DESC LIMIT 1').fetchone()
            if agr:
                conn.execute(
                    '''INSERT INTO agreement_signs (agreement_id,version,sub,signed_at,ip,signature,name,user_agent)
                       VALUES (?,?,?,?,?,?,?,?)''',
                    (agr['id'], agr['version'], key, '2026-01-02T00:00:00', '1.1.1.1',
                     'data:image/png;base64,aaa', sub, 'test'))
                # may also need countersign - check signed_current
        conn.commit(); conn.close()

    def test_today_and_page_show_missing_and_expired(self):
        expired = (date.today() - timedelta(days=5)).isoformat()
        self._seed_docs(coi_exp=expired, w9=False, agreement=False)
        self.login()
        page = self.c.get('/compliance').get_data(as_text=True)
        self.assertEqual(self.c.get('/compliance').status_code, 200)
        self.assertIn('Woody Rodriguez', page)
        self.assertIn('Expired ', page)
        self.assertIn('W-9 is not on file', page)
        self.assertIn('agreement is not signed', page.lower().replace('current ', ''))
        self.assertIn('1099', page)
        self.assertNotIn('\u2014', page)
        today = self.c.get('/api/today').get_json()
        titles = [a['title'] for a in today['actions']]
        self.assertIn('Sub compliance', titles)
        self.assertTrue(any('COI' in (a.get('detail') or '') or 'expired' in (a.get('detail') or '') for a in today['actions'] if a['title']=='Sub compliance'))
        # sub cannot open the page
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/compliance', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_schedule_warns_but_still_places(self):
        # non-compliant sub; placing a job still succeeds with a warning
        conn = fo.get_db()
        conn.execute(
            '''INSERT INTO jobs (id,title,customer,address,job_type,status,tech,created_at,created_by)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            ('TS-COMP1', 'Camera check', 'Memorial Family Clinic', '1450 Memorial Dr',
             'Service', 'assigned', '', '2026-11-01T00:00:00', 'Roland'))
        conn.commit(); conn.close()
        self.login()
        day = date.today().isoformat()
        r = self.c.post('/api/schedule/slots', data=json.dumps({
            'job_id': 'TS-COMP1', 'day': day, 'sub': 'Woody Rodriguez',
            'start': '08:00', 'end': '16:00'
        }), content_type='application/json')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body.get('ok'))
        self.assertTrue(body.get('warning'))
        self.assertIn('W-9', body['warning'])
        self.assertIn('warning only', body['warning'].lower())
        # job was actually placed
        conn = fo.get_db()
        slot = conn.execute('SELECT * FROM job_slots WHERE job_id=?', ('TS-COMP1',)).fetchone()
        conn.close()
        self.assertIsNotNone(slot)
        self.assertEqual((slot['sub'] or '').lower(), 'woody rodriguez')

    def test_w9_on_file_and_1099_csv(self):
        self.login()
        # mark W-9 on file without upload
        r = self.c.post('/compliance', data={'name': 'Woody Rodriguez', 'kind': 'w9', 'on_file': '1'},
                        follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn('Saved', html)
        self.assertIn('W-9 is on file', html)
        # punches for 1099
        year = date.today().year
        conn = fo.get_db()
        conn.execute('INSERT OR IGNORE INTO subs (key,rate_hourly,updated_at) VALUES (?,?,?)',
                     ('woody rodriguez', 45, '2026-01-01'))
        # some DBs may use different subs columns
        try:
            conn.execute('UPDATE subs SET rate_hourly=? WHERE key=?', (45, 'woody rodriguez'))
        except Exception:
            pass
        # inspect punches schema if insert fails
        start = '%d-03-01T08:00:00' % year
        end = '%d-03-01T16:00:00' % year
        conn.execute(
            '''INSERT INTO time_punches (sub,job_id,in_at,out_at,minutes) VALUES (?,?,?,?,?)''',
            ('woody rodriguez', 'TS-COMP1', start, end, 480))
        conn.commit(); conn.close()
        page = self.c.get('/compliance/1099?year=%d' % year).get_data(as_text=True)
        self.assertEqual(self.c.get('/compliance/1099?year=%d' % year).status_code, 200)
        self.assertIn('Woody Rodriguez', page)
        self.assertIn('8.0', page)  # 480 minutes
        self.assertNotIn('\u2014', page)
        csv = self.c.get('/compliance/1099.csv?year=%d' % year)
        self.assertEqual(csv.status_code, 200)
        self.assertIn('text/csv', csv.headers.get('Content-Type', ''))
        text = csv.get_data(as_text=True)
        self.assertIn('Woody Rodriguez', text)
        self.assertIn('8.0', text)
        # sub blocked
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/compliance/1099?year=%d' % year, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))


if __name__ == '__main__':
    unittest.main()
