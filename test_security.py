"""Security pass: login lockout, healthz, uploads, office-only money routes, link expiry."""
import io, os, tempfile, unittest, shutil, sys, time
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix='fo-sec-test-')
os.environ['DATA_DIR'] = TMP
os.environ['DB_PATH'] = os.path.join(TMP, 'fieldops.db')
os.environ['UPLOAD_FOLDER'] = os.path.join(TMP, 'uploads')
os.environ['BACKUP_DIR'] = os.path.join(TMP, 'backups')
os.environ['ROLAND_PASSWORD'] = 'TestPass1234'
os.environ['OFFICE_NAMES'] = 'Roland'
os.environ['WOODY_RODRIGUEZ_PASSWORD'] = 'TestPass1234'
os.environ['SECRET_KEY'] = 'test-secret-local-only'
os.environ.pop('RENDER', None)
os.environ['CSRF_STRICT'] = '1'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fieldops_app as fo
import fieldops_quote as quote
import fieldops_platform as plat
import fieldops_security as sec


class SecurityTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def _csrf(self, path='/login'):
        r = self.c.get(path)
        html = r.get_data(as_text=True)
        import re
        m = re.search(r'name="_csrf" value="([^"]+)"', html)
        return m.group(1) if m else ''

    def login(self, name='Roland', pw='TestPass1234'):
        token = self._csrf()
        r = self.c.post('/login', data={'name': name, 'password': pw, '_csrf': token}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_healthz_is_plain_ok(self):
        r = self.c.get('/healthz')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(), b'ok')

    def test_login_lockout_after_five_misses(self):
        fo.LOGIN_FAILS.clear()
        for i in range(5):
            token = self._csrf()
            r = self.c.post('/login', data={'name': 'Roland', 'password': 'wrong-password', '_csrf': token})
            self.assertEqual(r.status_code, 200)
            self.assertIn('Wrong name or password', r.get_data(as_text=True))
        token = self._csrf()
        r = self.c.post('/login', data={'name': 'Roland', 'password': 'TestPass1234', '_csrf': token})
        self.assertIn('Too many wrong tries', r.get_data(as_text=True))
        fo.LOGIN_FAILS.clear()

    def test_csrf_token_required_when_strict(self):
        # no token
        r = self.c.post('/login', data={'name': 'Roland', 'password': 'TestPass1234'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('Reload and try again', r.get_data(as_text=True))
        # with token from the form
        self.login()
        self.c.get('/logout')

    def test_exe_upload_is_rejected(self):
        self.login()
        r = self.c.post('/api/jobs/TS-2026-0142/plans',
                        data={'files': (io.BytesIO(b'MZ-this-is-not-an-image'), 'payload.exe')},
                        content_type='multipart/form-data')
        self.assertIn(r.status_code, (400, 403))
        self.assertNotIn('payload.exe', os.listdir(fo.app.config['UPLOAD_FOLDER']))

    def test_sub_cannot_open_money_routes(self):
        self.login('Woody Rodriguez', 'TestPass1234')
        for path in ('/settings', '/catalog', '/quotes/new', '/compliance', '/compliance/1099', '/compliance/1099.csv'):
            r = self.c.get(path, follow_redirects=False)
            self.assertIn(r.status_code, (302, 303, 403), path)
            body = r.get_data(as_text=True)
            self.assertNotIn('Estimate terms and conditions', body)
            self.assertNotIn('Cost $', body)

    def test_revoked_quote_link_is_gone(self):
        self.login()
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Lockout Clinic')
        row, err = quote.save_quote(conn, {
            'title': 'Lobby', 'company_id': co['id'],
            'lines': [{'name': 'Dome Cam', 'qty': 1, 'cost': 10, 'sell': 20}],
        }, created_by='Roland')
        self.assertIsNone(err)
        quote.set_default_terms(conn, quote.DEFAULT_TERMS)
        sent = quote.ensure_token(conn, row['id'])
        token = sent['token']
        conn.execute('UPDATE quotes SET token_revoked_at=? WHERE id=?', ('2026-01-01T00:00:00', row['id']))
        conn.commit(); conn.close()
        r = self.c.get('/q/' + token)
        self.assertEqual(r.status_code, 410)
        self.assertIn('no longer available', r.get_data(as_text=True))

    def test_secure_cookie_flags(self):
        r = self.c.get('/login')
        cookie = r.headers.get('Set-Cookie') or ''
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertIn('_csrf', r.get_data(as_text=True))
        self.assertIn("default-src 'self'", r.headers.get('Content-Security-Policy') or '')
