"""Nightly backup endpoint and local restore."""
import os, sqlite3, tempfile, unittest, shutil, sys, tarfile, subprocess
from pathlib import Path

TMP = tempfile.mkdtemp(prefix='fo-bak-test-')
os.environ['DATA_DIR'] = TMP
os.environ['DB_PATH'] = os.path.join(TMP, 'fieldops.db')
os.environ['UPLOAD_FOLDER'] = os.path.join(TMP, 'uploads')
os.environ['BACKUP_DIR'] = os.path.join(TMP, 'backups')
os.environ['ROLAND_PASSWORD'] = 'TestPass1234'
os.environ['OFFICE_NAMES'] = 'Roland'
os.environ['WOODY_RODRIGUEZ_PASSWORD'] = 'TestPass1234'
os.environ['SECRET_KEY'] = 'test-secret-local-only'
os.environ['BACKUP_KEY'] = 'test-backup-key-32chars-xxxxxx'
os.environ.pop('RENDER', None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fieldops_app as fo
import fieldops_backup as bak


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(fo.app.config['UPLOAD_FOLDER'], exist_ok=True)
        Path(fo.app.config['UPLOAD_FOLDER'], 'note.txt').write_text('job photo sample', encoding='utf-8')
        conn = sqlite3.connect(os.environ['DB_PATH'])
        conn.execute("INSERT INTO companies (name, name_key) VALUES ('Backup Co', 'backup co')")
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()
        self.key = os.environ['BACKUP_KEY']

    def test_missing_or_wrong_key_is_rejected(self):
        r = self.c.get('/api/office/backup')
        self.assertEqual(r.status_code, 403)
        r = self.c.get('/api/office/backup', headers={'X-Backup-Key': 'wrong-key'})
        self.assertEqual(r.status_code, 403)
        # a session login must not be enough without the key
        token_page = self.c.get('/login').get_data(as_text=True)
        import re
        m = re.search(r'name="_csrf" value="([^"]+)"', token_page)
        token = m.group(1) if m else ''
        self.c.post('/login', data={'name': 'Roland', 'password': 'TestPass1234', '_csrf': token})
        r = self.c.get('/api/office/backup')
        self.assertEqual(r.status_code, 403)

    def test_backup_downloads_and_restores(self):
        r = self.c.get('/api/office/backup', headers={'X-Backup-Key': self.key})
        self.assertEqual(r.status_code, 200)
        self.assertIn('gzip', (r.mimetype or '').lower() + (r.headers.get('Content-Type') or '').lower())
        data = r.get_data()
        self.assertGreater(len(data), 100)
        archive = os.path.join(TMP, 'pulled.tgz')
        Path(archive).write_bytes(data)
        dest = os.path.join(TMP, 'restore')
        db, uploads = bak.restore_archive(archive, dest)
        self.assertTrue(os.path.isfile(db))
        conn = sqlite3.connect(db)
        try:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('jobs', names)
            row = conn.execute("SELECT name FROM companies WHERE name='Backup Co'").fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()
        self.assertTrue(uploads)
        self.assertTrue(os.path.isfile(os.path.join(uploads, 'note.txt')))
        self.assertEqual(Path(uploads, 'note.txt').read_text(encoding='utf-8'), 'job photo sample')

    def test_pull_script_prunes_and_saves(self):
        # Serve the already-tested endpoint through the script against the test client archive is
        # covered above. Here, confirm the shipped script is executable and mentions 14 days and
        # the live URL, and that a local pack + restore stays consistent.
        script = Path(__file__).parent / 'scripts' / 'pull-nightly-backup.sh'
        self.assertTrue(os.access(script, os.X_OK))
        text = script.read_text(encoding='utf-8')
        self.assertIn('KEEP_DAYS', text)
        self.assertIn('14', text)
        self.assertIn('truesense-fieldops.onrender.com/api/office/backup', text)
        self.assertIn('X-Backup-Key', text)
        packed = bak.pack_archive(os.environ['DB_PATH'], os.environ['UPLOAD_FOLDER'],
                                  os.path.join(TMP, 'direct.tgz'))
        with tarfile.open(packed, 'r:gz') as tar:
            names = tar.getnames()
        self.assertIn('fieldops.db', names)
        self.assertTrue(any(n == 'uploads' or n.startswith('uploads/') for n in names))


if __name__ == '__main__':
    unittest.main()
