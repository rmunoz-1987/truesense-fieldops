"""Phase 2 quote catalog, frozen prices, add-ons, versions, and accept flow."""
import os, tempfile, unittest, shutil, sys, csv, io

TMP = tempfile.mkdtemp(prefix='fo-quote-test-')
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
import fieldops_quote as quote
import fieldops_platform as plat

SIG = 'data:image/png;base64,aaa'


class QuoteTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_catalog_default_markup_and_csv(self):
        conn = fo.get_db()
        self.assertAlmostEqual(quote.sell_from_cost(100), 125.0)
        added, updated, errors = quote.import_catalog_csv(conn, 'name,cost,sku\n4MP dome,80,CAM-4\nNVR 8ch,200,NVR-8\n')
        self.assertEqual(errors, [])
        self.assertEqual(added, 2)
        cam = conn.execute("SELECT * FROM catalog_items WHERE sku='CAM-4'").fetchone()
        self.assertEqual(cam['cost'], 80)
        self.assertEqual(cam['sell'], 100)  # 80 * 1.25
        # update via second import with explicit sell
        added, updated, errors = quote.import_catalog_csv(conn, 'name,cost,sell,sku\n4MP dome,80,110,CAM-4\n')
        self.assertEqual(updated, 1)
        cam = conn.execute("SELECT * FROM catalog_items WHERE sku='CAM-4'").fetchone()
        self.assertEqual(cam['sell'], 110)
        conn.commit(); conn.close()
        self.login()
        r = self.c.get('/catalog')
        self.assertEqual(r.status_code, 200)
        self.assertIn('4MP dome', r.get_data(as_text=True))
        self.c.get('/logout')
        self.login('Woody Rodriguez', 'TestPass1234')
        r = self.c.get('/catalog', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_quote_freezes_prices_and_versions(self):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Quote Co')
        conn.execute(
            'INSERT INTO opportunities (company_id,title,stage,amount,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
            (co['id'], 'Lobby cameras', 'lead', 0, 'Roland', '2026-01-01', '2026-01-01'))
        oid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute(
            'INSERT INTO catalog_items (name,cost,sell,active,created_at,updated_at) VALUES (?,?,?,1,?,?)',
            ('Dome', 80, 100, '2026-01-01', '2026-01-01'))
        cat_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        row, err = quote.save_quote(conn, {
            'title': 'Lobby cameras', 'company_id': co['id'], 'opportunity_id': oid,
            'lines': [
                {'name': 'Dome', 'qty': 4, 'cost': 80, 'sell': 100, 'catalog_id': cat_id},
                {'name': 'Extra monitor', 'qty': 1, 'cost': 120, 'sell': 150, 'optional': True},
            ],
        }, created_by='Roland')
        self.assertIsNone(err)
        qid = row['id']
        # catalog price changes should not change the frozen quote
        conn.execute('UPDATE catalog_items SET sell=999 WHERE id=?', (cat_id,))
        again = conn.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
        lines = quote.lines_of(again)
        self.assertEqual(lines[0]['sell'], 100)
        self.assertEqual(quote.quote_totals(lines, set())['total'], 400)
        self.assertEqual(quote.quote_totals(lines, {1})['total'], 550)
        # version
        neu, err = quote.save_quote(conn, {
            'title': 'Lobby cameras', 'company_id': co['id'], 'opportunity_id': oid,
            'parent_id': qid, 'lines': lines,
        }, created_by='Roland')
        self.assertIsNone(err)
        self.assertEqual(neu['version'], 2)
        self.assertEqual(neu['parent_id'], qid)
        conn.commit(); conn.close()

    def test_accept_creates_job_and_wins_opportunity(self):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Accept Co')
        conn.execute(
            'INSERT INTO opportunities (company_id,title,stage,amount,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
            (co['id'], 'Front door', 'proposal', 0, 'Roland', '2026-01-01', '2026-01-01'))
        oid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        row, err = quote.save_quote(conn, {
            'title': 'Front door access', 'company_id': co['id'], 'opportunity_id': oid,
            'lines': [
                {'name': 'Maglock', 'qty': 2, 'cost': 100, 'sell': 200},
                {'name': 'Video doorbell', 'qty': 1, 'cost': 80, 'sell': 120, 'optional': True},
            ],
        }, created_by='Roland')
        self.assertIsNone(err)
        quote.set_default_terms(conn, quote.DEFAULT_TERMS)
        row = quote.ensure_token(conn, row['id'])
        token = row['token']
        self.assertTrue(token)
        conn.commit(); conn.close()
        # open tracking (no login)
        r = self.c.get('/q/' + token)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Front door access', html)
        self.assertIn('Video doorbell', html)
        self.assertIn('License B31163401', html)
        self.assertIn('I agree to the terms and conditions', html)
        self.assertIn('10. Warranty', html)
        self.assertIn('Valid until', html)
        self.assertNotIn('Deposit due to schedule', html)  # total is under the $1,000 deposit minimum
        self.assertNotIn('Cost $', html)
        self.assertNotIn('$100', html)
        self.assertNotIn('Cost', html)
        r = self.c.get('/q/' + token)
        conn = fo.get_db()
        qrow = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(qrow['open_count'], 2)
        self.assertTrue(qrow['opened_at'])
        conn.close()
        # accept with the add-on ticked
        r = self.c.post('/q/%s/accept' % token, data={
            'name': 'Pat Client', 'signature': SIG, 'agree': '1', 'agree_terms': '1', 'addon': '1',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Approved by Pat Client', r.get_data(as_text=True))
        conn = fo.get_db()
        qrow = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(qrow['status'], 'accepted')
        self.assertEqual(qrow['total'], 520)  # 400 + 120
        self.assertTrue(qrow['job_id'])
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (qrow['job_id'],)).fetchone()
        self.assertEqual(job['sale_amount'], 520)
        self.assertEqual(job['customer'], 'Accept Co')
        self.assertEqual(job['company_id'], co['id'])
        opp = conn.execute('SELECT * FROM opportunities WHERE id=?', (oid,)).fetchone()
        self.assertEqual(opp['stage'], 'won')
        self.assertEqual(opp['job_id'], qrow['job_id'])
        conn.close()

    def test_start_quote_button_and_pages(self):
        self.login()
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Button Co')
        conn.execute(
            'INSERT INTO opportunities (company_id,title,stage,amount,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
            (co['id'], 'Warehouse cameras', 'qualified', 0, 'Roland', '2026-01-01', '2026-01-01'))
        oid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit(); conn.close()
        html = self.c.get('/opportunities/%d' % oid).get_data(as_text=True)
        self.assertIn('Start quote', html)
        self.assertIn('/quotes/new?opportunity_id=%d' % oid, html)
        r = self.c.get('/quotes/new?opportunity_id=%d' % oid)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Warehouse cameras', r.get_data(as_text=True))
        r = self.c.get('/quote')
        self.assertEqual(r.status_code, 200)
        html=r.get_data(as_text=True)
        self.assertIn('Catalog', html)
        self.assertIn('class="on">Quote</a>', html)
        html=self.c.get('/catalog').get_data(as_text=True)
        self.assertIn('class="on">Quote</a>', html)

    def test_client_lines_stack_on_phones_and_stay_tabular_for_print(self):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Phone Layout Co')
        long_desc = (
            'Labor - Alarm Service: Replace CR123A batteries in five (5) DSC Neo wireless '
            'motion sensors, relocate up to two (2) motion sensors obstructed by shelving '
            'to an optimal coverage location, remount, test sensor communication and walk-test '
            'all serviced zones.'
        )
        row, err = quote.save_quote(conn, {
            'title': 'Service call', 'company_id': co['id'],
            'lines': [
                {'name': 'Materials:Consumables', 'qty': 5, 'cost': 2, 'sell': 3.85,
                 'description': 'Materials - CR123A 3V lithium battery for wireless motion sensors'},
                {'name': 'Labor:T&M Labor Standard', 'qty': 1, 'cost': 80, 'sell': 150,
                 'description': long_desc},
                {'name': 'Subtotal', 'kind': 'subtotal', 'amount': 169.25},
            ],
        }, created_by='Roland')
        self.assertIsNone(err)
        sent = quote.ensure_token(conn, row['id'])
        token = sent['token']
        conn.commit(); conn.close()
        html = self.c.get('/q/' + token).get_data(as_text=True)
        printed = self.c.get('/q/' + token + '/print').get_data(as_text=True)
        self.login()
        office = self.c.get('/quotes/%d/print' % row['id'])
        self.assertEqual(office.status_code, 200)
        office_html = office.get_data(as_text=True)
        for page in (html, printed, office_html):
            self.assertIn('est-block', page)
            self.assertIn('ln-foot', page)
            self.assertIn('Qty 5 x $3.85', page)
            self.assertIn('Qty 1 x $150.00', page)
            self.assertIn(long_desc, page)
            self.assertIn('@media screen and (max-width:600px)', page)
            self.assertIn('.ln-foot{display:none !important}', page)
            self.assertNotIn('—', page)
        # The stacked layout is screen-only; the print page still has the table columns.
        self.assertIn('<th class="qty">Qty</th>', printed)
        self.assertIn('<th class="rate">Rate</th>', printed)

    def test_hides_trailing_matching_subtotal_and_keeps_group_subtotals(self):
        conn = fo.get_db()
        co = plat.upsert_company(conn, 'Subtotal Layout Co')
        # Trailing QBO-style subtotal equal to the summary should be hidden
        # on the client page, print/PDF, and office print.
        row, err = quote.save_quote(conn, {
            'title': 'Trailing subtotal', 'company_id': co['id'],
            'lines': [
                {'name': 'Materials:Consumables', 'qty': 5, 'cost': 2, 'sell': 3.85,
                 'description': 'CR123A batteries'},
                {'name': 'Labor:T&M Labor Standard', 'qty': 1, 'cost': 80, 'sell': 150},
                {'name': 'Fees:Trip Charge', 'qty': 1, 'cost': 40, 'sell': 75},
                {'name': 'Subtotal', 'kind': 'subtotal', 'amount': 244.25, 'qbo_line_id': 'st-1'},
            ],
        }, created_by='Roland')
        self.assertIsNone(err)
        sent = quote.ensure_token(conn, row['id'])
        token = sent['token']
        # Mid-list group subtotals stay, even when a later trailing subtotal matches the summary.
        grouped, err = quote.save_quote(conn, {
            'title': 'Grouped subtotals', 'company_id': co['id'],
            'lines': [
                {'name': 'Lobby cameras', 'qty': 2, 'cost': 100, 'sell': 200},
                {'name': 'Lobby labor', 'qty': 1, 'cost': 80, 'sell': 150},
                {'name': 'Lobby subtotal', 'kind': 'subtotal', 'amount': 550},
                {'name': 'Gate reader', 'qty': 1, 'cost': 80, 'sell': 120},
                {'name': 'Subtotal', 'kind': 'subtotal', 'amount': 670},
            ],
        }, created_by='Roland')
        self.assertIsNone(err)
        grouped_tok = quote.ensure_token(conn, grouped['id'])['token']
        conn.commit(); conn.close()

        lines, totals, _ = quote.decorate_client_lines(row)
        self.assertEqual(totals['subtotal'], 244.25)
        self.assertFalse(any((ln.get('kind') == 'subtotal') for ln in lines))

        html = self.c.get('/q/' + token).get_data(as_text=True)
        printed = self.c.get('/q/' + token + '/print').get_data(as_text=True)
        self.login()
        office = self.c.get('/quotes/%d/print' % row['id']).get_data(as_text=True)
        for page in (html, printed, office):
            self.assertNotIn('class="kind-subtotal"', page)
            self.assertIn('<span>Subtotal</span>', page)
            self.assertIn('$244.25', page)
            self.assertNotIn('—', page)

        g_lines, g_tot, _ = quote.decorate_client_lines(grouped)
        self.assertEqual(g_tot['subtotal'], 670)
        kinds = [(ln.get('name'), ln.get('kind'), ln.get('amount_label')) for ln in g_lines]
        self.assertEqual(
            [k for k in kinds if k[1] == 'subtotal'],
            [('Lobby subtotal', 'subtotal', '$550.00')])
        self.assertFalse(any(k[0] == 'Subtotal' and k[1] == 'subtotal' for k in kinds))
        ghtml = self.c.get('/q/' + grouped_tok).get_data(as_text=True)
        gprint = self.c.get('/q/' + grouped_tok + '/print').get_data(as_text=True)
        goffice = self.c.get('/quotes/%d/print' % grouped['id']).get_data(as_text=True)
        for page in (ghtml, gprint, goffice):
            self.assertIn('class="kind-subtotal"', page)
            self.assertIn('Lobby subtotal', page)
            self.assertIn('$550.00', page)
            self.assertIn('<span>Subtotal</span>', page)
            self.assertIn('$670.00', page)
            # The trailing duplicate summary line is gone; mid-list group subtotal remains.
            self.assertEqual(page.count('class="kind-subtotal"'), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
