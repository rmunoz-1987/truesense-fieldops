"""Second operating company (Datacom) beside True Sense.

company_id already means a customer, so operating companies live in `orgs`
and `user_orgs`. A central guard sets the current org. Scoped fetch helpers
404 on the wrong org, and list helpers always filter by the current org.
Catalog, stock, and sub compliance stay shared. QBO books never mix.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime

from flask import g, request, session, redirect, jsonify, has_request_context

ORG_TRUE_SENSE = 1
ORG_DATACOM = 2

TRUE_SENSE = {
    'id': ORG_TRUE_SENSE,
    'slug': 'true-sense',
    'short_name': 'True Sense',
    'name': 'True Sense Security',
    'legal_name': 'True Sense Security LLC',
    'license': 'B31163401',
    'phone': '713-364-8409',
    'email': 'info@truesensesecurity.com',
    'website': 'truesensesecurity.com',
    'address': '7341 Avenue E, Houston, TX 77011',
    'contact_line': '7341 Avenue E, Houston, TX 77011 · info@truesensesecurity.com · 713-364-8409',
    'logo_url': '/static/logo.png',
    'logo_pdf': 'logo-pdf.png',
    'job_prefix': 'TS',
    'qbo_sync': True,
}

# Pulled from https://www.datacomsecurity.com/ (footer legal line, license, contact).
DATACOM = {
    'id': ORG_DATACOM,
    'slug': 'datacom',
    'short_name': 'Datacom',
    'name': 'Datacom Security',
    'legal_name': 'DC & S Solutions Inc. dba Datacom Security Solutions',
    'license': 'B19441',
    'phone': '832-851-3004',
    'email': 'info@datacomsecurity.com',
    'website': 'datacomsecurity.com',
    'address': '10592 Fuqua St, #215 Houston, TX 77089',
    'contact_line': '10592 Fuqua St, #215 Houston, TX 77089 · info@datacomsecurity.com · 832-851-3004',
    'logo_url': '/static/datacom-logo.png',
    'logo_pdf': 'datacom-logo-pdf.png',
    'job_prefix': 'DC',
    'qbo_sync': False,
}

ORG_SEED = (TRUE_SENSE, DATACOM)
ORG_BY_ID = {o['id']: o for o in ORG_SEED}

# Tables that belong to one operating company. Catalog, stock, and sub
# compliance docs are shared and are not listed here.
SCOPED_TABLES = (
    'jobs', 'companies', 'opportunities', 'quotes', 'equipment',
    'service_plans', 'service_tickets', 'invoice_drafts', 'qbo_queue',
    'qbo_monthly', 'qbo_drafts', 'agreements', 'change_orders',
    'clients', 'share_links', 'material_requests',
)

NOT_FOUND_HTML = (
    '<!DOCTYPE html><html><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>Not found</title>'
    '<style>body{font-family:-apple-system,Arial,sans-serif;max-width:520px;'
    'margin:48px auto;padding:20px;color:#111;background:#f7f6ef}'
    'h1{font-size:1.6rem;margin:0 0 12px}p{line-height:1.45}'
    'a{color:#6b6a16;font-weight:700}</style></head>'
    '<body><h1>Not found</h1>'
    '<p>That record is not in this company.</p>'
    '<p><a href="/">Back</a></p></body></html>'
)

ENV_ORG_MAP = {
    'roland': (ORG_TRUE_SENSE, ORG_DATACOM),
    'tony la': (ORG_TRUE_SENSE,),
    'woody rodriguez': (ORG_TRUE_SENSE,),
}


def _now():
    return datetime.utcnow().isoformat()


def nkey(s):
    return ' '.join((s or '').split()).lower()


def _cols(conn, table):
    try:
        return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}
    except sqlite3.Error:
        return set()


def _add_col(conn, table, coldef):
    name = coldef.split()[0]
    if name in _cols(conn, table):
        return
    if table not in _tables(conn):
        return
    conn.execute('ALTER TABLE %s ADD COLUMN %s' % (table, coldef))


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def branding(org_id=None):
    if org_id is None:
        org_id = current_org_id()
    data = ORG_BY_ID.get(int(org_id or 0)) or TRUE_SENSE
    out = dict(data)
    root = os.path.dirname(os.path.abspath(__file__))
    out['logo_pdf_path'] = os.path.join(root, 'static', out['logo_pdf'])
    return out


def job_prefix(org_id=None):
    return branding(org_id)['job_prefix']


def next_job_id(conn, year=None, org_id=None):
    org_id = int(org_id or current_org_id())
    if year is None:
        year = datetime.utcnow().year
        try:
            import fieldops_app as fo
            year = fo.today_local().year
        except Exception:
            pass
    pre = '%s-%d-' % (job_prefix(org_id), int(year))
    top = 0
    sql = 'SELECT id FROM jobs WHERE id LIKE ?'
    args = [pre + '%']
    if 'org_id' in _cols(conn, 'jobs'):
        sql += ' AND org_id=?'
        args.append(org_id)
    for r in conn.execute(sql, args):
        try:
            top = max(top, int(str(r['id'])[len(pre):]))
        except (TypeError, ValueError):
            pass
    return pre + '%04d' % (top + 1)


def request_org_id(default=ORG_TRUE_SENSE):
    """For key-auth QBO APIs. Defaults to True Sense so the existing connector never sees Datacom books."""
    raw = None
    if _request_ready():
        raw = request.args.get('org_id')
        if raw is None and request.form:
            raw = request.form.get('org_id')
        if raw is None:
            try:
                js = request.get_json(silent=True) or {}
            except Exception:
                js = {}
            raw = js.get('org_id')
    try:
        oid = int(raw) if raw is not None and raw != '' else int(default)
    except (TypeError, ValueError):
        oid = int(default)
    if oid not in ORG_BY_ID:
        oid = int(default)
    return oid


def org_row(org_id):
    return ORG_BY_ID.get(int(org_id or 0))


def init_schema(conn):
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS orgs (
            id INTEGER PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            short_name TEXT,
            name TEXT NOT NULL,
            legal_name TEXT,
            license TEXT,
            phone TEXT,
            email TEXT,
            website TEXT,
            address TEXT,
            contact_line TEXT,
            logo_url TEXT,
            logo_pdf TEXT,
            job_prefix TEXT,
            qbo_sync INTEGER DEFAULT 0,
            created_at TEXT
        )''')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS user_orgs (
            name_key TEXT NOT NULL,
            org_id INTEGER NOT NULL,
            PRIMARY KEY (name_key, org_id)
        )''')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS org_settings (
            org_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (org_id, key)
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_user_orgs_org ON user_orgs(org_id)')


def seed_orgs(conn):
    when = _now()
    for o in ORG_SEED:
        conn.execute(
            '''INSERT OR IGNORE INTO orgs (
                id,slug,short_name,name,legal_name,license,phone,email,website,
                address,contact_line,logo_url,logo_pdf,job_prefix,qbo_sync,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (o['id'], o['slug'], o['short_name'], o['name'], o['legal_name'], o['license'],
             o['phone'], o['email'], o['website'], o['address'], o['contact_line'],
             o['logo_url'], o['logo_pdf'], o['job_prefix'], 1 if o['qbo_sync'] else 0, when))
        # Keep public branding in sync with the seeded constants (site-sourced).
        conn.execute(
            '''UPDATE orgs SET slug=?,short_name=?,name=?,legal_name=?,license=?,phone=?,
               email=?,website=?,address=?,contact_line=?,logo_url=?,logo_pdf=?,
               job_prefix=?,qbo_sync=? WHERE id=?''',
            (o['slug'], o['short_name'], o['name'], o['legal_name'], o['license'], o['phone'],
             o['email'], o['website'], o['address'], o['contact_line'], o['logo_url'],
             o['logo_pdf'], o['job_prefix'], 1 if o['qbo_sync'] else 0, o['id']))


def _rebuild_qbo_monthly(conn):
    cols = _cols(conn, 'qbo_monthly')
    if not cols:
        conn.execute(
            '''CREATE TABLE qbo_monthly (
                org_id INTEGER NOT NULL DEFAULT 1, month TEXT, invoiced REAL, paid REAL,
                synced_at TEXT, source TEXT, PRIMARY KEY (org_id, month))''')
        return
    if 'org_id' in cols:
        # Already rebuilt or column added. Ensure unique key.
        if 'ix_qbo_monthly_org_month' not in _indexes(conn):
            try:
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_qbo_monthly_org_month ON qbo_monthly(org_id, month)')
            except sqlite3.Error:
                pass
        return
    conn.execute(
        '''CREATE TABLE qbo_monthly_org (
            org_id INTEGER NOT NULL DEFAULT 1, month TEXT, invoiced REAL, paid REAL,
            synced_at TEXT, source TEXT, PRIMARY KEY (org_id, month))''')
    conn.execute(
        'INSERT INTO qbo_monthly_org (org_id,month,invoiced,paid,synced_at,source) '
        'SELECT 1,month,invoiced,paid,synced_at,source FROM qbo_monthly')
    conn.execute('DROP TABLE qbo_monthly')
    conn.execute('ALTER TABLE qbo_monthly_org RENAME TO qbo_monthly')


def _fix_unique_keys(conn):
    idx = _indexes(conn)
    if 'ix_companies_name_key' in idx:
        conn.execute('DROP INDEX IF EXISTS ix_companies_name_key')
    if 'companies' in _tables(conn):
        conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS ix_companies_org_key ON companies(org_id, name_key)')
    if 'ix_quotes_qbo_estimate' in idx:
        conn.execute('DROP INDEX IF EXISTS ix_quotes_qbo_estimate')
    if 'quotes' in _tables(conn):
        conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS ix_quotes_qbo_estimate '
            'ON quotes(org_id, qbo_estimate_id) WHERE IFNULL(qbo_estimate_id,"") != ""')
    if 'ix_sites_addr' in idx and 'sites' in _tables(conn):
        # sites stay unique per customer (company_id is the customer).
        pass


def _assign_existing_rows(conn):
    for table in SCOPED_TABLES:
        if table not in _tables(conn):
            continue
        if 'org_id' not in _cols(conn, table):
            continue
        conn.execute('UPDATE %s SET org_id=1 WHERE org_id IS NULL OR org_id=0' % table)


def _seed_env_user_orgs(conn):
    for key, orgs in ENV_ORG_MAP.items():
        # Do not wipe later DB-user rows that share a name; env names are reserved.
        conn.execute('DELETE FROM user_orgs WHERE name_key=?', (key,))
        for oid in orgs:
            conn.execute('INSERT OR IGNORE INTO user_orgs (name_key, org_id) VALUES (?,?)', (key, oid))
    # Existing database users with no assignment get True Sense only.
    try:
        for r in conn.execute('SELECT name_key FROM app_users'):
            has = conn.execute('SELECT 1 FROM user_orgs WHERE name_key=?', (r['name_key'],)).fetchone()
            if not has:
                conn.execute('INSERT OR IGNORE INTO user_orgs (name_key, org_id) VALUES (?,?)',
                             (r['name_key'], ORG_TRUE_SENSE))
    except sqlite3.Error:
        pass


def terms_for_org(org_id, source_terms):
    """Same official terms, with the company name and license swapped for Datacom."""
    text = source_terms or ''
    if int(org_id or 0) != ORG_DATACOM:
        return text
    repls = [
        ('TRUE SENSE SECURITY', 'DATACOM SECURITY'),
        ('True Sense Security LLC', DATACOM['legal_name']),
        ('True Sense Security\u2019s', 'Datacom Security\u2019s'),
        ("True Sense Security's", "Datacom Security's"),
        ('True Sense Security', 'Datacom Security'),
        ('B31163401', DATACOM['license']),
        ('7341 Avenue E, Houston, TX 77011', DATACOM['address']),
        ('truesensesecurity.com', DATACOM['website']),
        ('713-364-8409', DATACOM['phone']),
        ('info@truesensesecurity.com', DATACOM['email']),
    ]
    for a, b in repls:
        text = text.replace(a, b)
    return text


def agreement_for_org(org_id, source_body):
    return terms_for_org(org_id, source_body)


def _seed_datacom_terms_and_agreement(conn):
    try:
        import fieldops_quote as quote
        src = quote.DEFAULT_TERMS or quote.get_saved_terms(conn) or ''
    except Exception:
        src = ''
    if src:
        existing = conn.execute(
            'SELECT value FROM org_settings WHERE org_id=? AND key=?',
            (ORG_DATACOM, 'quote_terms')).fetchone()
        if not existing or not (existing['value'] or '').strip():
            conn.execute(
                'INSERT OR REPLACE INTO org_settings (org_id,key,value) VALUES (?,?,?)',
                (ORG_DATACOM, 'quote_terms', terms_for_org(ORG_DATACOM, src)))
    # Datacom sub agreement copy: same text, Datacom name and license.
    if 'agreements' in _tables(conn) and 'org_id' in _cols(conn, 'agreements'):
        has = conn.execute(
            'SELECT id FROM agreements WHERE org_id=?', (ORG_DATACOM,)).fetchone()
        if not has:
            src_ag = conn.execute(
                'SELECT body,version FROM agreements WHERE org_id=1 OR org_id IS NULL ORDER BY id DESC LIMIT 1'
            ).fetchone()
            if src_ag and src_ag['body']:
                body = agreement_for_org(ORG_DATACOM, src_ag['body'])
                conn.execute(
                    'INSERT INTO agreements (version,body,created_at,created_by,org_id) VALUES (?,?,?,?,?)',
                    (src_ag['version'] or '1.2', body, _now(), 'Seed Datacom', ORG_DATACOM))


def backup_before_orgs(backup_dir, db_path):
    """Write one pre-org migration copy. Safe to call again; will not overwrite."""
    if not backup_dir or not db_path or not os.path.exists(db_path):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, 'fieldops-pre-orgs.db')
    if os.path.exists(dst):
        return dst
    tmp = dst + '.tmp'
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(tmp)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    os.replace(tmp, dst)
    return dst


def migrate(conn, backup_dir=None, db_path=None):
    if backup_dir and db_path:
        try:
            path = backup_before_orgs(backup_dir, db_path)
            print('ORG_MIGRATE backup=%s' % ('yes' if path and os.path.exists(path) else 'no'), flush=True)
        except Exception:
            print('ORG_MIGRATE backup=fail', flush=True)
    init_schema(conn)
    seed_orgs(conn)
    for table in SCOPED_TABLES:
        _add_col(conn, table, 'org_id INTEGER NOT NULL DEFAULT 1')
    _rebuild_qbo_monthly(conn)
    _assign_existing_rows(conn)
    _fix_unique_keys(conn)
    _seed_env_user_orgs(conn)
    _seed_datacom_terms_and_agreement(conn)
    # Datacom supplier account settings stay blank until Roland fills them in.
    return True


def org_setting(conn, key, org_id=None, default=''):
    org_id = int(org_id or current_org_id() or 1)
    # True Sense keeps using the existing settings table as the source of truth.
    if org_id == ORG_TRUE_SENSE:
        try:
            r = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            if r and r['value'] is not None:
                return r['value']
        except sqlite3.Error:
            pass
    row = conn.execute(
        'SELECT value FROM org_settings WHERE org_id=? AND key=?', (org_id, key)).fetchone()
    if row and row['value'] is not None:
        return row['value']
    return default


def set_org_setting(conn, key, value, org_id=None):
    org_id = int(org_id or current_org_id() or 1)
    conn.execute(
        'INSERT OR REPLACE INTO org_settings (org_id,key,value) VALUES (?,?,?)',
        (org_id, key, value))
    if org_id == ORG_TRUE_SENSE:
        try:
            conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (key, value))
        except sqlite3.Error:
            pass


def orgs_for_key(conn, name_key):
    key = nkey(name_key)
    if key in ENV_ORG_MAP:
        return list(ENV_ORG_MAP[key])
    rows = list(conn.execute(
        'SELECT org_id FROM user_orgs WHERE name_key=? ORDER BY org_id', (key,)))
    if rows:
        return [int(r['org_id']) for r in rows]
    return [ORG_TRUE_SENSE]


def orgs_for_user(conn, name):
    return orgs_for_key(conn, nkey(name))


def set_user_orgs(conn, name_key, org_ids):
    key = nkey(name_key)
    if key in ENV_ORG_MAP:
        return 'Render logins keep the assignment set in the app.'
    ids = []
    for x in org_ids or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i in ORG_BY_ID and i not in ids:
            ids.append(i)
    if not ids:
        return 'Pick at least one company.'
    conn.execute('DELETE FROM user_orgs WHERE name_key=?', (key,))
    for i in ids:
        conn.execute('INSERT OR IGNORE INTO user_orgs (name_key, org_id) VALUES (?,?)', (key, i))
    return None


def user_can_org(conn, name, org_id):
    try:
        org_id = int(org_id)
    except (TypeError, ValueError):
        return False
    return org_id in orgs_for_user(conn, name)


def _request_ready():
    try:
        return has_request_context()
    except Exception:
        return False


def current_org_id(default=ORG_TRUE_SENSE):
    if _request_ready():
        try:
            oid = getattr(g, 'org_id', None)
            if oid:
                return int(oid)
        except Exception:
            pass
        try:
            raw = session.get('org_id')
            if raw:
                return int(raw)
        except Exception:
            pass
    return int(default or ORG_TRUE_SENSE)


def set_current_org(org_id):
    org_id = int(org_id)
    if _request_ready():
        session['org_id'] = org_id
        try:
            g.org_id = org_id
        except Exception:
            pass
    return org_id


def current_branding():
    return branding(current_org_id())


def qbo_sync_enabled(org_id=None):
    return bool(branding(org_id)['qbo_sync'])


def org_clause(alias=None):
    col = ('%s.org_id' % alias) if alias else 'org_id'
    return '(%s=?)' % col, [current_org_id()]


def _has_org(conn, table):
    return 'org_id' in _cols(conn, table)


def get_by_id(conn, table, key, value, org_id=None):
    """Fetch one row. Returns None when missing or in the wrong org."""
    org_id = int(org_id or current_org_id())
    if table not in _tables(conn):
        return None
    if _has_org(conn, table):
        return conn.execute(
            'SELECT * FROM %s WHERE %s=? AND org_id=?' % (table, key), (value, org_id)).fetchone()
    return conn.execute('SELECT * FROM %s WHERE %s=?' % (table, key), (value,)).fetchone()


def list_rows(conn, table, where='', args=(), order='', alias=None, org_id=None):
    """List rows in the current org. `where` should not include org_id."""
    org_id = int(org_id or current_org_id())
    if table not in _tables(conn):
        return []
    col = ('%s.org_id' % alias) if alias else 'org_id'
    sql = 'SELECT * FROM %s' % table
    parts = []
    params = []
    if _has_org(conn, table):
        parts.append('%s=?' % col)
        params.append(org_id)
    if where:
        parts.append('(%s)' % where)
        params.extend(list(args or ()))
    if parts:
        sql += ' WHERE ' + ' AND '.join(parts)
    if order:
        sql += ' ' + order
    return list(conn.execute(sql, params))


def get_job(conn, job_id, org_id=None):
    return get_by_id(conn, 'jobs', 'id', job_id, org_id=org_id)


def list_jobs(conn, where='', args=(), order='ORDER BY created_at DESC', org_id=None):
    return list_rows(conn, 'jobs', where=where, args=args, order=order, org_id=org_id)


def get_company(conn, company_id, org_id=None):
    return get_by_id(conn, 'companies', 'id', company_id, org_id=org_id)


def list_companies(conn, where='', args=(), order='ORDER BY name', org_id=None):
    return list_rows(conn, 'companies', where=where, args=args, order=order, org_id=org_id)


def get_quote(conn, quote_id, org_id=None):
    return get_by_id(conn, 'quotes', 'id', quote_id, org_id=org_id)


def list_quotes(conn, where='', args=(), order='ORDER BY id DESC', org_id=None):
    return list_rows(conn, 'quotes', where=where, args=args, order=order, org_id=org_id)


def get_opportunity(conn, oid, org_id=None):
    return get_by_id(conn, 'opportunities', 'id', oid, org_id=org_id)


def list_opportunities(conn, where='', args=(), order='ORDER BY id DESC', org_id=None):
    return list_rows(conn, 'opportunities', where=where, args=args, order=order, org_id=org_id)


def get_equipment(conn, eid, org_id=None):
    return get_by_id(conn, 'equipment', 'id', eid, org_id=org_id)


def belongs(row, org_id=None):
    if row is None:
        return False
    org_id = int(org_id or current_org_id())
    try:
        if 'org_id' in row.keys():
            val = row['org_id']
            if val is None:
                return org_id == ORG_TRUE_SENSE
            return int(val) == org_id
    except Exception:
        return False
    return True


def stamp(data, org_id=None):
    """Attach the current org_id to a write dict."""
    out = dict(data or {})
    out['org_id'] = int(org_id or current_org_id())
    return out


def not_found(as_json=None):
    want_json = as_json
    if want_json is None and _request_ready():
        path = request.path or ''
        want_json = path.startswith('/api')
        try:
            best = request.accept_mimetypes.best
            if best == 'application/json' and 'text/html' not in request.accept_mimetypes:
                want_json = True
        except Exception:
            pass
    if want_json:
        return jsonify({'error': 'Not found'}), 404
    return NOT_FOUND_HTML, 404, {'Content-Type': 'text/html; charset=utf-8'}


def resolve_user_org(conn, name, requested=None):
    allowed = orgs_for_user(conn, name)
    if not allowed:
        allowed = [ORG_TRUE_SENSE]
    if requested:
        try:
            req = int(requested)
        except (TypeError, ValueError):
            req = None
        if req in allowed:
            return req
    return allowed[0]


def register(fo):
    app = fo.app
    h = fo.h

    def _conn():
        return fo.get_db()

    def _me():
        return fo.me() or ''

    @app.before_request
    def org_guard():
        if not _me():
            return
        conn = None
        try:
            conn = _conn()
            allowed = orgs_for_user(conn, _me())
            raw = session.get('org_id')
            current = None
            try:
                current = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                current = None
            if current not in allowed:
                current = allowed[0] if allowed else ORG_TRUE_SENSE
                session['org_id'] = current
            g.org_id = current
            g.org_ids = allowed
        except Exception:
            g.org_id = ORG_TRUE_SENSE
            g.org_ids = [ORG_TRUE_SENSE]
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/org/switch', methods=['POST'])
    def org_switch():
        if not _me():
            return redirect('/login')
        raw = request.form.get('org_id') or (request.get_json(silent=True) or {}).get('org_id')
        conn = _conn()
        allowed = orgs_for_user(conn, _me())
        conn.close()
        try:
            oid = int(raw)
        except (TypeError, ValueError):
            oid = None
        if oid not in allowed:
            return not_found()
        set_current_org(oid)
        nxt = request.form.get('next') or request.referrer or '/'
        if not nxt.startswith('/'):
            nxt = '/'
        return redirect(nxt)

    def switcher_html():
        if not _me():
            return ''
        ids = []
        try:
            ids = list(getattr(g, 'org_ids', None) or [])
        except Exception:
            ids = []
        if len(ids) < 2:
            return ''
        cur = current_org_id()
        bits = ['<form class="fo-org-switch" method="post" action="/org/switch">']
        for oid in ids:
            b = branding(oid)
            on = ' on' if oid == cur else ''
            bits.append(
                '<button type="submit" name="org_id" value="%d" class="fo-org-btn%s"%s>%s</button>'
                % (oid, on, ' aria-current="page"' if oid == cur else '', h(b['short_name'])))
        bits.append('</form>')
        return ''.join(bits)

    fo.orgs = ORG_BY_ID
    fo.ORG_TRUE_SENSE = ORG_TRUE_SENSE
    fo.ORG_DATACOM = ORG_DATACOM
    fo.current_org_id = current_org_id
    fo.current_branding = current_branding
    fo.org_branding = branding
    fo.org_switcher_html = switcher_html
    fo.orgs_for_user = lambda name, conn=None: _orgs_for(fo, name, conn)
    fo.user_can_org = lambda name, oid, conn=None: _can(fo, name, oid, conn)
    fo.org_not_found = not_found
    fo.get_org_job = get_job
    fo.list_org_jobs = list_jobs
    fo.get_org_company = get_company
    fo.get_org_quote = get_quote
    fo.get_org_opportunity = get_opportunity
    fo.job_prefix = job_prefix
    fo._next_job_id = lambda conn: next_job_id(conn)
    fo.qbo_sync_enabled = qbo_sync_enabled
    fo.org_setting = org_setting
    fo.set_org_setting = set_org_setting
    fo.set_user_orgs = set_user_orgs
    fo.org_belongs = belongs
    fo.terms_for_org = terms_for_org


def _orgs_for(fo, name, conn):
    close = False
    if conn is None:
        conn = fo.get_db(); close = True
    try:
        return orgs_for_user(conn, name)
    finally:
        if close:
            conn.close()


def _can(fo, name, oid, conn):
    close = False
    if conn is None:
        conn = fo.get_db(); close = True
    try:
        return user_can_org(conn, name, oid)
    finally:
        if close:
            conn.close()
