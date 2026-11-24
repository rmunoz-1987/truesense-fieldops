"""One-time, read-only import of open QuickBooks estimates into quotes and opportunities.

Pending or Accepted estimates from the last 12 months are matched to a company
and site, line items are mapped to the catalog when possible, and QBO unit
prices are frozen on the quote. Already-imported estimates are skipped.
This module never writes to QuickBooks.
"""
from __future__ import annotations

from datetime import date

import fieldops_platform as plat
import fieldops_quote as quote

OPEN_STATUSES = ('pending', 'accepted')
CREATED_BY = 'QBO import'


def _cols(conn, table):
    return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}


def _add_col(conn, table, coldef):
    name = coldef.split()[0]
    if name in _cols(conn, table):
        return
    conn.execute('ALTER TABLE %s ADD COLUMN %s' % (table, coldef))


def init_schema(conn):
    _add_col(conn, 'quotes', 'qbo_estimate_id TEXT')
    _add_col(conn, 'quotes', 'qbo_doc_number TEXT')
    _add_col(conn, 'quotes', 'qbo_txn_status TEXT')
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS ix_quotes_qbo_estimate '
        'ON quotes(qbo_estimate_id) WHERE IFNULL(qbo_estimate_id,"") != ""'
    )


def _text(v):
    if v is None:
        return ''
    if isinstance(v, dict):
        return (v.get('value') or v.get('Value') or v.get('name') or v.get('Name') or '').strip()
    return str(v).strip()


def _ref_name(ref):
    if not isinstance(ref, dict):
        return _text(ref)
    return (ref.get('name') or ref.get('Name') or '').strip()


def format_addr(addr):
    if not isinstance(addr, dict):
        return ''
    parts = []
    for key in ('Line1', 'Line2', 'Line3', 'Line4', 'Line5'):
        val = (addr.get(key) or '').strip()
        if val:
            parts.append(val)
    city = (addr.get('City') or '').strip()
    state = (addr.get('CountrySubDivisionCode') or '').strip()
    postal = (addr.get('PostalCode') or '').strip()
    tail = ' '.join(x for x in (city, state, postal) if x)
    if tail:
        parts.append(tail)
    return ', '.join(parts)[:300]


def cutoff_12_months(today=None):
    today = today or date.today()
    try:
        return today.replace(year=today.year - 1)
    except ValueError:
        return date(today.year - 1, 2, 28)


def parse_txn_date(raw):
    text = _text(raw)[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _memo(est):
    memo = est.get('CustomerMemo')
    if isinstance(memo, dict):
        return (memo.get('value') or memo.get('Value') or '').strip()
    return _text(memo)


def _status(est):
    return (_text(est.get('TxnStatus') or est.get('txnStatus'))).lower()


def _estimate_id(est):
    return _text(est.get('Id') or est.get('id'))


def _doc_number(est):
    return _text(est.get('DocNumber') or est.get('docNumber'))[:40]


def match_catalog(conn, name, sku=''):
    sku = (sku or '').strip()
    name = (name or '').strip()
    if sku:
        row = conn.execute(
            'SELECT * FROM catalog_items WHERE LOWER(TRIM(IFNULL(sku,"")))=?',
            (sku.lower(),)).fetchone()
        if row:
            return row
    if name:
        row = conn.execute(
            'SELECT * FROM catalog_items WHERE LOWER(TRIM(name))=?',
            (name.lower(),)).fetchone()
        if row:
            return row
    return None


def extract_lines(conn, est):
    raw = est.get('Line') or est.get('line') or []
    if isinstance(raw, dict):
        raw = [raw]
    out, notes = [], []
    for ln in raw:
        if not isinstance(ln, dict):
            continue
        detail_type = _text(ln.get('DetailType') or ln.get('detailType'))
        if detail_type == 'DescriptionOnly' or detail_type == 'DescriptionOnlyLineDetail':
            desc = _text(ln.get('Description') or (ln.get('DescriptionLineDetail') or {}).get('Description'))
            if desc:
                notes.append(desc)
            continue
        if detail_type and detail_type != 'SalesItemLineDetail':
            continue
        detail = ln.get('SalesItemLineDetail') or ln.get('salesItemLineDetail') or {}
        if not isinstance(detail, dict):
            detail = {}
        item = detail.get('ItemRef') or detail.get('itemRef') or {}
        name = _ref_name(item) or _text(ln.get('Description'))
        if not name:
            continue
        sku = _text(item.get('value') or item.get('Value')) if isinstance(item, dict) else ''
        qty = detail.get('Qty')
        if qty is None:
            qty = detail.get('qty')
        unit_price = detail.get('UnitPrice')
        if unit_price is None:
            unit_price = detail.get('unitPrice')
        amount = ln.get('Amount')
        if amount is None:
            amount = ln.get('amount')
        try:
            qty = float(qty) if qty is not None else 0
        except (TypeError, ValueError):
            qty = 0
        try:
            unit_price = float(unit_price) if unit_price is not None else None
        except (TypeError, ValueError):
            unit_price = None
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount = None
        if (not qty or qty <= 0) and unit_price and amount and unit_price > 0:
            qty = round(amount / unit_price, 4)
        if qty <= 0:
            qty = 1
        if unit_price is None:
            unit_price = (amount / qty) if (amount is not None and qty) else 0
        cat = match_catalog(conn, name, sku='')
        # QBO ItemRef.value is an item id, not a SKU. Match by name only unless the
        # description/name equals a catalog sku.
        by_sku = match_catalog(conn, '', sku=name)
        if not cat and by_sku:
            cat = by_sku
        cost = float(cat['cost'] or 0) if cat else 0
        catalog_id = cat['id'] if cat else None
        out.append({
            'name': name,
            'qty': qty,
            'cost': cost,
            'sell': unit_price,
            'catalog_id': catalog_id,
            'unit': (cat['unit'] if cat and cat['unit'] else '') if cat else '',
        })
    return out, notes


def existing_quote(conn, qbo_id):
    if not qbo_id:
        return None
    return conn.execute(
        'SELECT * FROM quotes WHERE qbo_estimate_id=?', (qbo_id,)).fetchone()


def import_one(conn, est, today=None):
    """Import one QBO Estimate object. Returns (status, info)."""
    if not isinstance(est, dict):
        return 'error', {'error': 'Estimate must be an object.'}
    qbo_id = _estimate_id(est)
    if not qbo_id:
        return 'error', {'error': 'Missing estimate Id.'}
    status = _status(est)
    if status not in OPEN_STATUSES:
        return 'skipped', {'qbo_estimate_id': qbo_id, 'reason': 'status %s' % (status or 'blank')}
    txn = parse_txn_date(est.get('TxnDate') or est.get('txnDate'))
    if not txn:
        return 'skipped', {'qbo_estimate_id': qbo_id, 'reason': 'missing TxnDate'}
    if txn < cutoff_12_months(today):
        return 'skipped', {'qbo_estimate_id': qbo_id, 'reason': 'older than 12 months'}
    found = existing_quote(conn, qbo_id)
    if found:
        return 'unchanged', {
            'qbo_estimate_id': qbo_id,
            'quote_id': found['id'],
            'qbo_doc_number': found['qbo_doc_number'] or _doc_number(est),
        }
    customer = _ref_name(est.get('CustomerRef') or est.get('customerRef'))
    if not customer:
        return 'error', {'qbo_estimate_id': qbo_id, 'error': 'Missing CustomerRef name.'}
    lines, extra_notes = extract_lines(conn, est)
    if not lines:
        return 'error', {'qbo_estimate_id': qbo_id, 'error': 'No sales line items.'}

    bill = format_addr(est.get('BillAddr') or est.get('BillAddress') or est.get('billAddr'))
    ship = format_addr(est.get('ShipAddr') or est.get('ShipAddress') or est.get('shipAddr'))
    email = ''
    raw_email = est.get('BillEmail') or est.get('billEmail')
    if isinstance(raw_email, dict):
        email = (raw_email.get('Address') or raw_email.get('address') or '').strip()[:120]
    else:
        email = _text(raw_email)[:120]
    phone = ''
    raw_phone = est.get('BillPhone') or {}
    if isinstance(raw_phone, dict):
        phone = (raw_phone.get('FreeFormNumber') or '').strip()[:40]

    when = (est.get('TxnDate') or '')[:10] or plat.now_iso()
    if len(when) == 10:
        when = when + 'T00:00:00'
    company = plat.upsert_company(
        conn, customer, phone=phone, email=email, billing_address=bill, when=when)
    site = None
    if ship or bill:
        site = plat.upsert_site(conn, company['id'], address=ship or bill, when=when)

    doc = _doc_number(est)
    title = ('Estimate %s' % doc) if doc else ('QBO estimate %s' % qbo_id)
    memo = _memo(est)
    notes = []
    notes.append('Imported from QuickBooks estimate %s.' % (doc or qbo_id))
    if memo:
        notes.append(memo)
    notes.extend(extra_notes)
    note_text = '\n'.join(notes)[:2000]

    stage = 'won' if status == 'accepted' else 'proposal'
    won_at = when if stage == 'won' else None
    cur = conn.execute(
        '''INSERT INTO opportunities (company_id,contact_id,site_id,title,stage,amount,notes,created_by,created_at,updated_at,won_at,lost_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (company['id'], None, site['id'] if site else None, title, stage, 0, note_text,
         CREATED_BY, when, when, won_at, None))
    opp_id = cur.lastrowid
    row, err = quote.save_quote(conn, {
        'title': title,
        'company_id': company['id'],
        'site_id': site['id'] if site else None,
        'opportunity_id': opp_id,
        'notes': note_text,
        'lines': lines,
    }, created_by=CREATED_BY)
    if err or not row:
        return 'error', {'qbo_estimate_id': qbo_id, 'error': err or 'Could not save quote.'}
    conn.execute(
        "UPDATE quotes SET status='sent', qbo_estimate_id=?, qbo_doc_number=?, qbo_txn_status=?, "
        "sent_at=COALESCE(sent_at, ?), created_at=?, updated_at=? WHERE id=?",
        (qbo_id, doc, status.title(), when, when, when, row['id']))
    amount = row['total'] or 0
    conn.execute(
        'UPDATE opportunities SET amount=?, updated_at=? WHERE id=?',
        (amount, when, opp_id))
    saved = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
    return 'imported', {
        'qbo_estimate_id': qbo_id,
        'qbo_doc_number': doc,
        'quote_id': saved['id'],
        'opportunity_id': opp_id,
        'company_id': company['id'],
        'total': saved['total'],
    }


def unwrap_estimates(payload):
    """Accept a list, a single estimate, or a QBO QueryResponse wrapper."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    if 'Id' in payload or 'id' in payload:
        return [payload]
    if 'estimates' in payload:
        items = payload.get('estimates') or []
        return items if isinstance(items, list) else [items]
    qr = payload.get('QueryResponse') or payload.get('queryResponse') or {}
    items = qr.get('Estimate') or qr.get('estimate') or []
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return items
    return []


def import_estimates(conn, payload, today=None):
    init_schema(conn)
    items = unwrap_estimates(payload)
    if items is None:
        return {'imported': 0, 'unchanged': 0, 'skipped': 0, 'errors': [{'error': 'estimates must be a list'}]}
    today = today or date.today()
    imported, unchanged, skipped, errors, details = 0, 0, 0, [], []
    for est in items:
        status, info = import_one(conn, est, today=today)
        info = info or {}
        if status == 'imported':
            imported += 1
            details.append(info)
        elif status == 'unchanged':
            unchanged += 1
        elif status == 'skipped':
            skipped += 1
        else:
            errors.append(info)
    return {
        'imported': imported,
        'unchanged': unchanged,
        'skipped': skipped,
        'errors': errors,
        'quotes': details,
    }


def register(fo):
    app = fo.app
    request = fo.request
    jsonify = fo.jsonify

    @app.route('/api/qbo/import-estimates', methods=['POST'])
    def qbo_import_estimates():
        bad = fo.sync_key_ok()
        if bad:
            return bad
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({'error': 'JSON body required'}), 400
        conn = fo.get_db()
        result = import_estimates(conn, payload)
        conn.commit()
        conn.close()
        return jsonify(result)
