"""Service call tickets: an intake queue separate from project jobs.

Sources: office, phone call, or the client portal. Office can assign a sub,
put the ticket on the schedule, convert it to a job, or queue an invoice draft.
Nothing is sent to the client. Money stays office-only.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

STATUSES = [
    ('new', 'New'),
    ('scheduled', 'Scheduled'),
    ('in_progress', 'In progress'),
    ('waiting_parts', 'Waiting parts'),
    ('resolved', 'Resolved'),
    ('closed', 'Closed'),
]
STATUS_SET = {k for k, _ in STATUSES}
PRIORITIES = [('low', 'Low'), ('normal', 'Normal'), ('high', 'High'), ('urgent', 'Urgent')]
PRI_SET = {k for k, _ in PRIORITIES}
SOURCES = [('office', 'Office'), ('phone', 'Phone call'), ('portal', 'Client portal'), ('plan', 'Service plan')]
OPEN_STATUSES = ('new', 'scheduled', 'in_progress', 'waiting_parts')
BACKUP_NAME = 'fieldops-pre-service-ops.db'


def now_iso():
    return datetime.utcnow().isoformat()


def ckey(s):
    return ' '.join((s or '').split()).lower()


def status_label(k):
    return dict(STATUSES).get(k, (k or 'New').replace('_', ' ').title())


def pri_label(k):
    return dict(PRIORITIES).get(k, 'Normal')


def backup_once(backup_dir, db_path):
    """One snapshot before this batch of schema changes. Will not overwrite."""
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, BACKUP_NAME)
    if os.path.exists(dst) or not os.path.exists(db_path):
        print('SERVICE_OPS_BACKUP backup=%s' % ('yes' if os.path.exists(dst) else 'no'), flush=True)
        return dst if os.path.exists(dst) else None
    tmp = dst + '.tmp'
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(tmp)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    os.replace(tmp, dst)
    print('SERVICE_OPS_BACKUP backup=yes', flush=True)
    return dst


def init_schema(conn, backup_dir=None, db_path=None):
    if backup_dir and db_path:
        backup_once(backup_dir, db_path)
    conn.execute('''CREATE TABLE IF NOT EXISTS service_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT UNIQUE,
        company_id INTEGER,
        site_id INTEGER,
        customer TEXT,
        address TEXT,
        phone TEXT,
        priority TEXT DEFAULT 'normal',
        issue TEXT,
        equipment_id INTEGER,
        device_label TEXT,
        status TEXT DEFAULT 'new',
        source TEXT DEFAULT 'office',
        assigned_sub TEXT,
        scheduled_date TEXT,
        start_time TEXT,
        end_time TEXT,
        job_id TEXT,
        plan_id INTEGER,
        amount REAL,
        created_by TEXT,
        created_at TEXT,
        updated_at TEXT,
        resolved_at TEXT,
        closed_at TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS ticket_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        kind TEXT,
        body TEXT,
        created_by TEXT,
        created_at TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_tickets_status ON service_tickets(status)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_tickets_company ON service_tickets(company_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_tickets_site ON service_tickets(site_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_ticket_events_tid ON ticket_events(ticket_id)')


def next_number(conn):
    y = datetime.utcnow().year
    pre = 'SC-%d-' % y
    top = 0
    for r in conn.execute('SELECT number FROM service_tickets WHERE number LIKE ?', (pre + '%',)):
        try:
            top = max(top, int((r['number'] or '')[len(pre):]))
        except (TypeError, ValueError):
            pass
    return '%s%04d' % (pre, top + 1)


def add_event(conn, ticket_id, kind, body, who=''):
    conn.execute(
        'INSERT INTO ticket_events (ticket_id,kind,body,created_by,created_at) VALUES (?,?,?,?,?)',
        (ticket_id, kind, (body or '')[:2000], (who or '')[:80], now_iso()))


def create_ticket(conn, data, created_by='', source='office'):
    issue = (data.get('issue') or '').strip()[:4000]
    if not issue:
        return None, 'Describe the issue.'
    customer = (data.get('customer') or '').strip()[:200]
    company_id = data.get('company_id') or None
    site_id = data.get('site_id') or None
    if company_id:
        co = conn.execute('SELECT * FROM companies WHERE id=?', (company_id,)).fetchone()
        if not co:
            return None, 'Company not found.'
        customer = customer or co['name']
    if site_id:
        site = conn.execute('SELECT * FROM sites WHERE id=?', (site_id,)).fetchone()
        if not site:
            return None, 'Site not found.'
        address = (data.get('address') or site['address'] or site['name'] or '')[:300]
        if not company_id:
            company_id = site['company_id']
    else:
        address = (data.get('address') or '')[:300]
    pri = data.get('priority') if data.get('priority') in PRI_SET else 'normal'
    src = source if source in dict(SOURCES) else 'office'
    num = next_number(conn)
    when = now_iso()
    cur = conn.execute(
        '''INSERT INTO service_tickets (
            number,company_id,site_id,customer,address,phone,priority,issue,equipment_id,device_label,
            status,source,assigned_sub,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'new', ?,?,?,?,?)''',
        (num, company_id, site_id, customer, address, (data.get('phone') or '')[:40], pri, issue,
         data.get('equipment_id') or None, (data.get('device_label') or '')[:120],
         src, (data.get('assigned_sub') or '')[:60] or None, (created_by or '')[:80], when, when))
    tid = cur.lastrowid
    add_event(conn, tid, 'created', 'Ticket opened from %s.' % dict(SOURCES).get(src, src), created_by)
    if data.get('assigned_sub'):
        add_event(conn, tid, 'assigned', 'Assigned to %s.' % data.get('assigned_sub'), created_by)
    return conn.execute('SELECT * FROM service_tickets WHERE id=?', (tid,)).fetchone(), None


def set_status(conn, ticket_id, status, who='', note=''):
    if status not in STATUS_SET:
        return 'Unknown status.'
    row = conn.execute('SELECT * FROM service_tickets WHERE id=?', (ticket_id,)).fetchone()
    if not row:
        return 'Ticket not found.'
    when = now_iso()
    fields = {'status': status, 'updated_at': when}
    if status == 'resolved':
        fields['resolved_at'] = when
    if status == 'closed':
        fields['closed_at'] = when
    conn.execute(
        'UPDATE service_tickets SET status=?, updated_at=?, resolved_at=COALESCE(?,resolved_at), closed_at=COALESCE(?,closed_at) WHERE id=?',
        (status, when, fields.get('resolved_at'), fields.get('closed_at'), ticket_id))
    body = 'Status set to %s.' % status_label(status)
    if note:
        body += ' ' + note
    add_event(conn, ticket_id, 'status', body, who)
    return None


def assign_and_schedule(conn, ticket_id, sub, day=None, start=None, end=None, who=''):
    row = conn.execute('SELECT * FROM service_tickets WHERE id=?', (ticket_id,)).fetchone()
    if not row:
        return None, 'Ticket not found.'
    sub = ' '.join((sub or '').split()).title()[:60]
    if not sub:
        return None, 'Pick a sub.'
    when = now_iso()
    day = (day or '')[:10] or None
    conn.execute(
        'UPDATE service_tickets SET assigned_sub=?, scheduled_date=?, start_time=?, end_time=?, status=?, updated_at=? WHERE id=?',
        (sub, day, (start or '')[:5] or None, (end or '')[:5] or None,
         'scheduled' if day else (row['status'] if row['status'] != 'new' else 'new'), when, ticket_id))
    if day:
        add_event(conn, ticket_id, 'scheduled', 'Scheduled for %s with %s.' % (day, sub), who)
    else:
        add_event(conn, ticket_id, 'assigned', 'Assigned to %s.' % sub, who)
    return conn.execute('SELECT * FROM service_tickets WHERE id=?', (ticket_id,)).fetchone(), None


def convert_to_job(fo, conn, ticket_id, who=''):
    row = conn.execute('SELECT * FROM service_tickets WHERE id=?', (ticket_id,)).fetchone()
    if not row:
        return None, 'Ticket not found.'
    if row['job_id'] and conn.execute('SELECT id FROM jobs WHERE id=?', (row['job_id'],)).fetchone():
        return conn.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone(), None
    # Reuse the app's job id helper if present.
    jid = None
    if hasattr(fo, '_next_job_id'):
        jid = fo._next_job_id(conn)
    if not jid:
        y = datetime.utcnow().year
        pre = 'TS-%d-' % y
        top = 0
        for r in conn.execute('SELECT id FROM jobs WHERE id LIKE ?', (pre + '%',)):
            try:
                top = max(top, int(str(r['id'])[len(pre):]))
            except (TypeError, ValueError):
                pass
        jid = '%s%04d' % (pre, top + 1)
    title = (row['issue'] or 'Service call')[:120]
    if len(row['issue'] or '') > 120:
        title = title.rsplit(' ', 1)[0] or title
    tech = row['assigned_sub'] or ''
    sd = row['scheduled_date']
    conn.execute(
        '''INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,phone,scheduled_date,created_by,job_type,company_id,site_id,contact_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (jid, title, row['customer'] or '', row['address'] or '', row['issue'] or '',
         'assigned', tech, now_iso(), row['phone'] or '', sd, who or 'Ticket', 'Service',
         row['company_id'], row['site_id'], None))
    # company_id/site_id columns may exist from platform migrate
    try:
        conn.execute('UPDATE jobs SET company_id=?, site_id=? WHERE id=?', (row['company_id'], row['site_id'], jid))
    except Exception:
        pass
    conn.execute('UPDATE service_tickets SET job_id=?, updated_at=? WHERE id=?', (jid, now_iso(), ticket_id))
    if sd and tech and hasattr(fo, 'upsert_slot') is False:
        try:
            import fieldops_ops as _ops
            _ops.upsert_slot(conn, jid, sd, tech, row['start_time'] or '08:00', row['end_time'] or '16:00')
        except Exception:
            pass
    elif sd and tech:
        try:
            import fieldops_ops as _ops
            _ops.upsert_slot(conn, jid, sd, tech, row['start_time'] or '08:00', row['end_time'] or '16:00')
        except Exception:
            pass
    if sd and row['status'] in ('new',):
        conn.execute("UPDATE service_tickets SET status='scheduled' WHERE id=?", (ticket_id,))
    add_event(conn, ticket_id, 'job', 'Converted to job %s.' % jid, who)
    return conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone(), None


def queue_invoice_draft(fo, conn, ticket_id, amount, memo='', who=''):
    row = conn.execute('SELECT * FROM service_tickets WHERE id=?', (ticket_id,)).fetchone()
    if not row:
        return None, 'Ticket not found.'
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return None, 'Enter an amount.'
    if amt <= 0:
        return None, 'Enter an amount greater than zero.'
    job, err = convert_to_job(fo, conn, ticket_id, who)
    if err:
        return None, err
    # refresh ticket
    row = conn.execute('SELECT * FROM service_tickets WHERE id=?', (ticket_id,)).fetchone()
    job = conn.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
    if job and (job['sale_amount'] or 0) == 0:
        conn.execute('UPDATE jobs SET sale_amount=? WHERE id=?', (amt, job['id']))
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
    payload = None
    if hasattr(fo, 'qbo_payload'):
        payload = fo.qbo_payload(conn, job)
        desc = (memo or ('Service call %s' % row['number']))[:4000]
        payload['line_items'] = [{'description': desc, 'qty': 1, 'rate': round(amt, 2), 'amount': round(amt, 2)}]
        payload['total'] = round(amt, 2)
        payload['kind'] = 'final'
        payload['private_note'] = 'Field Ops service ticket %s. Created as a draft. Do not send without Roland OK.' % row['number']
    conn.execute('UPDATE service_tickets SET amount=?, updated_at=? WHERE id=?', (amt, now_iso(), ticket_id))
    cur = conn.execute(
        '''INSERT INTO qbo_drafts (job_id,kind,amount,memo,status,payload,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (job['id'], 'final', amt, (memo or '')[:200], 'queued', json.dumps(payload or {}), who, now_iso(), now_iso()))
    add_event(conn, ticket_id, 'invoice', 'Invoice draft queued for $%.2f. Nothing was sent.' % amt, who)
    return conn.execute('SELECT * FROM qbo_drafts WHERE id=?', (cur.lastrowid,)).fetchone(), None


def tickets_for_site(conn, company_id=None, site_id=None, customer=None, address=None):
    q = 'SELECT * FROM service_tickets WHERE 1=1'
    args = []
    if site_id:
        q += ' AND site_id=?'; args.append(site_id)
    elif company_id:
        q += ' AND company_id=?'; args.append(company_id)
        if address:
            q += ' AND lower(trim(IFNULL(address,\'\')))=?'
            args.append(ckey(address))
    elif customer:
        q += ' AND lower(trim(IFNULL(customer,\'\')))=?'
        args.append(ckey(customer))
        if address:
            q += ' AND lower(trim(IFNULL(address,\'\')))=?'
            args.append(ckey(address))
    else:
        return []
    q += ' ORDER BY id DESC'
    return list(conn.execute(q, args))


def open_tickets(conn):
    return list(conn.execute(
        "SELECT * FROM service_tickets WHERE status IN ('new','scheduled','in_progress','waiting_parts') ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, id DESC"))


def unscheduled_tickets(conn, sub=None):
    q = "SELECT * FROM service_tickets WHERE status IN ('new','in_progress','waiting_parts') AND IFNULL(scheduled_date,'')='' AND IFNULL(job_id,'')=''"
    args = []
    if sub:
        q += ' AND (IFNULL(assigned_sub,\'\')=\'\' OR lower(trim(assigned_sub))=?)'
        args.append(ckey(sub))
    return list(conn.execute(q + ' ORDER BY id DESC', args))


def register(fo):
    from flask import request, redirect, session, jsonify

    app = fo.app
    h = fo.h
    money = fo.money

    def office_only():
        if not fo.me():
            return redirect('/login')
        if not fo.is_office():
            return redirect('/')
        return None

    def page(title, body, back=None, kicker='Ops', actions='', msg=''):
        session['fo_area'] = 'ops'
        act = actions or ''
        if back:
            act = ('<a class="plat-btn plat-btn-ghost" href="%s">Back</a>' % h(back)) + act
        return fo.ops_page(title,
            '<div class="plat-head"><div class="plat-head-text">'
            + ('<div class="plat-kicker">%s</div>' % h(kicker) if kicker else '')
            + '<h1 class="plat-title">%s<span class="plat-dot">.</span></h1></div>' % h(title)
            + ('<div class="plat-actions">%s</div>' % act if act else '')
            + '</div>'
            + (('<div class="plat-msg">%s</div>' % h(msg)) if msg else '')
            + body, back=False)

    def ticket_tray_items(conn, sub_f=None):
        items = []
        for r in unscheduled_tickets(conn, sub_f or None):
            items.append({
                'id': 'ticket:%d' % r['id'],
                'customer': r['customer'] or r['number'],
                'title': (r['issue'] or r['number'])[:80],
                'type': 'Ticket',
                'tech': r['assigned_sub'] or '',
                'ticket_id': r['id'],
            })
        return items

    fo.ticket_tray_items = ticket_tray_items

    def ticket_place_on_board(conn, ticket_id, day, sub, start='08:00', end='16:00', who=''):
        row, err = assign_and_schedule(conn, ticket_id, sub, day, start, end, who)
        if err:
            return None, err
        job, err = convert_to_job(fo, conn, ticket_id, who)
        if err:
            return None, err
        try:
            import fieldops_ops as _ops
            _ops.upsert_slot(conn, job['id'], day, sub, start or '08:00', end or '16:00')
        except Exception:
            fo.app.logger.exception('ticket slot failed')
        return job, None

    fo.ticket_place_on_board = ticket_place_on_board

    def tickets_today(conn, day):
        rows = open_tickets(conn)
        if not rows:
            return []
        n = len(rows)
        urgent = sum(1 for r in rows if r['priority'] == 'urgent' or r['status'] == 'new')
        detail = ', '.join((r['number'] + ' ' + (r['customer'] or '')) for r in rows[:3])
        icon = fo.ICONS.get('alert') or fo.ICONS.get('note') or '&#128196;'
        return [{'n': n, 'title': 'Service tickets', 'detail': detail,
                 'icon': icon, 'href': '/tickets', 'btn': 'Open', 'tone': 'red' if urgent else ''}]

    fo.tickets_today_actions = tickets_today

    def tickets_site_html(company_id=None, site_id=None, customer=None, address=None):
        conn = fo.get_db()
        rows = tickets_for_site(conn, company_id, site_id, customer, address)
        conn.close()
        if not rows:
            return '<div class="plat-panel"><h2>Service tickets</h2><div class="muted">No service tickets at this site yet.</div></div>'
        html = '<div class="plat-panel"><h2>Service ticket history</h2><ul class="plat-list">'
        for r in rows:
            html += (
                '<li><a href="/tickets/%d"><b>%s</b></a> <span class="plat-pill">%s</span> <span class="plat-pill">%s</span>'
                '<div class="muted">%s</div></li>' % (
                    r['id'], h(r['number']), h(status_label(r['status'])), h(pri_label(r['priority'])),
                    h((r['issue'] or '')[:180])))
        html += '</ul></div>'
        return html

    fo.tickets_site_html = tickets_site_html

    @app.route('/tickets')
    def tickets_list():
        gate = office_only()
        if gate:
            return gate
        show = request.args.get('status') or 'open'
        conn = fo.get_db()
        if show == 'all':
            rows = list(conn.execute('SELECT * FROM service_tickets ORDER BY id DESC'))
        elif show in STATUS_SET:
            rows = list(conn.execute('SELECT * FROM service_tickets WHERE status=? ORDER BY id DESC', (show,)))
        else:
            rows = open_tickets(conn)
        conn.close()
        body = '<div class="plat-pills">'
        for key, lab in [('open', 'Open')] + STATUSES + [('all', 'All')]:
            on = ' on' if (show == key or (key == 'open' and show not in STATUS_SET and show != 'all')) else ''
            href = '/tickets' if key == 'open' else '/tickets?status=' + key
            body += '<a class="plat-pill-link%s" href="%s">%s</a>' % (on, href, lab)
        body += '</div>'
        if rows:
            body += '<ul class="plat-list">'
            for r in rows:
                body += (
                    '<li><a href="/tickets/%d"><b>%s</b> %s</a> <span class="plat-pill">%s</span> <span class="plat-pill">%s</span>'
                    '<div class="muted">%s%s</div></li>' % (
                        r['id'], h(r['number']), h(r['customer'] or ''),
                        h(status_label(r['status'])), h(pri_label(r['priority'])),
                        h((r['issue'] or '')[:160]),
                        (' · ' + h(r['assigned_sub'])) if r['assigned_sub'] else ''))
            body += '</ul>'
        else:
            body += '<div class="muted">No tickets in this list. New calls, portal requests and plan visits land here, separate from project jobs.</div>'
        return page('Service tickets', body, actions='<a class="plat-btn plat-btn-gold" href="/tickets/new">+ Ticket</a>')

    def ticket_form(companies, sites, equipment, vals=None):
        vals = vals or {}
        co_opts = ''.join(
            '<option value="%d"%s>%s</option>' % (c['id'], ' selected' if str(vals.get('company_id') or '') == str(c['id']) else '', h(c['name']))
            for c in companies)
        site_opts = ''.join(
            '<option value="%d" data-company="%s"%s>%s</option>' % (
                s['id'], s['company_id'], ' selected' if str(vals.get('site_id') or '') == str(s['id']) else '',
                h((s['name'] or s['address'] or 'Site')))
            for s in sites)
        eq_opts = ''.join(
            '<option value="%d"%s>%s</option>' % (
                e['id'], ' selected' if str(vals.get('equipment_id') or '') == str(e['id']) else '',
                h(' · '.join(x for x in (e.get('device'), e.get('model'), e.get('site')) if x)))
            for e in equipment)
        pri = ''.join('<option value="%s"%s>%s</option>' % (k, ' selected' if (vals.get('priority') or 'normal') == k else '', lab) for k, lab in PRIORITIES)
        src = ''.join('<option value="%s"%s>%s</option>' % (k, ' selected' if (vals.get('source') or 'office') == k else '', lab) for k, lab in SOURCES if k != 'plan')
        return (
            '<form class="plat-form plat-panel" method="post">'
            '<div class="plat-row"><div><label>Company</label><select name="company_id"><option value="">None</option>%s</select></div>'
            '<div><label>Site</label><select name="site_id"><option value="">None</option>%s</select></div></div>'
            '<div class="plat-row"><div><label>Customer name</label><input name="customer" value="%s" maxlength="200"></div>'
            '<div><label>Phone</label><input name="phone" value="%s" maxlength="40"></div></div>'
            '<label>Address</label><input name="address" value="%s" maxlength="300">'
            '<div class="plat-row"><div><label>Priority</label><select name="priority">%s</select></div>'
            '<div><label>Source</label><select name="source">%s</select></div>'
            '<div><label>Installed device</label><select name="equipment_id"><option value="">None</option>%s</select></div></div>'
            '<label>Issue</label><textarea name="issue" rows="4" required placeholder="What is wrong, and who called">%s</textarea>'
            '<button class="plat-btn plat-btn-gold" type="submit">Save ticket</button></form>'
        ) % (co_opts, site_opts, h(vals.get('customer') or ''), h(vals.get('phone') or ''),
             h(vals.get('address') or ''), pri, src, eq_opts, h(vals.get('issue') or ''))

    @app.route('/tickets/new', methods=['GET', 'POST'])
    def tickets_new():
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        msg = ''
        if request.method == 'POST':
            data = {
                'company_id': int(request.form['company_id']) if (request.form.get('company_id') or '').isdigit() else None,
                'site_id': int(request.form['site_id']) if (request.form.get('site_id') or '').isdigit() else None,
                'customer': request.form.get('customer'),
                'phone': request.form.get('phone'),
                'address': request.form.get('address'),
                'priority': request.form.get('priority'),
                'issue': request.form.get('issue'),
                'equipment_id': int(request.form['equipment_id']) if (request.form.get('equipment_id') or '').isdigit() else None,
            }
            if data['equipment_id']:
                eq = conn.execute('SELECT * FROM equipment WHERE id=?', (data['equipment_id'],)).fetchone()
                if eq:
                    data['device_label'] = ' '.join(x for x in (eq['device'], eq['model']) if x)
            row, err = create_ticket(conn, data, fo.me(), request.form.get('source') or 'office')
            if err:
                msg = err
            else:
                conn.commit(); conn.close()
                return redirect('/tickets/%d' % row['id'])
        companies = list(conn.execute('SELECT id,name FROM companies WHERE IFNULL(archived,0)=0 ORDER BY name'))
        sites = list(conn.execute('SELECT id,company_id,name,address FROM sites ORDER BY name'))
        equipment = [dict(r) for r in conn.execute('SELECT id,client,site,device,model FROM equipment ORDER BY id DESC LIMIT 200')]
        pre = {'company_id': request.args.get('company_id'), 'site_id': request.args.get('site_id'),
               'customer': request.args.get('customer'), 'address': request.args.get('address'),
               'source': request.args.get('source') or 'office'}
        body = ticket_form(companies, sites, equipment, pre)
        conn.close()
        return page('New service ticket', body, back='/tickets', msg=msg)

    @app.route('/tickets/<int:tid>', methods=['GET', 'POST'])
    def tickets_detail(tid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM service_tickets WHERE id=?', (tid,)).fetchone()
        if not row:
            conn.close(); return redirect('/tickets')
        msg = ''
        if request.method == 'POST':
            a = request.form.get('action')
            if a == 'status':
                err = set_status(conn, tid, request.form.get('status'), fo.me(), request.form.get('note') or '')
                msg = err or 'Status updated.'
            elif a == 'assign':
                _, err = assign_and_schedule(conn, tid, request.form.get('sub'), request.form.get('day'),
                                             request.form.get('start'), request.form.get('end'), fo.me())
                msg = err or 'Saved.'
            elif a == 'note':
                body = (request.form.get('body') or '').strip()
                if body:
                    add_event(conn, tid, 'note', body, fo.me()); msg = 'Note added.'
                else:
                    msg = 'Write a note first.'
            elif a == 'to_job':
                job, err = convert_to_job(fo, conn, tid, fo.me())
                msg = err or ('Job %s is ready.' % job['id'])
            elif a == 'invoice':
                _, err = queue_invoice_draft(fo, conn, tid, request.form.get('amount'), request.form.get('memo') or '', fo.me())
                msg = err or 'Invoice draft queued. Nothing was sent.'
            if not msg.startswith('Write') and 'not found' not in (msg or '') and 'Enter' not in (msg or '') and 'Unknown' not in (msg or '') and 'Pick' not in (msg or ''):
                conn.commit()
            elif err if False else None:
                pass
            else:
                # commit successful mutations; validation errors still commit nothing new
                if msg in ('Status updated.', 'Saved.', 'Note added.') or (msg or '').startswith('Job ') or 'queued' in (msg or ''):
                    conn.commit()
            row = conn.execute('SELECT * FROM service_tickets WHERE id=?', (tid,)).fetchone()
        events = list(conn.execute('SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY id DESC', (tid,)))
        testers = []
        try:
            testers = list(fo.sub_logins())
        except Exception:
            testers = []
        # assigned names plus field logins
        seen = {ckey(n) for n in testers}
        for r in conn.execute("SELECT DISTINCT TRIM(tech) t FROM jobs WHERE IFNULL(TRIM(tech),'')!=''"):
            if ckey(r['t']) not in seen:
                testers.append(fo.nice_name(r['t'])); seen.add(ckey(r['t']))
        conn.close()
        sub_opts = ''.join('<option value="%s"%s>%s</option>' % (h(n), ' selected' if ckey(n) == ckey(row['assigned_sub']) else '', h(n)) for n in testers)
        st_opts = ''.join('<option value="%s"%s>%s</option>' % (k, ' selected' if row['status'] == k else '', lab) for k, lab in STATUSES)
        body = '<div class="plat-panel"><div class="muted">%s · %s · from %s</div><p>%s</p>' % (
            h(pri_label(row['priority'])), h(status_label(row['status'])),
            h(dict(SOURCES).get(row['source'], row['source'] or '')), h(row['issue'] or ''))
        if row['customer'] or row['address']:
            body += '<div class="muted">%s%s</div>' % (h(row['customer'] or ''), (' · ' + h(row['address'])) if row['address'] else '')
        if row['device_label']:
            body += '<div class="muted">Device: %s</div>' % h(row['device_label'])
        if row['job_id']:
            body += '<p><a class="plat-btn plat-btn-ghost" href="/?job=%s">Open job %s</a></p>' % (h(row['job_id']), h(row['job_id']))
        body += '</div>'
        body += (
            '<div class="plat-panel"><h2>Status</h2><form class="plat-form" method="post">'
            '<input type="hidden" name="action" value="status"><select name="status">%s</select>'
            '<input name="note" placeholder="Optional note">'
            '<button class="plat-btn plat-btn-ghost" type="submit">Update</button></form></div>'
        ) % st_opts
        body += (
            '<div class="plat-panel"><h2>Assign and schedule</h2>'
            '<p class="muted">Putting a date and sub on the ticket also creates a job and drops it on the schedule lanes.</p>'
            '<form class="plat-form" method="post"><input type="hidden" name="action" value="assign">'
            '<div class="plat-row"><div><label>Sub</label><select name="sub"><option value="">Pick a sub</option>%s</select></div>'
            '<div><label>Day</label><input type="date" name="day" value="%s"></div>'
            '<div><label>Start</label><input name="start" placeholder="08:00" value="%s"></div>'
            '<div><label>End</label><input name="end" placeholder="16:00" value="%s"></div></div>'
            '<button class="plat-btn plat-btn-gold" type="submit">Save</button></form></div>'
        ) % (sub_opts, h(row['scheduled_date'] or ''), h(row['start_time'] or ''), h(row['end_time'] or ''))
        if not row['job_id']:
            body += '<form method="post" class="plat-panel"><input type="hidden" name="action" value="to_job"><h2>Convert to a job</h2><p class="muted">Makes a Service job from this ticket. The ticket stays linked.</p><button class="plat-btn plat-btn-ghost" type="submit">Convert to job</button></form>'
        body += (
            '<form method="post" class="plat-panel"><input type="hidden" name="action" value="invoice"><h2>Invoice draft</h2>'
            '<p class="muted">Queues a QuickBooks draft only. Nothing is sent to the client.</p>'
            '<div class="plat-row"><div><label>Amount $</label><input name="amount" type="number" step="0.01" min="0" required></div>'
            '<div><label>Memo</label><input name="memo" maxlength="200"></div></div>'
            '<button class="plat-btn plat-btn-ghost" type="submit">Queue draft</button></form>'
        )
        body += '<div class="plat-panel"><h2>History</h2>'
        body += '<form class="plat-form" method="post"><input type="hidden" name="action" value="note"><label>Add a note</label><textarea name="body" rows="2" required></textarea><button class="plat-btn plat-btn-ghost" type="submit">Add note</button></form>'
        if events:
            body += '<ul class="plat-list">'
            for ev in events:
                when = (ev['created_at'] or '')[:16].replace('T', ' ')
                body += '<li><span class="plat-pill">%s</span> <span class="muted">%s%s</span><div>%s</div></li>' % (
                    h(ev['kind'] or 'note'), h(when), (' · ' + h(ev['created_by'])) if ev['created_by'] else '', h(ev['body'] or ''))
            body += '</ul>'
        body += '</div>'
        return page(row['number'], body, back='/tickets', kicker='Service ticket', msg=msg)

