"""Read-only QBO estimate import: open estimates from the last 12 months, idempotent."""
import os, tempfile, unittest, shutil, sys, json
from datetime import date, timedelta

TMP = tempfile.mkdtemp(prefix='fo-qboimp-test-')
os.environ['DATA_DIR'] = TMP
os.environ['DB_PATH'] = os.path.join(TMP, 'fieldops.db')
os.environ['UPLOAD_FOLDER'] = os.path.join(TMP, 'uploads')
os.environ['BACKUP_DIR'] = os.path.join(TMP, 'backups')
os.environ['ROLAND_PASSWORD'] = 'TestPass1234'
os.environ['OFFICE_NAMES'] = 'Roland'
os.environ['WOODY_RODRIGUEZ_PASSWORD'] = 'TestPass1234'
os.environ['SECRET_KEY'] = 'test-secret-local-only'
os.environ['QBO_SYNC_KEY'] = 'test-sync-key-local-only'
os.environ.pop('RENDER', None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fieldops_app as fo
import fieldops_qbo_import as qboimp
import fieldops_quote as quote
import fieldops_platform as plat

TODAY = date(2026, 11, 24)


def estimate(eid='101', doc='EST-101', status='Pending', days_ago=10, name='Memorial Family Clinic',
             unit_price=320, qty=2, item_name='4K dome camera', amount=None, txn=None, **extra):
    when = txn or (TODAY - timedelta(days=days_ago)).isoformat()
    amt = amount if amount is not None else unit_price * qty
    est = {
        'Id': eid,
        'DocNumber': doc,
        'TxnDate': when,
        'TxnStatus': status,
        'CustomerRef': {'value': '9', 'name': name},
        'BillAddr': {
            'Line1': '1234 Westheimer Rd',
            'City': 'Houston',
            'CountrySubDivisionCode': 'TX',
            'PostalCode': '77006',
        },
        'ShipAddr': {
            'Line1': '500 Memorial Dr',
            'City': 'Houston',
            'CountrySubDivisionCode': 'TX',
            'PostalCode': '77007',
        },
        'CustomerMemo': {'value': 'Lobby camera upgrade'},
        'Line': [
            {
                'DetailType': 'SalesItemLineDetail',
                'Description': item_name,
                'Amount': amt,
                'SalesItemLineDetail': {
                    'ItemRef': {'value': '33', 'name': item_name},
                    'Qty': qty,
                    'UnitPrice': unit_price,
                },
            },
            {
                'DetailType': 'SubTotalLineDetail',
                'Amount': amt,
            },
        ],
        'TotalAmt': amt,
    }
    est.update(extra)
    return est


class QboImportTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()
        conn = fo.get_db()
        conn.execute(
            'INSERT INTO catalog_items (name,sku,unit,category,cost,sell,active,created_at,updated_at) '
            'SELECT ?,?,?,?,?,?,1,?,? WHERE NOT EXISTS (SELECT 1 FROM catalog_items WHERE LOWER(name)=?)',
            ('4K dome camera', 'CAM-4K', 'ea', 'Cameras', 180, 999, '2026-01-01', '2026-01-01', '4k dome camera'))
        conn.commit(); conn.close()

    def test_imports_open_estimate_and_is_idempotent(self):
        conn = fo.get_db()
        result = qboimp.import_estimates(conn, [estimate()], today=TODAY)
        conn.commit()
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['unchanged'], 0)
        self.assertEqual(result['errors'], [])
        info = result['quotes'][0]
        q = conn.execute('SELECT * FROM quotes WHERE id=?', (info['quote_id'],)).fetchone()
        self.assertEqual(q['qbo_estimate_id'], '101')
        self.assertEqual(q['qbo_doc_number'], 'EST-101')
        self.assertEqual(q['qbo_txn_status'], 'Pending')
        self.assertEqual(q['status'], 'sent')
        self.assertNotIn('Imported from QuickBooks', q['notes'] or '')
        self.assertEqual((q['notes'] or '').strip(), '')
        lines = json.loads(q['lines_json'])
        items = [ln for ln in lines if (ln.get('kind') or 'item') == 'item']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], '4K dome camera')
        self.assertEqual(items[0]['description'], '4K dome camera')
        self.assertEqual(items[0]['qty'], 2)
        self.assertEqual(items[0]['sell'], 320)  # frozen QBO price, not catalog 999
        self.assertEqual(items[0]['cost'], 180)  # from catalog
        self.assertTrue(items[0]['catalog_id'])
        self.assertTrue(any((ln.get('kind') == 'subtotal') for ln in lines))
        self.assertEqual(q['total'], 640)
        self.assertEqual(q['qbo_customer_memo'], 'Lobby camera upgrade')
        self.assertIn('1234 Westheimer', q['qbo_bill_to'] or '')
        self.assertIn('500 Memorial', q['qbo_ship_to'] or '')
        self.assertTrue(q['qbo_import_hash'])
        co = conn.execute('SELECT * FROM companies WHERE id=?', (q['company_id'],)).fetchone()
        self.assertEqual(co['name'], 'Memorial Family Clinic')
        self.assertIn('1234 Westheimer', co['billing_address'] or '')
        site = conn.execute('SELECT * FROM sites WHERE id=?', (q['site_id'],)).fetchone()
        self.assertIn('500 Memorial', site['address'])
        opp = conn.execute('SELECT * FROM opportunities WHERE id=?', (q['opportunity_id'],)).fetchone()
        self.assertEqual(opp['stage'], 'proposal')
        self.assertEqual(opp['amount'], 640)
        self.assertEqual(opp['created_by'], 'QBO import')
        self.assertIn('Imported from QuickBooks estimate EST-101', opp['notes'] or '')
        # second pass does not duplicate
        again = qboimp.import_estimates(conn, [estimate()], today=TODAY)
        conn.commit()
        self.assertEqual(again['imported'], 0)
        self.assertEqual(again['unchanged'], 1)
        nq = conn.execute('SELECT COUNT(*) FROM quotes WHERE qbo_estimate_id=?', ('101',)).fetchone()[0]
        nopp = conn.execute(
            'SELECT COUNT(*) FROM opportunities WHERE id IN (SELECT opportunity_id FROM quotes WHERE qbo_estimate_id=?)',
            ('101',)).fetchone()[0]
        self.assertEqual(nq, 1)
        self.assertEqual(nopp, 1)
        # same company name reuses the row
        nco = conn.execute('SELECT COUNT(*) FROM companies WHERE name_key=?', (plat.nkey('Memorial Family Clinic'),)).fetchone()[0]
        self.assertEqual(nco, 1)
        conn.close()

    def test_skips_closed_and_old_and_accepts_query_wrapper(self):
        conn = fo.get_db()
        payload = {
            'QueryResponse': {
                'Estimate': [
                    estimate('201', 'OLD-1', 'Pending', days_ago=400),
                    estimate('202', 'CLOSED-1', 'Closed', days_ago=5),
                    estimate('203', 'OK-1', 'Accepted', days_ago=20, name='River Oaks Clinic',
                             item_name='Custom horn', unit_price=150, qty=1),
                ]
            }
        }
        result = qboimp.import_estimates(conn, payload, today=TODAY)
        conn.commit()
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(result['errors'], [])
        q = conn.execute('SELECT * FROM quotes WHERE qbo_estimate_id=?', ('203',)).fetchone()
        self.assertEqual(q['qbo_doc_number'], 'OK-1')
        self.assertEqual(q['qbo_txn_status'], 'Accepted')
        lines = json.loads(q['lines_json'])
        self.assertEqual(lines[0]['sell'], 150)
        self.assertEqual(lines[0]['cost'], 0)  # unmapped catalog item
        self.assertFalse(lines[0]['catalog_id'])
        opp = conn.execute('SELECT * FROM opportunities WHERE id=?', (q['opportunity_id'],)).fetchone()
        self.assertEqual(opp['stage'], 'won')
        conn.close()

    def test_api_requires_sync_key_and_imports(self):
        body = {'estimates': [estimate('301', 'API-1', 'Pending', days_ago=3, name='API Import Co')]}
        r = self.c.post('/api/qbo/import-estimates', json=body)
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/api/qbo/import-estimates', json=body, headers={'X-Sync-Key': 'wrong-key'})
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/api/qbo/import-estimates', json=body,
                        headers={'X-Sync-Key': 'test-sync-key-local-only'})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data['imported'], 1)
        self.assertEqual(data['quotes'][0]['qbo_doc_number'], 'API-1')
        # replay
        r = self.c.post('/api/qbo/import-estimates', json=body,
                        headers={'X-Sync-Key': 'test-sync-key-local-only'})
        self.assertEqual(r.get_json()['unchanged'], 1)
        self.assertEqual(r.get_json()['imported'], 0)

    def test_sync_key_is_not_logged_in_module(self):
        path = os.path.join(os.path.dirname(__file__), 'fieldops_qbo_import.py')
        with open(path) as f:
            src = f.read()
        self.assertNotIn('print(', src)
        self.assertNotIn('QBO_SYNC_KEY', src)

    def test_mirrors_full_qbo_estimate_and_renders_descriptions(self):
        est = {
            'Id': '258',
            'DocNumber': '1024',
            'TxnDate': '2026-11-21',
            'TxnStatus': 'Pending',
            'CustomerRef': {'name': 'All Star Security', 'value': '31'},
            'CustomerMemo': {'value': 'Please call before arrival.'},
            'BillAddr': {'Line1': '100 Main St', 'City': 'Houston', 'CountrySubDivisionCode': 'TX', 'PostalCode': '77002'},
            'ShipAddr': {'Line1': 'Job site, 50 Warehouse Rd', 'City': 'Houston', 'CountrySubDivisionCode': 'TX', 'PostalCode': '77011'},
            'ExpirationDate': '2026-12-06',
            'TotalAmt': 264.40,
            'Line': [
                {'Id': '5', 'DetailType': 'SalesItemLineDetail',
                 'Description': 'Materials - CR123A 3V lithium battery for wireless motion sensors',
                 'Amount': 19.25,
                 'SalesItemLineDetail': {'ItemRef': {'name': 'Materials:Consumables', 'value': '104'},
                                        'Qty': 5, 'UnitPrice': 3.85, 'TaxCodeRef': {'name': 'TAX'}}},
                {'Id': '6', 'DetailType': 'SalesItemLineDetail',
                 'Description': 'Labor - Alarm Service: Replace CR123A batteries in five (5) DSC Neo wireless motion sensors, relocate up to two (2) motion sensors obstructed by shelving to an optimal coverage location, remount, test sensor communication and walk-test all serviced zones.',
                 'Amount': 150,
                 'SalesItemLineDetail': {'ItemRef': {'name': 'Labor:T&M Labor Standard', 'value': '107'},
                                        'Qty': 1, 'UnitPrice': 150, 'TaxCodeRef': {'name': 'TAX'}}},
                {'Id': '7', 'DetailType': 'SalesItemLineDetail',
                 'Description': 'Trip Charge - Service call dispatch to job site',
                 'Amount': 75,
                 'SalesItemLineDetail': {'ItemRef': {'name': 'Fees:Trip Charge', 'value': '100'},
                                        'Qty': 1, 'UnitPrice': 75, 'TaxCodeRef': {'name': 'TAX'}}},
                {'Id': '8', 'DetailType': 'SalesItemLineDetail',
                 'Description': 'Clarifications / Exclusions - Pricing assumes standard ceiling heights reachable by ladder.',
                 'Amount': 0,
                 'SalesItemLineDetail': {'ItemRef': {'name': 'Services', 'value': '1'},
                                        'Qty': 1, 'UnitPrice': 0, 'TaxCodeRef': {'name': 'NON'}}},
                {'DetailType': 'SubTotalLineDetail', 'Amount': 244.25},
            ],
            'TxnTaxDetail': {'TotalTax': 20.15, 'TaxLine': [{
                'Amount': 20.15, 'DetailType': 'TaxLineDetail',
                'TaxLineDetail': {'TaxPercent': 8.25, 'NetAmountTaxable': 244.25}}]},
        }
        conn = fo.get_db()
        result = qboimp.import_estimates(conn, [est], today=TODAY)
        conn.commit()
        self.assertEqual(result['imported'], 1, result)
        q = conn.execute('SELECT * FROM quotes WHERE qbo_doc_number=?', ('1024',)).fetchone()
        self.assertEqual(round(q['total'], 2), 264.40)
        self.assertEqual(round(q['qbo_tax_amount'], 2), 20.15)
        self.assertEqual(q['qbo_tax_rate'], 8.25)
        self.assertEqual(q['qbo_expiration'], '2026-12-06')
        self.assertEqual(q['qbo_customer_memo'], 'Please call before arrival.')
        self.assertIn('100 Main St', q['qbo_bill_to'])
        self.assertIn('50 Warehouse Rd', q['qbo_ship_to'])
        lines = json.loads(q['lines_json'])
        items = [ln for ln in lines if (ln.get('kind') or 'item') == 'item']
        self.assertEqual([ln['name'] for ln in items], [
            'Materials:Consumables', 'Labor:T&M Labor Standard', 'Fees:Trip Charge', 'Services'])
        self.assertIn('CR123A 3V lithium battery', items[0]['description'])
        self.assertIn('relocate up to two (2) motion sensors', items[1]['description'])
        self.assertEqual(items[0]['qty'], 5)
        self.assertEqual(items[0]['sell'], 3.85)
        self.assertEqual(items[0]['amount'], 19.25)
        self.assertTrue(any(ln.get('kind') == 'subtotal' for ln in lines))
        # client view and print both show the description and tax
        row = quote.ensure_token(conn, q['id'])
        tok = row['token']
        conn.commit()
        conn.close()
        page = self.c.get('/q/' + tok)
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('CR123A 3V lithium battery', html)
        self.assertIn('relocate up to two (2) motion sensors', html)
        self.assertIn('Clarifications / Exclusions', html)
        self.assertIn('Sales tax', html)
        self.assertIn('8.25%', html)
        self.assertIn('$264.40', html)
        self.assertNotIn('Imported from QuickBooks', html)
        self.assertNotIn('>Scope</h2>', html)
        self.assertNotIn('Deposit due to schedule', html)
        self.assertIn('Please call before arrival.', html)
        self.assertIn('Bill to', html)
        self.assertIn('100 Main St', html)
        self.assertIn('50 Warehouse Rd', html)
        self.assertIn('2026-12-06', html)  # QBO expiration, not a default
        printed = self.c.get('/q/' + tok + '/print')
        self.assertIn('CR123A 3V lithium battery', printed.get_data(as_text=True))
        self.assertIn('$264.40', printed.get_data(as_text=True))

    def test_update_mode_resyncs_unless_edited(self):
        conn = fo.get_db()
        first = estimate('401', '401', 'Pending', days_ago=2, name='Update Co', unit_price=10, qty=1, item_name='Door contact')
        # first import is the old thin shape once saved
        result = qboimp.import_estimates(conn, [first], today=TODAY)
        conn.commit()
        self.assertEqual(result['imported'], 1)
        qid = result['quotes'][0]['quote_id']
        # richer payload with description and tax
        richer = estimate('401', '401', 'Pending', days_ago=2, name='Update Co', unit_price=10, qty=1,
                          item_name='Door contact', amount=10)
        richer['Line'][0]['Description'] = 'Door contact at the lobby entry, including magnet.'
        richer['TotalAmt'] = 10.83
        richer['TxnTaxDetail'] = {'TotalTax': 0.83, 'TaxLine': [{'TaxLineDetail': {'TaxPercent': 8.3}}]}
        again = qboimp.import_estimates(conn, [richer], today=TODAY, mode='update')
        conn.commit()
        self.assertEqual(again['updated'], 1, again)
        self.assertEqual(again['flagged'], 0)
        q = conn.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
        self.assertEqual(round(q['total'], 2), 10.83)
        self.assertIn('lobby entry', json.loads(q['lines_json'])[0]['description'])
        # office edit of a line price should be flagged and kept
        lines = json.loads(q['lines_json'])
        lines[0]['sell'] = 99
        lines[0]['amount'] = 99
        conn.execute('UPDATE quotes SET lines_json=?, total=?, updated_at=? WHERE id=?',
                     (json.dumps(lines), 99, '2026-11-24T12:00:00', qid))
        conn.commit()
        third = qboimp.import_estimates(conn, [richer], today=TODAY, mode='update')
        conn.commit()
        self.assertEqual(third['updated'], 0, third)
        self.assertEqual(third['flagged'], 1)
        self.assertEqual(third['flagged_quotes'][0]['quote_id'], qid)
        kept = conn.execute('SELECT total FROM quotes WHERE id=?', (qid,)).fetchone()
        self.assertEqual(kept['total'], 99)
        conn.close()


    def test_cleanup_strips_import_note_without_flagging(self):
        conn = fo.get_db()
        result = qboimp.import_estimates(conn, [estimate('501', '501', name='Cleanup Co')], today=TODAY)
        conn.commit()
        qid = result['quotes'][0]['quote_id']
        # Simulate a previously imported quote that still has the old client-facing sentence,
        # plus a real office scope note that must be kept.
        conn.execute(
            "UPDATE quotes SET notes=? WHERE id=?",
            ('Imported from QuickBooks estimate 501.\nReplace the lobby keypad.', qid))
        saved = conn.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
        conn.execute('UPDATE quotes SET qbo_import_hash=? WHERE id=?',
                     (qboimp.hash_quote_row(saved), qid))
        conn.commit()
        n = qboimp.cleanup_imported_scope_notes(conn)
        conn.commit()
        self.assertEqual(n, 1)
        q = conn.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
        self.assertEqual(q['notes'], 'Replace the lobby keypad.')
        self.assertNotIn('Imported from QuickBooks', q['notes'])
        # a later update re-sync should not flag this as an office edit
        again = qboimp.import_estimates(conn, [estimate('501', '501', name='Cleanup Co')], today=TODAY, mode='update')
        conn.commit()
        self.assertEqual(again['flagged'], 0, again)
        self.assertEqual(again['updated'], 1, again)
        kept = conn.execute('SELECT notes FROM quotes WHERE id=?', (qid,)).fetchone()
        self.assertEqual(kept['notes'], 'Replace the lobby keypad.')
        row = quote.ensure_token(conn, qid)
        tok = row['token']
        conn.commit(); conn.close()
        html = self.c.get('/q/' + tok).get_data(as_text=True)
        self.assertIn('Replace the lobby keypad.', html)
        self.assertIn('Scope', html)
        self.assertNotIn('Imported from QuickBooks', html)

    def test_mcp_payload_and_high_precision_rate(self):

        est = {
            'id': 'djQ:257',
            'reference_number': '1018',
            'txn_date': '2026-10-29',
            'accept_status': {'status': 'Pending'},
            'contact': {'display_name': 'Warwick Construction'},
            'amount': '22060.81',
            'lines': [
                {'id': '1', 'item_name': 'Cabling:Speaker Wire', 'description': 'Speaker wire run',
                 'quantity': '2500', 'rate': '0.041', 'amount': '102.50', 'taxable': True},
                {'id': '2', 'item_name': 'Labor:Low-Voltage Install Labor',
                 'description': 'Install labor', 'quantity': '1', 'rate': '100', 'amount': '100', 'taxable': True},
                {'subtotal': 202.5},
            ],
        }
        conn = fo.get_db()
        result = qboimp.import_estimates(conn, [est], today=TODAY)
        conn.commit()
        self.assertEqual(result['imported'], 1, result)
        q = conn.execute('SELECT * FROM quotes WHERE qbo_doc_number=?', ('1018',)).fetchone()
        lines = json.loads(q['lines_json'])
        wire = [ln for ln in lines if 'Speaker' in (ln.get('name') or '')][0]
        self.assertEqual(wire['sell'], 0.041)
        self.assertEqual(wire['amount'], 102.5)
        self.assertEqual(wire['qty'], 2500)
        self.assertEqual(q['qbo_estimate_id'], '257')
        # derived tax from TotalAmt - line sum
        self.assertAlmostEqual(q['total'], 22060.81, places=2)
        conn.close()

