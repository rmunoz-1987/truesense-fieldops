"""Client portal: view-only token, service request, Pay now for sent QBO invoices only."""
import os, tempfile, unittest, shutil, sys, json

TMP = tempfile.mkdtemp(prefix='fo-portal-test-')
os.environ['DATA_DIR'] = TMP
os.environ['DB_PATH'] = os.path.join(TMP, 'fieldops.db')
os.environ['UPLOAD_FOLDER'] = os.path.join(TMP, 'uploads')
os.environ['BACKUP_DIR'] = os.path.join(TMP, 'backups')
os.environ['ROLAND_PASSWORD'] = 'TestPass1234'
os.environ['OFFICE_NAMES'] = 'Roland'
os.environ['WOODY_RODRIGUEZ_PASSWORD'] = 'TestPass1234'
os.environ['SECRET_KEY'] = 'test-secret-local-only'
os.environ['QBO_SYNC_KEY'] = 'test-qbo-sync-key-local'
os.environ.pop('RENDER', None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fieldops_app as fo
import fieldops_platform as plat
import fieldops_portal as portal
import fieldops_quote as quote


class PortalTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def _company(self):
        conn = fo.get_db()
        try:
            co = plat.upsert_company(conn, 'Memorial Family Clinic')
            site = plat.upsert_site(conn, co['id'], address='1450 Memorial Dr', name='West clinic')
            if not conn.execute('SELECT 1 FROM equipment WHERE password=?', ('super-secret-nvr-pass',)).fetchone():
                conn.execute(
                    'INSERT INTO equipment (client,site,device,model,install_date,warranty_end,password,notes) VALUES (?,?,?,?,?,?,?,?)',
                    ('Memorial Family Clinic', '1450 Memorial Dr', 'Lobby NVR', 'Hanwha QRN-1630S',
                     '2025-03-12', '2028-03-12', 'super-secret-nvr-pass', 'Admin password on the sticker'))
            if not conn.execute('SELECT 1 FROM jobs WHERE id=?', ('TS-PORTAL-1',)).fetchone():
                conn.execute(
                    'INSERT INTO jobs (id,title,customer,address,status,job_type,company_id,site_id,created_at,sale_amount,private_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    ('TS-PORTAL-1', 'Lobby camera upgrade', 'Memorial Family Clinic', '1450 Memorial Dr',
                     'completed', 'Install', co['id'], site['id'], '2026-03-12', 3070, 'Internal cost note'))
            conn.commit()
            return co['id'], site['id']
        finally:
            conn.close()

    def test_portal_hides_passwords_and_takes_service_request(self):
        cid, sid = self._company()
        conn = fo.get_db()
        link, err = portal.create_link(conn, cid, sid, who='Roland')
        self.assertIsNone(err)
        token = link['token']
        conn.commit(); conn.close()
        r = self.c.get('/p/' + token)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Memorial Family Clinic', html)
        self.assertIn('Lobby NVR', html)
        self.assertIn('Hanwha QRN-1630S', html)
        self.assertNotIn('super-secret-nvr-pass', html)
        self.assertNotIn('Admin password', html)
        self.assertNotIn('3070', html)
        self.assertNotIn('Internal cost note', html)
        self.assertIn('Lobby camera upgrade', html)
        self.assertIn('Request service', html)
        self.assertNotIn('\u2014', html)
        r = self.c.post('/p/%s/service' % token, data={
            'issue': 'Lobby camera is offline after the storm.',
            'contact_name': 'Pat at the front desk', 'phone': '2815550148',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('SC-', r.get_data(as_text=True))
        conn = fo.get_db()
        tick = conn.execute("SELECT * FROM service_tickets WHERE source='portal' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(tick)
        self.assertEqual(tick['company_id'], cid)
        self.assertIn('Lobby camera is offline', tick['issue'])
        conn.close()
        conn = fo.get_db(); portal.revoke_link(conn, token, 'Roland'); conn.commit(); conn.close()
        self.assertEqual(self.c.get('/p/' + token).status_code, 404)

    def test_pay_now_only_for_sent_invoices_with_https_link(self):
        cid, sid = self._company()
        conn = fo.get_db()
        conn.execute(
            'INSERT INTO qbo_drafts (job_id,kind,amount,memo,status,payload,created_by,created_at,updated_at,pay_link) VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('TS-PORTAL-1', 'final', 3070, 'Final', 'queued', '{}', 'Roland', '2026-11-01', '2026-11-01',
             'https://paypal.intuit.com/draft-should-hide'))
        conn.commit()
        link, _ = portal.create_link(conn, cid, sid, who='Roland')
        token = link['token']
        conn.commit(); conn.close()
        html = self.c.get('/p/' + token).get_data(as_text=True)
        self.assertNotIn('Pay now', html)
        self.assertNotIn('draft-should-hide', html)
        headers = {'X-Sync-Key': 'test-qbo-sync-key-local'}
        payload = {
            'job_id': 'TS-PORTAL-1', 'status': 'created',
            'invoiceLink': 'https://paypal.intuit.com/pay/abc123',
            'doc_number': '1044', 'qbo_invoice_id': '99',
        }
        r = self.c.post('/api/qbo/result', json=payload, headers=headers)
        if r.status_code == 404:
            conn = fo.get_db()
            conn.execute('INSERT INTO qbo_queue (job_id,status,payload,queued_at,updated_at) VALUES (?,?,?,?,?)',
                         ('TS-PORTAL-1', 'queued', '{}', '2026-11-01', '2026-11-01'))
            conn.commit(); conn.close()
            r = self.c.post('/api/qbo/result', json=payload, headers=headers)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        html = self.c.get('/p/' + token).get_data(as_text=True)
        self.assertIn('Pay now', html)
        self.assertIn('https://paypal.intuit.com/pay/abc123', html)
        self.assertIn('1044', html)
        r = self.c.post('/api/qbo/result', json={
            'job_id': 'TS-PORTAL-1', 'status': 'created',
            'pay_link': 'http://evil.example/pay',
        }, headers=headers)
        self.assertEqual(r.status_code, 400)
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.post('/customers/%d/portal' % cid, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_quote_appears_and_office_can_copy_link(self):
        cid, sid = self._company()
        conn = fo.get_db()
        q, err = quote.save_quote(conn, {
            'title': 'Pharmacy hall cameras', 'company_id': cid,
            'lines': [{'name': 'Camera', 'qty': 1, 'sell': 400}],
        }, created_by='Roland')
        self.assertIsNone(err)
        quote.ensure_token(conn, q['id'])
        link, _ = portal.create_link(conn, cid, who='Roland')
        conn.commit(); conn.close()
        html = self.c.get('/p/' + link['token']).get_data(as_text=True)
        self.assertIn('Pharmacy hall cameras', html)
        self.assertIn('/q/', html)
        self.login()
        page = self.c.get('/customers/%d' % cid).get_data(as_text=True)
        self.assertIn('Client portal', page)
        self.assertIn('/p/' + link['token'], page)


if __name__ == '__main__':
    unittest.main()
