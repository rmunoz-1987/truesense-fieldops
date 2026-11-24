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
import secrets
from datetime import datetime

DEFAULT_MARKUP = 1.25


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
    conn.execute('CREATE INDEX IF NOT EXISTS ix_catalog_name ON catalog_items(name)')


def lines_of(row):
    try:
        data = json.loads(row['lines_json'] or '[]')
    except Exception:
        data = []
    return data if isinstance(data, list) else []


def freeze_line(name, qty, cost, sell, optional=False, catalog_id=None, unit=''):
    try:
        qty = float(qty or 0)
    except (TypeError, ValueError):
        qty = 0
    try:
        cost = float(cost or 0)
    except (TypeError, ValueError):
        cost = 0
    try:
        sell = float(sell or 0)
    except (TypeError, ValueError):
        sell = 0
    if sell <= 0 and cost > 0:
        sell = sell_from_cost(cost)
    qty = max(0, qty)
    return {
        'name': (name or '')[:200],
        'qty': qty,
        'unit': (unit or '')[:20],
        'cost': round(cost, 2),
        'sell': round(sell, 2),
        'optional': bool(optional),
        'catalog_id': catalog_id,
        'line_total': round(qty * sell, 2),
    }


def quote_totals(lines, selected=None):
    """selected is a set of optional line indexes the client ticked. None means all required plus no add-ons."""
    base = 0
    addons = 0
    for i, ln in enumerate(lines):
        amt = round(float(ln.get('qty') or 0) * float(ln.get('sell') or 0), 2)
        if ln.get('optional'):
            if selected is not None and i in selected:
                addons += amt
        else:
            base += amt
    return {'base': round(base, 2), 'addons': round(addons, 2), 'total': round(base + addons, 2)}


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
        name = (ln.get('name') or '').strip()
        if not name:
            continue
        frozen.append(freeze_line(
            name, ln.get('qty') or 1, ln.get('cost') or 0, ln.get('sell') or 0,
            optional=bool(ln.get('optional')), catalog_id=ln.get('catalog_id'), unit=ln.get('unit') or ''))
    if not frozen:
        return None, 'Add at least one line.'
    tot = quote_totals(frozen)['base']  # stored total is the required base; add-ons apply at accept
    when = now_iso()
    if quote_id:
        row = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
        if not row:
            return None, 'Quote not found.'
        if row['status'] == 'accepted':
            return None, 'This quote is accepted. Start a new version to change it.'
        conn.execute(
            'UPDATE quotes SET title=?,notes=?,lines_json=?,total=?,updated_at=?,company_id=?,site_id=?,contact_id=?,opportunity_id=?,job_id=? WHERE id=?',
            (title, (data.get('notes') or '')[:2000], json.dumps(frozen), tot, when,
             data.get('company_id'), data.get('site_id'), data.get('contact_id'),
             data.get('opportunity_id'), data.get('job_id') or row['job_id'], quote_id))
        return conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone(), None
    ver = next_version(conn, data.get('opportunity_id'), data.get('parent_id'))
    cur = conn.execute(
        '''INSERT INTO quotes (opportunity_id,job_id,company_id,site_id,contact_id,title,version,parent_id,
           status,notes,lines_json,total,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,'draft',?,?,?,?,?,?)''',
        (data.get('opportunity_id'), data.get('job_id'), data.get('company_id'), data.get('site_id'),
         data.get('contact_id'), title, ver, data.get('parent_id'),
         (data.get('notes') or '')[:2000], json.dumps(frozen), tot, created_by, when, when))
    return conn.execute('SELECT * FROM quotes WHERE id=?', (cur.lastrowid,)).fetchone(), None


def ensure_token(conn, quote_id):
    row = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
    if not row:
        return None
    if row['token']:
        return row
    token = secrets.token_urlsafe(24)
    when = now_iso()
    conn.execute(
        "UPDATE quotes SET token=?, status=CASE WHEN status='draft' THEN 'sent' ELSE status END, updated_at=? WHERE id=?",
        (token, when, quote_id))
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
           lines_json=?, total=?, updated_at=? WHERE id=?''',
        (when, (name or '')[:100], signature or '', (ip or '')[:80], json.dumps(kept), totals['total'], when, quote['id']))
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
    opt_set = set(opts)
    for i, name in enumerate(names):
        name = (name or '').strip()
        if not name:
            continue
        qty = qtys[i] if i < len(qtys) else 1
        cost = costs[i] if i < len(costs) else 0
        sell = sells[i] if i < len(sells) else 0
        cid = cids[i] if i < len(cids) else ''
        unit = units[i] if i < len(units) else ''
        optional = (str(i) in opt_set) or (form.get('line_optional_%d' % i) == '1')
        # checkbox arrays often send 'on' for that index via hidden flags
        lines.append({
            'name': name, 'qty': qty, 'cost': cost, 'sell': sell,
            'optional': optional, 'catalog_id': int(cid) if str(cid).isdigit() else None,
            'unit': unit,
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
            '<form class="plat-form" method="post">'
            '<input type="hidden" name="action" value="add">'
            '<label>Name</label><input name="name" required maxlength="200">'
            '<div class="plat-row"><div><label>SKU</label><input name="sku"></div>'
            '<div><label>Unit</label><input name="unit" placeholder="each"></div>'
            '<div><label>Category</label><input name="category"></div></div>'
            '<div class="plat-row"><div><label>Cost $</label><input name="cost" type="number" step="0.01" min="0" value=""></div>'
            '<div><label>Sell $</label><input name="sell" type="number" step="0.01" min="0" placeholder="cost + 25%"></div></div>'
            '<button class="plat-btn plat-btn-gold" type="submit">Add to catalog</button></form></div>')
        body += (
            '<div class="plat-panel"><h2>Import CSV</h2>'
            '<p class="muted">Header row required. Columns: name, cost, sell (optional), sku, unit, category. If sell is blank, it is set to cost plus 25 percent.</p>'
            '<form class="plat-form" method="post" action="/catalog/import" enctype="multipart/form-data">'
            '<input type="file" name="file" accept=".csv,text/csv" required>'
            '<button class="plat-btn plat-btn-ghost" type="submit">Import</button></form></div>')
        if rows:
            body += '<div class="plat-panel"><h2>Items</h2>'
            for r in rows:
                body += (
                    '<form class="plat-form" method="post" style="border-bottom:1px solid var(--line);padding:10px 0">'
                    '<input type="hidden" name="action" value="save"><input type="hidden" name="id" value="%d">'
                    '<div class="plat-row"><div style="flex:2"><label>Name</label><input name="name" value="%s" required></div>'
                    '<div><label>SKU</label><input name="sku" value="%s"></div>'
                    '<div><label>Unit</label><input name="unit" value="%s"></div></div>'
                    '<div class="plat-row"><div><label>Category</label><input name="category" value="%s"></div>'
                    '<div><label>Cost $</label><input name="cost" type="number" step="0.01" min="0" value="%s"></div>'
                    '<div><label>Sell $</label><input name="sell" type="number" step="0.01" min="0" value="%s"></div></div>'
                    '<button class="plat-btn plat-btn-ghost" type="submit">Save</button> ' % (
                        r['id'], h(r['name']), h(r['sku'] or ''), h(r['unit'] or ''), h(r['category'] or ''),
                        (('%g' % r['cost']) if r['cost'] else '0'), (('%g' % r['sell']) if r['sell'] else '0')))
                if r['active']:
                    body += ('<button class="plat-btn plat-btn-ghost" type="submit" name="action" value="off">Deactivate</button>'
                             if True else '')
                else:
                    body += '<span class="muted">Inactive</span> <button class="plat-btn plat-btn-ghost" type="submit" name="action" value="on">Reactivate</button>'
                body += '</form>'
            body += '</div>'
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
        form = (
            '<form class="plat-form" method="post" action="%s" id="quote-form">'
            '<label>Title</label><input name="title" required maxlength="200" value="%s">'
            '<label>Company</label><select name="company_id" required><option value="">Pick a company</option>%s</select>'
            '<input type="hidden" name="opportunity_id" value="%s">'
            '<input type="hidden" name="job_id" value="%s">'
            '<input type="hidden" name="parent_id" value="%s">'
            '<label>Notes (shown on the client link)</label><textarea name="notes" rows="3">%s</textarea>'
            '<h2>Lines</h2><p class="muted">Sell prices are copied from the catalog and then frozen on this quote. Mark a line as an add-on if the client can tick it on or off. The one estimate still has a single total.</p>'
            '<div id="quote-lines">' % (
                h(action), h(vals.get('title') or ''), co_opts,
                h(str(vals.get('opportunity_id') or '')), h(str(vals.get('job_id') or '')),
                h(str(vals.get('parent_id') or '')), h(vals.get('notes') or ''))
        )
        for i, ln in enumerate(lines):
            form += (
                '<div class="plat-panel" style="margin:8px 0;padding:12px" data-line>'
                '<label>Item</label><input name="line_name" value="%s" required>'
                '<div class="plat-row"><div><label>Qty</label><input name="line_qty" type="number" step="any" min="0" value="%s"></div>'
                '<div><label>Cost $</label><input name="line_cost" type="number" step="0.01" min="0" value="%s"></div>'
                '<div><label>Sell $</label><input name="line_sell" type="number" step="0.01" min="0" value="%s"></div></div>'
                '<input type="hidden" name="line_catalog_id" value="%s">'
                '<input type="hidden" name="line_unit" value="%s">'
                '<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="line_optional" value="%d"%s style="width:auto"> Optional add-on the client can tick</label>'
                '</div>' % (
                    h(ln.get('name') or ''), ln.get('qty') if ln.get('qty') not in (None, '') else 1,
                    (('%g' % ln['cost']) if ln.get('cost') not in (None, '') else ''),
                    (('%g' % ln['sell']) if ln.get('sell') not in (None, '') else ''),
                    h(str(ln.get('catalog_id') or '')), h(ln.get('unit') or ''),
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
            'd.innerHTML=\'<label>Item</label><input name="line_name" required>'
            + '<div class="plat-row"><div><label>Qty</label><input name="line_qty" type="number" step="any" min="0" value="1"></div>'
            + '<div><label>Cost $</label><input name="line_cost" type="number" step="0.01" min="0"></div>'
            + '<div><label>Sell $</label><input name="line_sell" type="number" step="0.01" min="0"></div></div>'
            + '<input type="hidden" name="line_catalog_id"><input type="hidden" name="line_unit">'
            + '<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="line_optional" value="\'+n+\'" style="width:auto"> Optional add-on the client can tick</label>\';'
            'box.appendChild(d)}'
            'function foAddCat(){const s=document.getElementById("cat-pick");if(!s.value)return;'
            'const o=s.options[s.selectedIndex];const box=document.getElementById("quote-lines");'
            'const n=box.querySelectorAll("[data-line]").length;'
            'const d=document.createElement("div");d.className="plat-panel";d.style.cssText="margin:8px 0;padding:12px";d.setAttribute("data-line","");'
            'const name=o.textContent.split(" · ")[0];'
            'd.innerHTML=\'<label>Item</label><input name="line_name" required value="\'+name.replace(/"/g,"&quot;")+\'">'
            + '<div class="plat-row"><div><label>Qty</label><input name="line_qty" type="number" step="any" min="0" value="1"></div>'
            + '<div><label>Cost $</label><input name="line_cost" type="number" step="0.01" min="0" value="\'+o.dataset.cost+\'"></div>'
            + '<div><label>Sell $</label><input name="line_sell" type="number" step="0.01" min="0" value="\'+o.dataset.sell+\'"></div></div>'
            + '<input type="hidden" name="line_catalog_id" value="\'+s.value+\'"><input type="hidden" name="line_unit" value="\'+(o.dataset.unit||"")+\'">'
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
        companies = conn.execute('SELECT id,name FROM companies WHERE IFNULL(archived,0)=0 ORDER BY name').fetchall()
        catalog = conn.execute('SELECT * FROM catalog_items WHERE active=1 ORDER BY name COLLATE NOCASE').fetchall()
        versions = []
        if row['opportunity_id']:
            versions = conn.execute(
                'SELECT id,version,status,total,updated_at FROM quotes WHERE opportunity_id=? ORDER BY version',
                (row['opportunity_id'],)).fetchall()
        vals = {
            'title': row['title'], 'notes': row['notes'] or '',
            'company_id': row['company_id'] or '', 'opportunity_id': row['opportunity_id'] or '',
            'job_id': row['job_id'] or '', 'lines': lines_of(row),
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
        if row['status'] != 'accepted':
            body += ('<form method="post"><input type="hidden" name="action" value="link">'
                     '<button class="plat-btn plat-btn-gold" type="submit">%s</button></form>' % (
                         'Make client link' if not row['token'] else 'Refresh link status'))
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
            'title': row['title'], 'notes': row['notes'],
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
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:640px;margin:auto;padding:14px}
h1{font-size:1.4rem;margin:10px 0 4px}h2{font-size:1.1rem;color:#a49f2a;margin:20px 0 6px;border-bottom:2px solid #a49f2a}
.muted{color:#666}.card{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:14px;margin:10px 0}
.btn{width:100%;background:#2e7d32;color:#fff;border:0;padding:18px;border-radius:10px;font-size:1.2rem;font-weight:700}
.ok{background:#e8f5e9;border:2px solid #2e7d32;border-radius:12px;padding:14px;font-weight:700;font-size:1.1rem;color:#1b5e20}
table{width:100%;border-collapse:collapse;table-layout:fixed}td,th{border-bottom:1px solid #eee;padding:8px 6px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th:nth-child(2),td:nth-child(2){width:18%;text-align:center}th:nth-child(3),td:nth-child(3){width:28%;text-align:right;white-space:nowrap}
input[type=text]{width:100%;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:8px 0}
.total{font-size:1.6rem;font-weight:800;margin-top:8px}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;display:block;margin:0 auto">
<h1>{{ q.title }}</h1>
<div>{{ company }}</div>
{% if q.notes %}<div class="card" style="white-space:pre-wrap">{{ q.notes }}</div>{% endif %}
{% if accepted %}
<div class="ok">Approved by {{ q.accepted_name }} on {{ q.accepted_at[:10] }}. Total {{ money(q.total) }}.</div>
{% else %}
<form method="post" action="/q/{{ q.token }}/accept" onsubmit="return estSubmit()">
<input type="hidden" name="signature" id="est-sig">
<h2>Estimate</h2>
<div class="card">
<table>
<tr><th>Item</th><th>Qty</th><th>Price</th></tr>
{% for ln in lines %}
<tr>
<td>{% if ln.optional %}<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="addon" value="{{ ln.idx }}" {% if ln.idx in selected %}checked{% endif %} onchange="foTot()" style="width:22px;height:22px;margin:0"> {{ ln.name }} <span class="muted">(optional)</span></label>{% else %}{{ ln.name }}{% endif %}</td>
<td>{{ ln.qty_label }}</td>
<td data-amt="{{ ln.line_total }}">{{ money(ln.line_total) }}</td>
</tr>
{% endfor %}
</table>
<div class="total" id="tot">{{ money(totals.total) }}</div>
<div class="muted" id="tot-note">This is the total for the items above{% if has_addons %}, including any optional add-ons you tick{% endif %}.</div>
</div>
<h2>Sign to approve</h2>
<input type="text" name="name" placeholder="Your full name" required maxlength="100">
<div class="muted">Sign with your finger below</div>
<canvas id="est-pad" width="600" height="200" style="width:100%;height:160px;border:2px solid #ccc;border-radius:10px;background:#fff;touch-action:none"></canvas>
<button type="button" style="background:#eee;color:#111;border:0;border-radius:10px;padding:12px;width:100%;font-size:1rem" onclick="estClear()">Clear signature</button>
<label style="display:flex;gap:10px;align-items:center;margin:12px 0;font-size:1.05rem"><input type="checkbox" name="agree" value="1" required style="width:28px;height:28px;margin:0"> I approve this estimate and price.</label>
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
function estSubmit(){if(!estDrawn){alert("Please sign in the box");return false}document.getElementById("est-sig").value=document.getElementById("est-pad").toDataURL("image/png");return true}
function foTot(){var t=0;document.querySelectorAll("tr").forEach(function(tr){var box=tr.querySelector("input[name=addon]");var cell=tr.querySelector("[data-amt]");if(!cell)return;if(box&&!box.checked)return;t+=parseFloat(cell.getAttribute("data-amt")||0)});document.getElementById("tot").textContent="$"+t.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}
</script>
{% endif %}
<div class="muted" style="margin-top:24px;text-align:center">True Sense Security</div></body></html>'''

    @app.route('/q/<token>')
    def quote_client(token):
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM quotes WHERE token=?', (token,)).fetchone()
        if not row:
            conn.close()
            return ('This link is not valid.', 404)
        record_open(conn, row['id'])
        row = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
        company = ''
        if row['company_id']:
            co = conn.execute('SELECT name FROM companies WHERE id=?', (row['company_id'],)).fetchone()
            company = co['name'] if co else ''
        conn.close()
        lines = []
        for i, ln in enumerate(lines_of(row)):
            item = dict(ln); item['idx'] = i
            qv = item.get('qty') or 0
            item['qty_label'] = ('%g' % qv)
            lines.append(item)
        # After accept, all remaining lines are in; selected = all
        selected = set()
        if row['status'] == 'accepted':
            selected = set(range(len(lines)))
        totals = quote_totals(lines, selected if row['status'] == 'accepted' else set())
        from flask import render_template_string
        return render_template_string(
            CLIENT_HTML, q=row, company=company, lines=lines, money=money,
            totals=totals, accepted=row['status'] == 'accepted',
            selected=selected, has_addons=any(ln.get('optional') for ln in lines))

    @app.route('/q/<token>/accept', methods=['POST'])
    def quote_client_accept(token):
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM quotes WHERE token=?', (token,)).fetchone()
        if not row:
            conn.close(); return ('This link is not valid.', 404)
        if row['status'] == 'accepted':
            conn.close(); return redirect('/q/' + token)
        name = (request.form.get('name') or '').strip()[:100]
        sig = request.form.get('signature') or ''
        if not name or not sig.startswith('data:image/png;base64,'):
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
