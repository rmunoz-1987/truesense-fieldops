"""Estimate terms: default True Sense T&C, frozen on send, clarifications, print, accept."""
import os, tempfile, unittest, shutil, sys
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix='fo-terms-test-')
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
import fieldops_quote as quote
import fieldops_platform as plat

SIG = 'data:image/png;base64,aaa'


class QuoteTermsTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def _activate_terms(self, conn=None):
        own = conn is None
        if own:
            conn = fo.get_db()
        err = quote.set_default_terms(conn, quote.DEFAULT_TERMS)
        self.assertIsNone(err)
        if own:
            conn.commit(); conn.close()

    def _quote(self, clarifications='Lift work billed extra. Fire alarm by others.'):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Memorial Family Clinic')
        row, err = quote.save_quote(conn, {
            'title': 'Lobby camera upgrade', 'company_id': co['id'],
            'notes': 'Replace the lobby NVR and add two 4K dome cameras.',
            'clarifications': clarifications,
            'lines': [
                {'name': '4K dome camera', 'qty': 2, 'cost': 180, 'sell': 320},
                {'name': 'NVR 8-channel', 'qty': 1, 'cost': 400, 'sell': 720},
            ],
        }, created_by='Roland')
        self.assertIsNone(err)
        conn.commit(); conn.close()
        return row

    def test_draft_terms_are_not_seeded_until_saved(self):
        conn = fo.get_db()
        conn.execute('DELETE FROM settings WHERE key=?', (quote.QUOTE_TERMS_KEY,))
        conn.commit()
        quote.seed_default_terms(conn)
        conn.commit()
        saved = quote.get_saved_terms(conn)
        seeded = conn.execute("SELECT value FROM settings WHERE key=?", (quote.QUOTE_TERMS_KEY,)).fetchone()
        conn.close()
        self.assertEqual(saved, '')
        self.assertTrue(not seeded or not (seeded['value'] or '').strip())
        self.assertIn('8. Warranty: True Sense Security warrants its labor for 1 year', quote.DEFAULT_TERMS)
        self.assertIn('Manufacturer warranties apply to equipment.', quote.DEFAULT_TERMS)
        self.assertIn('50% deposit is due to schedule the work', quote.DEFAULT_TERMS)
        self.login()
        page = self.c.get('/settings').get_data(as_text=True)
        self.assertIn('not used on client links until you save', page)
        self.assertIn('Draft wording (not live)', page)
        self.assertIn('8. Warranty:', page)

    def test_terms_freeze_on_send_and_later_edits_do_not_change_them(self):
        row = self._quote()
        conn = fo.get_db()
        self._activate_terms(conn)
        sent = quote.ensure_token(conn, row['id'])
        self.assertTrue(sent['token'])
        self.assertEqual((sent['terms_text'] or '').strip(), quote.DEFAULT_TERMS)
        self.assertTrue(sent['sent_at'])
        frozen = sent['terms_text']
        # change default settings
        quote.set_default_terms(conn, 'CHANGED TERMS ONLY')
        conn.commit()
        again = quote.ensure_token(conn, row['id'])
        self.assertEqual(again['terms_text'], frozen)
        conn.close()
        html = self.c.get('/q/' + sent['token']).get_data(as_text=True)
        self.assertIn('8. Warranty:', html)
        self.assertNotIn('CHANGED TERMS ONLY', html)
        self.assertIn('Lift work billed extra', html)
        self.assertIn('Valid until', html)
        # date + 15 days
        until = quote.valid_until(again)
        start = quote.estimate_date(again)
        self.assertEqual(until, (datetime.strptime(start, '%Y-%m-%d').date() + timedelta(days=15)).isoformat())
        # 50% of 320*2+720 = 1360, deposit 680
        self.assertIn('$680.00', html)
        self.assertIn('License B31163401', html)
        self.assertIn('I agree to the terms and conditions', html)
        self.assertNotIn('\u2014', html)

    def test_accept_requires_terms_and_stores_timestamp(self):
        row = self._quote()
        conn = fo.get_db()
        self._activate_terms(conn)
        sent = quote.ensure_token(conn, row['id'])
        token = sent['token']
        conn.commit(); conn.close()
        r = self.c.post('/q/%s/accept' % token, data={
            'name': 'Pat Client', 'signature': SIG, 'agree': '1',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        conn = fo.get_db()
        q = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
        self.assertNotEqual(q['status'], 'accepted')
        conn.close()
        r = self.c.post('/q/%s/accept' % token, data={
            'name': 'Pat Client', 'signature': SIG, 'agree': '1', 'agree_terms': '1',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Approved by Pat Client', r.get_data(as_text=True))
        conn = fo.get_db()
        q = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(q['status'], 'accepted')
        self.assertTrue(q['terms_agreed_at'])
        self.assertEqual((q['terms_text'] or '').strip(), quote.DEFAULT_TERMS)
        conn.close()

    def test_print_includes_terms_and_settings_are_office_only(self):
        row = self._quote()
        conn = fo.get_db()
        self._activate_terms(conn)
        sent = quote.ensure_token(conn, row['id'])
        token = sent['token']
        conn.commit(); conn.close()
        html = self.c.get('/q/%s/print' % token).get_data(as_text=True)
        self.assertEqual(self.c.get('/q/%s/print' % token).status_code, 200)
        self.assertIn('8. Warranty:', html)
        self.assertIn('Lift work billed extra', html)
        self.assertIn('Print this estimate', html)
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/settings', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.c.get('/logout')
        self.login()
        page = self.c.get('/settings').get_data(as_text=True)
        self.assertIn('Estimate terms and conditions', page)
        self.assertIn('8. Warranty:', page)
        office_print = self.c.get('/quotes/%d/print' % row['id'])
        self.assertEqual(office_print.status_code, 200)
        self.assertIn('8. Warranty:', office_print.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
