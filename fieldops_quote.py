"""Phase 2 Quote: price catalog, frozen quotes, versions, add-ons, client link.

Prices are office-only in the catalog. The client link shows sell prices on the
one estimate, including optional add-ons the client can tick, then sign.
Nothing is sent by the app (no email or texts). Open tracking is recorded when
the client opens the link.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
from datetime import datetime, timedelta, date
from pathlib import Path


DEFAULT_MARKUP = 1.25
QUOTE_TERMS_KEY = 'quote_terms'
QUOTE_DEPOSIT_MIN_KEY = 'quote_deposit_min'
DEFAULT_DEPOSIT_MIN = 1000.0
LICENSE_NO = 'B31163401'
VALID_DAYS = 15


def _load_default_terms():
    try:
        return Path(__file__).resolve().parent.joinpath('truesense_terms.txt').read_text(encoding='utf-8')
    except Exception:
        return ''


DEFAULT_TERMS = _load_default_terms()


def _cols(conn, table):
    return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}


def _add_col(conn, table, coldef):
    name = coldef.split()[0]
    if name in _cols(conn, table):
        return
    conn.execute('ALTER TABLE %s ADD COLUMN %s' % (table, coldef))


def seed_default_terms(conn):
    """Write the approved True Sense terms once, if no live default is set yet."""
    stored = (DEFAULT_TERMS or '').strip()
    if not stored:
        return False
    row = conn.execute('SELECT value FROM settings WHERE key=?', (QUOTE_TERMS_KEY,)).fetchone()
    if row and (row['value'] or '').strip():
        return False
    if not stored.endswith('\n'):
        stored = stored + '\n'
    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (QUOTE_TERMS_KEY, stored))
    return True


def get_saved_terms(conn):
    """Live default terms saved in Settings."""
    try:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (QUOTE_TERMS_KEY,)).fetchone()
    except Exception:
        return ''
    return ((row['value'] if row else '') or '').strip()


def get_default_terms(conn):
    """Live default terms. Same as get_saved_terms after seed or a Settings save."""
    return get_saved_terms(conn)


def set_default_terms(conn, text):
    text = (text or '').strip()
    if not text:
        return 'Terms cannot be empty.'
    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (QUOTE_TERMS_KEY, text))
    return None


def quote_number(row):
    try:
        doc = (row['qbo_doc_number'] or '') if 'qbo_doc_number' in row.keys() else ''
    except Exception:
        doc = ''
    if doc:
        return doc
    return 'Q-%d' % row['id']


def estimate_date(row):
    sent = ''
    try:
        sent = (row['sent_at'] or '') if 'sent_at' in row.keys() else ''
    except Exception:
        sent = ''
    raw = sent or (row['created_at'] or '')
    return (raw or '')[:10]


def valid_until(row, days=VALID_DAYS):
    try:
        exp = (row['qbo_expiration'] or '') if 'qbo_expiration' in row.keys() else ''
    except Exception:
        exp = ''
    if exp:
        return str(exp)[:10]
    d = estimate_date(row)
    if not d:
        return ''
    try:
        return (datetime.strptime(d, '%Y-%m-%d').date() + timedelta(days=days)).isoformat()
    except ValueError:
        return ''


def get_deposit_min(conn=None):
    """Minimum estimate total that still asks for a 50% deposit. Default $1,000."""
    owned = False
    if conn is None:
        try:
            import fieldops_app as fo
            conn = fo.get_db()
            owned = True
        except Exception:
            return DEFAULT_DEPOSIT_MIN
    try:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (QUOTE_DEPOSIT_MIN_KEY,)).fetchone()
        if row and str(row['value'] or '').strip() != '':
            val = float(row['value'])
            if val >= 0:
                return val
    except (TypeError, ValueError, Exception):
        pass
    finally:
        if owned and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return DEFAULT_DEPOSIT_MIN


def set_deposit_min(conn, raw):
    text = (raw or '').strip().replace(',', '')
    if text == '':
        val = DEFAULT_DEPOSIT_MIN
    else:
        try:
            val = float(text)
        except (TypeError, ValueError):
            return 'Deposit minimum must be a number.'
        if val < 0:
            return 'Deposit minimum cannot be negative.'
    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                 (QUOTE_DEPOSIT_MIN_KEY, str(val)))
    return None


def deposit_applies(total, min_amount=None, conn=None):
    try:
        total = float(total or 0)
    except (TypeError, ValueError):
        total = 0.0
    if min_amount is None:
        min_amount = get_deposit_min(conn)
    try:
        min_amount = float(min_amount)
    except (TypeError, ValueError):
        min_amount = DEFAULT_DEPOSIT_MIN
    return total >= min_amount and total > 0


def deposit_of(total, min_amount=None, conn=None):
    try:
        total = float(total or 0)
    except (TypeError, ValueError):
        return 0.0
    if not deposit_applies(total, min_amount=min_amount, conn=conn):
        return 0.0
    return round(total * 0.5, 2)


def terms_of(row):
    try:
        text = (row['terms_text'] or '') if 'terms_text' in row.keys() else ''
    except Exception:
        text = ''
    return (text or '').strip()


def clarifications_of(row):
    try:
        text = (row['clarifications'] or '') if 'clarifications' in row.keys() else ''
    except Exception:
        text = ''
    return (text or '').strip()


def _rowcol(row, key, default=''):
    try:
        if key in row.keys() and row[key] is not None:
            return row[key]
    except Exception:
        pass
    return default



IMPORT_SCOPE_NOTE_RE = re.compile(
    r'(?im)^Imported from QuickBooks estimate [A-Za-z0-9._-]+\.\s*'
)


def strip_import_scope_note(text):
    """Remove the internal QBO import sentence from client-facing scope notes."""
    return IMPORT_SCOPE_NOTE_RE.sub('', text or '').strip()


def client_scope_of(row):
    try:
        notes = (row['notes'] or '') if 'notes' in row.keys() else ''
    except Exception:
        notes = ''
    return strip_import_scope_note(notes)


def customer_memo_of(row):
    return (_rowcol(row, 'qbo_customer_memo') or '').strip()


def bill_to_of(row):
    return (_rowcol(row, 'qbo_bill_to') or '').strip()


def ship_to_of(row):
    return (_rowcol(row, 'qbo_ship_to') or '').strip()


def tax_rate_label(rate):
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return ''
    if not rate:
        return ''
    text = ('%g' % rate)
    return text + '%'



def now_iso():
    return datetime.utcnow().isoformat()


def money(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0
    return '$%s' % ('{:,.2f}'.format(n))


def sell_from_cost(cost, markup=DEFAULT_MARKUP):
    try:
        cost = float(cost or 0)
    except (TypeError, ValueError):
        cost = 0
    return round(cost * markup, 2)


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS catalog_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        sku TEXT,
        unit TEXT,
        category TEXT,
        cost REAL DEFAULT 0,
        sell REAL DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at TEXT,
        updated_at TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS quotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opportunity_id INTEGER,
        job_id TEXT,
        company_id INTEGER,
        site_id INTEGER,
        contact_id INTEGER,
        title TEXT,
        version INTEGER DEFAULT 1,
        parent_id INTEGER,
        status TEXT DEFAULT 'draft',
        notes TEXT,
        lines_json TEXT,
        token TEXT,
        opened_at TEXT,
        open_count INTEGER DEFAULT 0,
        accepted_at TEXT,
        accepted_name TEXT,
        signature TEXT,
        accepted_ip TEXT,
        total REAL DEFAULT 0,
        created_by TEXT,
        created_at TEXT,
        updated_at TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_quotes_opp ON quotes(opportunity_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_quotes_token ON quotes(token)')
    for col in ('terms_text TEXT', 'clarifications TEXT', 'sent_at TEXT', 'terms_agreed_at TEXT',
                'qbo_customer_memo TEXT', 'qbo_bill_to TEXT', 'qbo_ship_to TEXT', 'qbo_expiration TEXT',
                'qbo_tax_rate REAL', 'qbo_tax_amount REAL', 'qbo_discount_amount REAL', 'qbo_subtotal REAL',
                'qbo_import_hash TEXT', 'qbo_imported_at TEXT'):
        _add_col(conn, 'quotes', col)
    try:
        seed_default_terms(conn)
    except Exception:
        pass
    conn.execute('CREATE INDEX IF NOT EXISTS ix_catalog_name ON catalog_items(name)')


def lines_of(row):
    try:
        data = json.loads(row['lines_json'] or '[]')
    except Exception:
        data = []
    return data if isinstance(data, list) else []


def freeze_line(name, qty, cost, sell, optional=False, catalog_id=None, unit='', source='', plan_key='',
                description='', kind='item', amount=None, taxable=False, qbo_line_id=''):
    kind = (kind or 'item') or 'item'
    try:
        qty = float(qty if qty is not None else 0)
    except (TypeError, ValueError):
        qty = 0
    try:
        cost = float(cost or 0)
    except (TypeError, ValueError):
        cost = 0
    try:
        sell = float(sell or 0) if sell is not None else 0
    except (TypeError, ValueError):
        sell = 0
    if kind == 'item' and sell <= 0 and cost > 0 and not description and amount in (None, '', 0, 0.0):
        sell = sell_from_cost(cost)
    qty = qty if kind in ('text', 'subtotal', 'group', 'tax', 'discount') else max(0, qty)
    desc = description if isinstance(description, str) else ('' if description is None else str(description))
    name = (name or '').strip()
    if not name and desc:
        name = desc.split('\n', 1)[0][:300]
    line_total = None
    if amount is not None and amount != '':
        try:
            line_total = round(float(amount), 2)
        except (TypeError, ValueError):
            line_total = None
    if line_total is None:
        if kind == 'discount':
            line_total = -abs(round(qty * sell if sell and qty else sell, 2))
        elif kind in ('tax', 'subtotal', 'group'):
            line_total = round(float(sell or 0), 2)
        elif kind == 'text':
            line_total = 0.0
        else:
            line_total = round(qty * sell, 2)
    # Keep rate precision (some QBO unit prices are more than 2 decimals).
    if kind == 'item':
        sell_out = round(sell, 6) if sell else 0
    else:
        sell_out = round(sell, 2)
    out = {
        'name': name[:300],
        'description': desc,
        'qty': qty,
        'unit': (unit or '')[:20],
        'cost': round(cost, 2),
        'sell': sell_out,
        'optional': bool(optional),
        'catalog_id': catalog_id,
        'line_total': line_total,
        'kind': kind,
        'taxable': bool(taxable),
    }
    if amount is not None and amount != '':
        try:
            out['amount'] = round(float(amount), 2)
        except (TypeError, ValueError):
            out['amount'] = line_total
    else:
        out['amount'] = line_total
    if qbo_line_id:
        out['qbo_line_id'] = str(qbo_line_id)
    if source:
        out['source'] = source
        out['plan_key'] = plan_key or ''
    return out


def line_amount(ln):
    kind = ln.get('kind') or 'item'
    if kind in ('text', 'description', 'group', 'subtotal'):
        return 0.0
    if kind == 'tax':
        return 0.0
    if kind == 'discount':
        return 0.0
    if ln.get('amount') not in (None, ''):
        try:
            return round(float(ln['amount']), 2)
        except (TypeError, ValueError):
            pass
    try:
        return round(float(ln.get('qty') or 0) * float(ln.get('sell') or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def quote_totals(lines, selected=None, tax_amount=None, discount_amount=None):
    """selected is a set of optional line indexes the client ticked. None means all required plus no add-ons."""
    base = 0
    addons = 0
    discount = 0.0 if discount_amount is None else abs(float(discount_amount or 0))
    tax = 0.0 if tax_amount is None else float(tax_amount or 0)
    saw_tax = tax_amount is not None
    saw_disc = discount_amount is not None
    for i, ln in enumerate(lines or []):
        kind = ln.get('kind') or 'item'
        if kind == 'tax':
            if not saw_tax:
                tax += float(ln.get('amount') or ln.get('sell') or ln.get('line_total') or 0)
            continue
        if kind == 'discount':
            if not saw_disc:
                discount += abs(float(ln.get('amount') or ln.get('sell') or 0))
            continue
        if kind not in ('item', ''):
            continue
        amt = line_amount(ln)
        if ln.get('optional'):
            if selected is not None and i in selected:
                addons += amt
        else:
            base += amt
    subtotal = round(base + addons, 2)
    discount = round(discount, 2)
    tax = round(tax, 2)
    total = round(subtotal - discount + tax, 2)
    return {
        'base': round(base, 2),
        'addons': round(addons, 2),
        'subtotal': subtotal,
        'discount': discount,
        'tax': tax,
        'total': total,
    }


def import_catalog_csv(conn, text, created_by=''):
    """Return (added, updated, errors). Sell defaults to cost plus 25 percent."""
    added = updated = 0
    errors = []
    f = io.StringIO(text or '')
    # strip BOM
    sample = f.read(4); f.seek(0)
    try:
        reader = csv.DictReader(f)
    except Exception as e:
        return 0, 0, ['Could not read that file.']
    if not reader.fieldnames:
        return 0, 0, ['The file needs a header row (name, cost, sell).']
    fields = { (n or '').strip().lower().replace(' ', '_'): n for n in reader.fieldnames }
    def cell(row, *keys):
        for k in keys:
            src = fields.get(k)
            if src and row.get(src) not in (None, ''):
                return str(row.get(src)).strip()
        return ''
    when = now_iso()
    for i, row in enumerate(reader, start=2):
        name = cell(row, 'name', 'item', 'description', 'product')
        if not name:
            errors.append('Row %d: name is required.' % i)
            continue
        cost_s = cell(row, 'cost', 'unit_cost', 'our_cost')
        sell_s = cell(row, 'sell', 'price', 'unit_price', 'sell_price')
        try:
            cost = float(cost_s or 0)
        except ValueError:
            errors.append('Row %d: cost must be a number.' % i); continue
        try:
            sell = float(sell_s) if sell_s else sell_from_cost(cost)
        except ValueError:
            errors.append('Row %d: sell must be a number.' % i); continue
        sku = cell(row, 'sku', 'item_no', 'part')
        unit = cell(row, 'unit', 'uom')
        category = cell(row, 'category', 'group')
        existing = None
        if sku:
            existing = conn.execute('SELECT id FROM catalog_items WHERE LOWER(TRIM(IFNULL(sku,"")))=?', (sku.lower(),)).fetchone()
        if not existing:
            existing = conn.execute(
                'SELECT id FROM catalog_items WHERE LOWER(TRIM(name))=? AND LOWER(TRIM(IFNULL(sku,"")))=?',
                (name.lower(), (sku or '').lower())).fetchone()
        if existing:
            conn.execute(
                'UPDATE catalog_items SET name=?,sku=?,unit=?,category=?,cost=?,sell=?,active=1,updated_at=? WHERE id=?',
                (name[:200], sku[:40], unit[:20], category[:80], cost, sell, when, existing['id']))
            updated += 1
        else:
            conn.execute(
                'INSERT INTO catalog_items (name,sku,unit,category,cost,sell,active,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?)',
                (name[:200], sku[:40], unit[:20], category[:80], cost, sell, when, when))
            added += 1
    return added, updated, errors


def next_version(conn, opportunity_id, parent_id=None):
    if parent_id:
        row = conn.execute('SELECT version FROM quotes WHERE id=?', (parent_id,)).fetchone()
        return int(row['version'] or 1) + 1 if row else 1
    if opportunity_id:
        top = conn.execute(
            'SELECT MAX(version) AS v FROM quotes WHERE opportunity_id=?', (opportunity_id,)).fetchone()
        return int(top['v'] or 0) + 1
    return 1


def save_quote(conn, data, quote_id=None, created_by=''):
    title = (data.get('title') or '').strip()[:200]
    if not title:
        return None, 'Title is required.'
    lines = data.get('lines') or []
    if not lines:
        return None, 'Add at least one line.'
    frozen = []
    for ln in lines:
        kind = ln.get('kind') or 'item'
        name = (ln.get('name') or '').strip()
        desc = ln.get('description') or ''
        if not name and not desc and kind == 'item':
            continue
        qty = ln.get('qty')
        if qty is None:
            qty = 0 if kind != 'item' else 1
        frozen.append(freeze_line(
            name, qty, ln.get('cost') or 0, ln.get('sell') or 0,
            optional=bool(ln.get('optional')), catalog_id=ln.get('catalog_id'), unit=ln.get('unit') or '',
            source=ln.get('source') or '', plan_key=ln.get('plan_key') or '',
            description=desc, kind=kind, amount=ln.get('amount'),
            taxable=bool(ln.get('taxable')), qbo_line_id=ln.get('qbo_line_id') or ''))
    if not frozen:
        return None, 'Add at least one line.'
    tot = quote_totals(
        frozen,
        tax_amount=data.get('tax_amount'),
        discount_amount=data.get('discount_amount'),
    )['total']
    when = now_iso()
    if quote_id:
        row = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
        if not row:
            return None, 'Quote not found.'
        if row['status'] == 'accepted':
            return None, 'This quote is accepted. Start a new version to change it.'
        conn.execute(
            'UPDATE quotes SET title=?,notes=?,clarifications=?,lines_json=?,total=?,updated_at=?,company_id=?,site_id=?,contact_id=?,opportunity_id=?,job_id=? WHERE id=?',
            (title, (data.get('notes') or '')[:2000], (data.get('clarifications') or '')[:4000], json.dumps(frozen), tot, when,
             data.get('company_id'), data.get('site_id'), data.get('contact_id'),
             data.get('opportunity_id'), data.get('job_id') or row['job_id'], quote_id))
        return conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone(), None
    ver = next_version(conn, data.get('opportunity_id'), data.get('parent_id'))
    cur = conn.execute(
        '''INSERT INTO quotes (opportunity_id,job_id,company_id,site_id,contact_id,title,version,parent_id,
           status,notes,clarifications,lines_json,total,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?)''',
        (data.get('opportunity_id'), data.get('job_id'), data.get('company_id'), data.get('site_id'),
         data.get('contact_id'), title, ver, data.get('parent_id'),
         (data.get('notes') or '')[:2000], (data.get('clarifications') or '')[:4000], json.dumps(frozen), tot, created_by, when, when))
    return conn.execute('SELECT * FROM quotes WHERE id=?', (cur.lastrowid,)).fetchone(), None


def ensure_token(conn, quote_id):
    row = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
    if not row:
        return None
    when = now_iso()
    sets, args = [], []
    if not row['token']:
        sets.append('token=?'); args.append(secrets.token_urlsafe(24))
        sets.append("status=CASE WHEN status='draft' THEN 'sent' ELSE status END")
    exp = ''
    try:
        exp = (row['token_expires_at'] or '') if 'token_expires_at' in row.keys() else ''
    except Exception:
        exp = ''
    if not exp:
        try:
            import fieldops_security as _sec
            sets.append('token_expires_at=?'); args.append(_sec.expiry_iso())
        except Exception:
            pass
    sent = ''
    try:
        sent = (row['sent_at'] or '') if 'sent_at' in row.keys() else ''
    except Exception:
        sent = ''
    if not sent:
        sets.append('sent_at=?'); args.append(when)
    if not terms_of(row):
        saved = get_saved_terms(conn)
        if saved:
            sets.append('terms_text=?'); args.append(saved)
    if sets:
        sets.append('updated_at=?'); args.append(when)
        args.append(quote_id)
        conn.execute('UPDATE quotes SET ' + ', '.join(sets) + ' WHERE id=?', args)
    # Move opportunity to proposal when a link is made
    if row['opportunity_id'] and (row['status'] in (None, 'draft', 'sent')):
        conn.execute(
            "UPDATE opportunities SET stage=CASE WHEN stage IN ('won','lost') THEN stage ELSE 'proposal' END, updated_at=? WHERE id=?",
            (when, row['opportunity_id']))
    return conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()


def record_open(conn, quote_id):
    row = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
    if not row:
        return
    when = now_iso()
    opened = row['opened_at'] or when
    conn.execute(
        'UPDATE quotes SET opened_at=?, open_count=IFNULL(open_count,0)+1, updated_at=? WHERE id=?',
        (opened, when, quote_id))
    conn.commit()


def _next_job_id(conn):
    from datetime import date
    y = date.today().year
    pre = 'TS-%d-' % y
    top = 0
    for r in conn.execute('SELECT id FROM jobs WHERE id LIKE ?', (pre + '%',)):
        try:
            top = max(top, int(r['id'][len(pre):]))
        except ValueError:
            pass
    return pre + '%04d' % (top + 1)


def accept_quote(conn, quote, selected, name, signature, ip, fo=None):
    """Freeze selected add-ons, mark accepted, update/create the job, set opportunity Won."""
    if quote['status'] == 'accepted':
        return quote, None
    lines = lines_of(quote)
    selected = set(selected or [])
    totals = quote_totals(lines, selected)
    when = now_iso()
    # Drop unticked add-ons from the frozen accepted snapshot
    kept = []
    for i, ln in enumerate(lines):
        if ln.get('optional') and i not in selected:
            continue
        kept.append(ln)
    conn.execute(
        '''UPDATE quotes SET status='accepted', accepted_at=?, accepted_name=?, signature=?, accepted_ip=?,
           lines_json=?, total=?, terms_agreed_at=?, updated_at=? WHERE id=?''',
        (when, (name or '')[:100], signature or '', (ip or '')[:80], json.dumps(kept), totals['total'], when, when, quote['id']))
    job_id = quote['job_id']
    # Create or update job
    title = quote['title']
    company = conn.execute('SELECT * FROM companies WHERE id=?', (quote['company_id'],)).fetchone() if quote['company_id'] else None
    site = conn.execute('SELECT * FROM sites WHERE id=?', (quote['site_id'],)).fetchone() if quote['site_id'] else None
    customer = company['name'] if company else title
    address = (site['address'] if site else '') or (company['billing_address'] if company else '') or ''
    phone = (company['phone'] if company else '') or ''
    if not job_id:
        job_id = _next_job_id(conn)
        tech = ''
        if fo:
            tech = fo.default_sub() or fo.me() or ''
        conn.execute(
            '''INSERT INTO jobs (id,title,customer,address,phone,scope,status,tech,created_at,scheduled_date,created_by,job_type,company_id,site_id,contact_id,sale_amount)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (job_id, title, customer, address, phone, title, 'assigned', tech, when, None,
             (quote['created_by'] or ''), 'Install', quote['company_id'], quote['site_id'], quote['contact_id'], totals['total']))
        try:
            import fieldops_platform as plat
            plat.link_job(conn, conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone(), company_id=quote['company_id'])
        except Exception:
            pass
        conn.execute('UPDATE quotes SET job_id=? WHERE id=?', (job_id, quote['id']))
        if quote['opportunity_id']:
            conn.execute('UPDATE opportunities SET job_id=? WHERE id=? AND IFNULL(job_id,"")=""', (job_id, quote['opportunity_id']))
    else:
        conn.execute('UPDATE jobs SET sale_amount=? WHERE id=?', (totals['total'], job_id))
    # Sync the one estimate onto the job (sell prices only)
    desc = title
    items = ['%g x %s' % (ln.get('qty') or 1, ln.get('name')) for ln in kept]
    est = {'desc': desc, 'items': items, 'price': totals['total'], 'note': quote['notes'] or '',
           'signed_name': name, 'signature': signature or '', 'signed_at': when, 'ip': ip or ''}
    conn.execute('UPDATE jobs SET estimate_json=?, sale_amount=? WHERE id=?', (json.dumps(est), totals['total'], job_id))
    if quote['opportunity_id']:
        conn.execute(
            "UPDATE opportunities SET stage='won', amount=?, job_id=COALESCE(job_id, ?), won_at=?, lost_at=NULL, updated_at=? WHERE id=?",
            (totals['total'], job_id, when, when, quote['opportunity_id']))
    return conn.execute('SELECT * FROM quotes WHERE id=?', (quote['id'],)).fetchone(), None



def decorate_client_lines(row, selected=None):
    """Shape stored lines for the client page and print/PDF view."""
    lines = []
    raw = lines_of(row)
    for i, ln in enumerate(raw):
        item = dict(ln)
        item['idx'] = i
        kind = item.get('kind') or 'item'
        item['kind'] = kind
        qv = item.get('qty')
        if kind in ('text', 'subtotal', 'group', 'tax', 'discount') or qv in (None, ''):
            item['qty_label'] = '' if kind != 'item' else ''
        else:
            try:
                qf = float(qv)
                item['qty_label'] = ('%g' % qf)
            except (TypeError, ValueError):
                item['qty_label'] = str(qv)
        if kind == 'item':
            item['show_rate'] = True
            item['rate'] = item.get('sell') or 0
            amt = line_amount(item)
            item['amt_value'] = amt
            item['amount_label'] = money(amt)
        elif kind == 'text':
            item['show_rate'] = False
            item['rate'] = 0
            item['amt_value'] = 0
            item['amount_label'] = ''
        elif kind == 'subtotal':
            item['show_rate'] = False
            item['rate'] = 0
            amt = item.get('amount') or item.get('line_total') or 0
            item['amt_value'] = 0
            item['amount_label'] = money(amt)
        elif kind == 'discount':
            item['show_rate'] = False
            amt = abs(float(item.get('amount') or item.get('sell') or 0))
            item['amt_value'] = 0
            item['amount_label'] = money(amt)
        elif kind == 'tax':
            item['show_rate'] = False
            amt = float(item.get('amount') or item.get('sell') or 0)
            item['amt_value'] = 0
            item['amount_label'] = money(amt)
        elif kind == 'group':
            item['show_rate'] = False
            amt = item.get('amount') or 0
            item['amt_value'] = 0
            item['amount_label'] = money(amt) if amt else ''
        else:
            item['show_rate'] = True
            item['rate'] = item.get('sell') or 0
            item['amt_value'] = line_amount(item)
            item['amount_label'] = money(item['amt_value'])
        lines.append(item)
    tax = _rowcol(row, 'qbo_tax_amount', None)
    disc = _rowcol(row, 'qbo_discount_amount', None)
    totals = quote_totals(raw, selected, tax_amount=tax, discount_amount=disc)
    rate = _rowcol(row, 'qbo_tax_rate', None)
    return lines, totals, tax_rate_label(rate)


def parse_lines_from_form(form, catalog=None):
    """Read repeated line fields name_i, qty_i, cost_i, sell_i, optional_i, catalog_id_i."""
    lines = []
    # names come as line-name, or indexed
    names = form.getlist('line_name') if hasattr(form, 'getlist') else [form.get('line_name')]
    qtys = form.getlist('line_qty') if hasattr(form, 'getlist') else []
    costs = form.getlist('line_cost') if hasattr(form, 'getlist') else []
    sells = form.getlist('line_sell') if hasattr(form, 'getlist') else []
    opts = form.getlist('line_optional') if hasattr(form, 'getlist') else []
    cids = form.getlist('line_catalog_id') if hasattr(form, 'getlist') else []
    units = form.getlist('line_unit') if hasattr(form, 'getlist') else []
    descs = form.getlist('line_description') if hasattr(form, 'getlist') else []
    kinds = form.getlist('line_kind') if hasattr(form, 'getlist') else []
    amts = form.getlist('line_amount') if hasattr(form, 'getlist') else []
    taxes = form.getlist('line_taxable') if hasattr(form, 'getlist') else []
    opt_set = set(opts)
    for i, name in enumerate(names):
        name = (name or '').strip()
        desc = descs[i] if i < len(descs) else ''
        kind = (kinds[i] if i < len(kinds) else 'item') or 'item'
        if not name and not (desc or '').strip() and kind == 'item':
            continue
        qty = qtys[i] if i < len(qtys) else 1
        cost = costs[i] if i < len(costs) else 0
        sell = sells[i] if i < len(sells) else 0
        cid = cids[i] if i < len(cids) else ''
        unit = units[i] if i < len(units) else ''
        amount = amts[i] if i < len(amts) else ''
        taxable = (taxes[i] if i < len(taxes) else '') in ('1', 'on', 'true', 'True')
        optional = (str(i) in opt_set) or (form.get('line_optional_%d' % i) == '1')
        lines.append({
            'name': name, 'qty': qty, 'cost': cost, 'sell': sell,
            'optional': optional, 'catalog_id': int(cid) if str(cid).isdigit() else None,
            'unit': unit, 'description': desc, 'kind': kind,
            'amount': amount or None, 'taxable': taxable,
        })
    return lines


def register(fo):
    from flask import request, redirect, Response
    import urllib.parse

    app = fo.app
    h = fo.h

    def office_only():
        if not fo.me():
            return redirect('/login')
        if not fo.is_office():
            return redirect('/')
        return None

    def page(title, body, back=None, kicker='Quote', actions='', msg=''):
        from flask import session
        session['fo_area'] = 'quote'
        act = actions or ''
        if back:
            act = ('<a class="plat-btn plat-btn-ghost" href="%s">Back</a>' % h(back)) + act
            actions = act
        return fo.ops_page(title,
            '<div class="plat-head"><div class="plat-head-text">'
            + ('<div class="plat-kicker">%s</div>' % h(kicker) if kicker else '')
            + '<h1 class="plat-title">%s<span class="plat-dot">.</span></h1></div>' % h(title)
            + ('<div class="plat-actions">%s</div>' % actions if actions else '')
            + '</div>'
            + (('<div class="plat-msg">%s</div>' % h(msg)) if msg else '')
            + body, back=False)

    def quote_home_extra(conn):
        rows = conn.execute(
            '''SELECT q.*, c.name AS company FROM quotes q
               LEFT JOIN companies c ON c.id=q.company_id
               ORDER BY q.updated_at DESC LIMIT 80'''
        ).fetchall()
        body = '<div class="plat-panel-h" style="margin:0 0 10px"><h2 style="margin:0">Quotes</h2></div>'
        if not rows:
            body += '<div class="muted" style="margin-bottom:16px">No quotes yet. Start one from an opportunity, or add a catalog and write a quote here.</div>'
        else:
            body += '<div class="plat-cards">'
            for r in rows:
                lab = {'draft': 'Draft', 'sent': 'Link ready', 'accepted': 'Accepted'}.get(r['status'] or 'draft', r['status'])
                opened = ''
                if r['open_count']:
                    opened = ' · Opened %d time%s' % (r['open_count'], '' if r['open_count'] == 1 else 's')
                body += (
                    '<a class="plat-card" href="/quotes/%d"><b>%s</b> <span class="plat-pill">%s</span>'
                    '<div>Version %d</div><div class="muted">%s · %s%s</div></a>' % (
                        r['id'], h(r['title'] or 'Quote'), h(lab), r['version'] or 1,
                        h(r['company'] or ''), money(r['total'] or 0), opened))
            body += '</div>'
        return body

    fo.quote_home_extra = quote_home_extra

    @app.route('/catalog', methods=['GET', 'POST'])
    def catalog_page():
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        msg = ''
        if request.method == 'POST':
            action = request.form.get('action') or 'add'
            if action == 'add':
                name = (request.form.get('name') or '').strip()
                if not name:
                    msg = 'Name is required.'
                else:
                    try:
                        cost = float(request.form.get('cost') or 0)
                    except ValueError:
                        cost = 0
                    try:
                        sell = float(request.form.get('sell') or 0)
                    except ValueError:
                        sell = 0
                    if sell <= 0:
                        sell = sell_from_cost(cost)
                    when = now_iso()
                    conn.execute(
                        'INSERT INTO catalog_items (name,sku,unit,category,cost,sell,active,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?)',
                        (name[:200], (request.form.get('sku') or '').strip()[:40],
                         (request.form.get('unit') or '').strip()[:20],
                         (request.form.get('category') or '').strip()[:80], cost, sell, when, when))
                    conn.commit(); msg = 'Item added. Sell price is frozen on a quote when you add it there.'
            elif action == 'save' and request.form.get('id'):
                try:
                    cid = int(request.form.get('id'))
                    cost = float(request.form.get('cost') or 0)
                    sell = float(request.form.get('sell') or 0)
                except (TypeError, ValueError):
                    cid = 0; cost = sell = 0
                if sell <= 0:
                    sell = sell_from_cost(cost)
                if cid:
                    conn.execute(
                        'UPDATE catalog_items SET name=?,sku=?,unit=?,category=?,cost=?,sell=?,updated_at=? WHERE id=?',
                        ((request.form.get('name') or '').strip()[:200],
                         (request.form.get('sku') or '').strip()[:40],
                         (request.form.get('unit') or '').strip()[:20],
                         (request.form.get('category') or '').strip()[:80],
                         cost, sell, now_iso(), cid))
                    conn.commit(); msg = 'Saved.'
            elif action in ('off', 'on') and request.form.get('id'):
                try:
                    cid = int(request.form.get('id'))
                except ValueError:
                    cid = 0
                if cid:
                    conn.execute('UPDATE catalog_items SET active=?, updated_at=? WHERE id=?',
                                 (1 if action == 'on' else 0, now_iso(), cid))
                    conn.commit()
        q = (request.args.get('q') or '').strip()[:80]
        sql = 'SELECT * FROM catalog_items'
        args = []
        if q:
            sql += ' WHERE LOWER(name||" "||IFNULL(sku,"")||" "||IFNULL(category,"")) LIKE ?'
            args.append('%' + q.lower() + '%')
        sql += ' ORDER BY active DESC, name COLLATE NOCASE'
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        body = '<p class="muted">Cost and sell stay in the office. Default markup is 25 percent when sell is left blank. Prices are copied onto a quote and then frozen, so a later catalog change does not change a quote already written.</p>'
        body += (
            '<form class="plat-filter" method="get" action="/catalog">'
            '<input type="search" name="q" value="%s" placeholder="Search the catalog">'
            '<button class="plat-btn plat-btn-ghost" type="submit">Search</button></form>' % h(q))
        body += (
            '<div class="plat-panel"><h2>Add item</h2>'
            '<form class="plat-form cat-row" method="post">'
            '<input type="hidden" name="action" value="add">'
            '<label>Name<input name="name" required maxlength="200" placeholder="Item name"></label>'
            '<label>SKU<input name="sku" placeholder="SKU"></label>'
            '<label>Unit<input name="unit" placeholder="each"></label>'
            '<label>Category<input name="category" placeholder="Category"></label>'
            '<label>Cost $<input name="cost" type="number" step="0.01" min="0"></label>'
            '<label>Sell $<input name="sell" type="number" step="0.01" min="0" placeholder="cost + 25%"></label>'
            '<button class="plat-btn plat-btn-gold" type="submit">Add</button></form></div>')
        body += (
            '<div class="plat-panel"><h2>Import CSV</h2>'
            '<p class="muted">Header row required. Columns: name, cost, sell (optional), sku, unit, category. If sell is blank, it is set to cost plus 25 percent.</p>'
            '<form class="plat-form" method="post" action="/catalog/import" enctype="multipart/form-data">'
            '<input type="file" name="file" accept=".csv,text/csv" required>'
            '<button class="plat-btn plat-btn-ghost" type="submit">Import</button></form></div>')
        if rows:
            body += '<div class="plat-panel"><h2>Items</h2><div class="cat-list">'
            for r in rows:
                body += (
                    '<form class="plat-form cat-row" method="post">'
                    '<input type="hidden" name="action" value="save"><input type="hidden" name="id" value="%d">'
                    '<label>Name<input name="name" value="%s" required></label>'
                    '<label>SKU<input name="sku" value="%s"></label>'
                    '<label>Unit<input name="unit" value="%s"></label>'
                    '<label>Category<input name="category" value="%s"></label>'
                    '<label>Cost $<input name="cost" type="number" step="0.01" min="0" value="%s"></label>'
                    '<label>Sell $<input name="sell" type="number" step="0.01" min="0" value="%s"></label>'
                    '<span class="cat-btns"><button class="plat-btn plat-btn-ghost" type="submit">Save</button>' % (
                        r['id'], h(r['name']), h(r['sku'] or ''), h(r['unit'] or ''), h(r['category'] or ''),
                        (('%g' % r['cost']) if r['cost'] else '0'), (('%g' % r['sell']) if r['sell'] else '0')))
                if r['active']:
                    body += '<button class="plat-btn plat-btn-ghost" type="submit" name="action" value="off">Off</button>'
                else:
                    body += '<button class="plat-btn plat-btn-ghost" type="submit" name="action" value="on">On</button>'
                body += '</span></form>'
            body += '</div></div>'
        else:
            body += '<div class="muted">The catalog is empty.</div>'
        actions = '<a class="plat-btn plat-btn-gold" href="/quotes/new">+ Quote</a>'
        return page('Catalog', body, kicker='Quote', actions=actions, msg=msg)

    @app.route('/catalog/import', methods=['POST'])
    def catalog_import():
        gate = office_only()
        if gate:
            return gate
        f = request.files.get('file')
        if not f or not f.filename:
            return redirect('/catalog')
        raw = f.read()
        try:
            text = raw.decode('utf-8-sig')
        except Exception:
            text = raw.decode('latin-1', errors='replace')
        conn = fo.get_db()
        added, updated, errors = import_catalog_csv(conn, text, fo.me())
        conn.commit(); conn.close()
        # stash message via query is ugly; just land on catalog. Flash via session?
        from flask import session
        session['fo_msg'] = 'Imported %d new, %d updated.%s' % (
            added, updated, ((' ' + errors[0]) if errors else ''))
        return redirect('/catalog')

    def _editor(conn, vals, action, submit, companies, catalog, contacts=None, sites=None):
        lines = vals.get('lines') or []
        if not lines:
            lines = [{'name': '', 'qty': 1, 'cost': '', 'sell': '', 'optional': False}]
        co_opts = ''.join('<option value="%d"%s>%s</option>' % (
            c['id'], ' selected' if str(c['id']) == str(vals.get('company_id') or '') else '', h(c['name']))
            for c in companies)
        cat_opts = ''.join(
            '<option value="%d" data-sell="%s" data-cost="%s" data-unit="%s">%s%s</option>' % (
                c['id'], c['sell'] or 0, c['cost'] or 0, h(c['unit'] or ''),
                h(c['name']), (' · ' + money(c['sell'])) if c['sell'] else '')
            for c in catalog)
        qbo_doc = (vals.get('qbo_doc_number') or '').strip()
        qbo_banner = ''
        if qbo_doc:
            qbo_banner = ('<p class="muted">QuickBooks estimate %s. This note is office only '
                          'and is not shown on the client link.</p>' % h(qbo_doc))
        form = (
            '<form class="plat-form" method="post" action="%s" id="quote-form">'
            '<label>Title</label><input name="title" required maxlength="200" value="%s">'
            '<label>Company</label><select name="company_id" required><option value="">Pick a company</option>%s</select>'
            '<input type="hidden" name="opportunity_id" value="%s">'
            '<input type="hidden" name="job_id" value="%s">'
            '<input type="hidden" name="parent_id" value="%s">'
            '<label>Notes (shown on the client link)</label><textarea name="notes" rows="3">%s</textarea>%s'
            '<label>Clarifications (job-specific, shown above the terms)</label><textarea name="clarifications" rows="3" placeholder="Lift work billed extra. Fire alarm by others.">%s</textarea>'
            '<h2>Lines</h2><p class="muted">Sell prices are copied from the catalog and then frozen on this quote. Mark a line as an add-on if the client can tick it on or off. The one estimate still has a single total.</p>'
            '<div id="quote-lines">' % (
                h(action), h(vals.get('title') or ''), co_opts,
                h(str(vals.get('opportunity_id') or '')), h(str(vals.get('job_id') or '')),
                h(str(vals.get('parent_id') or '')), h(strip_import_scope_note(vals.get('notes') or '')),
                qbo_banner,
                h(vals.get('clarifications') or ''))
        )
        for i, ln in enumerate(lines):
            sell_val = ln.get('sell')
            sell_s = ('%.6g' % float(sell_val)) if sell_val not in (None, '') else ''
            amt_s = '' if ln.get('amount') in (None, '') else ('%g' % ln.get('amount'))
            form += (
                '<div class="plat-panel" style="margin:8px 0;padding:12px" data-line>'
                '<label>Item</label><input name="line_name" value="%s">'
                '<label>Description</label><textarea name="line_description" rows="3">%s</textarea>'
                '<div class="plat-row"><div><label>Qty</label><input name="line_qty" type="number" step="any" min="0" value="%s"></div>'
                '<div><label>Cost $</label><input name="line_cost" type="number" step="0.01" min="0" value="%s"></div>'
                '<div><label>Rate $</label><input name="line_sell" type="number" step="any" min="0" value="%s"></div></div>'
                '<input type="hidden" name="line_catalog_id" value="%s">'
                '<input type="hidden" name="line_unit" value="%s">'
                '<input type="hidden" name="line_kind" value="%s">'
                '<input type="hidden" name="line_amount" value="%s">'
                '<input type="hidden" name="line_taxable" value="%s">'
                '<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="line_optional" value="%d"%s style="width:auto"> Optional add-on the client can tick</label>'
                '</div>' % (
                    h(ln.get('name') or ''), h(ln.get('description') or ''),
                    ln.get('qty') if ln.get('qty') not in (None, '') else 1,
                    (('%g' % ln['cost']) if ln.get('cost') not in (None, '') else ''),
                    sell_s,
                    h(str(ln.get('catalog_id') or '')), h(ln.get('unit') or ''),
                    h(ln.get('kind') or 'item'), h(amt_s),
                    '1' if ln.get('taxable') else '',
                    i, ' checked' if ln.get('optional') else ''))
        form += (
            '</div>'
            '<div class="plat-row" style="align-items:flex-end"><div style="flex:2"><label>Add from catalog</label>'
            '<select id="cat-pick"><option value="">Pick an item</option>%s</select></div>'
            '<button class="plat-btn plat-btn-ghost" type="button" onclick="foAddCat()">Add item</button></div>'
            '<button class="plat-btn plat-btn-ghost" type="button" onclick="foAddBlank()">Add blank line</button> '
            '<button class="plat-btn plat-btn-gold" type="submit">%s</button></form>'
            '<script>function foAddBlank(){const box=document.getElementById("quote-lines");'
            'const n=box.querySelectorAll("[data-line]").length;'
            'const d=document.createElement("div");d.className="plat-panel";d.style.cssText="margin:8px 0;padding:12px";d.setAttribute("data-line","");'
            'd.innerHTML=\'<label>Item</label><input name="line_name">'
            + '<label>Description</label><textarea name="line_description" rows="3"></textarea>'
            + '<div class="plat-row"><div><label>Qty</label><input name="line_qty" type="number" step="any" min="0" value="1"></div>'
            + '<div><label>Cost $</label><input name="line_cost" type="number" step="0.01" min="0"></div>'
            + '<div><label>Rate $</label><input name="line_sell" type="number" step="any" min="0"></div></div>'
            + '<input type="hidden" name="line_catalog_id"><input type="hidden" name="line_unit">'
            + '<input type="hidden" name="line_kind" value="item"><input type="hidden" name="line_amount"><input type="hidden" name="line_taxable">'
            + '<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="line_optional" value="\'+n+\'" style="width:auto"> Optional add-on the client can tick</label>\';'
            'box.appendChild(d)}'
            'function foAddCat(){const s=document.getElementById("cat-pick");if(!s.value)return;'
            'const o=s.options[s.selectedIndex];const box=document.getElementById("quote-lines");'
            'const n=box.querySelectorAll("[data-line]").length;'
            'const d=document.createElement("div");d.className="plat-panel";d.style.cssText="margin:8px 0;padding:12px";d.setAttribute("data-line","");'
            'const name=o.textContent.split(" · ")[0];'
            'd.innerHTML=\'<label>Item</label><input name="line_name" value="\'+name.replace(/"/g,"&quot;")+\'">'
            + '<label>Description</label><textarea name="line_description" rows="3"></textarea>'
            + '<div class="plat-row"><div><label>Qty</label><input name="line_qty" type="number" step="any" min="0" value="1"></div>'
            + '<div><label>Cost $</label><input name="line_cost" type="number" step="0.01" min="0" value="\'+o.dataset.cost+\'"></div>'
            + '<div><label>Rate $</label><input name="line_sell" type="number" step="any" min="0" value="\'+o.dataset.sell+\'"></div></div>'
            + '<input type="hidden" name="line_catalog_id" value="\'+s.value+\'"><input type="hidden" name="line_unit" value="\'+(o.dataset.unit||"")+\'">'
            + '<input type="hidden" name="line_kind" value="item"><input type="hidden" name="line_amount"><input type="hidden" name="line_taxable">'
            + '<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="line_optional" value="\'+n+\'" style="width:auto"> Optional add-on the client can tick</label>\';'
            'box.appendChild(d);s.value=""}</script>'
        ) % (cat_opts, h(submit))
        return form

    # Fix checkbox: HTML forms only send checked boxes, so optional flags by index can drift.
    # Use a hidden 0 plus checkbox named line_optional_flag as parallel list... 
    # I'll parse: for each line_name, check if form.getlist('line_optional') contains that index.
    # When adding via JS we set value to current index; when saving we also send line_opt_flags as 0/1 list.
    # Safer parse in POST: I'll add hidden line_flag=0 and checkbox line_flag=1 with same name - last wins? No, getlist would have both.
    # We'll use optional as a parallel getlist of hidden "0" and only checked add "1" - doesn't work.
    # Best: parse in route with request.form getlist and a second getlist of line_is_optional all hidden default 0, JS sets? 
    # I'll change checkboxes to name="line_optional_i" after all. parse_lines_from_form already checks line_optional_%d.
    # Update the form to use line_optional_0 names... JS must increment. I'll fix parse to also treat getlist values that equal index.

    @app.route('/quotes/new', methods=['GET', 'POST'])
    def quote_new():
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        opp_id = request.values.get('opportunity_id') or ''
        try:
            opp_id_i = int(opp_id) if opp_id else 0
        except ValueError:
            opp_id_i = 0
        opp = conn.execute('SELECT * FROM opportunities WHERE id=?', (opp_id_i,)).fetchone() if opp_id_i else None
        msg = ''
        if request.method == 'POST':
            data = {
                'title': request.form.get('title'),
                'notes': request.form.get('notes'),
                'clarifications': request.form.get('clarifications'),
                'company_id': int(request.form.get('company_id') or 0) or None,
                'opportunity_id': int(request.form.get('opportunity_id') or 0) or None,
                'job_id': (request.form.get('job_id') or '').strip() or None,
                'parent_id': int(request.form.get('parent_id') or 0) or None,
                'lines': parse_lines_from_form(request.form),
            }
            row, err = save_quote(conn, data, created_by=fo.me())
            if err:
                msg = err
            else:
                if data['opportunity_id']:
                    conn.execute(
                        "UPDATE opportunities SET stage=CASE WHEN stage IN ('won','lost','proposal') THEN stage ELSE 'estimating' END, updated_at=? WHERE id=?",
                        (now_iso(), data['opportunity_id']))
                conn.commit(); qid = row['id']; conn.close()
                return redirect('/quotes/%d' % qid)
        companies = conn.execute('SELECT id,name FROM companies WHERE IFNULL(archived,0)=0 ORDER BY name').fetchall()
        catalog = conn.execute('SELECT * FROM catalog_items WHERE active=1 ORDER BY name COLLATE NOCASE').fetchall()
        vals = {
            'title': (request.form.get('title') or (opp['title'] if opp else '')),
            'notes': request.form.get('notes') or '',
            'company_id': request.form.get('company_id') or (opp['company_id'] if opp else ''),
            'opportunity_id': request.form.get('opportunity_id') or (opp['id'] if opp else ''),
            'job_id': request.form.get('job_id') or (opp['job_id'] if opp else ''),
            'parent_id': request.form.get('parent_id') or '',
            'lines': parse_lines_from_form(request.form) if request.method == 'POST' else [],
        }
        form = _editor(conn, vals, '/quotes/new', 'Save quote', companies, catalog)
        conn.close()
        back = '/opportunities/%d' % opp_id_i if opp_id_i else '/quote'
        return page('New quote', form, back=back, kicker='Quote', msg=msg)

    @app.route('/quotes/<int:qid>', methods=['GET', 'POST'])
    def quote_edit(qid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
        if not row:
            conn.close(); return redirect('/quote')
        msg = ''
        if request.method == 'POST' and request.form.get('action') == 'save':
            data = {
                'title': request.form.get('title'),
                'notes': request.form.get('notes'),
                'clarifications': request.form.get('clarifications'),
                'company_id': int(request.form.get('company_id') or 0) or None,
                'opportunity_id': int(request.form.get('opportunity_id') or 0) or None,
                'job_id': (request.form.get('job_id') or '').strip() or None,
                'lines': parse_lines_from_form(request.form),
            }
            row, err = save_quote(conn, data, quote_id=qid, created_by=fo.me())
            if err:
                msg = err
            else:
                conn.commit(); msg = 'Saved. Prices on this quote stay as you saved them.'
                row = conn.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
        elif request.method == 'POST' and request.form.get('action') == 'link':
            row = ensure_token(conn, qid)
            conn.commit(); msg = 'Client link is ready. Copy it and send it yourself. The app does not send anything.'
        elif request.method == 'POST' and request.form.get('action') == 'revoke_link':
            conn.execute(
                'UPDATE quotes SET token_revoked_at=?, token_revoked_by=?, updated_at=? WHERE id=? AND IFNULL(token,"")!=""',
                (now_iso(), fo.me(), now_iso(), qid))
            conn.commit()
            msg = 'Client link was turned off. The old link will no longer open.'
            row = conn.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
        companies = conn.execute('SELECT id,name FROM companies WHERE IFNULL(archived,0)=0 ORDER BY name').fetchall()
        catalog = conn.execute('SELECT * FROM catalog_items WHERE active=1 ORDER BY name COLLATE NOCASE').fetchall()
        versions = []
        if row['opportunity_id']:
            versions = conn.execute(
                'SELECT id,version,status,total,updated_at FROM quotes WHERE opportunity_id=? ORDER BY version',
                (row['opportunity_id'],)).fetchall()
        vals = {
            'title': row['title'], 'notes': row['notes'] or '', 'clarifications': clarifications_of(row),
            'company_id': row['company_id'] or '', 'opportunity_id': row['opportunity_id'] or '',
            'job_id': row['job_id'] or '', 'lines': lines_of(row),
            'qbo_doc_number': (row['qbo_doc_number'] or '') if 'qbo_doc_number' in row.keys() else '',
        }
        body = ''
        if row['status'] == 'accepted':
            body += '<div class="plat-panel"><p><b>Accepted</b> by %s on %s. Total %s.</p>' % (
                h(row['accepted_name'] or 'client'), h((row['accepted_at'] or '')[:19].replace('T', ' ')), money(row['total']))
            if row['job_id']:
                body += '<p>Job: <a href="/?job=%s">%s</a></p>' % (urllib.parse.quote(row['job_id']), h(row['job_id']))
            body += '<p class="muted">Start a new version if you need to change the price or scope.</p></div>'
        else:
            body += _editor(conn, vals, '/quotes/%d' % qid, 'Save quote', companies, catalog)
            # The editor form needs action=save. I'll add a hidden via wrapping... patch: editor doesn't include action.
            body = body.replace('<form class="plat-form" method="post" action="/quotes/%d" id="quote-form">' % qid,
                                '<form class="plat-form" method="post" action="/quotes/%d" id="quote-form"><input type="hidden" name="action" value="save">' % qid)
        # Link + tracking
        body += '<div class="plat-panel"><h2>Client link</h2>'
        if row['token']:
            url = request.host_url.rstrip('/') + '/q/' + row['token']
            body += '<p>Copy this link and send it yourself. The app never emails or texts the client.</p>'
            body += '<input readonly value="%s" onclick="this.select()">' % h(url)
            if row['open_count']:
                body += '<p>Opened %d time%s. First opened %s.</p>' % (
                    row['open_count'], '' if row['open_count'] == 1 else 's',
                    h((row['opened_at'] or '')[:19].replace('T', ' ')))
            else:
                body += '<p class="muted">Not opened yet.</p>'
            revoked = ''
            exp = ''
            try:
                revoked = (row['token_revoked_at'] or '') if 'token_revoked_at' in row.keys() else ''
                exp = (row['token_expires_at'] or '') if 'token_expires_at' in row.keys() else ''
            except Exception:
                pass
            if revoked:
                body += '<p class="muted">This link was turned off on %s.</p>' % h(revoked[:19].replace('T', ' '))
            elif exp:
                body += '<p class="muted">Link expires %s.</p>' % h(exp[:10])
            if row['token'] and not revoked:
                body += ('<form method="post" style="display:inline"><input type="hidden" name="action" value="revoke_link">'
                         '<button class="plat-btn plat-btn-ghost" type="submit">Turn off link</button></form>')
        if row['status'] != 'accepted':
            body += ('<form method="post"><input type="hidden" name="action" value="link">'
                     '<button class="plat-btn plat-btn-gold" type="submit">%s</button></form>' % (
                         'Make client link' if not row['token'] else 'Refresh link status')) + (' <a class="plat-btn plat-btn-ghost" href="/quotes/%d/print" target="_blank">Print</a>' % qid)
        body += '</div>'
        body += '<div class="plat-panel"><h2>Versions</h2>'
        if versions:
            body += '<ul class="plat-list">' + ''.join(
                '<li><a href="/quotes/%d"><b>Version %d</b></a> <span class="plat-pill">%s</span> %s</li>' % (
                    v['id'], v['version'] or 1, h(v['status'] or 'draft'), money(v['total'] or 0))
                for v in versions) + '</ul>'
        else:
            body += '<p class="muted">This is version %d.</p>' % (row['version'] or 1)
        if row['status'] == 'accepted' or True:
            body += '<a class="plat-btn plat-btn-ghost" href="/quotes/%d/version">New version</a>' % qid
        body += '</div>'
        conn.close()
        back = '/opportunities/%d' % row['opportunity_id'] if row['opportunity_id'] else '/quote'
        return page(row['title'] or 'Quote', body, back=back, kicker='Version %d' % (row['version'] or 1), msg=msg)

    @app.route('/quotes/<int:qid>/version')
    def quote_new_version(qid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
        if not row:
            conn.close(); return redirect('/quote')
        data = {
            'title': row['title'], 'notes': row['notes'], 'clarifications': clarifications_of(row),
            'company_id': row['company_id'], 'opportunity_id': row['opportunity_id'],
            'job_id': row['job_id'], 'site_id': row['site_id'], 'contact_id': row['contact_id'],
            'parent_id': row['id'], 'lines': lines_of(row),
        }
        neu, err = save_quote(conn, data, created_by=fo.me())
        if err:
            conn.close(); return redirect('/quotes/%d' % qid)
        conn.commit(); nid = neu['id']; conn.close()
        return redirect('/quotes/%d' % nid)

    def quote_home_patch():
        pass

    # Client-facing one estimate
    CLIENT_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>True Sense Security: {{ q.title }}</title>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;overflow-wrap:anywhere;background:#f7f6ef;color:#1d1f14;margin:0;padding:0}
.wrap{max-width:880px;margin:0 auto;padding:24px 20px 48px}
@media(min-width:800px){.wrap{padding:36px 28px 64px}}
.hdr{display:grid;grid-template-columns:auto 1fr auto;gap:16px 20px;align-items:center;border-bottom:2px solid #a49f2a;padding-bottom:16px;margin-bottom:20px}
.hdr img{height:56px}
@media(max-width:700px){
  .hdr{grid-template-columns:1fr;justify-items:center;text-align:center}
  .meta{text-align:center}
  .hdr img{margin:0 auto}
}
.brand{font-weight:800;font-size:1.15rem;letter-spacing:-.02em}
.muted{color:#6b6d60}
.meta{text-align:right;font-size:.95rem;line-height:1.45}
h1{font-size:1.7rem;margin:8px 0 4px;letter-spacing:-.03em}
h2{font-size:1.05rem;color:#5f5b16;margin:22px 0 8px;border-bottom:2px solid #a49f2a;padding-bottom:4px}
.card{background:#fff;border:1px solid #ecece6;border-radius:14px;padding:16px 18px;margin:10px 0}
.pre{white-space:pre-wrap}
.btn{width:100%;background:linear-gradient(145deg,#baa205,#7a7a12);color:#fff;border:0;padding:18px;border-radius:12px;font-size:1.15rem;font-weight:700;cursor:pointer}
.ok{background:#e8f5e9;border:2px solid #2e7d32;border-radius:12px;padding:14px;font-weight:700;font-size:1.1rem;color:#1b5e20}
table{width:100%;border-collapse:collapse;table-layout:fixed}
td,th{border-bottom:1px solid #eee;padding:10px 8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th.qty,td.qty{width:10%;text-align:center}
th.rate,td.rate,th.amt,td.amt{width:16%;text-align:right;white-space:nowrap}
.desc{white-space:pre-wrap;font-size:.95rem;margin:4px 0 0;color:#333;line-height:1.45}
tr.kind-text td{border-bottom:1px solid #eee}
tr.kind-subtotal td,tr.kind-tax td,tr.kind-discount td{font-weight:700;background:#fafaf6}
tr.kind-tax td{background:#f3f0e0}
.breakdown{margin:10px 0 0;display:grid;grid-template-columns:1fr auto;gap:4px 16px;justify-items:end}
.breakdown span{justify-self:start}
input[type=text]{width:100%;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:8px 0}
.addon-lab{display:flex;align-items:flex-start;gap:8px;margin:0}
.addon-lab input{width:22px;height:22px;margin:0;flex:0 0 22px}
.addon-tag{flex:0 0 auto;white-space:nowrap;font-style:normal;font-size:.75rem;background:#f3f0e0;color:#6b6a16;border-radius:6px;padding:1px 6px}
.totals{margin-top:12px}
.total{font-size:1.7rem;font-weight:800}
.deposit{font-size:1.05rem;margin-top:6px}
.deposit b{font-size:1.2rem}
details.terms{background:#fff;border:1px solid #ecece6;border-radius:14px;padding:0;margin:10px 0}
details.terms summary{cursor:pointer;font-weight:700;padding:14px 18px;list-style:none}
details.terms summary::-webkit-details-marker{display:none}
details.terms summary:after{content:"Show";float:right;color:#6b6d60;font-weight:600;font-size:.9rem}
details.terms[open] summary:after{content:"Hide"}
.terms-body{padding:0 18px 16px;white-space:pre-wrap;line-height:1.5;font-size:.98rem}
.check{display:flex;gap:10px;align-items:flex-start;margin:12px 0;font-size:1.05rem}
.check input{width:28px;height:28px;margin:0;flex:0 0 28px}
.foot{margin-top:28px;text-align:center;color:#6b6d60;font-size:.9rem}
@media print{
  body{background:#fff}
  .noprint{display:none !important}
  .wrap{max-width:none;padding:0}
  details.terms{border:0}
  details.terms summary:after{display:none}
}
</style></head><body>
<div class="wrap">
<header class="hdr">
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security">
<div><div class="brand">True Sense Security</div><div class="muted">License {{ license }}</div></div>
<div class="meta">
  <div><b>{{ number }}</b>{% if q.version %} &middot; Version {{ q.version }}{% endif %}</div>
  <div>Date {{ est_date }}</div>
  {% if valid_until %}<div>Valid until {{ valid_until }}</div>{% endif %}
  <div class="noprint"><a href="/q/{{ q.token }}/print">Print</a></div>
</div>
</header>
<h1>{{ q.title }}</h1>
{% if company %}<div>Prepared for <b>{{ company }}</b></div>{% endif %}
{% if bill_to or ship_to %}
<div class="card" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
  {% if bill_to %}<div><div class="muted">Bill to</div><div class="pre">{{ bill_to }}</div></div>{% endif %}
  {% if ship_to %}<div><div class="muted">Ship to / job site</div><div class="pre">{{ ship_to }}</div></div>{% endif %}
</div>
{% endif %}
{% if customer_memo %}<h2>Message</h2><div class="card pre">{{ customer_memo }}</div>{% endif %}
{% if scope %}<h2>Scope</h2><div class="card pre">{{ scope }}</div>{% endif %}
{% if accepted %}
<div class="ok">Approved by {{ q.accepted_name }} on {{ q.accepted_at[:10] }}. Total {{ money(q.total) }}.</div>
{% endif %}
<h2>Estimate</h2>
<div class="card">
<table>
<tr><th>Item</th><th class="qty">Qty</th><th class="rate">Rate</th><th class="amt">Amount</th></tr>
{% for ln in lines %}
<tr class="kind-{{ ln.kind or 'item' }}">
<td>{% if ln.optional and not accepted and ln.kind == 'item' %}<label class="addon-lab"><input type="checkbox" name="addon" value="{{ ln.idx }}" form="est-form" {% if ln.idx in selected %}checked{% endif %} onchange="foTot()"> <span>{{ ln.name }}</span> <em class="addon-tag">Add-on</em></label>{% else %}{{ ln.name }}{% if ln.optional %} <em class="addon-tag">Add-on</em>{% endif %}{% endif %}{% if ln.description and ln.description != ln.name %}<div class="desc">{{ ln.description }}</div>{% endif %}</td>
<td class="qty">{{ ln.qty_label }}</td>
<td class="rate">{% if ln.show_rate %}{{ money(ln.rate) }}{% endif %}</td>
<td class="amt" data-amt="{{ ln.amt_value }}" data-kind="{{ ln.kind }}">{{ ln.amount_label }}</td>
</tr>
{% endfor %}
</table>
<div class="totals" data-tax="{{ totals.tax }}" data-discount="{{ totals.discount }}" data-deposit-min="{{ deposit_min }}">
  <div class="breakdown">
    <span>Subtotal</span><b id="sub">{{ money(totals.subtotal) }}</b>
    {% if totals.discount %}<span>Discount</span><b id="disc">{{ money(totals.discount) }}</b>{% endif %}
    {% if totals.tax or tax_rate %}<span>Sales tax{% if tax_rate %} ({{ tax_rate }}){% endif %}</span><b id="tax">{{ money(totals.tax) }}</b>{% endif %}
  </div>
  <div class="total" id="tot">{{ money(totals.total) }}</div>
  {% if deposit %}<div class="deposit">Deposit due to schedule (50%): <b id="dep">{{ money(deposit) }}</b></div>{% endif %}
  <div class="muted" id="tot-note">This is the total for the items above{% if has_addons and not accepted %}, including any optional add-ons you tick{% endif %}. The balance is due upon completion, net 15.</div>
</div>
</div>
{% if clarifications %}
<h2>Clarifications</h2>
<div class="card pre">{{ clarifications }}</div>
{% endif %}
{% if terms %}
<h2>Terms and conditions</h2>
<details class="terms" open>
<summary>Terms and conditions</summary>
<div class="terms-body">{{ terms }}</div>
</details>
{% endif %}
{% if not accepted %}
<form method="post" action="/q/{{ q.token }}/accept" id="est-form" class="noprint" onsubmit="return estSubmit()">
<input type="hidden" name="signature" id="est-sig">
<h2>Sign to approve</h2>
<input type="text" name="name" placeholder="Your full name" required maxlength="100">
<div class="muted">Sign with your finger below</div>
<canvas id="est-pad" width="600" height="200" style="width:100%;height:160px;border:2px solid #ccc;border-radius:10px;background:#fff;touch-action:none"></canvas>
<button type="button" class="noprint" style="background:#eee;color:#111;border:0;border-radius:10px;padding:12px;width:100%;font-size:1rem;margin-top:8px" onclick="estClear()">Clear signature</button>
{% if terms %}<label class="check"><input type="checkbox" name="agree_terms" value="1" required> I agree to the terms and conditions.</label>{% endif %}
<label class="check"><input type="checkbox" name="agree" value="1" required> I approve this estimate and price.</label>
<button class="btn" type="submit">Approve and Sign</button>
</form>
<script>
var estDrawn=false;
(function(){var c=document.getElementById("est-pad"),x=c.getContext("2d"),on=false;x.lineWidth=4;x.lineCap="round";x.strokeStyle="#111";
function p(e){var r=c.getBoundingClientRect();return [(e.clientX-r.left)*c.width/r.width,(e.clientY-r.top)*c.height/r.height]}
c.addEventListener("pointerdown",function(e){on=true;var q=p(e);x.beginPath();x.moveTo(q[0],q[1]);c.setPointerCapture(e.pointerId)});
c.addEventListener("pointermove",function(e){if(!on)return;var q=p(e);x.lineTo(q[0],q[1]);x.stroke();estDrawn=true});
c.addEventListener("pointerup",function(){on=false})})();
function estClear(){var c=document.getElementById("est-pad");c.getContext("2d").clearRect(0,0,c.width,c.height);estDrawn=false}
function estSubmit(){var terms=document.querySelector("input[name=agree_terms]");if(terms&&!terms.checked){alert("Please agree to the terms and conditions");return false}if(!estDrawn){alert("Please sign in the box");return false}document.getElementById("est-sig").value=document.getElementById("est-pad").toDataURL("image/png");return true}
function foTot(){var t=0;document.querySelectorAll("tr").forEach(function(tr){var box=tr.querySelector("input[name=addon]");var cell=tr.querySelector("[data-amt]");if(!cell)return;var kind=cell.getAttribute("data-kind")||"item";if(kind!=="item")return;if(box&&!box.checked)return;t+=parseFloat(cell.getAttribute("data-amt")||0)});var box=document.querySelector(".totals");var tax=parseFloat((box&&box.getAttribute("data-tax"))||0);var disc=parseFloat((box&&box.getAttribute("data-discount"))||0);var min=parseFloat((box&&box.getAttribute("data-deposit-min"))||0);var sub=document.getElementById("sub");if(sub)sub.textContent="$"+t.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});t=t-disc+tax;document.getElementById("tot").textContent="$"+t.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});var row=document.querySelector(".deposit");var dep=document.getElementById("dep");if(row&&dep){if(t>=min&&t>0){row.style.display="";dep.textContent="$"+(t*0.5).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}else{row.style.display="none"}}}
</script>
{% endif %}
<div class="foot">True Sense Security &middot; License {{ license }}</div>
</div>
</body></html>
'''

    def _quote_link_gone(row):
        try:
            import fieldops_security as _sec
            if _sec.token_block_reason(row):
                return (_sec.LINK_GONE, 410)
        except Exception:
            pass
        return None

    @app.route('/q/<token>')
    def quote_client(token):
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM quotes WHERE token=?', (token,)).fetchone()
        if not row:
            conn.close()
            return ('This link is not valid.', 404)
        gone = _quote_link_gone(row)
        if gone:
            conn.close(); return gone
        record_open(conn, row['id'])
        row = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
        company = ''
        if row['company_id']:
            co = conn.execute('SELECT name FROM companies WHERE id=?', (row['company_id'],)).fetchone()
            company = co['name'] if co else ''
        if terms_of(row):
            terms = terms_of(row)
        else:
            terms = get_default_terms(conn)
        conn.close()
        selected = set()
        if row['status'] == 'accepted':
            selected = set(range(len(lines_of(row))))
        lines, totals, rate_label = decorate_client_lines(row, selected if row['status'] == 'accepted' else set())
        from flask import render_template_string
        clar = clarifications_of(row)
        est = estimate_date(row)
        until = valid_until(row)
        dep_min = get_deposit_min()
        dep = deposit_of(totals['total'], min_amount=dep_min)
        return render_template_string(
            CLIENT_HTML, q=row, company=company, lines=lines, money=money,
            totals=totals, accepted=row['status'] == 'accepted',
            selected=selected, has_addons=any(ln.get('optional') for ln in lines),
            terms=terms, clarifications=clar, est_date=est, valid_until=until,
            deposit=dep, deposit_min=dep_min, scope=client_scope_of(row),
            number=quote_number(row), license=LICENSE_NO,
            bill_to=bill_to_of(row), ship_to=ship_to_of(row),
            customer_memo=customer_memo_of(row), tax_rate=rate_label)

    @app.route('/q/<token>/accept', methods=['POST'])
    def quote_client_accept(token):
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM quotes WHERE token=?', (token,)).fetchone()
        if not row:
            conn.close(); return ('This link is not valid.', 404)
        gone = _quote_link_gone(row)
        if gone:
            conn.close(); return gone
        if row['status'] == 'accepted':
            conn.close(); return redirect('/q/' + token)
        name = (request.form.get('name') or '').strip()[:100]
        sig = request.form.get('signature') or ''
        if not name or not sig.startswith('data:image/png;base64,'):
            conn.close(); return redirect('/q/' + token)
        if (terms_of(row) or get_default_terms(conn)) and request.form.get('agree_terms') != '1':
            conn.close(); return redirect('/q/' + token)
        selected = []
        for v in request.form.getlist('addon'):
            try:
                selected.append(int(v))
            except ValueError:
                pass
        ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
        accept_quote(conn, row, selected, name, sig, ip, fo=fo)
        conn.commit(); conn.close()
        return redirect('/q/' + token)

    def _render_quote_client(row, company, print_mode=False):
        from flask import render_template_string, Response
        selected = set(range(len(lines_of(row)))) if row['status'] == 'accepted' else set()
        lines, totals, rate_label = decorate_client_lines(row, selected if row['status'] == 'accepted' else set())
        terms = terms_of(row) or ''
        if not terms:
            conn = fo.get_db(); terms = get_default_terms(conn); conn.close()
        dep_min = get_deposit_min()
        html = render_template_string(
            CLIENT_HTML, q=row, company=company, lines=lines, money=money,
            totals=totals, accepted=row['status'] == 'accepted',
            selected=selected, has_addons=any(ln.get('optional') for ln in lines),
            terms=terms, clarifications=clarifications_of(row),
            est_date=estimate_date(row), valid_until=valid_until(row),
            deposit=deposit_of(totals['total'], min_amount=dep_min), deposit_min=dep_min,
            scope=client_scope_of(row), number=quote_number(row),
            license=LICENSE_NO,
            bill_to=bill_to_of(row), ship_to=ship_to_of(row),
            customer_memo=customer_memo_of(row), tax_rate=rate_label)
        if print_mode:
            html = html.replace('</style>', '@media screen{.print-hint{display:block;text-align:center;margin:8px 0 16px}}@media print{.print-hint{display:none}}</style>')
            html = html.replace('<div class="wrap">', '<div class="wrap"><p class="print-hint noprint"><button class="btn" type="button" onclick="window.print()" style="max-width:280px;margin:0 auto;display:block">Print this estimate</button></p>')
        return html

    @app.route('/q/<token>/print')
    def quote_client_print(token):
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM quotes WHERE token=?', (token,)).fetchone()
        if not row:
            conn.close(); return ('This link is not valid.', 404)
        gone = _quote_link_gone(row)
        if gone:
            conn.close(); return gone
        company = ''
        if row['company_id']:
            co = conn.execute('SELECT name FROM companies WHERE id=?', (row['company_id'],)).fetchone()
            company = co['name'] if co else ''
        if not terms_of(row):
            # do not mutate; print the current default for older quotes
            pass
        terms_fallback = get_default_terms(conn)
        conn.close()
        # _render will refetch default if needed
        return _render_quote_client(row, company, print_mode=True)

    @app.route('/quotes/<int:qid>/print')
    def quote_office_print(qid):
        if not fo.is_office():
            return redirect('/')
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM quotes WHERE id=?', (qid,)).fetchone()
        if not row:
            conn.close(); return redirect('/quote')
        company = ''
        if row['company_id']:
            co = conn.execute('SELECT name FROM companies WHERE id=?', (row['company_id'],)).fetchone()
            company = co['name'] if co else ''
        conn.close()
        return _render_quote_client(row, company, print_mode=True)

    # Enhance /quote home footer and prepend quotes. We wrap after platform registered.
    # Patch the existing muted sentence by providing extra + actions via a before-render hook already set.

    @app.before_request
    def _quote_area():
        pass

    # Monkeypatch platform quote_home output: we add a second route? Instead hook ops_page is hard.
    # We'll replace quote_home view function.
    def quote_home_rich():
        gate = office_only()
        if gate:
            return gate
        from flask import session
        session['fo_area'] = 'quote'
        conn = fo.get_db()
        extra = quote_home_extra(conn)
        # Keep the existing job estimates list by calling original logic inline
        rows = conn.execute(
            "SELECT id,title,customer,address,estimate_json,sale_amount,status,job_type FROM jobs WHERE IFNULL(job_type,'')!='Office Task' ORDER BY created_at DESC"
        ).fetchall()
        items = []
        for r in rows:
            try:
                est = fo.load_estimate(r)
            except Exception:
                continue
            if not est.get('ready') and not (r['sale_amount'] or 0):
                continue
            items.append((r, est, fo.est_stage(est) if hasattr(fo, 'est_stage') else 'draft'))
        conn.close()
        body = extra
        body += '<div class="plat-panel-h" style="margin:18px 0 10px"><h2 style="margin:0">Job estimates</h2></div>'
        if items:
            body += '<div class="plat-cards">'
            for r, est, st in items[:80]:
                lab = {'approved': 'Approved', 'lost': 'Lost', 'sent': 'Ready', 'draft': 'Draft'}.get(st, st or 'Draft')
                price = money(est.get('price') or r['sale_amount'] or 0)
                body += (
                    '<a class="plat-card" href="/job/%s/estimate"><b>%s</b> <span class="plat-pill">%s</span>'
                    '<div>%s</div><div class="muted">%s · %s</div></a>' % (
                        urllib.parse.quote(r['id']), h(r['id']), h(lab),
                        h(r['title'] or r['customer'] or ''), h(r['customer'] or ''), price))
            body += '</div>'
        else:
            body += '<div class="muted">No job estimates yet. New quotes appear in the list above.</div>'
        body += '<p class="muted">Catalog prices stay in the office. A quote freezes sell prices when you save it. The client sees one estimate, can tick optional add-ons, and signs on the link. The app does not send the link.</p>'
        actions = ('<a class="plat-btn plat-btn-ghost" href="/catalog">Catalog</a>'
                   '<a class="plat-btn plat-btn-gold" href="/quotes/new">+ Quote</a>')
        return page('Estimates', body, kicker='Quote', actions=actions)

    # Replace the platform /quote view
    app.view_functions['quote_home'] = quote_home_rich

    def quote_settings_html():
        if not fo.is_office():
            return ''
        conn = fo.get_db()
        text = get_saved_terms(conn)
        min_amt = get_deposit_min(conn)
        conn.close()
        live = bool(text)
        body = (
            '<form method="post" class="plat-form" style="margin:18px 0">'
            '<input type="hidden" name="action" value="save_quote_terms">'
            '<h2>Estimate terms and conditions</h2>'
        )
        if live:
            body += '<p class="muted">This text is copied onto a quote when you make the client link, so later edits here do not change a link that is already out. Nothing is sent.</p>'
            body += '<textarea name="quote_terms" rows="22" style="width:100%">' + fo.h(text) + '</textarea>'
            body += '<button class="plat-btn plat-btn-gold" type="submit" style="margin-top:10px">Save terms</button></form>'
        else:
            body += ('<p class="muted">No live default is set yet. Save the approved terms to use them on new client links. Nothing is sent.</p>')
            body += '<textarea name="quote_terms" rows="22" style="width:100%">' + fo.h(DEFAULT_TERMS) + '</textarea>'
            body += '<button class="plat-btn plat-btn-gold" type="submit" style="margin-top:10px">Save as live default</button></form>'
        shown = fo.h('%g' % min_amt)
        body += (
            '<form method="post" class="plat-form" style="margin:28px 0">'
            '<input type="hidden" name="action" value="save_quote_deposit_min">'
            '<h2>Deposit on estimates</h2>'
            '<p class="muted">A 50% deposit is shown on the client estimate and PDF when the total is '
            'at least this amount. Estimates under this amount do not show a deposit line. '
            'The default is $1,000, which covers small service quotes without asking for a deposit to schedule.</p>'
            '<label>Ask for a 50% deposit when the total is at least ($)</label>'
            '<input name="quote_deposit_min" type="number" step="0.01" min="0" value="' + shown + '" style="max-width:200px">'
            '<button class="plat-btn plat-btn-gold" type="submit" style="margin-top:10px">Save deposit rule</button></form>'
        )
        return body

    def quote_settings_post():
        action = request.form.get('action')
        if action == 'save_quote_deposit_min':
            conn = fo.get_db()
            err = set_deposit_min(conn, request.form.get('quote_deposit_min'))
            if err:
                conn.close(); return err
            conn.commit(); conn.close()
            return 'Deposit rule saved. It applies to the client estimate and PDF.'
        if action != 'save_quote_terms':
            return None
        conn = fo.get_db()
        err = set_default_terms(conn, request.form.get('quote_terms'))
        if err:
            conn.close(); return err
        conn.commit(); conn.close()
        return 'Terms saved. New client links will use this text. Already-sent quotes stay as they were.'

    fo.quote_settings_html = quote_settings_html
    fo.quote_settings_post = quote_settings_post
