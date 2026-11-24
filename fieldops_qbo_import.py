"""One-time, read-only import of open QuickBooks estimates into quotes and opportunities.

Pending or Accepted estimates from the last 12 months are matched to a company
and site. Every QBO line is kept in order: product name, full description, qty,
rate, amount, description-only and subtotal lines, tax, and discount. Prices
are frozen. Already-imported estimates are skipped unless mode=update, which
refreshes a quote in place unless someone edited it in the app after import.
This module never writes to QuickBooks.
"""
from __future__ import annotations

import hashlib
import json
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
    _add_col(conn, 'quotes', 'qbo_customer_memo TEXT')
    _add_col(conn, 'quotes', 'qbo_bill_to TEXT')
    _add_col(conn, 'quotes', 'qbo_ship_to TEXT')
    _add_col(conn, 'quotes', 'qbo_expiration TEXT')
    _add_col(conn, 'quotes', 'qbo_tax_rate REAL')
    _add_col(conn, 'quotes', 'qbo_tax_amount REAL')
    _add_col(conn, 'quotes', 'qbo_discount_amount REAL')
    _add_col(conn, 'quotes', 'qbo_subtotal REAL')
    _add_col(conn, 'quotes', 'qbo_import_hash TEXT')
    _add_col(conn, 'quotes', 'qbo_imported_at TEXT')
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS ix_quotes_qbo_estimate '
        'ON quotes(qbo_estimate_id) WHERE IFNULL(qbo_estimate_id,"") != ""'
    )


def _text(v):
    if v is None:
        return ''
    if isinstance(v, dict):
        return (v.get('value') or v.get('Value') or v.get('name') or v.get('Name')
                or v.get('address') or v.get('Address') or '').strip()
    return str(v).strip()


def _ref_name(ref):
    if not isinstance(ref, dict):
        return _text(ref)
    return (ref.get('name') or ref.get('Name') or '').strip()


def _num(v, default=None):
    if v is None or v == '':
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _money(v, default=0.0):
    n = _num(v, default)
    if n is None:
        return default
    return round(float(n), 2)


def _rowget(row, key, default=None):
    try:
        if key in row.keys():
            val = row[key]
            return default if val is None else val
    except Exception:
        pass
    return default


def format_addr(addr):
    if not addr:
        return ''
    if isinstance(addr, str):
        return addr.strip()[:300]
    if not isinstance(addr, dict):
        return _text(addr)[:300]
    parts = []
    for key in ('Line1', 'line1', 'Line2', 'line2', 'Line3', 'line3', 'Line4', 'line4', 'Line5', 'line5'):
        val = (addr.get(key) or '').strip() if isinstance(addr.get(key), str) else _text(addr.get(key))
        if val:
            parts.append(val)
    if not parts:
        for key in ('address', 'Address', 'formatted', 'full', 'value'):
            val = addr.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:300]
            if isinstance(val, dict):
                return format_addr(val)
    city = (addr.get('City') or addr.get('city') or '').strip()
    state = (addr.get('CountrySubDivisionCode') or addr.get('countrySubDivisionCode')
             or addr.get('state') or '').strip()
    postal = (addr.get('PostalCode') or addr.get('postalCode') or addr.get('zip') or '').strip()
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
    """Customer-facing message on the estimate, not the private note."""
    for key in ('CustomerMemo', 'customerMemo', 'CustomerMessage', 'customer_message',
                'customer_memo', 'Message', 'message'):
        val = est.get(key)
        if val:
            text = _text(val)
            if text:
                return text
    return ''


def _status(est):
    raw = est.get('TxnStatus') or est.get('txnStatus')
    if not raw:
        acc = est.get('accept_status') or est.get('AcceptStatus') or {}
        if isinstance(acc, dict):
            raw = acc.get('status') or acc.get('Status')
        elif acc:
            raw = acc
    return _text(raw).lower()


def _estimate_id(est):
    raw = _text(est.get('Id') or est.get('id'))
    if not raw:
        return ''
    # MCP ids look like "...:258"; the QBO txn id is the trailing number.
    if ':' in raw and raw.rsplit(':', 1)[-1].isdigit():
        return raw.rsplit(':', 1)[-1]
    link = _text(est.get('link') or '')
    if 'txnId' in link:
        import re
        m = re.search(r'txnId(?:=|%3D)(\d+)', link)
        if m:
            return m.group(1)
    return raw


def _doc_number(est):
    return _text(est.get('DocNumber') or est.get('docNumber') or est.get('reference_number'))[:40]


def _txn_date_raw(est):
    return est.get('TxnDate') or est.get('txnDate') or est.get('txn_date')


def _expiration(est):
    return _text(est.get('ExpirationDate') or est.get('expirationDate') or est.get('expiration_date'))[:10]


def _total_amt(est):
    return _num(est.get('TotalAmt') or est.get('totalAmt') or est.get('amount'))


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


def _catalog_row(conn, typ_name):
    cat = match_catalog(conn, typ_name, sku='')
    by_sku = match_catalog(conn, '', sku=typ_name) if typ_name else None
    return cat or by_sku


def _item_line(conn, name, description, qty, rate, amount, taxable=False, qbo_line_id='', kind='item'):
    cat = _catalog_row(conn, name) if name else None
    cost = float(cat['cost'] or 0) if cat else 0
    catalog_id = cat['id'] if cat else None
    unit = (cat['unit'] if cat and cat['unit'] else '') if cat else ''
    return {
        'name': name or '',
        'description': description or '',
        'qty': qty,
        'cost': cost,
        'sell': rate if rate is not None else 0,
        'catalog_id': catalog_id,
        'unit': unit,
        'kind': kind,
        'amount': amount,
        'taxable': bool(taxable),
        'qbo_line_id': qbo_line_id or '',
    }


def _is_mcp_subtotal(ln):
    if not isinstance(ln, dict):
        return False
    if ln.get('DetailType') or ln.get('detailType'):
        return False
    if 'subtotal' in ln and 'item_name' not in ln and 'quantity' not in ln and 'description' not in ln:
        return True
    return False


def extract_lines(conn, est):
    raw = est.get('Line') or est.get('line') or est.get('lines') or []
    if isinstance(raw, dict):
        raw = [raw]
    out = []

    def add_description_only(text, qbo_line_id=''):
        if text:
            out.append(_item_line(conn, '', text, 0, 0, 0, False, qbo_line_id, kind='text'))

    for ln in raw:
        if not isinstance(ln, dict):
            continue
        if _is_mcp_subtotal(ln):
            amt = _money(ln.get('subtotal'))
            out.append(_item_line(conn, 'Subtotal', '', 0, 0, amt, False, '', kind='subtotal'))
            continue

        detail_type = _text(ln.get('DetailType') or ln.get('detailType'))
        qid = _text(ln.get('Id') or ln.get('id'))

        if detail_type in ('DescriptionOnly', 'DescriptionOnlyLineDetail'):
            desc = _text(ln.get('Description') or (ln.get('DescriptionLineDetail') or {}).get('Description'))
            add_description_only(desc, qid)
            continue

        if detail_type in ('SubTotalLineDetail', 'SubTotalLine'):
            amt = _money(ln.get('Amount') or ln.get('amount'))
            out.append(_item_line(conn, 'Subtotal', '', 0, 0, amt, False, qid, kind='subtotal'))
            continue

        if detail_type in ('GroupLineDetail', 'GroupLine'):
            group = ln.get('GroupLineDetail') or ln.get('groupLineDetail') or {}
            gname = _ref_name(group.get('GroupItemRef') or group.get('groupItemRef')) or _text(ln.get('Description')) or 'Group'
            amt = _money(ln.get('Amount') or ln.get('amount') or group.get('Amount'))
            out.append(_item_line(conn, gname, _text(ln.get('Description')), 0, 0, amt, False, qid, kind='group'))
            nested = group.get('Line') or group.get('line') or []
            if isinstance(nested, dict):
                nested = [nested]
            # Recurse by temporarily walking children through the same rules.
            for child in nested:
                if isinstance(child, dict):
                    raw.append(child)  # will not work mid-for... don't do this
            # Handle nested immediately:
            for child in (group.get('Line') or group.get('line') or []):
                if not isinstance(child, dict):
                    continue
                # only simple sales or description children
                c_type = _text(child.get('DetailType') or child.get('detailType'))
                c_id = _text(child.get('Id') or child.get('id'))
                if c_type in ('DescriptionOnly', 'DescriptionOnlyLineDetail'):
                    add_description_only(_text(child.get('Description')), c_id)
                    continue
                if c_type in ('SubTotalLineDetail', 'SubTotalLine'):
                    out.append(_item_line(conn, 'Subtotal', '', 0, 0, _money(child.get('Amount')), False, c_id, kind='subtotal'))
                    continue
                parsed = _sales_from_qbo(conn, child)
                if parsed:
                    out.append(parsed)
            continue

        if detail_type in ('DiscountLineDetail', 'DiscountLine'):
            detail = ln.get('DiscountLineDetail') or ln.get('discountLineDetail') or {}
            amt = _money(ln.get('Amount') or ln.get('amount'))
            desc = _text(ln.get('Description')) or 'Discount'
            pct = _num(detail.get('Percent') or detail.get('percent'))
            if pct:
                desc = (desc + (' %s%%' % pct)).strip()
            out.append(_item_line(conn, 'Discount', desc, 1, amt, amt, False, qid, kind='discount'))
            continue

        if detail_type and detail_type not in ('SalesItemLineDetail', 'SalesItemLine', ''):
            continue

        # MCP sales line (no DetailType, has item_name or quantity)
        if not detail_type and ('item_name' in ln or 'quantity' in ln or 'itemName' in ln):
            parsed = _sales_from_mcp(conn, ln)
            if parsed:
                out.append(parsed)
            continue

        parsed = _sales_from_qbo(conn, ln)
        if parsed:
            out.append(parsed)

    return out


def _sales_from_mcp(conn, ln):
    name = _text(ln.get('item_name') or ln.get('itemName'))
    desc = ln.get('description') if ln.get('description') is not None else ''
    if not isinstance(desc, str):
        desc = _text(desc)
    qty = _num(ln.get('quantity') if 'quantity' in ln else ln.get('qty'))
    rate = _num(ln.get('rate') if 'rate' in ln else ln.get('unit_price'))
    amount = _num(ln.get('amount'))
    taxable = bool(ln.get('taxable'))
    if qty is None:
        qty = 0
    if amount is None and qty is not None and rate is not None:
        amount = qty * rate
    if not name and not desc:
        return None
    return _item_line(conn, name, desc, qty, rate, amount, taxable, _text(ln.get('id')), kind='item')


def _sales_from_qbo(conn, ln):
    detail = ln.get('SalesItemLineDetail') or ln.get('salesItemLineDetail') or {}
    if not isinstance(detail, dict):
        detail = {}
    item = detail.get('ItemRef') or detail.get('itemRef') or {}
    name = _ref_name(item)
    desc = ln.get('Description') if ln.get('Description') is not None else ln.get('description')
    if not isinstance(desc, str) and desc is not None:
        desc = _text(desc)
    if desc is None:
        desc = ''
    if not name:
        name = _text(desc).split('\n', 1)[0]
    qty = _num(detail.get('Qty') if 'Qty' in detail else detail.get('qty'))
    rate = _num(detail.get('UnitPrice') if 'UnitPrice' in detail else detail.get('unitPrice'))
    amount = _num(ln.get('Amount') if 'Amount' in ln else ln.get('amount'))
    tax_code = detail.get('TaxCodeRef') or detail.get('taxCodeRef') or {}
    tax_name = _ref_name(tax_code) or _text(tax_code)
    taxable = bool(ln.get('taxable')) if 'taxable' in ln else (tax_name.upper() in ('TAX', 'YES', 'TRUE') if tax_name else False)
    if 'Taxable' in detail or 'taxable' in detail:
        taxable = bool(detail.get('Taxable') if 'Taxable' in detail else detail.get('taxable'))
    if qty is None:
        qty = 0
    if amount is None and qty is not None and rate is not None:
        amount = qty * rate
    if not name and not desc:
        return None
    return _item_line(conn, name, desc, qty, rate, amount, taxable, _text(ln.get('Id') or ln.get('id')), kind='item')


def extract_header(est, lines):
    item_sub = 0.0
    taxable_sub = 0.0
    discount = 0.0
    for ln in lines:
        kind = ln.get('kind') or 'item'
        if kind == 'discount':
            discount += abs(_money(ln.get('amount')))
            continue
        if kind != 'item':
            continue
        amt = ln.get('amount')
        if amt is None:
            amt = (ln.get('qty') or 0) * (ln.get('sell') or 0)
        amt = _money(amt)
        item_sub += amt
        if ln.get('taxable'):
            taxable_sub += amt
    item_sub = round(item_sub, 2)
    taxable_sub = round(taxable_sub, 2)
    discount = round(discount, 2)

    tax_amount = None
    tax_rate = None  # stored as a percent, e.g. 8.25
    detail = est.get('TxnTaxDetail') or est.get('txnTaxDetail') or {}
    if isinstance(detail, dict) and detail:
        tax_amount = _num(detail.get('TotalTax') or detail.get('totalTax'))
        tlines = detail.get('TaxLine') or detail.get('taxLine') or []
        if isinstance(tlines, dict):
            tlines = [tlines]
        for tl in tlines:
            td = (tl or {}).get('TaxLineDetail') or (tl or {}).get('taxLineDetail') or {}
            pct = _num(td.get('TaxPercent') or td.get('taxPercent'))
            if pct is not None:
                tax_rate = pct
                break
    if tax_amount is None:
        tax_amount = _num(est.get('tax_amount') or est.get('TaxAmount') or est.get('taxAmount'))

    total = _total_amt(est)
    if tax_amount is None and total is not None:
        tax_amount = round(total - item_sub + discount, 2)
        if abs(tax_amount) < 0.005:
            tax_amount = 0.0
        elif tax_amount < 0:
            # leftover treated as extra discount if no discount line was present
            if not discount:
                discount = round(-tax_amount, 2)
                tax_amount = 0.0
            else:
                tax_amount = 0.0

    tax_amount = 0.0 if tax_amount is None else round(float(tax_amount), 2)
    if tax_rate is None and tax_amount and taxable_sub:
        tax_rate = round(tax_amount / taxable_sub * 100, 4)
    elif tax_rate is None and tax_amount and item_sub:
        tax_rate = round(tax_amount / item_sub * 100, 4)

    if total is None:
        total = round(item_sub - discount + tax_amount, 2)
    else:
        total = round(float(total), 2)

    bill = format_addr(est.get('BillAddr') or est.get('BillAddress') or est.get('billAddr')
                       or est.get('bill_to_address') or est.get('billTo'))
    ship = format_addr(est.get('ShipAddr') or est.get('ShipAddress') or est.get('shipAddr')
                       or est.get('ship_to_address') or est.get('shipTo'))
    email = ''
    raw_email = est.get('BillEmail') or est.get('billEmail') or (est.get('contact') or {}).get('email')
    if isinstance(raw_email, dict):
        email = (raw_email.get('Address') or raw_email.get('address') or '').strip()[:120]
    else:
        email = _text(raw_email)[:120]
    phone = ''
    raw_phone = est.get('BillPhone') or est.get('billPhone') or {}
    if isinstance(raw_phone, dict):
        phone = (raw_phone.get('FreeFormNumber') or raw_phone.get('freeFormNumber') or '').strip()[:40]
    customer = _ref_name(est.get('CustomerRef') or est.get('customerRef'))
    if not customer:
        contact = est.get('contact') or {}
        customer = _text(contact.get('display_name') or contact.get('name'))
    return {
        'customer': customer,
        'bill': bill,
        'ship': ship,
        'email': email,
        'phone': phone,
        'memo': _memo(est),
        'expiration': _expiration(est),
        'tax_amount': tax_amount,
        'tax_rate': tax_rate,
        'discount': discount,
        'subtotal': item_sub,
        'total': total,
    }


def existing_quote(conn, qbo_id):
    if not qbo_id:
        return None
    return conn.execute(
        'SELECT * FROM quotes WHERE qbo_estimate_id=?', (qbo_id,)).fetchone()


def snapshot_hash(title, notes, clarifications, lines, total, memo='', bill='', ship='',
                  tax_amount=0, discount=0, expiration=''):
    canon_lines = []
    for ln in lines or []:
        canon_lines.append({
            'name': ln.get('name') or '',
            'description': ln.get('description') or '',
            'qty': ln.get('qty'),
            'sell': ln.get('sell'),
            'amount': ln.get('amount'),
            'kind': ln.get('kind') or 'item',
            'optional': bool(ln.get('optional')),
            'taxable': bool(ln.get('taxable')),
        })
    payload = {
        'title': title or '',
        'notes': notes or '',
        'clarifications': clarifications or '',
        'lines': canon_lines,
        'total': total,
        'memo': memo or '',
        'bill': bill or '',
        'ship': ship or '',
        'tax_amount': tax_amount or 0,
        'discount': discount or 0,
        'expiration': expiration or '',
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode('utf-8')).hexdigest()


def hash_quote_row(row):
    return snapshot_hash(
        row['title'], row['notes'], _rowget(row, 'clarifications') or '',
        quote.lines_of(row), row['total'],
        _rowget(row, 'qbo_customer_memo') or '',
        _rowget(row, 'qbo_bill_to') or '',
        _rowget(row, 'qbo_ship_to') or '',
        _rowget(row, 'qbo_tax_amount') or 0,
        _rowget(row, 'qbo_discount_amount') or 0,
        _rowget(row, 'qbo_expiration') or '',
    )


def _thin_items(lines):
    out = []
    for ln in lines or []:
        if (ln.get('kind') or 'item') != 'item':
            continue
        sell = ln.get('sell')
        try:
            sell_r = round(float(sell or 0), 2)
        except (TypeError, ValueError):
            sell_r = sell
        try:
            qty_r = float(ln.get('qty') or 0)
        except (TypeError, ValueError):
            qty_r = ln.get('qty')
        out.append(((ln.get('name') or '').strip(), qty_r, sell_r))
    return out


def quote_was_edited(row, incoming_lines=None):
    """True if someone changed the quote in the app after the last import."""
    if (row['status'] or '') == 'accepted':
        return True
    stored = _rowget(row, 'qbo_import_hash') or ''
    if stored:
        return hash_quote_row(row) != stored
    ua = row['updated_at'] or ''
    ca = row['created_at'] or ''
    sa = _rowget(row, 'sent_at') or ''
    timestamp_changed = bool(ua and ua not in (ca, sa))
    if not timestamp_changed:
        return False
    # A later save of the original thin import (names and prices only) is not a
    # real edit; those quotes should still receive the missing QBO details.
    if incoming_lines is None:
        return True
    current = _thin_items(quote.lines_of(row))
    incoming = _thin_items(incoming_lines)
    return current != incoming


def _apply_qbo_fields(conn, quote_id, header, status, qbo_id, doc, when, digest):
    conn.execute(
        '''UPDATE quotes SET status='sent', qbo_estimate_id=?, qbo_doc_number=?, qbo_txn_status=?,
           sent_at=COALESCE(sent_at, ?), created_at=COALESCE(created_at, ?),
           qbo_customer_memo=?, qbo_bill_to=?, qbo_ship_to=?, qbo_expiration=?,
           qbo_tax_rate=?, qbo_tax_amount=?, qbo_discount_amount=?, qbo_subtotal=?,
           qbo_import_hash=?, qbo_imported_at=?, total=?, updated_at=?
           WHERE id=?''',
        (qbo_id, doc, status.title(), when, when,
         header['memo'][:2000], header['bill'][:300], header['ship'][:300], header['expiration'],
         header['tax_rate'], header['tax_amount'], header['discount'], header['subtotal'],
         digest, plat.now_iso(), header['total'], plat.now_iso(), quote_id))


def import_one(conn, est, today=None, mode='create'):
    """Import one estimate. Returns (status, info)."""
    if not isinstance(est, dict):
        return 'error', {'error': 'Estimate must be an object.'}
    qbo_id = _estimate_id(est)
    if not qbo_id:
        return 'error', {'error': 'Missing estimate Id.'}
    status = _status(est)
    if status not in OPEN_STATUSES:
        return 'skipped', {'qbo_estimate_id': qbo_id, 'reason': 'status %s' % (status or 'blank')}
    txn = parse_txn_date(_txn_date_raw(est))
    if not txn:
        return 'skipped', {'qbo_estimate_id': qbo_id, 'reason': 'missing TxnDate'}
    if txn < cutoff_12_months(today):
        return 'skipped', {'qbo_estimate_id': qbo_id, 'reason': 'older than 12 months'}

    lines = extract_lines(conn, est)
    item_lines = [ln for ln in lines if (ln.get('kind') or 'item') == 'item']
    if not item_lines:
        return 'error', {'qbo_estimate_id': qbo_id, 'error': 'No sales line items.'}
    header = extract_header(est, lines)
    if not header['customer']:
        return 'error', {'qbo_estimate_id': qbo_id, 'error': 'Missing CustomerRef name.'}

    found = existing_quote(conn, qbo_id)
    if found and mode != 'update':
        return 'unchanged', {
            'qbo_estimate_id': qbo_id,
            'quote_id': found['id'],
            'qbo_doc_number': found['qbo_doc_number'] or _doc_number(est),
        }
    if found and mode == 'update':
        if quote_was_edited(found, incoming_lines=lines):
            return 'flagged', {
                'qbo_estimate_id': qbo_id,
                'quote_id': found['id'],
                'qbo_doc_number': found['qbo_doc_number'] or _doc_number(est),
                'reason': 'edited in the app after import',
            }
        return _update_existing(conn, found, est, lines, header, status, qbo_id, txn)

    return _insert_new(conn, est, lines, header, status, qbo_id, txn)


def office_import_note(doc, qbo_id):
    """Office-only source label. Never stored on the client-facing quote scope."""
    return 'Imported from QuickBooks estimate %s.' % (doc or qbo_id)

def _insert_new(conn, est, lines, header, status, qbo_id, txn):
    doc = _doc_number(est)
    when = (_text(_txn_date_raw(est))[:10] or plat.now_iso())
    if len(when) == 10:
        when = when + 'T00:00:00'
    company = plat.upsert_company(
        conn, header['customer'], phone=header['phone'], email=header['email'],
        billing_address=header['bill'], when=when)
    site = None
    if header['ship'] or header['bill']:
        site = plat.upsert_site(conn, company['id'], address=header['ship'] or header['bill'], when=when)
    title = ('Estimate %s' % doc) if doc else ('QBO estimate %s' % qbo_id)
    office_note = office_import_note(doc, qbo_id)
    memo = header['memo'] or ''
    stage = 'won' if status == 'accepted' else 'proposal'
    won_at = when if stage == 'won' else None
    cur = conn.execute(
        '''INSERT INTO opportunities (company_id,contact_id,site_id,title,stage,amount,notes,created_by,created_at,updated_at,won_at,lost_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (company['id'], None, site['id'] if site else None, title, stage, header['total'], office_note,
         CREATED_BY, when, when, won_at, None))
    opp_id = cur.lastrowid
    row, err = quote.save_quote(conn, {
        'title': title,
        'company_id': company['id'],
        'site_id': site['id'] if site else None,
        'opportunity_id': opp_id,
        'notes': '',
        'lines': lines,
        'tax_amount': header['tax_amount'],
        'discount_amount': header['discount'],
    }, created_by=CREATED_BY)
    if err or not row:
        return 'error', {'qbo_estimate_id': qbo_id, 'error': err or 'Could not save quote.'}
    digest = snapshot_hash(title, '', '', quote.lines_of(row) if hasattr(quote, 'lines_of') else lines,
                           header['total'], memo, header['bill'], header['ship'],
                           header['tax_amount'], header['discount'], header['expiration'])
    # Recompute hash from the saved lines so it matches later reads.
    _apply_qbo_fields(conn, row['id'], header, status, qbo_id, doc, when, 'pending')
    saved = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
    digest = hash_quote_row(saved)
    conn.execute('UPDATE quotes SET qbo_import_hash=? WHERE id=?', (digest, row['id']))
    conn.execute(
        'UPDATE opportunities SET amount=?, updated_at=? WHERE id=?',
        (header['total'], when, opp_id))
    saved = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
    return 'imported', {
        'qbo_estimate_id': qbo_id,
        'qbo_doc_number': doc,
        'quote_id': saved['id'],
        'opportunity_id': opp_id,
        'company_id': company['id'],
        'total': saved['total'],
    }


def _update_existing(conn, found, est, lines, header, status, qbo_id, txn):
    doc = _doc_number(est) or (found['qbo_doc_number'] or '')
    when = (_text(_txn_date_raw(est))[:10] or '')
    if len(when) == 10:
        when = when + 'T00:00:00'
    title = found['title'] or (('Estimate %s' % doc) if doc else found['title'])
    # Keep a human title tied to the QBO doc number if it is still the generated one.
    if (found['title'] or '').startswith('Estimate ') or (found['title'] or '').startswith('QBO estimate '):
        title = ('Estimate %s' % doc) if doc else title
    note_text = quote.strip_import_scope_note(_rowget(found, 'notes') or '')
    row, err = quote.save_quote(conn, {
        'title': title,
        'company_id': found['company_id'],
        'site_id': found['site_id'],
        'contact_id': found['contact_id'],
        'opportunity_id': found['opportunity_id'],
        'job_id': found['job_id'],
        'notes': note_text,
        'clarifications': _rowget(found, 'clarifications') or '',
        'lines': lines,
        'tax_amount': header['tax_amount'],
        'discount_amount': header['discount'],
    }, quote_id=found['id'])
    if err or not row:
        return 'error', {'qbo_estimate_id': qbo_id, 'error': err or 'Could not update quote.'}
    if header['bill'] or header['ship'] or header['email'] or header['phone']:
        if found['company_id']:
            plat.upsert_company(
                conn, header['customer'], phone=header['phone'], email=header['email'],
                billing_address=header['bill'], when=when or plat.now_iso())
        if found['company_id'] and (header['ship'] or header['bill']):
            plat.upsert_site(conn, found['company_id'], address=header['ship'] or header['bill'],
                             when=when or plat.now_iso())
    _apply_qbo_fields(conn, found['id'], header, status, qbo_id, doc,
                      when or (found['sent_at'] or found['created_at']), 'pending')
    saved = conn.execute('SELECT * FROM quotes WHERE id=?', (found['id'],)).fetchone()
    digest = hash_quote_row(saved)
    conn.execute('UPDATE quotes SET qbo_import_hash=? WHERE id=?', (digest, found['id']))
    if found['opportunity_id']:
        conn.execute(
            'UPDATE opportunities SET amount=?, updated_at=? WHERE id=?',
            (header['total'], plat.now_iso(), found['opportunity_id']))
    saved = conn.execute('SELECT * FROM quotes WHERE id=?', (found['id'],)).fetchone()
    return 'updated', {
        'qbo_estimate_id': qbo_id,
        'qbo_doc_number': doc,
        'quote_id': saved['id'],
        'opportunity_id': found['opportunity_id'],
        'company_id': found['company_id'],
        'total': saved['total'],
    }


def cleanup_imported_scope_notes(conn):
    """Strip the old import sentence from client-facing scope on existing quotes.

    Recompute qbo_import_hash when it was set, so a later update re-sync does not
    treat this cleanup as an office edit.
    """
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM quotes WHERE IFNULL(qbo_estimate_id,'') != ''"
    ).fetchall()
    changed = 0
    for row in rows:
        notes = row['notes'] if 'notes' in row.keys() else ''
        notes = notes or ''
        cleaned = quote.strip_import_scope_note(notes)
        if cleaned == notes:
            continue
        conn.execute('UPDATE quotes SET notes=? WHERE id=?', (cleaned, row['id']))
        if _rowget(row, 'qbo_import_hash'):
            saved = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
            conn.execute(
                'UPDATE quotes SET qbo_import_hash=? WHERE id=?',
                (hash_quote_row(saved), row['id']))
        changed += 1
    return changed


def unwrap_estimates(payload):

    """Accept a list, a single estimate, or a QBO QueryResponse wrapper."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    if payload.get('mode') and ('estimates' in payload or 'QueryResponse' in payload or 'queryResponse' in payload):
        # wrapper with mode; unwrap the inner payload
        inner = payload.get('estimates')
        if inner is not None:
            return unwrap_estimates(inner)
        qr = payload.get('QueryResponse') or payload.get('queryResponse') or {}
        return unwrap_estimates(qr if 'Estimate' in qr or 'estimate' in qr else payload)
    if 'Id' in payload or 'id' in payload or 'reference_number' in payload:
        # a single estimate (raw or MCP)
        if payload.get('DocNumber') or payload.get('TxnDate') or payload.get('Line') or payload.get('lines') or payload.get('accept_status'):
            return [payload]
        if 'Id' in payload or 'id' in payload:
            # could be a lone estimate
            if any(k in payload for k in ('TxnStatus', 'txnStatus', 'CustomerRef', 'customerRef', 'contact', 'TotalAmt', 'amount')):
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


def import_estimates(conn, payload, today=None, mode='create'):
    init_schema(conn)
    if isinstance(payload, dict) and payload.get('mode') in ('update', 'create'):
        mode = payload.get('mode') or mode
    items = unwrap_estimates(payload)
    if items is None:
        return {'imported': 0, 'updated': 0, 'unchanged': 0, 'skipped': 0, 'flagged': 0,
                'errors': [{'error': 'estimates must be a list'}], 'quotes': [], 'flagged_quotes': []}
    today = today or date.today()
    imported = updated = unchanged = skipped = 0
    errors, details, flagged = [], [], []
    for est in items:
        status, info = import_one(conn, est, today=today, mode=mode)
        info = info or {}
        if status == 'imported':
            imported += 1
            details.append(info)
        elif status == 'updated':
            updated += 1
            details.append(info)
        elif status == 'unchanged':
            unchanged += 1
        elif status == 'skipped':
            skipped += 1
        elif status == 'flagged':
            flagged.append(info)
        else:
            errors.append(info)
    return {
        'imported': imported,
        'updated': updated,
        'unchanged': unchanged,
        'skipped': skipped,
        'flagged': len(flagged),
        'errors': errors,
        'quotes': details,
        'flagged_quotes': flagged,
    }


def register(fo):
    app = fo.app
    request = fo.request
    jsonify = fo.jsonify
    try:
        conn = fo.get_db()
        cleanup_imported_scope_notes(conn)
        conn.commit()
        conn.close()
    except Exception:
        # Schema may not be ready in some unit-test import orders.
        pass

    @app.route('/api/qbo/import-estimates', methods=['POST'])
    def qbo_import_estimates():
        bad = fo.sync_key_ok()
        if bad:
            return bad
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({'error': 'JSON body required'}), 400
        mode = 'create'
        if isinstance(payload, dict) and payload.get('mode') in ('update', 'create'):
            mode = payload['mode']
        elif request.args.get('mode') in ('update', 'create'):
            mode = request.args.get('mode')
        conn = fo.get_db()
        result = import_estimates(conn, payload, mode=mode)
        conn.commit()
        conn.close()
        return jsonify(result)
