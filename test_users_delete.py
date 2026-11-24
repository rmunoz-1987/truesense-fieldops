"""Office can delete database logins, with confirm, guards, audit, and history kept."""
import os, tempfile, unittest, shutil, sys

TMP = tempfile.mkdtemp(prefix='fo-udel-test-')
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


class UsersDeleteTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), name)

    def add_user(self, name, role, pw):
        self.login()
        r = self.c.post('/users', data={'name': name, 'role': role, 'password': pw}, follow_redirects=True)
        self.assertIn('User added', r.get_data(as_text=True), name)
        self.c.get('/logout')
        return self.uid(name)

    def uid(self, name):
        conn = fo.get_db()
        row = conn.execute('SELECT id FROM app_users WHERE name_key=?', (users.nkey(name),)).fetchone()
        conn.close()
        self.assertIsNotNone(row, name)
        return row['id']

    def test_users_page_shows_delete_for_db_users_only(self):
        uid = self.add_user('Dana Field', 'field', 'GoodPass1234')
        self.login()
        html = self.c.get('/users').get_data(as_text=True)
        self.assertIn('href="/users/%d/delete"' % uid, html)
        # Render logins stay read-only
        render = html.split('<h2>Database users</h2>')[0]
        self.assertIn('Set in Render', render)
        self.assertIn('Roland', render)
        self.assertNotIn('/delete', render)
        self.c.get('/logout')

    def test_confirm_page_requires_typed_name_and_has_csrf_field(self):
        uid = self.add_user('Confirm User', 'field', 'GoodPass1234')
        self.login()
        r = self.c.get('/users/%d/delete' % uid)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Type the name to confirm', html)
        self.assertIn('name="confirm_name"', html)
        self.assertIn('name="_csrf"', html)
        self.assertIn('Confirm User', html)
        self.assertIn('action="/users/%d/delete"' % uid, html)
        # wrong name leaves the login in place
        r = self.c.post('/users/%d/delete' % uid, data={'confirm_name': 'Nope'}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Type the name to confirm', r.get_data(as_text=True))
        conn = fo.get_db()
        self.assertIsNotNone(conn.execute('SELECT id FROM app_users WHERE id=?', (uid,)).fetchone())
        conn.close()
        self.c.get('/logout')

    def test_delete_removes_login_writes_audit_keeps_history_and_ends_session(self):
        uid = self.add_user('History User', 'field', 'GoodPass1234')
        # history rows keep the name as plain text
        conn = fo.get_db()
        conn.execute(
            "INSERT INTO jobs (id,title,customer,address,status,tech,created_at,created_by) VALUES (?,?,?,?,?,?,?,?)",
            ('TS-TEST-UDEL', 'Delete history job', 'Acme', '1 Main', 'assigned', 'History User', '2026-01-01', 'History User'))
        conn.execute(
            "INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)",
            ('TS-TEST-UDEL', 'History User', 'Arrived on site', '2026-01-01T12:00:00'))
        conn.execute(
            "INSERT INTO quotes (title,status,created_by,created_at,updated_at,total) VALUES (?,?,?,?,?,?)",
            ('Old quote', 'draft', 'History User', '2026-01-01', '2026-01-01', 0))
        conn.commit()
        conn.close()

        held = fo.app.test_client()
        r = held.post('/login', data={'name': 'History User', 'password': 'GoodPass1234'}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        r = held.get('/', follow_redirects=False)
        self.assertEqual(r.status_code, 200)

        self.login()
        r = self.c.post('/users/%d/delete' % uid, data={'confirm_name': 'History User'}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue((r.headers.get('Location') or '').endswith('/users'))

        conn = fo.get_db()
        self.assertIsNone(conn.execute('SELECT id FROM app_users WHERE id=?', (uid,)).fetchone())
        audit = conn.execute(
            "SELECT * FROM user_audit WHERE action='delete' AND user_key=?",
            ('history user',)).fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual(audit['actor'], 'Roland')
        self.assertEqual(audit['user_name'], 'History User')
        job = conn.execute("SELECT tech,created_by FROM jobs WHERE id='TS-TEST-UDEL'").fetchone()
        self.assertEqual(job['tech'], 'History User')
        self.assertEqual(job['created_by'], 'History User')
        upd = conn.execute("SELECT author FROM job_updates WHERE job_id='TS-TEST-UDEL'").fetchone()
        self.assertEqual(upd['author'], 'History User')
        q = conn.execute("SELECT created_by FROM quotes WHERE title='Old quote'").fetchone()
        self.assertEqual(q['created_by'], 'History User')
        conn.close()

        # leftover session no longer authenticates
        r = held.get('/', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue((r.headers.get('Location') or '').endswith('/login'))

        # login with the deleted account fails
        self.c.get('/logout')
        r = self.c.post('/login', data={'name': 'History User', 'password': 'GoodPass1234'}, follow_redirects=True)
        self.assertIn('Wrong name or password', r.get_data(as_text=True))

        # a later login with the same name does not revive the old cookie
        self.login()
        r = self.c.post('/users', data={'name': 'History User', 'role': 'field', 'password': 'NewPass1234x'}, follow_redirects=True)
        self.assertIn('User added', r.get_data(as_text=True))
        r = held.get('/', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue((r.headers.get('Location') or '').endswith('/login'))
        self.c.get('/logout')

    def test_cannot_delete_self(self):
        uid = self.add_user('Casey Office', 'office', 'OfficePass1234')
        self.login('Casey Office', 'OfficePass1234')
        r = self.c.post('/users/%d/delete' % uid, data={'confirm_name': 'Casey Office'}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('cannot delete your own login', r.get_data(as_text=True))
        conn = fo.get_db()
        self.assertIsNotNone(conn.execute('SELECT id FROM app_users WHERE id=?', (uid,)).fetchone())
        conn.close()
        self.c.get('/logout')

    def test_cannot_delete_last_active_office_user(self):
        uid = self.add_user('Last Office', 'office', 'OfficePass1234')
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM app_users WHERE id=?', (uid,)).fetchone()
        old = list(fo.OFFICE_NAMES)
        fo.OFFICE_NAMES = []
        try:
            err = users.delete_user(conn, uid, 'Someone Else', 'Last Office', fo=fo)
            self.assertIsNotNone(err)
            self.assertIn('last office', err.lower())
            still = conn.execute('SELECT id FROM app_users WHERE id=?', (uid,)).fetchone()
            self.assertIsNotNone(still)
        finally:
            fo.OFFICE_NAMES = old
            conn.close()

    def test_field_and_estimator_cannot_delete(self):
        target = self.add_user('Target Field', 'field', 'GoodPass1234')
        field_id = self.add_user('Sam Field', 'field', 'GoodPass1234')
        self.login('Sam Field', 'GoodPass1234')
        r = self.c.get('/users/%d/delete' % target, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        r = self.c.post('/users/%d/delete' % target, data={'confirm_name': 'Target Field'}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.c.get('/logout')
        self.login()
        r = self.c.post('/users', data={'name': 'Alex Estimator', 'role': 'estimator', 'password': 'Estimator9!x'}, follow_redirects=True)
        self.assertIn('User added', r.get_data(as_text=True))
        self.c.get('/logout')
        self.login('Alex Estimator', 'Estimator9!x')
        r = self.c.get('/users/%d/delete' % target, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/users/%d/delete' % target, data={'confirm_name': 'Target Field'}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        conn = fo.get_db()
        self.assertIsNotNone(conn.execute('SELECT id FROM app_users WHERE id=?', (target,)).fetchone())
        conn.close()
        self.c.get('/logout')
        # silence unused
        self.assertTrue(field_id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
