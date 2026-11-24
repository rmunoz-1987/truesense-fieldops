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
        self.assertIn('Imported from QuickBooks estimate EST-101', q['notes'])
        lines = json.loads(q['lines_json'])
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['name'], '4K dome camera')
        self.assertEqual(lines[0]['qty'], 2)
        self.assertEqual(lines[0]['sell'], 320)  # frozen QBO price, not catalog 999
        self.assertEqual(lines[0]['cost'], 180)  # from catalog
        self.assertTrue(lines[0]['catalog_id'])
        self.assertEqual(q['total'], 640)
        co = conn.execute('SELECT * FROM companies WHERE id=?', (q['company_id'],)).fetchone()
        self.assertEqual(co['name'], 'Memorial Family Clinic')
        self.assertIn('1234 Westheimer', co['billing_address'] or '')
        site = conn.execute('SELECT * FROM sites WHERE id=?', (q['site_id'],)).fetchone()
        self.assertIn('500 Memorial', site['address'])
        opp = conn.execute('SELECT * FROM opportunities WHERE id=?', (q['opportunity_id'],)).fetchone()
        self.assertEqual(opp['stage'], 'proposal')
        self.assertEqual(opp['amount'], 640)
        self.assertEqual(opp['created_by'], 'QBO import')
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
