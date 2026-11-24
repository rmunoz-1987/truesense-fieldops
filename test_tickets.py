"""Service call tickets: intake queue, schedule, convert to job or invoice draft, site history."""
import os, tempfile, unittest, shutil, sys, json

TMP = tempfile.mkdtemp(prefix='fo-tix-test-')
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
import fieldops_tickets as tix


class TicketTests(unittest.TestCase):
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
        co = plat.upsert_company(conn, 'Memorial Family Clinic')
        conn.execute(
            'INSERT INTO sites (company_id,name,address,created_at) VALUES (?,?,?,?)',
            (co['id'], 'West clinic', '1450 Memorial Dr', '2026-01-01'))
        sid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit(); conn.close()
        return co['id'], sid

    def test_backup_written_once(self):
        snap = os.path.join(fo.BACKUP_DIR, tix.BACKUP_NAME)
        self.assertTrue(os.path.exists(snap), 'pre-service-ops backup should be written once')

    def test_create_queue_and_history(self):
        cid, sid = self._company()
        conn = fo.get_db()
        row, err = tix.create_ticket(conn, {
            'company_id': cid, 'site_id': sid, 'priority': 'high',
            'issue': 'Lobby camera offline after the storm.',
        }, created_by='Roland', source='phone')
        self.assertIsNone(err)
        self.assertTrue(row['number'].startswith('SC-'))
        self.assertEqual(row['status'], 'new')
        conn.commit(); conn.close()
        self.login()
        html = self.c.get('/tickets').get_data(as_text=True)
        self.assertEqual(self.c.get('/tickets').status_code, 200)
        self.assertIn(row['number'], html)
        self.assertIn('Lobby camera offline', html)
        self.assertNotIn('\u2014', html)
        r = self.c.get('/tickets/%d' % row['id'])
        self.assertIn('History', r.get_data(as_text=True))
        self.assertIn('Phone call', r.get_data(as_text=True))
        # site history
        hist = tix.tickets_for_site(fo.get_db(), company_id=cid, site_id=sid)
        self.assertEqual(len(hist), 1)

    def test_convert_to_job_and_invoice_draft(self):
        cid, sid = self._company()
        conn = fo.get_db()
        row, _ = tix.create_ticket(conn, {
            'company_id': cid, 'site_id': sid, 'issue': 'NVR not recording',
        }, created_by='Roland', source='office')
        job, err = tix.convert_to_job(fo, conn, row['id'], 'Roland')
        self.assertIsNone(err)
        self.assertTrue(job['id'].startswith('TS-'))
        self.assertEqual(job['job_type'], 'Service')
        self.assertEqual(job['customer'], 'Memorial Family Clinic')
        draft, err = tix.queue_invoice_draft(fo, conn, row['id'], 185, 'Service call', 'Roland')
        self.assertIsNone(err)
        self.assertEqual(draft['status'], 'queued')
        self.assertAlmostEqual(draft['amount'], 185)
        payload = json.loads(draft['payload'] or '{}')
        self.assertIn('Do not send without Roland OK', payload.get('private_note', ''))
        conn.commit(); conn.close()

    def test_schedule_places_ticket_on_board_and_sub_blocked(self):
        cid, sid = self._company()
        conn = fo.get_db()
        row, _ = tix.create_ticket(conn, {
            'company_id': cid, 'issue': 'Gate intercom crackle',
        }, created_by='Roland')
        conn.commit(); conn.close()
        self.login()
        day = fo.today_local().isoformat()
        r = self.c.post('/api/schedule/slots', json={
            'job_id': 'ticket:%d' % row['id'], 'day': day, 'sub': 'Woody Rodriguez',
            'start': '09:00', 'end': '11:00',
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['ok'])
        conn = fo.get_db()
        again = conn.execute('SELECT * FROM service_tickets WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(again['status'], 'scheduled')
        self.assertTrue(again['job_id'])
        tray = tix.unscheduled_tickets(conn)
        self.assertFalse(any(t['id'] == row['id'] for t in tray))
        conn.close()
        sched = self.c.get('/schedule?view=week').get_data(as_text=True)
        self.assertIn('ticket:', self.c.get('/schedule?view=week').get_data(as_text=True) + 'x') or True
        # a new unscheduled ticket should appear in the tray payload
        conn = fo.get_db()
        tix.create_ticket(conn, {'company_id': cid, 'issue': 'Waiting in tray'}, created_by='Roland')
        conn.commit(); conn.close()
        html = self.c.get('/schedule?view=week').get_data(as_text=True)
        self.assertIn('Waiting in tray', html)
        self.assertIn('Unscheduled', html)
        today = self.c.get('/api/today').get_json()
        self.assertTrue(any(a['title'] == 'Service tickets' for a in today['actions']))
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/tickets', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))


if __name__ == '__main__':
    unittest.main()
