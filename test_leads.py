"""Leads: Zack import, fit grade, start survey, lost reason, follow-ups, phone list."""
import os, tempfile, unittest, shutil, sys, json
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix='fo-leads-test-')
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
import fieldops_leads as leads
import fieldops_platform as plat
import fieldops_users as users
import fieldops_crm as crm



def zack_payload(prefix, phone_a='713-555-0100', phone_b='713-555-0200'):
    return json.dumps({
        'leads': [
            {
                'company': prefix + ' Acme Warehouse',
                'fit': 'A',
                'address': '100 Industrial Rd, Houston TX 77001',
                'phone': phone_a,
                'contact': 'Pat Buyer',
                'email': 'pat@acme.example',
                'why': 'New 80,000 SF warehouse going up this quarter.',
                'source': 'https://example.com/acme',
                'subject': 'Site cameras at the new warehouse',
                'body': 'Hi Pat,\n\nI saw the new warehouse on Industrial Rd.\n',
            },
            {
                'company': prefix + ' Bayou Offices',
                'fit': 'B',
                'address': '200 Westheimer Rd, Houston TX 77006',
                'phone': phone_b,
                'contact': 'Sam Lee',
                'why': 'Office remodel, access control likely.',
                'subject': 'Access control for Bayou Offices',
                'body': 'Hi Sam,\n\nQuick note about the remodel.\n',
            },
        ]
    })


class LeadsTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), name)

    def add_estimator(self, name='Alex Estimator', pw='Estimator9!x'):
        self.login()
        r = self.c.post('/users', data={'name': name, 'role': 'estimator', 'password': pw}, follow_redirects=True)
        self.assertIn('User added', r.get_data(as_text=True))
        self.c.get('/logout')
        return name, pw

    def test_parse_zack_json_and_csv(self):
        recs, err = leads.parse_leads_payload(zack_payload('Parse'), filename='leads_2026-11-18.json')
        self.assertIsNone(err)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]['company'], 'Parse Acme Warehouse')
        self.assertEqual(recs[0]['fit'], 'A')
        self.assertEqual(recs[0]['lead_source'], 'zack')
        self.assertEqual(recs[0]['phone_key'], '7135550100')
        self.assertIn(leads.DRAFT_MARK, recs[0]['draft'])
        self.assertIn('Site cameras at the new warehouse', recs[0]['draft'])
        csv_text = (
            'company,address,phone,fit,why,subject,body\n'
            'Csv Co,9 Main St,832-555-9999,C,Timing is good,Hello,Hi there\n'
        )
        recs, err = leads.parse_leads_payload(csv_text, filename='leads.csv')
        self.assertIsNone(err)
        self.assertEqual(recs[0]['company'], 'Csv Co')
        self.assertEqual(recs[0]['fit'], 'C')

        # older prospect shape
        raw = json.dumps([{
            'prospect': 'Old Prospect LLC',
            'why_now': 'Breaking ground next week',
            'to': 'info@old.example',
            'source_url': 'https://old.example',
            'contact_route': 'Houston office 281-555-0444',
            'subject': 'Hi',
            'body': 'Hello team',
        }])
        recs, err = leads.parse_leads_payload(raw)
        self.assertIsNone(err)
        self.assertEqual(recs[0]['company'], 'Old Prospect LLC')
        self.assertEqual(recs[0]['phone_key'], '2815550444')
        self.assertEqual(recs[0]['email'], 'info@old.example')

    def test_import_skips_duplicates_and_keeps_draft_as_notes(self):
        conn = fo.get_db()
        recs, err = leads.parse_leads_payload(zack_payload('Dup', phone_a='713-555-1100', phone_b='713-555-1200'))
        self.assertIsNone(err)
        first = leads.import_leads(conn, recs, created_by='Roland', created_at='2026-11-18T12:00:00')
        conn.commit()
        self.assertEqual(first['added'], 2)
        self.assertEqual(first['skipped'], 0)
        second = leads.import_leads(conn, recs, created_by='Roland')
        self.assertEqual(second['added'], 0)
        self.assertEqual(second['skipped'], 2)
        # same phone, different company name, still a duplicate
        twin, err = leads.parse_leads_payload(json.dumps([{
            'company': 'Dup Acme Warehouse Houston',
            'phone': '713-555-1100',
            'fit': 'B',
            'why': 'Same desk number',
        }]))
        self.assertIsNone(err)
        third = leads.import_leads(conn, twin, created_by='Roland')
        self.assertEqual(third['added'], 0)
        self.assertEqual(third['skipped'], 1)
        # same company plus address, different phone
        addr, err = leads.parse_leads_payload(json.dumps([{
            'company': 'Dup Acme Warehouse',
            'address': '100 Industrial Rd, Houston TX 77001',
            'phone': '832-555-7777',
            'why': 'Same site',
        }]))
        fourth = leads.import_leads(conn, addr, created_by='Roland')
        self.assertEqual(fourth['added'], 0)
        self.assertEqual(fourth['skipped'], 1)
        rows = list(conn.execute('SELECT * FROM opportunities ORDER BY id'))
        self.assertEqual(len([r for r in rows if (r['title'] or '').startswith('Dup ')]), 2)
        acme = next(r for r in rows if r['title']=='Dup Acme Warehouse')
        self.assertEqual(acme['lead_source'], 'zack')
        self.assertEqual(acme['fit_grade'], 'A')
        self.assertIn(leads.DRAFT_MARK, acme['notes'] or '')
        self.assertIn('Hi Pat', acme['notes'] or '')
        notes = list(conn.execute('SELECT body FROM crm_activities WHERE opportunity_id=?', (acme['id'],)))
        self.assertTrue(any(leads.DRAFT_MARK in (n['body'] or '') for n in notes))
        self.assertIsNone(acme['contacted_at'])
        # nothing was queued or sent
        drafts = list(conn.execute("SELECT body,phone FROM msg_drafts WHERE IFNULL(body,'') LIKE '%Acme%' OR IFNULL(phone,'') LIKE '%555-1100%'"))
        self.assertEqual(drafts, [])
        conn.close()

    def test_import_form_is_office_only_and_list_shows_fit_and_buttons(self):
        self.login()
        payload = zack_payload('List', phone_a='713-555-2100', phone_b='713-555-2200')
        r = self.c.post('/leads/import', data={'payload': payload}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Imported 2', html)
        self.assertIn('List Acme Warehouse', html)
        self.assertIn('class="fit-badge fit-A"', html)
        self.assertIn('Zack', html)
        self.assertIn('>Call</a>', html)
        self.assertIn('tel:+17135552100', html)
        self.assertIn('>Notes</a>', html)
        self.assertIn('>Start survey</button>', html)
        self.assertIn('Import Zack leads', html)
        # sort by fit: A before B
        a = html.find('List Acme Warehouse')
        b = html.find('List Bayou Offices')
        self.assertTrue(0 <= a < b)
        self.c.get('/logout')
        name, pw = self.add_estimator()
        self.login(name, pw)
        r = self.c.get('/leads')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('List Acme Warehouse', html)
        self.assertNotIn('Import Zack leads', html)
        r = self.c.post('/leads/import', data={'payload': payload}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/leads', follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/leads/import', data={'payload': payload}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)

    def test_start_survey_carries_customer_and_site(self):
        conn = fo.get_db()
        recs, _ = leads.parse_leads_payload(zack_payload('Survey', phone_a='713-555-3100', phone_b='713-555-3200'))
        result = leads.import_leads(conn, recs[:1], created_by='Roland')
        self.assertEqual(result['added'], 1)
        oid = result['ids'][0]
        conn.commit(); conn.close()
        self.login()
        r = self.c.post('/opportunities/%d/survey' % oid, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        loc = r.headers.get('Location') or ''
        self.assertIn('/?job=', loc)
        conn = fo.get_db()
        opp = conn.execute('SELECT * FROM opportunities WHERE id=?', (oid,)).fetchone()
        self.assertTrue(opp['survey_job_id'])
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (opp['survey_job_id'],)).fetchone()
        self.assertEqual(job['job_type'], 'Survey')
        self.assertEqual(job['customer'], 'Survey Acme Warehouse')
        self.assertIn('100 Industrial', job['address'] or '')
        self.assertEqual(job['company_id'], opp['company_id'])
        self.assertEqual(job['site_id'], opp['site_id'])
        conn.close()
        html = self.c.get('/opportunities/%d' % oid).get_data(as_text=True)
        self.assertIn('Start quote', html)
        self.assertIn('Open survey', html)
        self.assertIn('Start a job from this', html)
        # second start reuses the same survey
        r = self.c.post('/opportunities/%d/survey' % oid, follow_redirects=False)
        self.assertIn(opp['survey_job_id'], r.headers.get('Location') or '')

    def test_lost_reason_is_required(self):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Lost Co')
        conn.execute(
            'INSERT INTO opportunities (company_id,title,stage,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?)',
            (co['id'], 'Camera bid', 'lead', 'Roland', '2026-11-01', '2026-11-01'))
        oid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit(); conn.close()
        self.login()
        r = self.c.post('/api/opportunities/%d/stage' % oid, json={'stage': 'lost'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json().get('need'), 'lost_reason')
        conn = fo.get_db()
        self.assertEqual(conn.execute('SELECT stage FROM opportunities WHERE id=?', (oid,)).fetchone()['stage'], 'lead')
        conn.close()
        r = self.c.post('/api/opportunities/%d/stage' % oid,
                        json={'stage': 'lost', 'lost_reason': 'other'})
        self.assertEqual(r.status_code, 400)
        r = self.c.post('/api/opportunities/%d/stage' % oid,
                        json={'stage': 'lost', 'lost_reason': 'price', 'lost_note': 'Too high'})
        self.assertEqual(r.status_code, 200)
        conn = fo.get_db()
        row = conn.execute('SELECT stage,lost_reason,lost_note FROM opportunities WHERE id=?', (oid,)).fetchone()
        conn.close()
        self.assertEqual(row['stage'], 'lost')
        self.assertEqual(row['lost_reason'], 'price')
        self.assertEqual(row['lost_note'], 'Too high')
        html = self.c.get('/sales').get_data(as_text=True)
        self.assertIn('>Won</small>', html)
        self.assertIn('>Lost</small>', html)
        self.assertIn('id="fo-lost-modal"', self.c.get('/opportunities').get_data(as_text=True))
        self.assertIn('Why was this lost?', self.c.get('/opportunities').get_data(as_text=True))

    def test_followups_on_today_with_age(self):
        today = fo.today_local()
        old = (today - timedelta(days=3)).isoformat() + 'T12:00:00'
        older = (today - timedelta(days=10)).isoformat() + 'T12:00:00'
        conn = fo.get_db()
        a = plat.upsert_company(conn, 'Fresh Lead Co')
        b = plat.upsert_company(conn, 'Stale Lead Co')
        conn.execute(
            'INSERT INTO opportunities (company_id,title,stage,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?)',
            (a['id'], 'Fresh cameras', 'lead', 'Roland', old, old))
        fresh_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute(
            'INSERT INTO opportunities (company_id,title,stage,created_by,created_at,updated_at,contacted_at,last_activity_at) VALUES (?,?,?,?,?,?,?,?)',
            (b['id'], 'Stale cameras', 'qualified', 'Roland', older, older, older, older))
        stale_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit(); conn.close()
        self.login()
        data = self.c.get('/api/today').get_json()
        titles = [x['title'] for x in data['actions']]
        self.assertIn('New leads not contacted', titles)
        self.assertIn('Stale leads', titles)
        fresh = next(x for x in data['actions'] if x['title'] == 'New leads not contacted')
        self.assertIn('Fresh Lead Co', fresh['detail'])
        self.assertIn('3 days', fresh['detail'])
        self.assertEqual(fresh['href'], '/leads?need=contact')
        stale = next(x for x in data['actions'] if x['title'] == 'Stale leads')
        self.assertIn('Stale Lead Co', stale['detail'])
        self.assertTrue('10 days' in stale['detail'] or 'days' in stale['detail'])
        html = self.c.get('/leads?need=contact').get_data(as_text=True)
        self.assertIn('Fresh Lead Co', html)
        self.assertIn('Not contacted', html)
        # logging a real call clears the uncontacted reminder
        conn = fo.get_db()
        crm.add_activity(conn, 'call', 'Reached Pat at the front desk.', company_id=a['id'],
                         opportunity_id=fresh_id, created_by='Roland')
        conn.commit(); conn.close()
        data = self.c.get('/api/today').get_json()
        fresh_now = [x for x in data['actions'] if x['title'] == 'New leads not contacted']
        if fresh_now:
            self.assertNotIn('Fresh Lead Co', fresh_now[0]['detail'])
        # no auto messages
        conn = fo.get_db()
        drafts = list(conn.execute("SELECT body,phone FROM msg_drafts WHERE IFNULL(body,'') LIKE '%Acme%' OR IFNULL(phone,'') LIKE '%555-1100%'"))
        self.assertEqual(drafts, [])
        conn.close()

    def test_csrf_field_on_import_and_survey_forms(self):
        self.login()
        html = self.c.get('/leads').get_data(as_text=True)
        self.assertIn('name="_csrf"', html)
        self.assertIn('action="/leads/import"', html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
