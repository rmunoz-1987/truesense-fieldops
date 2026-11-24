"""Leads on top of the existing Sales opportunities board.

Office and estimators can work leads. Subs get 403. Zack's weekly files
are imported by an office upload or paste (the live app cannot read the
box). Draft emails are stored as notes only and are never sent.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta

import fieldops_platform as plat
import fieldops_crm as crm

LEAD_SOURCES = [
    ('zack', 'Zack'),
    ('website', 'Website'),
    ('referral', 'Referral'),
    ('google', 'Google'),
    ('repeat', 'Repeat'),
    ('other', 'Other'),
]
SOURCE_SET = {k for k, _ in LEAD_SOURCES}
FIT_GRADES = ('A', 'B', 'C')
LOST_REASONS = [
    ('price', 'Price'),
    ('timing', 'Timing'),
    ('no_fit', 'No fit'),
    ('no_response', 'No response'),
    ('competitor', 'Lost to competitor'),
    ('other', 'Other'),
]
LOST_SET = {k for k, _ in LOST_REASONS}
OPEN_STAGES = ('lead', 'qualified', 'estimating', 'proposal')
DRAFT_MARK = 'Zack draft email (not sent)'
CONTACT_DAYS = 2
STALE_DAYS = 7

FORBIDDEN_HTML = (
    '<!DOCTYPE html><html><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>Office and estimators</title>'
    '<style>body{font-family:-apple-system,Arial,sans-serif;max-width:520px;'
    'margin:48px auto;padding:20px;color:#111;background:#f7f6ef}'
    'h1{font-size:1.6rem;margin:0 0 12px}p{line-height:1.45}'
    'a{color:#6b6a16;font-weight:700}</style></head>'
    '<body><h1>Office and estimators</h1>'
    '<p>Leads are for the office and estimators.</p>'
    '<p><a href="/">Back</a></p></body></html>'
)


def now_iso():
    return datetime.utcnow().isoformat()


def today_local(fo=None):
    if fo is not None:
        try:
            return fo.today_local()
        except Exception:
            pass
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('America/Chicago')).date()
    except Exception:
        return date.today()


def nkey(s):
    return plat.nkey(s)


def source_label(key):
    return dict(LEAD_SOURCES).get(key or '', key or '')


def lost_label(key):
    return dict(LOST_REASONS).get(key or '', key or '')


def _add_col(conn, table, coldef):
    plat._add_col(conn, table, coldef)


def init_schema(conn):
    for col in (
        'lead_source TEXT',
        'fit_grade TEXT',
        'lost_reason TEXT',
        'lost_note TEXT',
        'last_activity_at TEXT',
        'contacted_at TEXT',
        'survey_job_id TEXT',
        'import_key TEXT',
    ):
        try:
            _add_col(conn, 'opportunities', col)
        except Exception:
            pass
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS ix_opp_import ON opportunities(import_key)')
    except Exception:
        pass
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS ix_opp_source ON opportunities(lead_source)')
    except Exception:
        pass


def is_draft_note(body):
    return (body or '').lstrip().startswith(DRAFT_MARK)


def touch_opportunity(conn, opportunity_id, contacted=False, when=None):
    if not opportunity_id:
        return
    when = when or now_iso()
    row = conn.execute('SELECT contacted_at FROM opportunities WHERE id=?', (opportunity_id,)).fetchone()
    if not row:
        return
    if contacted and not (row['contacted_at'] if 'contacted_at' in row.keys() else None):
        conn.execute(
            'UPDATE opportunities SET last_activity_at=?, contacted_at=?, updated_at=? WHERE id=?',
            (when, when, when, opportunity_id))
    else:
        conn.execute(
            'UPDATE opportunities SET last_activity_at=?, updated_at=? WHERE id=?',
            (when, when, opportunity_id))


def phones_in(text):
    found = re.findall(r'\+?1?[\s\-.(]*\d{3}[\s\-.)]*\d{3}[\s\-]*\d{4}', text or '')
    out = []
    seen = set()
    for p in found:
        d = re.sub(r'\D', '', p)
        if len(d) == 11 and d.startswith('1'):
            d = d[1:]
        if len(d) == 10 and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def phone_key(text):
    ps = phones_in(text or '')
    return ps[0] if ps else ''


def tel_href(text):
    d = phone_key(text)
    return ('tel:+1' + d) if d else ''


def age_days(iso, today=None):
    today = today or date.today()
    raw = (iso or '')[:10]
    if not raw:
        return 0
    try:
        return (today - datetime.strptime(raw, '%Y-%m-%d').date()).days
    except ValueError:
        return 0


def age_label(days):
    d = max(0, int(days or 0))
    if d == 1:
        return '1 day'
    return '%d days' % d


def _text(*vals):
    parts = []
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() != 'none':
            parts.append(s)
    return ' '.join(parts)


def normalize_record(raw, default_source='zack'):
    if not isinstance(raw, dict):
        return None
    company = _text(raw.get('company'), raw.get('prospect'), raw.get('name'))
    if not company:
        return None
    address = _text(raw.get('address'), raw.get('property_or_project'))
    phone_raw = _text(raw.get('phone'), raw.get('contact_route'))
    email = _text(raw.get('email'), raw.get('to'))
    if '@' not in email:
        email = ''
    contact = _text(raw.get('contact'), raw.get('contact_name'))
    title_extra = _text(raw.get('contact_title'))
    fit = (_text(raw.get('fit')) or '').upper()
    if fit not in FIT_GRADES:
        fit = ''
    why = _text(raw.get('why'), raw.get('why_now'), raw.get('whats_happening'), raw.get('note'))
    kind = _text(raw.get('type'), raw.get('category'), raw.get('segment'))
    source_url = _text(raw.get('source'), raw.get('source_url'))
    subject = _text(raw.get('subject'))
    body = (raw.get('body') or '').strip()
    src = (raw.get('lead_source') or default_source or 'zack').strip().lower()
    if src not in SOURCE_SET:
        src = 'other'
    title = company[:200]
    notes_parts = []
    if kind:
        notes_parts.append('Type: ' + kind)
    if why:
        notes_parts.append(why)
    if source_url:
        notes_parts.append('Source: ' + source_url)
    if title_extra and contact:
        notes_parts.append('Contact: %s, %s' % (contact, title_extra))
    elif title_extra:
        notes_parts.append('Contact title: ' + title_extra)
    notes = '\n'.join(notes_parts).strip()[:2000]
    draft = ''
    if subject or body:
        lines = [DRAFT_MARK]
        if subject:
            lines.append('Subject: ' + subject)
        if email:
            lines.append('To: ' + email)
        if body:
            lines.append('')
            lines.append(body)
        draft = '\n'.join(lines).strip()[:4000]
    pkey = phone_key(phone_raw)
    akey = nkey(address) if address else ''
    ckey = nkey(company)
    import_key = 'zack|%s|%s' % (ckey, akey or pkey or nkey(email or title))
    return {
        'company': company[:200],
        'address': address[:300],
        'phone': (phone_raw or '')[:80],
        'phone_key': pkey,
        'email': email[:120],
        'contact': contact[:120],
        'contact_title': title_extra[:80],
        'fit': fit,
        'lead_source': src,
        'title': title,
        'notes': notes,
        'draft': draft,
        'import_key': import_key[:200],
    }


def parse_leads_payload(text, filename=''):
    """Return (records, error). Accepts Zack JSON or a CSV with a header row."""
    raw = (text or '').strip()
    if not raw:
        return [], 'Paste or upload a JSON or CSV file first.'
    name = (filename or '').lower()
    recs = []
    if not name.endswith('.csv') and (raw[0] in '[{'):
        try:
            data = json.loads(raw)
        except ValueError:
            return [], 'That JSON could not be read. Check the file and try again.'
        rows = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for k in ('leads', 'items', 'records', 'data'):
                if isinstance(data.get(k), list):
                    rows = data[k]
                    break
            if not rows:
                # first list value, or a single object
                lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
                if len(lists) == 1:
                    rows = lists[0]
                elif all(k in data for k in ('company', 'prospect') if False) or 'company' in data or 'prospect' in data:
                    rows = [data]
        for row in rows:
            rec = normalize_record(row)
            if rec:
                recs.append(rec)
        if not recs:
            return [], 'No leads were found in that JSON.'
        return recs, None
    # CSV
    try:
        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            return [], 'That CSV needs a header row.'
        for row in reader:
            rec = normalize_record({(k or '').strip().lower(): v for k, v in row.items()})
            if rec:
                recs.append(rec)
    except Exception:
        return [], 'That CSV could not be read.'
    if not recs:
        return [], 'No leads were found in that CSV.'
    return recs, None


def _find_duplicate(conn, rec):
    if rec.get('import_key'):
        row = conn.execute(
            'SELECT id FROM opportunities WHERE import_key=?', (rec['import_key'],)).fetchone()
        if row:
            return row['id'], 'import_key'
    company = plat.get_company_by_key(conn, nkey(rec['company']))
    if company and rec.get('address'):
        site = conn.execute(
            'SELECT id FROM sites WHERE company_id=? AND address_key=?',
            (company['id'], nkey(rec['address']))).fetchone()
        if site:
            opp = conn.execute(
                'SELECT id FROM opportunities WHERE company_id=? AND site_id=?',
                (company['id'], site['id'])).fetchone()
            if opp:
                return opp['id'], 'company_address'
    pkey = rec.get('phone_key') or ''
    if pkey:
        # same phone on a company, contact, or site that already has an opportunity
        hits = list(conn.execute(
            '''SELECT DISTINCT o.id
               FROM opportunities o
               JOIN companies c ON c.id=o.company_id
               LEFT JOIN contacts t ON t.company_id=c.id
               LEFT JOIN sites s ON s.company_id=c.id
               WHERE REPLACE(REPLACE(REPLACE(REPLACE(IFNULL(c.phone,''),'-',''),' ',''),'(',''),')','') LIKE ?
                  OR REPLACE(REPLACE(REPLACE(REPLACE(IFNULL(t.phone,''),'-',''),' ',''),'(',''),')','') LIKE ?
                  OR REPLACE(REPLACE(REPLACE(REPLACE(IFNULL(s.phone,''),'-',''),' ',''),'(',''),')','') LIKE ?''',
            ('%' + pkey, '%' + pkey, '%' + pkey)))
        if hits:
            return hits[0]['id'], 'phone'
    return None, None


def import_leads(conn, records, created_by='', when=None, created_at=None):
    """Insert new opportunities. Skip duplicates by company plus address, or phone.

    Draft emails go on the opportunity as notes only. Nothing is sent.
    """
    when = when or now_iso()
    created_at = created_at or when
    added = 0
    skipped = 0
    errors = 0
    ids = []
    for rec in records or []:
        if not rec or not rec.get('company'):
            errors += 1
            continue
        dup_id, _how = _find_duplicate(conn, rec)
        if dup_id:
            skipped += 1
            continue
        co = plat.upsert_company(
            conn, rec['company'], phone=rec.get('phone') or '',
            email=rec.get('email') or '', billing_address=rec.get('address') or '', when=when)
        if not co:
            errors += 1
            continue
        site = None
        if rec.get('address'):
            site = plat.upsert_site(
                conn, co['id'], address=rec['address'], phone=rec.get('phone') or '', when=when)
        contact = None
        if rec.get('contact') or rec.get('phone') or rec.get('email'):
            contact = plat.ensure_contact(
                conn, co['id'], rec.get('contact') or rec['company'],
                role='other', phone=rec.get('phone') or '', email=rec.get('email') or '',
                title=rec.get('contact_title') or '', when=when)
        notes = rec.get('notes') or ''
        if rec.get('draft'):
            notes = (notes + ('\n\n' if notes else '') + rec['draft']).strip()[:4000]
        cur = conn.execute(
            '''INSERT INTO opportunities (
                company_id,contact_id,site_id,title,stage,amount,notes,created_by,
                created_at,updated_at,lead_source,fit_grade,import_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (co['id'], contact['id'] if contact else None, site['id'] if site else None,
             rec['title'][:200], 'lead', 0, notes, (created_by or '')[:80],
             created_at, when, rec.get('lead_source') or 'zack', rec.get('fit') or None,
             rec.get('import_key') or None))
        oid = cur.lastrowid
        if rec.get('draft'):
            crm.add_activity(
                conn, 'note', rec['draft'], company_id=co['id'],
                contact_id=contact['id'] if contact else None, opportunity_id=oid,
                happened_at=created_at, created_by=created_by or 'Zack import')
        ids.append(oid)
        added += 1
    return {'added': added, 'skipped': skipped, 'errors': errors, 'ids': ids}


def last_real_activity(conn, oid, created_at=None):
    rows = list(conn.execute(
        'SELECT happened_at, body, kind FROM crm_activities WHERE opportunity_id=? ORDER BY happened_at DESC, id DESC',
        (oid,)))
    for r in rows:
        if is_draft_note(r['body']):
            continue
        return r['happened_at']
    return created_at


def followup_leads(conn, today=None):
    """New uncontacted leads (2+ days) and stale open leads (no real activity in 7 days)."""
    today = today or date.today()
    rows = list(conn.execute(
        '''SELECT o.*, c.name AS company, s.address AS site_address, t.phone AS contact_phone,
                  c.phone AS company_phone, s.phone AS site_phone
           FROM opportunities o
           JOIN companies c ON c.id=o.company_id
           LEFT JOIN sites s ON s.id=o.site_id
           LEFT JOIN contacts t ON t.id=o.contact_id
           WHERE o.stage IN (%s)'''
        % ','.join('?' * len(OPEN_STAGES)),
        OPEN_STAGES))
    uncontacted = []
    stale = []
    seen = set()
    for r in rows:
        rec = dict(r)
        age = age_days(rec.get('created_at'), today)
        rec['age'] = age
        rec['age_label'] = age_label(age)
        contacted = rec.get('contacted_at')
        last = rec.get('last_activity_at') or last_real_activity(conn, rec['id'], rec.get('created_at'))
        rec['last_real'] = last
        if rec.get('stage') == 'lead' and not contacted and age >= CONTACT_DAYS:
            uncontacted.append(rec)
            seen.add(rec['id'])
            continue
        if rec['id'] in seen:
            continue
        last_age = age_days(last, today) if last else age
        if last_age >= STALE_DAYS:
            rec['age'] = last_age
            rec['age_label'] = age_label(last_age)
            stale.append(rec)
    uncontacted.sort(key=lambda x: -x['age'])
    stale.sort(key=lambda x: -x['age'])
    return uncontacted, stale


def today_actions(conn, day, fo=None):
    today = None
    if day:
        try:
            today = datetime.strptime(str(day)[:10], '%Y-%m-%d').date()
        except ValueError:
            today = None
    today = today or today_local(fo)
    uncontacted, stale = followup_leads(conn, today)
    icon = '&#128197;'
    if fo is not None:
        icon = fo.ICONS.get('bell') or fo.ICONS.get('note') or icon
    acts = []
    if uncontacted:
        detail = ', '.join(
            '%s (%s)' % (r.get('company') or r.get('title'), r['age_label']) for r in uncontacted[:3])
        acts.append({
            'n': len(uncontacted), 'title': 'New leads not contacted',
            'detail': detail, 'icon': icon, 'href': '/leads?need=contact',
            'btn': 'Open', 'tone': 'red',
        })
    if stale:
        detail = ', '.join(
            '%s (%s)' % (r.get('company') or r.get('title'), r['age_label']) for r in stale[:3])
        acts.append({
            'n': len(stale), 'title': 'Stale leads',
            'detail': detail, 'icon': icon, 'href': '/leads?need=stale',
            'btn': 'Open', 'tone': '',
        })
    return acts


def followup_items(conn, day, fo=None):
    """Estimator Today rows for the same reminders. Tasks and list links only."""
    acts = today_actions(conn, day, fo)
    out = []
    for a in acts:
        out.append({
            'id': 'lead-' + (a.get('href') or ''),
            'title': a['title'] + ' (%d)' % a['n'],
            'detail': a.get('detail') or '',
            'href': a.get('href') or '/leads',
        })
    return out


def next_job_id(conn, fo):
    y = fo.today_local().year
    pre = '%s-%d-' % (__import__('fieldops_orgs').job_prefix(), y)
    top = 0
    for r in conn.execute('SELECT id FROM jobs WHERE id LIKE ?', (pre + '%',)):
        try:
            top = max(top, int(r['id'][len(pre):]))
        except ValueError:
            pass
    return pre + '%04d' % (top + 1)


def start_survey(conn, oid, fo):
    """Create a Survey job from a lead, carrying customer and site. Link it on the opportunity."""
    row = conn.execute('SELECT * FROM opportunities WHERE id=?', (oid,)).fetchone()
    if not row:
        return None, 'Lead not found.'
    existing = row['survey_job_id'] if 'survey_job_id' in row.keys() else None
    if existing:
        job = conn.execute('SELECT id FROM jobs WHERE id=?', (existing,)).fetchone()
        if job:
            return existing, None
    co = conn.execute('SELECT * FROM companies WHERE id=?', (row['company_id'],)).fetchone()
    site = conn.execute('SELECT * FROM sites WHERE id=?', (row['site_id'],)).fetchone() if row['site_id'] else None
    contact = conn.execute('SELECT * FROM contacts WHERE id=?', (row['contact_id'],)).fetchone() if row['contact_id'] else None
    customer = (co['name'] if co else '') or 'Customer'
    address = (site['address'] if site else '') or ''
    phone = ''
    if contact and contact['phone']:
        phone = contact['phone']
    elif site and site['phone']:
        phone = site['phone']
    elif co and co['phone']:
        phone = co['phone']
    jid = next_job_id(conn, fo)
    title = (row['title'] or 'Survey')[:200]
    if not title.lower().startswith('survey'):
        title = 'Survey: ' + title
        title = title[:200]
    tech = fo.me() if fo.is_estimator() else (fo.default_sub() or fo.me())
    if not tech:
        return None, 'Pick a sub for this survey.'
    when = now_iso()
    sd = fo.today_local().isoformat()
    lines = []
    try:
        t = conn.execute(
            "SELECT items FROM checklist_templates WHERE LOWER(name) LIKE '%survey%' ORDER BY id LIMIT 1").fetchone()
        if t:
            lines = [x for x in json.loads(t['items']) if str(x).strip()]
    except Exception:
        lines = []
    chk = json.dumps([{'text': x, 'done': False} for x in lines])
    conn.execute(
        '''INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,phone,checklist,
             scheduled_date,created_by,job_type,company_id,site_id,contact_id,org_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (jid, title, customer, address, '', 'assigned', tech, when, (phone or '')[:40], chk,
         sd, fo.me(), 'Survey', row['company_id'], row['site_id'], row['contact_id'], __import__('fieldops_orgs').current_org_id()))
    try:
        plat.link_job(conn, conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone(),
                      company_id=row['company_id'])
    except Exception:
        pass
    conn.execute(
        'UPDATE opportunities SET survey_job_id=?, updated_at=? WHERE id=?',
        (jid, when, oid))
    touch_opportunity(conn, oid, contacted=False, when=when)
    return jid, None


def apply_lost(conn, oid, reason, note, when=None):
    reason = (reason or '').strip()
    note = (note or '').strip()[:1000]
    if reason not in LOST_SET:
        return 'Pick a lost reason.'
    if reason == 'other' and not note:
        return 'Add a short note for Other.'
    when = when or now_iso()
    conn.execute(
        '''UPDATE opportunities SET stage=?, lost_reason=?, lost_note=?, lost_at=?,
           won_at=NULL, updated_at=? WHERE id=?''',
        ('lost', reason, note, when, when, oid))
    return None


def register(fo):
    from flask import request, redirect, jsonify

    app = fo.app
    h = fo.h

    def leads_gate(forbid=True):
        if not fo.me():
            return redirect('/login')
        if not fo.may_estimate():
            if forbid:
                return fo.app.response_class(FORBIDDEN_HTML, status=403, mimetype='text/html')
            return redirect('/')
        return None

    def office_gate():
        if not fo.me():
            return redirect('/login')
        if not fo.is_office():
            if fo.is_estimator():
                return fo.app.response_class(FORBIDDEN_HTML, status=403, mimetype='text/html')
            return fo.app.response_class(FORBIDDEN_HTML, status=403, mimetype='text/html')
        return None

    def page(title, body, back='/leads', kicker='Sales', actions='', msg=''):
        act = actions or ''
        if back:
            act = ('<a class="plat-btn plat-btn-ghost" href="%s">Back</a>' % h(back)) + act
        head = (
            '<div class="plat-head"><div class="plat-head-text">'
            + ('<div class="plat-kicker">%s</div>' % h(kicker) if kicker else '')
            + '<h1 class="plat-title">%s<span class="plat-dot">.</span></h1></div>' % h(title)
            + ('<div class="plat-actions">%s</div>' % act if act else '')
            + '</div>'
        )
        banner = ('<div class="plat-msg">%s</div>' % h(msg)) if msg else ''
        return fo.ops_page(title, head + banner + body, back=False, msg='')

    def lead_phone(row):
        return row.get('contact_phone') or row.get('site_phone') or row.get('company_phone') or ''

    def fit_badge(grade):
        g = (grade or '').upper()
        if g not in FIT_GRADES:
            return ''
        return '<span class="fit-badge fit-%s" title="Zack fit">%s</span>' % (g, g)

    def load_leads(conn, source='', fit='', need='', sort='fit'):
        sql = '''SELECT o.*, c.name AS company, s.name AS site_name, s.address AS site_address,
                        t.name AS contact_name, t.phone AS contact_phone,
                        c.phone AS company_phone, s.phone AS site_phone
                 FROM opportunities o
                 JOIN companies c ON c.id=o.company_id
                 LEFT JOIN sites s ON s.id=o.site_id
                 LEFT JOIN contacts t ON t.id=o.contact_id'''
        args = []
        where = []
        if source and source in SOURCE_SET:
            where.append('o.lead_source=?')
            args.append(source)
        if fit and fit in FIT_GRADES:
            where.append('o.fit_grade=?')
            args.append(fit)
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        today = today_local(fo)
        uncontacted, stale = followup_leads(conn, today)
        uids = {r['id'] for r in uncontacted}
        sids = {r['id'] for r in stale}
        out = []
        for r in rows:
            r['age'] = age_days(r.get('created_at'), today)
            r['age_label'] = age_label(r['age'])
            r['need_contact'] = r['id'] in uids
            r['need_stale'] = r['id'] in sids
            if need == 'contact' and not r['need_contact']:
                continue
            if need == 'stale' and not r['need_stale']:
                continue
            out.append(r)
        if sort == 'age':
            out.sort(key=lambda r: (-r['age'], (r.get('company') or '').lower()))
        elif sort == 'company':
            out.sort(key=lambda r: ((r.get('company') or '').lower(), r['id']))
        elif sort == 'updated':
            out.sort(key=lambda r: (r.get('updated_at') or '', r['id']), reverse=True)
        else:
            # fit A, B, C, then blank
            rank = {'A': 0, 'B': 1, 'C': 2}
            out.sort(key=lambda r: (rank.get((r.get('fit_grade') or '').upper(), 9),
                                    -r['age'], (r.get('company') or '').lower()))
        return out

    @app.route('/leads')
    def leads_list():
        gate = leads_gate()
        if gate:
            return gate
        try:
            fo.session['fo_area'] = 'sales'
        except Exception:
            pass
        source = (request.args.get('source') or '').strip().lower()
        fit = (request.args.get('fit') or '').strip().upper()
        need = (request.args.get('need') or '').strip().lower()
        sort = (request.args.get('sort') or 'fit').strip().lower()
        if sort not in ('fit', 'age', 'company', 'updated'):
            sort = 'fit'
        conn = fo.get_db()
        rows = load_leads(conn, source=source, fit=fit, need=need, sort=sort)
        is_office = fo.is_office()
        conn.close()
        flash = ''
        if request.args.get('imported') is not None:
            try:
                a = int(request.args.get('imported') or 0)
                s = int(request.args.get('skipped') or 0)
            except ValueError:
                a, s = 0, 0
            flash = 'Imported %d. Skipped %d duplicate%s.' % (a, s, '' if s == 1 else 's')

        def pill(href, label, on):
            return '<a class="plat-pill-link%s" href="%s">%s</a>' % (' on' if on else '', h(href), h(label))

        def href(**kw):
            q = {'source': source, 'fit': fit, 'need': need, 'sort': sort}
            q.update(kw)
            parts = ['%s=%s' % (k, v) for k, v in q.items() if v]
            return '/leads' + (('?' + '&'.join(parts)) if parts else '')

        filters = '<div class="plat-pills lead-filters">'
        filters += pill('/leads?sort=' + sort, 'All sources', not source)
        for k, lab in LEAD_SOURCES:
            filters += pill(href(source=k), lab, source == k)
        filters += '</div><div class="plat-pills lead-filters">'
        filters += pill(href(fit=''), 'All fit', not fit)
        for g in FIT_GRADES:
            filters += pill(href(fit=g), 'Fit ' + g, fit == g)
        filters += '</div><div class="plat-pills lead-filters">'
        filters += pill(href(need=''), 'Any age', not need)
        filters += pill(href(need='contact'), 'Not contacted (2+ days)', need == 'contact')
        filters += pill(href(need='stale'), 'Stale (7+ days)', need == 'stale')
        filters += '</div><div class="plat-pills lead-filters">'
        filters += '<span class="muted" style="margin-right:8px">Sort</span>'
        for k, lab in (('fit', 'Fit'), ('age', 'Age'), ('company', 'Company'), ('updated', 'Updated')):
            filters += pill(href(sort=k), lab, sort == k)
        filters += '</div>'

        cards = ''
        if not rows:
            cards = '<div class="muted">No leads in this list.</div>'
        else:
            for r in rows:
                phone = lead_phone(r)
                call = tel_href(phone)
                where = r.get('site_address') or r.get('site_name') or ''
                meta_bits = [r.get('company') or '', source_label(r.get('lead_source')), r.get('age_label')]
                if where:
                    meta_bits.append(where)
                stage = dict(plat.OPP_STAGES).get(r.get('stage') or '', r.get('stage') or '')
                flags = ''
                if r.get('need_contact'):
                    flags += '<span class="plat-pill">Not contacted</span> '
                elif r.get('need_stale'):
                    flags += '<span class="plat-pill">Stale</span> '
                btns = '<div class="lead-btns">'
                if call:
                    btns += '<a class="big-btn" href="%s">Call</a>' % h(call)
                else:
                    btns += '<span class="big-btn gray" style="opacity:.55">No phone</span>'
                btns += '<a class="big-btn gray" href="/opportunities/%d#lead-notes">Notes</a>' % r['id']
                if r.get('survey_job_id'):
                    btns += '<a class="big-btn gray" href="/?job=%s">Open survey</a>' % h(r['survey_job_id'])
                else:
                    btns += (
                        '<form method="post" action="/opportunities/%d/survey" class="lead-survey-form">'
                        '<button class="big-btn" type="submit">Start survey</button></form>'
                    ) % r['id']
                btns += '</div>'
                cards += (
                    '<div class="plat-card lead-card" data-id="%d">'
                    '<a href="/opportunities/%d"><b>%s</b></a> %s'
                    '<div class="muted">%s</div>'
                    '<div class="muted">%s %s</div>'
                    '%s</div>'
                ) % (
                    r['id'], r['id'], h(r['title']), fit_badge(r.get('fit_grade')),
                    h(' · '.join(x for x in meta_bits if x)),
                    h(stage), flags,
                    btns,
                )
            cards = '<div class="lead-list">' + cards + '</div>'

        import_html = ''
        if is_office:
            import_html = (
                '<div class="plat-panel" id="zack-import"><h2>Import Zack leads</h2>'
                '<p class="muted">The live app cannot read files on the box. Upload or paste Zack\'s weekly '
                'JSON or CSV here. Duplicates are skipped by company plus address, or by phone. '
                'Draft emails are saved as notes only and are never sent.</p>'
                '<form class="plat-form" method="post" action="/leads/import" enctype="multipart/form-data">'
                '<label>JSON or CSV file</label><input type="file" name="file" accept=".json,.csv,application/json,text/csv">'
                '<label>Or paste the file</label><textarea name="payload" rows="8" placeholder="Paste JSON or CSV"></textarea>'
                '<button class="plat-btn plat-btn-gold" type="submit">Import Zack leads</button></form></div>'
            )
        board_link = '<a class="plat-btn plat-btn-ghost" href="/opportunities">Board</a>'
        actions = board_link
        if is_office:
            actions = '<a class="plat-btn plat-btn-gold" href="/leads#import-zack">Import Zack leads</a> ' + actions
        actions += ' <a class="plat-btn plat-btn-ghost" href="/opportunities/new">+ Opportunity</a>'
        body = filters + cards + import_html
        return page('Leads', body, back='/sales', kicker='Sales', actions=actions, msg=flash)

    @app.route('/leads/import', methods=['POST'])
    def leads_import():
        gate = office_gate()
        if gate:
            return gate
        payload = (request.form.get('payload') or '').strip()
        filename = ''
        storage = request.files.get('file') if request.files else None
        if storage and storage.filename:
            filename = storage.filename
            try:
                payload = storage.read().decode('utf-8-sig', errors='replace')
            except Exception:
                return page('Import Zack leads', '<p>That file could not be read.</p>',
                            back='/leads', msg='Could not read the file.')
        recs, err = parse_leads_payload(payload, filename=filename)
        if err:
            return page('Import Zack leads', '<p>%s</p><p><a href="/leads">Back to Leads</a></p>' % h(err),
                        back='/leads', msg=err)
        conn = fo.get_db()
        # Prefer a created date from the filename (leads_2026-11-18.json) so aging is honest.
        created_at = now_iso()
        m = re.search(r'(20\d{2}-\d{2}-\d{2})', filename or '')
        if m:
            created_at = m.group(1) + 'T12:00:00'
        result = import_leads(conn, recs, created_by=fo.me(), created_at=created_at)
        conn.commit()
        conn.close()
        msg = 'Imported %d. Skipped %d duplicate%s.' % (
            result['added'], result['skipped'], '' if result['skipped'] == 1 else 's')
        if result['errors']:
            msg += ' %d row%s could not be imported.' % (
                result['errors'], '' if result['errors'] == 1 else 's')
        return redirect('/leads?imported=%d&skipped=%d' % (result['added'], result['skipped']))

    @app.route('/opportunities/<int:oid>/survey', methods=['POST'])
    def opportunity_survey(oid):
        gate = leads_gate()
        if gate:
            return gate
        conn = fo.get_db()
        jid, err = start_survey(conn, oid, fo)
        if err:
            conn.close()
            return page('Start survey', '<p>%s</p><p><a href="/opportunities/%d">Back</a></p>' % (h(err), oid),
                        back='/opportunities/%d' % oid, msg=err)
        conn.commit()
        conn.close()
        return redirect('/?job=' + jid)

    # hooks used by Today
    fo.leads_today_actions = lambda conn, day: today_actions(conn, day, fo)
    fo.leads_followup_items = lambda conn, day: followup_items(conn, day, fo)
    fo.leads_apply_lost = apply_lost
    fo.leads_fit_badge = fit_badge
    fo.lead_source_label = source_label
    fo.LEAD_SOURCES = LEAD_SOURCES
    fo.FIT_GRADES = FIT_GRADES
    fo.LOST_REASONS = LOST_REASONS
