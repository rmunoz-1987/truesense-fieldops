"""Phase 4 Ops: change orders, invoice drafts, costing, slots, timesheets, and the schedule board.

Drafts only. Nothing is sent to clients or vendors. Money is office-only.
The schedule follows the Consolidator Ops board in structure (crew lanes, day columns,
time on the card, earlier/today/later, unscheduled tray) with True Sense branding.
"""
from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import datetime, date, timedelta, time

KINDS = ('deposit', 'progress', 'final')
CO_STATUS = ('draft', 'sent', 'accepted')
TS_STATUS = ('open', 'submitted', 'approved')
DAY_START, DAY_END = 6, 20  # hours shown on the day grid
DEFAULT_START, DEFAULT_END = '08:00', '16:00'
SUB_COLORS = ['#3d4a16', '#5f5b16', '#2f3d1a', '#4a3d12', '#3a4a2a', '#6b5a16', '#2c3a18', '#4d4520']


def now_iso():
    return datetime.utcnow().isoformat()


def ckey(name):
    return ' '.join((name or '').split()).lower()


def parse_day(s):
    try:
        return datetime.strptime((s or '')[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def parse_hhmm(s, fallback=DEFAULT_START):
    s = (s or '').strip()
    for fmt in ('%H:%M', '%H:%M:%S', '%I:%M %p', '%I:%M%p'):
        try:
            return datetime.strptime(s.replace('.', ''), fmt).strftime('%H:%M')
        except ValueError:
            continue
    return fallback


def nice_time(s):
    try:
        t = datetime.strptime(parse_hhmm(s, '00:00'), '%H:%M')
        return t.strftime('%I:%M %p').lstrip('0')
    except ValueError:
        return s or ''


def week_monday(d):
    return d - timedelta(days=d.weekday())


def color_for(sub):
    k = ckey(sub)
    if not k:
        return '#5f5b16'
    return SUB_COLORS[sum(ord(ch) for ch in k) % len(SUB_COLORS)]


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS job_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        day TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        sub TEXT,
        created_at TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_job_slots_day ON job_slots(day)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_job_slots_job ON job_slots(job_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS change_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        title TEXT,
        notes TEXT,
        lines_json TEXT,
        total REAL DEFAULT 0,
        status TEXT DEFAULT 'draft',
        token TEXT UNIQUE,
        created_by TEXT,
        created_at TEXT,
        updated_at TEXT,
        accepted_at TEXT,
        accepted_name TEXT,
        signature TEXT,
        accepted_ip TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS qbo_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'final',
        amount REAL,
        memo TEXT,
        status TEXT DEFAULT 'draft',
        payload TEXT,
        created_by TEXT,
        created_at TEXT,
        updated_at TEXT,
        qbo_invoice_id TEXT,
        doc_number TEXT,
        note TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_qbo_drafts_job ON qbo_drafts(job_id, kind)')
    conn.execute('''CREATE TABLE IF NOT EXISTS timesheet_weeks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sub TEXT NOT NULL,
        week_start TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        approved_by TEXT,
        approved_at TEXT,
        notes TEXT,
        UNIQUE(sub, week_start)
    )''')


def freeze_line(name, qty, sell):
    try:
        qty = float(qty or 0)
    except (TypeError, ValueError):
        qty = 0
    try:
        sell = float(sell or 0)
    except (TypeError, ValueError):
        sell = 0
    return {'name': (name or '').strip()[:200], 'qty': qty, 'sell': sell, 'amount': round(qty * sell, 2)}


def co_total(lines):
    return round(sum((ln.get('amount') or 0) for ln in (lines or [])), 2)


def accepted_co_total(conn, job_id):
    r = conn.execute("SELECT SUM(total) s FROM change_orders WHERE job_id=? AND status='accepted'", (job_id,)).fetchone()
    return round((r['s'] if r and r['s'] else 0) or 0, 2)


def job_sale(conn, row):
    base = 0.0
    try:
        base = float(row['sale_amount'] or 0)
    except (TypeError, ValueError):
        base = 0.0
    return round(base, 2)


def sub_on_job(conn, job_id, sub_key):
    if not sub_key:
        return False
    row = conn.execute('SELECT tech FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row and ckey(row['tech']) == sub_key:
        return True
    n = conn.execute('SELECT 1 FROM job_slots WHERE job_id=? AND lower(trim(sub))=?', (job_id, sub_key)).fetchone()
    return bool(n)


def slots_in_range(conn, start, end, sub=None):
    q = 'SELECT * FROM job_slots WHERE day>=? AND day<=?'
    args = [start, end]
    if sub:
        q += ' AND lower(trim(sub))=?'
        args.append(ckey(sub))
    rows = list(conn.execute(q + ' ORDER BY day, start_time, id', args))
    have = {(r['job_id'], r['day']) for r in rows}
    # Synthetic slots for jobs that have a date and sub but no slot rows yet.
    jq = "SELECT * FROM jobs WHERE status!='completed' AND scheduled_date>=? AND scheduled_date<=? AND IFNULL(scheduled_date,'')!=''"
    jargs = [start, end]
    if sub:
        jq += ' AND lower(trim(tech))=?'
        jargs.append(ckey(sub))
    extra = []
    for j in conn.execute(jq, jargs):
        if (j['id'], j['scheduled_date']) in have:
            continue
        if not j['tech']:
            continue
        extra.append({
            'id': 0, 'job_id': j['id'], 'day': j['scheduled_date'],
            'start_time': DEFAULT_START, 'end_time': DEFAULT_END, 'sub': j['tech'],
            'synthetic': 1,
        })
    return list(rows) + extra


def sync_job_from_slots(conn, job_id):
    slots = list(conn.execute('SELECT * FROM job_slots WHERE job_id=? ORDER BY day, start_time, id', (job_id,)))
    if not slots:
        conn.execute('UPDATE jobs SET scheduled_date=NULL WHERE id=?', (job_id,))
        return
    first = slots[0]
    conn.execute('UPDATE jobs SET scheduled_date=?, tech=? WHERE id=?', (first['day'], first['sub'] or None, job_id))


def upsert_slot(conn, job_id, day, sub, start=DEFAULT_START, end=DEFAULT_END, slot_id=None):
    job = conn.execute('SELECT id FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        return None, 'Job not found.'
    day_s = parse_day(day)
    if not day_s:
        return None, 'Pick a valid day.'
    sub = ' '.join((sub or '').split()).title()[:60]
    if not sub:
        return None, 'Pick a sub.'
    start = parse_hhmm(start, DEFAULT_START)
    end = parse_hhmm(end, DEFAULT_END)
    if end <= start:
        return None, 'End time must be after start time.'
    if slot_id:
        row = conn.execute('SELECT * FROM job_slots WHERE id=?', (slot_id,)).fetchone()
        if not row:
            return None, 'Slot not found.'
        conn.execute('UPDATE job_slots SET day=?, start_time=?, end_time=?, sub=? WHERE id=?',
                     (day_s.isoformat(), start, end, sub, slot_id))
        sid = slot_id
    else:
        cur = conn.execute(
            'INSERT INTO job_slots (job_id,day,start_time,end_time,sub,created_at) VALUES (?,?,?,?,?,?)',
            (job_id, day_s.isoformat(), start, end, sub, now_iso()))
        sid = cur.lastrowid
    sync_job_from_slots(conn, job_id)
    return conn.execute('SELECT * FROM job_slots WHERE id=?', (sid,)).fetchone(), None


def delete_slot(conn, slot_id):
    row = conn.execute('SELECT * FROM job_slots WHERE id=?', (slot_id,)).fetchone()
    if not row:
        return 'Slot not found.'
    conn.execute('DELETE FROM job_slots WHERE id=?', (slot_id,))
    sync_job_from_slots(conn, row['job_id'])
    return None


def place_job(conn, job_id, day, sub, start=DEFAULT_START, end=DEFAULT_END, slot_id=None, replace_day=False):
    """Move one slot, or create a first slot from the unscheduled tray."""
    if slot_id:
        return upsert_slot(conn, job_id, day, sub, start, end, slot_id)
    existing = list(conn.execute('SELECT * FROM job_slots WHERE job_id=?', (job_id,)))
    if len(existing) == 1 and not replace_day:
        return upsert_slot(conn, job_id, day, sub, start or existing[0]['start_time'], end or existing[0]['end_time'], existing[0]['id'])
    return upsert_slot(conn, job_id, day, sub, start, end, None)


def ensure_co_token(conn, coid):
    row = conn.execute('SELECT * FROM change_orders WHERE id=?', (coid,)).fetchone()
    if not row:
        return None
    if row['token']:
        if row['status'] == 'draft':
            conn.execute("UPDATE change_orders SET status='sent', updated_at=? WHERE id=?", (now_iso(), coid))
            return conn.execute('SELECT * FROM change_orders WHERE id=?', (coid,)).fetchone()
        return row
    token = secrets.token_urlsafe(24)
    conn.execute(
        "UPDATE change_orders SET token=?, status=CASE WHEN status='draft' THEN 'sent' ELSE status END, updated_at=? WHERE id=?",
        (token, now_iso(), coid))
    return conn.execute('SELECT * FROM change_orders WHERE id=?', (coid,)).fetchone()


def save_change_order(conn, data, coid=None, created_by=''):
    job_id = (data.get('job_id') or '').strip()
    if not conn.execute('SELECT id FROM jobs WHERE id=?', (job_id,)).fetchone():
        return None, 'Job not found.'
    title = (data.get('title') or 'Change order').strip()[:200]
    notes = (data.get('notes') or '').strip()[:2000]
    lines = []
    for ln in data.get('lines') or []:
        name = (ln.get('name') or '').strip()
        if not name:
            continue
        lines.append(freeze_line(name, ln.get('qty') or 1, ln.get('sell') or 0))
    if not lines:
        return None, 'Add at least one line.'
    tot = co_total(lines)
    when = now_iso()
    if coid:
        row = conn.execute('SELECT * FROM change_orders WHERE id=?', (coid,)).fetchone()
        if not row:
            return None, 'Change order not found.'
        if row['status'] == 'accepted':
            return None, 'This change order is already accepted.'
        conn.execute(
            'UPDATE change_orders SET title=?, notes=?, lines_json=?, total=?, updated_at=? WHERE id=?',
            (title, notes, json.dumps(lines), tot, when, coid))
        return conn.execute('SELECT * FROM change_orders WHERE id=?', (coid,)).fetchone(), None
    cur = conn.execute(
        '''INSERT INTO change_orders (job_id,title,notes,lines_json,total,status,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,'draft',?,?,?)''',
        (job_id, title, notes, json.dumps(lines), tot, created_by, when, when))
    return conn.execute('SELECT * FROM change_orders WHERE id=?', (cur.lastrowid,)).fetchone(), None


def accept_change_order(conn, row, name, sig, ip, fo=None):
    if row['status'] == 'accepted':
        return row
    when = now_iso()
    conn.execute(
        "UPDATE change_orders SET status='accepted', accepted_at=?, accepted_name=?, signature=?, accepted_ip=?, updated_at=? WHERE id=?",
        (when, name, sig, ip, when, row['id']))
    job = conn.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
    if job:
        sale = job_sale(conn, job) + (row['total'] or 0)
        # Keep estimate_json if present; append a change-order note in scope-safe fields.
        notes = (job['private_notes'] or '')
        extra = 'Accepted change order #%d: %s (%0.2f).' % (row['id'], row['title'] or 'Change order', row['total'] or 0)
        notes = (notes + '\n' + extra).strip()[:4000]
        conn.execute('UPDATE jobs SET sale_amount=?, private_notes=? WHERE id=?', (sale, notes, job['id']))
        try:
            est = json.loads(job['estimate_json'] or 'null')
        except Exception:
            est = None
        if isinstance(est, dict):
            items = list(est.get('items') or [])
            for ln in json.loads(row['lines_json'] or '[]'):
                items.append(ln.get('name') or 'Change order line')
            est['items'] = items
            try:
                est['price'] = float(est.get('price') or 0) + (row['total'] or 0)
            except (TypeError, ValueError):
                est['price'] = row['total'] or 0
            conn.execute('UPDATE jobs SET estimate_json=? WHERE id=?', (json.dumps(est), job['id']))
    return conn.execute('SELECT * FROM change_orders WHERE id=?', (row['id'],)).fetchone()


def punch_minutes_in_week(conn, sub, week_start):
    start = week_start
    end = (parse_day(week_start) + timedelta(days=7)).isoformat() if parse_day(week_start) else week_start
    key = ckey(sub)
    total = 0
    for r in conn.execute(
        "SELECT minutes, in_at, out_at FROM time_punches WHERE lower(trim(sub))=? AND in_at>=? AND in_at<?",
        (key, start, end)):
        m = r['minutes'] or 0
        if not m and r['in_at'] and r['out_at']:
            try:
                a = datetime.fromisoformat(r['in_at'].replace('Z', ''))
                b = datetime.fromisoformat(r['out_at'].replace('Z', ''))
                m = max(0, int((b - a).total_seconds() // 60))
            except Exception:
                m = 0
        total += m
    return total


def register(fo):
    from flask import request, redirect, session, jsonify, render_template_string, Response

    app = fo.app
    h = fo.h
    money = fo.money

    def office_only():
        if not fo.me():
            return redirect('/login')
        if not fo.is_office():
            return redirect('/')
        return None

    def page(title, body, back=None, kicker='Ops', actions='', msg='', area='ops'):
        session['fo_area'] = area
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

    def job_label(row):
        return (row['customer'] or row['title'] or row['id'] or 'Job')

    def techs_list(conn, jobs=None):
        names, seen = [], set()
        def add(n):
            n = fo.nice_name(n)
            k = ckey(n)
            if n and k not in seen:
                names.append(n); seen.add(k)
        for r in conn.execute("SELECT DISTINCT TRIM(tech) t FROM jobs WHERE status!='completed' AND IFNULL(TRIM(tech),'')!=''"):
            add(r['t'])
        for r in conn.execute("SELECT DISTINCT TRIM(sub) t FROM job_slots WHERE IFNULL(TRIM(sub),'')!=''"):
            add(r['t'])
        if jobs:
            for j in jobs:
                add(j.get('tech'))
        return names

    def fo_ops_sub_on_job(job_id, sub_key):
        conn = fo.get_db()
        try:
            return sub_on_job(conn, job_id, sub_key)
        finally:
            conn.close()

    fo.ops_sub_on_job = fo_ops_sub_on_job

    # ---------- Schedule board ----------
    def schedule_board(default_view='week'):
        gate = office_only()
        if gate:
            return gate
        session['fo_area'] = 'ops'
        view = (request.args.get('view') or default_view).lower()
        if view not in ('day', 'week', 'fortnight', 'month'):
            view = default_view
        t = fo.today_local()
        raw = request.args.get('start') or ''
        start = parse_day(raw) or t
        sub_f = (request.args.get('sub') or '').strip()
        if view == 'day':
            days_n, period_start = 1, start
        elif view == 'week':
            period_start = week_monday(start)
            days_n = 7
        elif view == 'fortnight':
            period_start = start
            days_n = 14
        else:
            period_start = start.replace(day=1)
            if period_start.month == 12:
                last = period_start.replace(year=period_start.year + 1, month=1, day=1) - timedelta(days=1)
            else:
                last = period_start.replace(month=period_start.month + 1, day=1) - timedelta(days=1)
            days_n = last.day
        period_end = period_start + timedelta(days=days_n - 1)
        if view == 'week':
            earlier = period_start - timedelta(days=7)
            later = period_start + timedelta(days=7)
        elif view == 'fortnight':
            earlier = period_start - timedelta(days=14)
            later = period_start + timedelta(days=14)
        elif view == 'month':
            earlier = (period_start - timedelta(days=1)).replace(day=1)
            later = period_end + timedelta(days=1)
        else:
            earlier = period_start - timedelta(days=1)
            later = period_start + timedelta(days=1)
        conn = fo.get_db()
        techs = techs_list(conn)
        if sub_f:
            techs = [x for x in techs if ckey(x) == ckey(sub_f)] or techs
        slot_rows = slots_in_range(conn, period_start.isoformat(), period_end.isoformat(), sub_f or None)
        job_ids = {s['job_id'] if not isinstance(s, dict) else s['job_id'] for s in slot_rows}
        jobs = {r['id']: r for r in conn.execute('SELECT * FROM jobs') if r['id'] in job_ids or (r['status'] != 'completed')}
        unsched = [r for r in jobs.values()
                   if r['status'] != 'completed'
                   and not r['scheduled_date']
                   and not conn.execute('SELECT 1 FROM job_slots WHERE job_id=?', (r['id'],)).fetchone()]
        if sub_f:
            unsched = [r for r in unsched if not r['tech'] or ckey(r['tech']) == ckey(sub_f)]
        co = fo.sub_companies(conn)
        payload_slots = []
        for s in slot_rows:
            if not isinstance(s, dict):
                s = dict(s)
            j = jobs.get(s['job_id'])
            if not j:
                continue
            payload_slots.append({
                'id': s.get('id') or 0,
                'job_id': s['job_id'],
                'day': s['day'],
                'start': s.get('start_time') or DEFAULT_START,
                'end': s.get('end_time') or DEFAULT_END,
                'sub': s.get('sub') or j['tech'] or '',
                'synthetic': int(s.get('synthetic') or 0),
                'customer': j['customer'] or '',
                'title': j['title'] or '',
                'type': j['job_type'] or 'Job',
                'status': j['status'] or '',
                'color': color_for(s.get('sub') or j['tech'] or ''),
            })
        tray = [{'id': r['id'], 'customer': r['customer'] or '', 'title': r['title'] or '',
                 'type': r['job_type'] or 'Job', 'tech': r['tech'] or ''} for r in unsched]
        extra_tray = getattr(fo, 'ticket_tray_items', None)
        if callable(extra_tray):
            try:
                tray.extend(extra_tray(conn, sub_f or None) or [])
            except Exception:
                fo.app.logger.exception('ticket tray failed')
        conn.close()
        days = []
        for i in range(days_n):
            d = period_start + timedelta(days=i)
            days.append({'iso': d.isoformat(), 'dow': d.strftime('%a').upper(),
                         'num': d.day, 'mon': d.strftime('%b').upper(), 'today': d == t})
        # month grid padding
        month_pad = period_start.weekday() if view == 'month' else 0
        if period_start == period_end:
            range_lab = period_start.strftime('%b %-d, %Y')
        else:
            range_lab = '%s to %s' % (period_start.strftime('%b %-d'), period_end.strftime('%b %-d, %Y'))
        views = (('day', 'Day'), ('week', 'Week'), ('fortnight', 'Fortnight'), ('month', 'Month'))
        def href(v=view, st=None, sb=sub_f):
            q = {'view': v, 'start': (st or period_start).isoformat()}
            if sb:
                q['sub'] = sb
            from urllib.parse import urlencode
            path = '/calendar' if request.path.startswith('/calendar') else '/schedule'
            return path + '?' + urlencode(q)
        conn = fo.get_db(); all_techs = techs_list(conn); conn.close()
        tech_opts = '<option value="">All subs</option>' + ''.join(
            '<option value="%s"%s>%s</option>' % (h(n), ' selected' if ckey(n) == ckey(sub_f) else '', h(n))
            for n in all_techs)
        body = SCHED_CSS + '''
<p class="sked-lede">Where the openings are. Click an empty day to book into it. Drag a job to move it, or drag the bottom edge in Day view to change the hours. On a phone, tap the job, then tap the box where it goes. Times are in America/Chicago.</p>
<div class="sked-toolbar">
  <div class="sked-nav">
    <a class="plat-btn plat-btn-ghost" href="%(earlier)s">Earlier</a>
    <a class="plat-btn plat-btn-ghost" href="%(today)s">Today</a>
    <a class="plat-btn plat-btn-ghost" href="%(later)s">Later</a>
    <span class="sked-range">%(range)s</span>
  </div>
  <div class="sked-tools">
  <form class="sked-filter" method="get">
    <input type="hidden" name="view" value="%(view)s">
    <input type="hidden" name="start" value="%(start)s">
    <label>Sub <select name="sub" onchange="this.form.submit()">%(opts)s</select></label>
  </form>
  <div class="sked-views">%(view_btns)s</div>
  </div>
</div>
<div class="sked-wrap" data-view="%(view)s">
  <div id="sked-board" class="sked-board"></div>
  <aside class="sked-tray" id="sked-tray"><h2>Unscheduled</h2><div id="sked-tray-list"></div>
    <p class="muted">Jobs with no date yet. Drag one onto the board, or tap it and then tap a day.</p>
  </aside>
</div>
<div id="sked-bar" class="sked-bar hidden"></div>
<div id="sked-pop" class="sked-pop hidden"></div>
<script>
const SKED = %(json)s;
</script>
''' % {
            'earlier': h(href(st=earlier)),
            'today': h(href(st=t)),
            'later': h(href(st=later)),
            'range': h(range_lab),
            'view': h(view),
            'start': h(period_start.isoformat()),
            'opts': tech_opts,
            'view_btns': ''.join(
                '<a class="plat-btn %s" href="%s">%s</a>' % (
                    'plat-btn-gold' if v == view else 'plat-btn-ghost', h(href(v=v)), lab)
                for v, lab in views),
            'json': json.dumps({
                'view': view, 'days': days, 'techs': techs, 'slots': payload_slots,
                'tray': tray, 'monthPad': month_pad, 'dayStart': DAY_START, 'dayEnd': DAY_END,
                'companies': co, 'start': period_start.isoformat(),
            }),
        }
        body += SCHED_JS
        title = 'Calendar' if request.path.startswith('/calendar') else 'Schedule'
        return page(title, body, kicker='Ops', actions='<a class="plat-btn plat-btn-ghost" href="/new-job">+ Job</a>')

    fo.ops_schedule_page = lambda: schedule_board('week')
    fo.ops_calendar_page = lambda: schedule_board('month')

    @app.route('/api/schedule/slots', methods=['GET', 'POST', 'DELETE'])
    def api_slots():
        gate = office_only()
        if gate:
            return jsonify({'error': 'Office only'}), 403
        conn = fo.get_db()
        if request.method == 'GET':
            start = request.args.get('start') or fo.today_local().isoformat()
            end = request.args.get('end') or start
            rows = [dict(r) if not isinstance(r, dict) else r for r in slots_in_range(conn, start, end, request.args.get('sub'))]
            conn.close()
            return jsonify(rows)
        d = request.get_json(silent=True) or {}
        if request.method == 'DELETE' or d.get('delete'):
            err = delete_slot(conn, int(d.get('id') or 0))
            if err:
                conn.close(); return jsonify({'error': err}), 400
            conn.commit(); conn.close()
            return jsonify({'ok': True})
        job_id = str(d.get('job_id') or '')
        if job_id.startswith('ticket:') or d.get('ticket_id'):
            try:
                tid = int(d.get('ticket_id') or job_id.split(':',1)[1])
            except (TypeError, ValueError, IndexError):
                conn.close(); return jsonify({'error':'Bad ticket'}),400
            place = getattr(fo, 'ticket_place_on_board', None)
            if not callable(place):
                conn.close(); return jsonify({'error':'Tickets are not available'}),400
            job, err = place(conn, tid, d.get('day'), d.get('sub'), d.get('start') or '08:00', d.get('end') or '16:00', fo.me())
            if err:
                conn.close(); return jsonify({'error': err}),400
            conn.commit()
            warn = None
            cw = getattr(fo, 'compliance_warning', None)
            if callable(cw):
                warn = cw(conn, d.get('sub'))
            conn.close()
            out = {'ok': True, 'job_id': job['id'] if job else None}
            if warn:
                out['warning'] = warn
            return jsonify(out)
        slot_id = d.get('id') or None
        if slot_id:
            try:
                slot_id = int(slot_id)
            except (TypeError, ValueError):
                slot_id = None
            if slot_id == 0:
                slot_id = None
                # materialize a synthetic slot by creating one
        row, err = place_job(conn, job_id, d.get('day'), d.get('sub'), d.get('start') or DEFAULT_START,
                             d.get('end') or DEFAULT_END, slot_id)
        if err:
            conn.close(); return jsonify({'error': err}), 400
        # notify like the old schedule API
        old = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        warn = None
        cw = getattr(fo, 'compliance_warning', None)
        if callable(cw):
            warn = cw(conn, d.get('sub') or (old['tech'] if old else ''))
        conn.commit(); conn.close()
        if old:
            try:
                fo.push_to_sub(d.get('sub') or old['tech'], 'Schedule change: ' + (old['title'] or job_id),
                               'Now ' + fo.nice_day(d.get('day')), '/?job=' + job_id)
            except Exception:
                pass
        out = {'ok': True, 'slot': dict(row)}
        if warn:
            out['warning'] = warn
        return jsonify(out)

    # ---------- Change orders ----------
    def lines_from_form():
        names = request.form.getlist('line_name')
        qtys = request.form.getlist('line_qty')
        sells = request.form.getlist('line_sell')
        out = []
        for i, name in enumerate(names):
            out.append({'name': name, 'qty': qtys[i] if i < len(qtys) else 1, 'sell': sells[i] if i < len(sells) else 0})
        return out

    @app.route('/change-orders', methods=['GET'])
    def change_orders_list():
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        rows = list(conn.execute(
            '''SELECT c.*, j.customer, j.title AS job_title FROM change_orders c
               JOIN jobs j ON j.id=c.job_id ORDER BY c.id DESC'''))
        conn.close()
        body = '<div class="plat-panel"><div class="plat-panel-h"><h2>Change orders</h2><a class="plat-btn plat-btn-gold" href="/change-orders/new">+ Change order</a></div>'
        if rows:
            body += '<ul class="plat-list">'
            for r in rows:
                body += (
                    '<li><a href="/change-orders/%d"><b>%s</b></a> <span class="plat-pill">%s</span>'
                    '<div class="muted">%s · %s · %s</div></li>' % (
                        r['id'], h(r['title'] or 'Change order'), h(r['status']),
                        h(r['job_id']), h(r['customer'] or r['job_title'] or ''), money(r['total'] or 0)))
            body += '</ul>'
        else:
            body += '<div class="muted">None yet. Price extra work, make a client link, and add it to the job when they accept. The app does not send the link.</div>'
        body += '</div>'
        return page('Change orders', body)

    def co_form(row, jobs):
        lines = json.loads(row['lines_json'] or '[]') if row else [{'name': '', 'qty': 1, 'sell': 0}]
        if not lines:
            lines = [{'name': '', 'qty': 1, 'sell': 0}]
        job_opts = ''.join(
            '<option value="%s"%s>%s · %s</option>' % (
                h(j['id']), ' selected' if row and row['job_id'] == j['id'] else '',
                h(j['id']), h(j['customer'] or j['title'] or ''))
            for j in jobs)
        rows_html = ''
        for ln in lines:
            rows_html += (
                '<div class="plat-row co-line"><div><label>Item</label><input name="line_name" value="%s" required></div>'
                '<div><label>Qty</label><input name="line_qty" type="number" step="0.01" min="0" value="%s"></div>'
                '<div><label>Sell $</label><input name="line_sell" type="number" step="0.01" min="0" value="%s"></div></div>'
                % (h(ln.get('name') or ''), h(str(ln.get('qty') or 1)), h(str(ln.get('sell') or 0))))
        action = ('/change-orders/%d' % row['id']) if row else '/change-orders/new'
        return (
            '<form class="plat-form plat-panel" method="post" action="%s">'
            '<label>Job</label><select name="job_id" required><option value="">Pick a job</option>%s</select>'
            '<label>Title</label><input name="title" required maxlength="200" value="%s">'
            '<label>Notes (office and client)</label><textarea name="notes" rows="2">%s</textarea>'
            '<h3>Lines</h3>%s'
            '<button class="plat-btn plat-btn-ghost" type="button" onclick="var d=document.querySelector(\'.co-line\').cloneNode(true);d.querySelectorAll(\'input\').forEach(function(i){i.value=i.name===\'line_qty\'?1:\'\'});this.previousElementSibling.append?this.before(d):this.parentNode.insertBefore(d,this);">Add line</button> '
            '<button class="plat-btn plat-btn-gold" type="submit">Save</button></form>'
        ) % (action, job_opts, h((row['title'] if row else 'Change order') or 'Change order'),
             h((row['notes'] if row else '') or ''), rows_html)

    @app.route('/change-orders/new', methods=['GET', 'POST'])
    def change_order_new():
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        msg = ''
        if request.method == 'POST':
            row, err = save_change_order(conn, {
                'job_id': request.form.get('job_id'), 'title': request.form.get('title'),
                'notes': request.form.get('notes'), 'lines': lines_from_form(),
            }, created_by=fo.me())
            if err:
                msg = err
            else:
                conn.commit(); conn.close()
                return redirect('/change-orders/%d' % row['id'])
        pre = request.args.get('job_id') or ''
        jobs = list(conn.execute("SELECT id,customer,title FROM jobs WHERE status!='completed' OR id=? ORDER BY created_at DESC LIMIT 200", (pre,)))
        blank = {'job_id': pre, 'title': 'Change order', 'notes': '', 'lines_json': '[]', 'id': 0} if pre else None
        form = co_form(blank, jobs)
        conn.close()
        return page('New change order', form, back='/change-orders', msg=msg)

    @app.route('/change-orders/<int:coid>', methods=['GET', 'POST'])
    def change_order_edit(coid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM change_orders WHERE id=?', (coid,)).fetchone()
        if not row:
            conn.close(); return redirect('/change-orders')
        msg = ''
        if request.method == 'POST' and request.form.get('action') == 'link':
            ensure_co_token(conn, coid)
            conn.commit()
            row = conn.execute('SELECT * FROM change_orders WHERE id=?', (coid,)).fetchone()
            msg = 'Client link is ready. Copy it and send it yourself. The app does not send it.'
        elif request.method == 'POST' and row['status'] != 'accepted':
            row, err = save_change_order(conn, {
                'job_id': request.form.get('job_id') or row['job_id'],
                'title': request.form.get('title'), 'notes': request.form.get('notes'),
                'lines': lines_from_form(),
            }, coid=coid)
            if err:
                msg = err
            else:
                conn.commit()
                msg = 'Saved.'
                row = conn.execute('SELECT * FROM change_orders WHERE id=?', (coid,)).fetchone()
        jobs = list(conn.execute("SELECT id,customer,title FROM jobs WHERE status!='completed' OR id=? ORDER BY created_at DESC LIMIT 200", (row['job_id'],)))
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
        conn.close()
        body = ''
        if row['status'] == 'accepted':
            body += '<div class="plat-panel"><p>Accepted by %s on %s. The amount was added to the job sale. Nothing was sent from the app.</p></div>' % (
                h(row['accepted_name'] or 'the client'), h((row['accepted_at'] or '')[:16].replace('T', ' ')))
        else:
            body += co_form(row, jobs)
        if row['status'] != 'accepted':
            body += '<div class="plat-panel"><h2>Client link</h2><p class="muted">The client can review the price, sign, and accept. That adds the amount to the job. The app never sends this link.</p>'
            if row['token']:
                url = request.host_url.rstrip('/') + '/co/' + row['token']
                body += '<input readonly value="%s" onclick="this.select()" style="width:100%%">' % h(url)
            body += '<form method="post"><input type="hidden" name="action" value="link"><button class="plat-btn plat-btn-gold" type="submit">%s</button></form></div>' % (
                'Make client link' if not row['token'] else 'Refresh link')
        if job:
            body += '<p class="muted">Job <a href="/?job=%s">%s</a> · %s</p>' % (
                h(job['id']), h(job['id']), h(job['customer'] or job['title'] or ''))
        return page(row['title'] or 'Change order', body, back='/change-orders', msg=msg)

    CO_HTML = '''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Change order · True Sense Security</title>
<style>
*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:640px;margin:auto;padding:16px}
h1{font-size:1.35rem;margin:10px 0 4px}h2{font-size:1.05rem;color:#6b6a16;margin:22px 0 8px;border-bottom:2px solid #a49f2a;padding-bottom:4px}
.muted{color:#666}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #eee;padding:8px;text-align:left;vertical-align:top}
.card{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:14px;margin:10px 0}
input[type=text]{width:100%;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:8px 0}
.btn{width:100%;background:#2e7d32;color:#fff;border:0;padding:18px;border-radius:10px;font-size:1.2rem;font-weight:700}
.ok{background:#e8f5e9;border:2px solid #2e7d32;border-radius:12px;padding:14px;font-weight:700;color:#1b5e20}
canvas{border:1px solid #ddd;border-radius:10px;width:100%;height:160px;touch-action:none;background:#fff}
</style></head><body>
<img src="/static/logo.png" alt="True Sense Security" style="height:48px;display:block;margin:0 auto">
<h1>{{ co.title }}</h1>
<div>{{ job_label }}</div>
{% if co.notes %}<div class="card">{{ co.notes }}</div>{% endif %}
<h2>Change order</h2>
<table><tr><th>Item</th><th>Qty</th><th>Price</th></tr>
{% for ln in lines %}<tr><td>{{ ln.name }}</td><td>{{ ln.qty }}</td><td>{{ money(ln.amount) }}</td></tr>{% endfor %}
<tr><td colspan="2"><b>Total</b></td><td><b>{{ money(co.total) }}</b></td></tr></table>
{% if accepted %}
<div class="ok" style="margin-top:16px">Accepted. Thank you{% if co.accepted_name %}, {{ co.accepted_name }}{% endif %}.</div>
{% else %}
<h2>Sign to approve</h2>
<form method="post" action="/co/{{ co.token }}/accept" onsubmit="return foSubmit()">
<input type="text" name="name" required maxlength="100" placeholder="Your full name" autocomplete="name">
<div class="muted">Sign with your finger below</div>
<canvas id="pad" width="600" height="200"></canvas>
<input type="hidden" name="signature" id="sig">
<button type="button" class="btn" style="background:#eee;color:#111;margin:8px 0" onclick="foClear()">Clear signature</button>
<label style="display:flex;gap:8px;align-items:center;margin:10px 0"><input type="checkbox" required style="width:22px;height:22px;margin:0"> I approve this change order and price.</label>
<button class="btn" type="submit">Approve and Sign</button>
</form>
<script>
var drawn=false,c=document.getElementById("pad"),x=c.getContext("2d"),on=false;
(function(){var r=c.getBoundingClientRect();c.width=r.width*2;c.height=r.height*2;x.scale(c.width/r.width,c.height/r.height);x.lineWidth=2;x.lineCap="round";x.strokeStyle="#111";
function pos(e){var b=c.getBoundingClientRect(),p=e.touches?e.touches[0]:e;return {x:p.clientX-b.left,y:p.clientY-b.top}}
function start(e){on=true;var p=pos(e);x.beginPath();x.moveTo(p.x,p.y);e.preventDefault()}
function move(e){if(!on)return;var p=pos(e);x.lineTo(p.x,p.y);x.stroke();drawn=true;e.preventDefault()}
c.addEventListener("pointerdown",start);c.addEventListener("pointermove",move);
c.addEventListener("pointerup",function(){on=false});c.addEventListener("pointerleave",function(){on=false});
c.addEventListener("touchstart",start,{passive:false});c.addEventListener("touchmove",move,{passive:false});})();
function foClear(){var r=c.getBoundingClientRect();x.clearRect(0,0,r.width,r.height);drawn=false}
function foSubmit(){if(!drawn){alert("Please sign in the box");return false}document.getElementById("sig").value=c.toDataURL("image/png");return true}
</script>
{% endif %}
<div class="muted" style="margin-top:24px;text-align:center">True Sense Security</div>
</body></html>'''

    @app.route('/co/<token>')
    def change_order_client(token):
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM change_orders WHERE token=?', (token,)).fetchone()
        if not row:
            conn.close(); return ('This link is not valid.', 404)
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
        conn.close()
        lines = json.loads(row['lines_json'] or '[]')
        return render_template_string(
            CO_HTML, co=row, lines=lines, money=money,
            accepted=row['status'] == 'accepted',
            job_label=(job_label(job) if job else row['job_id']))

    @app.route('/co/<token>/accept', methods=['POST'])
    def change_order_accept(token):
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM change_orders WHERE token=?', (token,)).fetchone()
        if not row:
            conn.close(); return ('This link is not valid.', 404)
        if row['status'] == 'accepted':
            conn.close(); return redirect('/co/' + token)
        name = (request.form.get('name') or '').strip()[:100]
        sig = request.form.get('signature') or ''
        if not name or not sig.startswith('data:image/png;base64,'):
            conn.close(); return redirect('/co/' + token)
        ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
        accept_change_order(conn, row, name, sig, ip, fo=fo)
        conn.commit(); conn.close()
        return redirect('/co/' + token)

    # ---------- QBO drafts ----------
    def build_kind_payload(conn, job, kind, amount, memo):
        base = fo.qbo_payload(conn, job)
        kind = kind if kind in KINDS else 'final'
        amt = None
        try:
            amt = float(amount) if amount not in (None, '') else None
        except (TypeError, ValueError):
            amt = None
        if amt is not None:
            desc = (memo or (kind.title() + ' for ' + (job['title'] or job['id'])))[:4000]
            base['line_items'] = [{'description': desc, 'qty': 1, 'rate': round(amt, 2), 'amount': round(amt, 2)}]
            base['total'] = round(amt, 2)
        base['kind'] = kind
        base['customer_memo'] = (memo or base.get('customer_memo') or '')[:1000]
        base['private_note'] = 'Field Ops %s draft for job %s. Created as a draft. Do not send without Roland OK.' % (kind, job['id'])
        return base

    def qbo_extra(conn):
        drafts = list(conn.execute(
            '''SELECT d.*, j.customer, j.title AS job_title FROM qbo_drafts d
               JOIN jobs j ON j.id=d.job_id ORDER BY d.id DESC LIMIT 80'''))
        jobs = list(conn.execute("SELECT id,customer,title,sale_amount FROM jobs ORDER BY created_at DESC LIMIT 200"))
        opts = ''.join('<option value="%s">%s · %s</option>' % (h(j['id']), h(j['id']), h(j['customer'] or j['title'] or '')) for j in jobs)
        html = '<div class="plat-panel" style="margin-top:18px"><h2>Deposit, progress and final drafts</h2>'
        html += '<p class="muted">These sit in the queue as drafts only. Your bot can pick them up with the kind on the pending list (final is the default, same as before). Nothing is sent to the client.</p>'
        html += (
            '<form class="plat-form" method="post">'
            '<input type="hidden" name="action" value="ops_draft">'
            '<div class="plat-row"><div><label>Job</label><select name="job_id" required><option value="">Pick a job</option>%s</select></div>'
            '<div><label>Kind</label><select name="kind"><option value="deposit">Deposit</option><option value="progress">Progress</option><option value="final" selected>Final</option></select></div>'
            '<div><label>Amount $</label><input name="amount" type="number" step="0.01" min="0" placeholder="Uses the job total if blank"></div></div>'
            '<label>Memo</label><input name="memo" maxlength="200" placeholder="Optional note on the draft">'
            '<button class="plat-btn plat-btn-gold" type="submit">Save draft in the queue</button></form>'
        ) % opts
        if drafts:
            html += '<ul class="plat-list">'
            for d in drafts:
                html += '<li><b>%s</b> <span class="plat-pill">%s</span> <span class="plat-pill">%s</span>' \
                        '<div class="muted">%s · %s · %s</div></li>' % (
                            h(d['customer'] or d['job_title'] or d['job_id']), h(d['kind']), h(d['status']),
                            h(d['job_id']), money(d['amount'] if d['amount'] is not None else 0), h(d['created_at'] or '')[:16])
            html += '</ul>'
        html += '</div>'
        return html

    fo.ops_qbo_extra = qbo_extra

    def ops_qbo_post(conn, form):
        if form.get('action') != 'ops_draft':
            return None
        jid = (form.get('job_id') or '').strip()
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not job:
            return 'Job not found.'
        kind = form.get('kind') if form.get('kind') in KINDS else 'final'
        payload = build_kind_payload(conn, job, kind, form.get('amount'), form.get('memo'))
        amt = payload['total']
        when = now_iso()
        conn.execute(
            '''INSERT INTO qbo_drafts (job_id,kind,amount,memo,status,payload,created_by,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (jid, kind, amt, (form.get('memo') or '')[:200], 'queued', json.dumps(payload), fo.me(), when, when))
        if kind == 'final' and not conn.execute(
                "SELECT 1 FROM qbo_queue WHERE job_id=? AND status IN ('queued','created','paid')", (jid,)).fetchone():
            if payload.get('line_items') and payload.get('customer', {}).get('name'):
                conn.execute(
                    'INSERT OR REPLACE INTO qbo_queue (job_id,status,payload,queued_at,queued_by,updated_at) VALUES (?,?,?,?,?,?)',
                    (jid, 'queued', json.dumps(payload), fo.now_local_str() if hasattr(fo, 'now_local_str') else when, fo.me(),
                     fo.now_local_str() if hasattr(fo, 'now_local_str') else when))
        return 'Saved a %s draft. It will not be sent until you tell the bot to create it in QuickBooks.' % kind

    fo.ops_qbo_post = ops_qbo_post

    def pending_with_kind(kind, status, existing):
        """Return existing final queue rows, or drafts of another kind, without dropping old fields."""
        kind = (kind or 'final').lower()
        if kind not in KINDS:
            kind = 'final'
        if kind == 'final':
            out = []
            for item in existing:
                if 'kind' not in item:
                    item = dict(item)
                    item['kind'] = 'final'
                out.append(item)
            return out
        conn = fo.get_db()
        st = status if status in ('queued', 'created', 'draft', 'paid', 'error') else 'queued'
        rows = conn.execute('SELECT * FROM qbo_drafts WHERE kind=? AND status=? ORDER BY created_at', (kind, st)).fetchall()
        out = []
        for r in rows:
            try:
                p = json.loads(r['payload'] or '{}')
            except Exception:
                p = {}
            p = dict(p)
            p['status'] = r['status']
            p['qbo_invoice_id'] = r['qbo_invoice_id']
            p['queued_at'] = r['created_at']
            p['kind'] = r['kind']
            p['draft_id'] = r['id']
            if r['job_id'] and 'job_id' not in p:
                p['job_id'] = r['job_id']
            out.append(p)
        conn.close()
        return out

    fo.ops_qbo_pending = pending_with_kind

    def apply_qbo_result(d):
        draft_id = d.get('draft_id')
        if not draft_id:
            return None
        try:
            draft_id = int(draft_id)
        except (TypeError, ValueError):
            return 'bad draft_id'
        st = d.get('status')
        if st not in ('created', 'paid', 'error'):
            return 'status must be created, paid or error'
        conn = fo.get_db()
        x = conn.execute('SELECT * FROM qbo_drafts WHERE id=?', (draft_id,)).fetchone()
        if not x:
            conn.close(); return 'draft not found'
        conn.execute(
            'UPDATE qbo_drafts SET status=?, qbo_invoice_id=COALESCE(?,qbo_invoice_id), doc_number=COALESCE(?,doc_number), note=?, updated_at=? WHERE id=?',
            (st,
             (str(d['qbo_invoice_id'])[:40] if d.get('qbo_invoice_id') else None),
             (str(d['doc_number'])[:40] if d.get('doc_number') else None),
             str(d.get('note') or '')[:300], now_iso(), draft_id))
        conn.commit(); conn.close()
        return 'ok'

    fo.ops_qbo_result = apply_qbo_result

    # ---------- Costing ----------
    @app.route('/costing')
    def costing_page():
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        rows = list(conn.execute(
            "SELECT * FROM jobs WHERE status!='completed' OR date(completed_at)>=date('now','-45 days') ORDER BY (status='completed'), COALESCE(scheduled_date, created_at) DESC"))
        body = '<p class="muted">Live job costing for the office. Sale includes accepted change orders already added to the job. Subs never see this page.</p>'
        body += '<div class="plat-panel" style="overflow:auto"><table class="plat-table cost-table"><thead><tr>'
        body += ''.join('<th>%s</th>' % x for x in ('Job', 'Customer', 'Sale', 'Labor', 'Materials', 'Change orders', 'Cost', 'Profit'))
        body += '</tr></thead><tbody>'
        sums = {'sale': 0, 'labor': 0, 'parts': 0, 'co': 0, 'cost': 0, 'profit': 0}
        for r in rows:
            labor = fo.job_labor(conn, r)['total']
            parts = fo.job_parts_cost(conn, r['id'])
            co = accepted_co_total(conn, r['id'])
            sale = job_sale(conn, r)
            # sale_amount already includes accepted COs
            cost = labor + parts
            profit = sale - cost
            for k, v in (('sale', sale), ('labor', labor), ('parts', parts), ('co', co), ('cost', cost), ('profit', profit)):
                sums[k] += v
            tone = ' style="color:#b00020"' if profit < 0 else ''
            body += (
                '<tr><td><a href="/?job=%s"><b>%s</b></a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td%s>%s</td></tr>'
                % (h(r['id']), h(r['id']), h(r['customer'] or r['title'] or ''),
                   money(sale), money(labor), money(parts), money(co), money(cost), tone, money(profit)))
        body += ('<tr><th>Total</th><th></th><th>%s</th><th>%s</th><th>%s</th><th>%s</th><th>%s</th><th>%s</th></tr>'
                 % (money(sums['sale']), money(sums['labor']), money(sums['parts']), money(sums['co']), money(sums['cost']), money(sums['profit'])))
        body += '</tbody></table></div>'
        conn.close()
        return page('Job costing', body, actions='<a class="plat-btn plat-btn-ghost" href="/change-orders">Change orders</a>')

    # ---------- Timesheets ----------
    @app.route('/timesheets')
    def timesheets_page():
        gate = office_only()
        if gate:
            return gate
        t = fo.today_local()
        start = parse_day(request.args.get('start')) or week_monday(t)
        start = week_monday(start)
        end = start + timedelta(days=6)
        prev = start - timedelta(days=7)
        nxt = start + timedelta(days=7)
        conn = fo.get_db()
        subs = techs_list(conn)
        rates = {r['key']: r for r in conn.execute('SELECT * FROM subs')}
        sheets = {(ckey(r['sub'])): r for r in conn.execute('SELECT * FROM timesheet_weeks WHERE week_start=?', (start.isoformat(),))}
        body = '<p class="muted">Weekly hours from clock-ins. Approve a week, then export sub pay as CSV. Rates stay in the office. Nothing is sent.</p>'
        body += '<div class="sked-nav" style="margin:0 0 16px"><a class="plat-btn plat-btn-ghost" href="/timesheets?start=%s">Earlier</a>' % prev.isoformat()
        body += '<a class="plat-btn plat-btn-ghost" href="/timesheets">This week</a>'
        body += '<a class="plat-btn plat-btn-ghost" href="/timesheets?start=%s">Later</a>' % nxt.isoformat()
        body += '<span class="sked-range">%s to %s</span></div>' % (h(start.strftime('%b %-d')), h(end.strftime('%b %-d, %Y')))
        body += '<div class="plat-panel" style="overflow:auto"><table class="plat-table cost-table"><thead><tr>'
        body += ''.join('<th>%s</th>' % x for x in ('Sub', 'Hours', 'Hourly rate', 'Flat', 'Pay', 'Status', ''))
        body += '</tr></thead><tbody>'
        any_rows = False
        for name in subs:
            mins = punch_minutes_in_week(conn, name, start.isoformat())
            sheet = sheets.get(ckey(name))
            if mins <= 0 and not sheet:
                continue
            any_rows = True
            rate = rates.get(ckey(name)) or {}
            rh = rate['rate_hourly'] if rate and rate['rate_hourly'] is not None else None
            rf = rate['rate_flat'] if rate and rate['rate_flat'] is not None else None
            hours = round(mins / 60, 2)
            pay = round(hours * (rh or 0) + (rf or 0), 2)
            st = (sheet['status'] if sheet else 'open')
            body += '<tr><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>' % (
                h(name), hours, money(rh or 0) if rh is not None else '-',
                money(rf or 0) if rf is not None else '-', money(pay) if (rh or rf) else 'No rate', h(st))
            if st != 'approved':
                body += ('<form method="post" action="/timesheets/approve" style="display:inline">'
                         '<input type="hidden" name="sub" value="%s"><input type="hidden" name="week_start" value="%s">'
                         '<button class="plat-link-btn" type="submit">Approve</button></form>' % (h(name), start.isoformat()))
            else:
                body += '<span class="muted">by %s</span>' % h(sheet['approved_by'] or '')
            body += '</td></tr>'
        if not any_rows:
            body += '<tr><td colspan="7" class="muted">No clock-ins this week.</td></tr>'
        body += '</tbody></table></div>'
        body += '<p><a class="plat-btn plat-btn-ghost" href="/timesheets/export?start=%s">Download sub-pay CSV</a></p>' % start.isoformat()
        conn.close()
        return page('Timesheets', body)

    @app.route('/timesheets/approve', methods=['POST'])
    def timesheets_approve():
        gate = office_only()
        if gate:
            return gate
        sub = (request.form.get('sub') or '').strip()
        ws = parse_day(request.form.get('week_start'))
        if not sub or not ws:
            return redirect('/timesheets')
        ws = week_monday(ws).isoformat()
        conn = fo.get_db()
        conn.execute(
            '''INSERT INTO timesheet_weeks (sub,week_start,status,approved_by,approved_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(sub, week_start) DO UPDATE SET status='approved', approved_by=excluded.approved_by, approved_at=excluded.approved_at''',
            (sub, ws, 'approved', fo.me(), now_iso()))
        conn.commit(); conn.close()
        return redirect('/timesheets?start=' + ws)

    @app.route('/timesheets/export')
    def timesheets_export():
        gate = office_only()
        if gate:
            return gate
        t = fo.today_local()
        start = week_monday(parse_day(request.args.get('start')) or t)
        conn = fo.get_db()
        subs = techs_list(conn)
        rates = {r['key']: r for r in conn.execute('SELECT * FROM subs')}
        sheets = {(ckey(r['sub'])): r for r in conn.execute('SELECT * FROM timesheet_weeks WHERE week_start=?', (start.isoformat(),))}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['sub', 'week_start', 'week_end', 'hours', 'rate_hourly', 'rate_flat', 'pay', 'status'])
        for name in subs:
            mins = punch_minutes_in_week(conn, name, start.isoformat())
            sheet = sheets.get(ckey(name))
            if mins <= 0 and not sheet:
                continue
            rate = rates.get(ckey(name)) or {}
            rh = rate['rate_hourly'] if rate and 'rate_hourly' in rate.keys() else None
            rf = rate['rate_flat'] if rate and 'rate_flat' in rate.keys() else None
            hours = round(mins / 60, 2)
            pay = round(hours * (rh or 0) + (rf or 0), 2)
            w.writerow([name, start.isoformat(), (start + timedelta(days=6)).isoformat(), hours,
                        rh if rh is not None else '', rf if rf is not None else '', pay,
                        (sheet['status'] if sheet else 'open')])
        conn.close()
        data = buf.getvalue()
        return Response(data, mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=sub-pay-%s.csv' % start.isoformat()})


    # end register
    fo.ops_job_days = lambda conn, job_id: [r['day'] for r in conn.execute('SELECT day FROM job_slots WHERE job_id=?', (job_id,))]


SCHED_CSS = '''
<style>
.sked-lede{color:var(--muted);margin:0 0 14px;max-width:52rem}
.sked-toolbar{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;margin:0 0 14px}
.sked-nav,.sked-views,.sked-tools{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-left:auto}
.sked-nav{margin-left:0;flex:1 1 100%}
.sked-range{font-weight:650;margin-left:4px;white-space:nowrap}
.sked-filter label{display:flex;gap:6px;align-items:center;font-weight:650;font-size:.85rem;color:var(--muted);white-space:nowrap}
.sked-filter select{padding:8px 10px;border-radius:10px;border:1px solid var(--line);background:#fff}
.sked-wrap{display:grid;grid-template-columns:1fr minmax(14rem,18rem);gap:16px;align-items:start}
.sked-board{overflow:auto;background:#fff;border:1px solid var(--line);border-radius:14px}
.sked-tray{background:#fff;border:1px solid var(--line);border-radius:14px;padding:14px;position:sticky;top:calc(var(--fo-head, 80px) + 12px)}
.sked-tray h2{margin:0 0 8px}
.sked-cell,.sked-hour,.sked-name,.sked-head{min-width:0}
.sked-card,.tray-card,.sked-block{overflow-wrap:normal !important;word-break:normal !important;hyphens:none !important;box-sizing:border-box}
.sked-card,.tray-card{display:block;width:100%;background:#3d4a16;color:#fff;border-radius:8px;padding:7px 8px 7px 10px;border-left:4px solid #c9a227;font-size:.8rem;line-height:1.25;margin:0 0 6px;cursor:grab;user-select:none;overflow:hidden}
.sked-card b,.tray-card b,.sked-block b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sked-card .t,.tray-card .t,.sked-block .t{display:block;white-space:normal;font-size:.72rem}
.sked-card b,.tray-card b{display:block;font-size:.85rem}
.sked-card .t,.tray-card .t{opacity:.9;font-size:.75rem}
.sked-card.sel,.tray-card.sel{outline:3px solid #c9a227}
.sked-grid{display:grid;min-width:720px}
.sked-grid.week,.sked-grid.fortnight{grid-template-columns:8.5rem repeat(var(--cols),minmax(6.5rem,1fr))}
.sked-head,.sked-cell,.sked-name{border-bottom:1px solid var(--line);border-right:1px solid var(--line);padding:8px}
.sked-head{text-align:center;background:#fafaf6;position:sticky;top:0;z-index:2}
.sked-head small{display:block;font-weight:400;color:var(--muted);font-size:.7rem}
.sked-head.today{box-shadow:inset 0 -3px 0 #c9a227}
.sked-name{font-weight:700;background:#fff;position:sticky;left:0;z-index:1}
.sked-cell{min-height:88px;vertical-align:top;background:#fff}
.sked-cell.hot,.sked-hour.hot{background:#f3f0e0}
.sked-month{display:grid;grid-template-columns:repeat(7,1fr);min-width:640px}
.sked-month .sked-cell{min-height:110px}
.sked-mhead{text-align:center;font-size:.75rem;font-weight:700;color:var(--muted);padding:8px;background:#fafaf6}
.sked-day-grid{display:grid;grid-template-columns:3.5rem repeat(var(--cols),minmax(7rem,1fr));min-width:640px}
.sked-hour-lab{padding:0 6px;font-size:.7rem;color:var(--muted);border-right:1px solid var(--line);border-bottom:1px solid #f0f0ea;height:28px}
.sked-hour{position:relative;height:28px;border-right:1px solid var(--line);border-bottom:1px solid #f0f0ea}
.sked-block{position:absolute;left:3px;right:3px;background:#3d4a16;color:#fff;border-radius:8px;border-left:4px solid #c9a227;padding:4px 6px;font-size:.75rem;overflow:hidden;z-index:1;cursor:grab}
.sked-block .handle{position:absolute;left:0;right:0;bottom:0;height:10px;cursor:ns-resize}
.sked-bar{position:sticky;bottom:8px;background:#3d4a16;color:#fff;padding:10px 14px;border-radius:12px;margin-top:12px;z-index:5}
.sked-pop{position:fixed;inset:auto 12px 12px 12px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:14px;box-shadow:0 8px 30px rgba(0,0,0,.15);z-index:20;max-width:420px;margin:auto}
.cost-table{width:100%;border-collapse:collapse;font-size:.95rem}
.cost-table th,.cost-table td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
.cost-table th{font-size:.75rem;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.hidden{display:none}
@media (max-width:900px){
  .sked-wrap{grid-template-columns:1fr}
  .sked-tray{position:static}
  .sked-grid.week,.sked-grid.fortnight,.sked-day-grid,.sked-month{min-width:100%}
}
</style>
'''

SCHED_JS = r'''
<script>
(function(){
  const data=SKED, board=document.getElementById('sked-board'), tray=document.getElementById('sked-tray-list');
  const bar=document.getElementById('sked-bar'), pop=document.getElementById('sked-pop');
  let sel=null;
  function h(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
  function nice(t){if(!t)return '';const [H,M]=t.split(':');const n=+H;const ap=n>=12?'PM':'AM';const hr=((n+11)%12)+1;return hr+':'+(M||'00')+' '+ap}
  function card(s, compact){
    const style='background:'+s.color+';border-left-color:#c9a227';
    return '<div class="sked-card" draggable="true" data-id="'+h(String(s.id||0))+'" data-job="'+h(s.job_id)+'" data-day="'+h(s.day)+'" data-sub="'+h(s.sub)+'" data-start="'+h(s.start)+'" data-end="'+h(s.end)+'" style="'+style+'"><b>'+h(s.customer||s.title||s.job_id)+'</b><div class="t">'+h(nice(s.start))+' to '+h(nice(s.end))+'</div></div>';
  }
  function trayCard(j){
    return '<div class="tray-card" draggable="true" data-job="'+h(j.id)+'" data-id="0"><b>'+h(j.customer||j.title||j.id)+'</b><div class="t">'+h(j.type||'Job')+' · '+h(j.id)+'</div></div>';
  }
  function slotsFor(day, sub){
    return data.slots.filter(s=>s.day===day && (s.sub||'').toLowerCase()===(sub||'').toLowerCase());
  }
  function renderTray(){
    tray.innerHTML=data.tray.length?data.tray.map(trayCard).join(''):'<div class="muted">No unscheduled jobs.</div>';
  }
  function renderWeek(){
    const cols=data.days.length;
    let html='<div class="sked-grid '+data.view+'" style="--cols:'+cols+'"><div class="sked-head">Sub</div>';
    data.days.forEach(d=>{html+='<div class="sked-head'+(d.today?' today':'')+'">'+h(d.dow)+'<small>'+h(d.num)+' '+h(d.mon)+'</small></div>'});
    data.techs.forEach(tech=>{
      html+='<div class="sked-name">'+h(tech)+'</div>';
      data.days.forEach(d=>{
        const items=slotsFor(d.iso, tech);
        html+='<div class="sked-cell" data-day="'+h(d.iso)+'" data-sub="'+h(tech)+'">'+items.map(s=>card(s)).join('')+'</div>';
      });
    });
    html+='</div>';
    board.innerHTML=html;
  }
  function renderMonth(){
    const heads=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    let html='<div class="sked-month">';
    heads.forEach(x=>html+='<div class="sked-mhead">'+x+'</div>');
    for(let i=0;i<(data.monthPad||0);i++) html+='<div class="sked-cell"></div>';
    data.days.forEach(d=>{
      const items=data.slots.filter(s=>s.day===d.iso);
      html+='<div class="sked-cell'+(d.today?' today':'')+'" data-day="'+h(d.iso)+'"><div class="muted"><b>'+h(String(d.num))+'</b></div>'+items.map(s=>card(s,true)).join('')+'</div>';
    });
    html+='</div>';
    board.innerHTML=html;
  }
  function minutes(t){const [H,M]=(t||'00:00').split(':');return (+H)*60+(+M||0)}
  function fromMin(m){const h=Math.floor(m/60),mm=m%60;return String(h).padStart(2,'0')+':'+String(mm).padStart(2,'0')}
  function renderDay(){
    const day=data.days[0]&&data.days[0].iso;
    const start=data.dayStart*60, end=data.dayEnd*60, step=30;
    const rows=(end-start)/step;
    let html='<div class="sked-day-grid" style="--cols:'+data.techs.length+'"><div class="sked-head"></div>';
    data.techs.forEach(t=>html+='<div class="sked-head">'+h(t)+'</div>');
    for(let r=0;r<rows;r++){
      const m=start+r*step;
      html+='<div class="sked-hour-lab">'+(r%2===0?h(nice(fromMin(m))):'')+'</div>';
      data.techs.forEach(tech=>{
        html+='<div class="sked-hour" data-day="'+h(day)+'" data-sub="'+h(tech)+'" data-start="'+fromMin(m)+'"></div>';
      });
    }
    html+='</div>';
    board.innerHTML=html;
    const grid=board.querySelector('.sked-day-grid');
    const top0=grid.querySelector('.sked-hour').offsetTop;
    const rowH=28;
    data.slots.filter(s=>s.day===day).forEach(s=>{
      const col=data.techs.findIndex(t=>(t||'').toLowerCase()===(s.sub||'').toLowerCase());
      if(col<0)return;
      const cells=[...grid.querySelectorAll('.sked-hour')].filter(c=>c.dataset.sub===s.sub);
      if(!cells[0])return;
      const y=((minutes(s.start)-start)/step)*rowH;
      const hgt=Math.max(rowH,((minutes(s.end)-minutes(s.start))/step)*rowH);
      const el=document.createElement('div');
      el.className='sked-block';
      el.draggable=true;
      el.dataset.job=s.job_id; el.dataset.id=String(s.id||0); el.dataset.day=s.day; el.dataset.sub=s.sub; el.dataset.start=s.start; el.dataset.end=s.end;
      el.style.background=s.color; el.style.top=y+'px'; el.style.height=hgt+'px';
      el.innerHTML='<b>'+h(s.customer||s.title||s.job_id)+'</b><div class="t">'+h(nice(s.start))+' to '+h(nice(s.end))+'</div><div class="handle"></div>';
      cells[0].style.position='relative';
      cells[0].appendChild(el);
      el.querySelector('.handle').addEventListener('pointerdown',ev=>{
        ev.stopPropagation(); ev.preventDefault();
        const startY=ev.clientY, orig=minutes(s.end);
        const move=e=>{
          const dm=Math.round(((e.clientY-startY)/rowH))*step;
          let endm=Math.max(minutes(s.start)+step, Math.min(end, orig+dm));
          el.dataset.end=fromMin(endm);
          el.style.height=(((endm-minutes(s.start))/step)*rowH)+'px';
          el.querySelector('.t').textContent=nice(s.start)+' to '+nice(fromMin(endm));
        };
        const up=e=>{
          document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up);
          post({id:s.id,job_id:s.job_id,day:s.day,sub:s.sub,start:s.start,end:el.dataset.end});
        };
        document.addEventListener('pointermove',move); document.addEventListener('pointerup',up);
      });
    });
  }
  function render(){
    if(data.view==='day') renderDay();
    else if(data.view==='month') renderMonth();
    else renderWeek();
    renderTray();
    bind();
  }
  function show(msg){bar.textContent=msg;bar.classList.remove('hidden')}
  function hide(){bar.classList.add('hidden')}
  function pick(el){
    document.querySelectorAll('.sked-card.sel,.tray-card.sel,.sked-block.sel').forEach(x=>x.classList.remove('sel'));
    const job=el.dataset.job;
    if(sel&&sel.job===job&&sel.id===el.dataset.id){sel=null;hide();return}
    sel={job:job,id:el.dataset.id,start:el.dataset.start,end:el.dataset.end};
    el.classList.add('sel');
    show('Tap the box where '+(el.querySelector('b')?el.querySelector('b').textContent:job)+' goes. Tap the job again to cancel.');
  }
  async function post(body){
    show('Saving...');
    const r=await fetch('/api/schedule/slots',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json().catch(()=>({}));
    if(r.ok){ if(d.warning) alert(d.warning); location.reload(); }
    else { alert(d.error||'Could not save'); hide()}
  }
  function place(day, sub, start, end){
    if(!sel) return;
    post({id:sel.id,job_id:sel.job,day:day,sub:sub||sel.sub||(data.techs[0]||''),start:start||sel.start||'08:00',end:end||sel.end||'16:00'});
  }
  function openBook(day, sub, start){
    if(sel){place(day,sub,start);return}
    if(!data.tray.length){location.href='/new-job?date='+encodeURIComponent(day)+'&tech='+encodeURIComponent(sub||'');return}
    pop.classList.remove('hidden');
    pop.innerHTML='<b>Book into '+(day)+(sub?(' · '+h(sub)):'')+'</b><p class="muted">Pick an unscheduled job, or start a new one.</p>'+
      data.tray.map(j=>'<button class="plat-btn plat-btn-ghost" style="display:block;width:100%;margin:0 0 6px;text-align:left" data-job="'+h(j.id)+'">'+h(j.customer||j.title||j.id)+' · '+h(j.id)+'</button>').join('')+
      '<a class="plat-btn plat-btn-gold" href="/new-job?date='+encodeURIComponent(day)+'&tech='+encodeURIComponent(sub||'')+'">New job</a> '+
      '<button class="plat-btn plat-btn-ghost" type="button" id="sked-cancel">Cancel</button>';
    pop.querySelectorAll('[data-job]').forEach(b=>b.addEventListener('click',()=>{sel={job:b.dataset.job,id:'0'};place(day,sub,start)}));
    pop.querySelector('#sked-cancel').addEventListener('click',()=>pop.classList.add('hidden'));
  }
  function bind(){
    board.querySelectorAll('.sked-card,.sked-block,.tray-card').forEach(el=>{
      el.addEventListener('click',e=>{e.stopPropagation();pick(el)});
      el.addEventListener('dragstart',e=>{e.dataTransfer.setData('text/plain',JSON.stringify({job:el.dataset.job,id:el.dataset.id,start:el.dataset.start,end:el.dataset.end}));sel={job:el.dataset.job,id:el.dataset.id,start:el.dataset.start,end:el.dataset.end}});
    });
    document.querySelectorAll('.tray-card').forEach(el=>{
      el.addEventListener('click',e=>{e.stopPropagation();pick(el)});
      el.addEventListener('dragstart',e=>{e.dataTransfer.setData('text/plain',JSON.stringify({job:el.dataset.job,id:'0'}));sel={job:el.dataset.job,id:'0'}});
    });
    board.querySelectorAll('.sked-cell,.sked-hour').forEach(td=>{
      td.addEventListener('click',e=>{if(e.target!==td&&!td.classList.contains('sked-cell'))return; openBook(td.dataset.day,td.dataset.sub,td.dataset.start)});
      td.addEventListener('dragover',e=>{e.preventDefault();td.classList.add('hot')});
      td.addEventListener('dragleave',()=>td.classList.remove('hot'));
      td.addEventListener('drop',e=>{
        e.preventDefault();td.classList.remove('hot');
        let p={}; try{p=JSON.parse(e.dataTransfer.getData('text/plain')||'{}')}catch(err){}
        sel=sel||p;
        if(!sel||!sel.job)return;
        place(td.dataset.day, td.dataset.sub||'', td.dataset.start, null);
      });
    });
  }
  render();
})();
</script>
'''
