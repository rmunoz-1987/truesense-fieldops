"""Phase 3 CRM: activities, tasks, contact timeline, and a simple sales dashboard.

Activities are call, meeting, or note. Tasks have due dates and show on Today.
Nothing is sent to clients. Money stays office-only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

ACTIVITY_KINDS = [('call', 'Call'), ('meeting', 'Meeting'), ('note', 'Note')]
KIND_SET = {k for k, _ in ACTIVITY_KINDS}


def now_iso():
    return datetime.utcnow().isoformat()


def nkey(s):
    return ' '.join((s or '').split()).lower()


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS crm_activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL DEFAULT 'note',
        body TEXT,
        company_id INTEGER,
        contact_id INTEGER,
        opportunity_id INTEGER,
        happened_at TEXT,
        created_by TEXT,
        created_at TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS crm_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        due TEXT,
        notes TEXT,
        done INTEGER DEFAULT 0,
        company_id INTEGER,
        contact_id INTEGER,
        opportunity_id INTEGER,
        created_by TEXT,
        created_at TEXT,
        done_by TEXT,
        done_at TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_crm_act_co ON crm_activities(company_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_crm_act_ct ON crm_activities(contact_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_crm_act_op ON crm_activities(opportunity_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_crm_task_due ON crm_tasks(done, due)')


def add_activity(conn, kind, body, company_id=None, contact_id=None, opportunity_id=None, happened_at=None, created_by=''):
    kind = kind if kind in KIND_SET else 'note'
    body = (body or '').strip()[:4000]
    if not body:
        return None, 'Write a short note about the call, meeting, or what happened.'
    when = happened_at or now_iso()
    cur = conn.execute(
        'INSERT INTO crm_activities (kind,body,company_id,contact_id,opportunity_id,happened_at,created_by,created_at) VALUES (?,?,?,?,?,?,?,?)',
        (kind, body, company_id or None, contact_id or None, opportunity_id or None, when, (created_by or '')[:80], now_iso()))
    return cur.lastrowid, None


def add_task(conn, title, due, notes='', company_id=None, contact_id=None, opportunity_id=None, created_by=''):
    title = (title or '').strip()[:200]
    if not title:
        return None, 'Task title is required.'
    due = (due or '').strip()[:10]
    if due:
        try:
            datetime.strptime(due, '%Y-%m-%d')
        except ValueError:
            return None, 'Due date must be YYYY-MM-DD.'
    cur = conn.execute(
        'INSERT INTO crm_tasks (title,due,notes,done,company_id,contact_id,opportunity_id,created_by,created_at) VALUES (?,?,?,0,?,?,?,?,?)',
        (title, due or None, (notes or '').strip()[:2000], company_id or None, contact_id or None,
         opportunity_id or None, (created_by or '')[:80], now_iso()))
    return cur.lastrowid, None


def complete_task(conn, task_id, who):
    row = conn.execute('SELECT * FROM crm_tasks WHERE id=?', (task_id,)).fetchone()
    if not row:
        return 'Task not found.'
    conn.execute('UPDATE crm_tasks SET done=1, done_by=?, done_at=? WHERE id=?', (who, now_iso(), task_id))
    return None


def reopen_task(conn, task_id):
    n = conn.execute('UPDATE crm_tasks SET done=0, done_by=NULL, done_at=NULL WHERE id=?', (task_id,)).rowcount
    return None if n else 'Task not found.'


def due_tasks(conn, day):
    return list(conn.execute(
        '''SELECT t.*, c.name AS company, ct.name AS contact, o.title AS opportunity
           FROM crm_tasks t
           LEFT JOIN companies c ON c.id=t.company_id
           LEFT JOIN contacts ct ON ct.id=t.contact_id
           LEFT JOIN opportunities o ON o.id=t.opportunity_id
           WHERE t.done=0 AND t.due IS NOT NULL AND t.due<=?
           ORDER BY t.due, t.id''', (day,)))


def timeline(conn, company_id=None, contact_id=None, opportunity_id=None, limit=80):
    """Mixed activity + task history, newest first."""
    where = []
    args = []
    if contact_id:
        where.append('(a.contact_id=? OR (a.contact_id IS NULL AND a.company_id=(SELECT company_id FROM contacts WHERE id=?)))')
        args.extend([contact_id, contact_id])
    elif opportunity_id:
        where.append('(a.opportunity_id=? OR (a.opportunity_id IS NULL AND a.company_id=(SELECT company_id FROM opportunities WHERE id=?)))')
        args.extend([opportunity_id, opportunity_id])
    elif company_id:
        where.append('a.company_id=?')
        args.append(company_id)
    else:
        return []
    sql = (
        'SELECT a.id, a.kind, a.body, a.happened_at AS at, a.created_by, a.contact_id, a.opportunity_id, '
        '"activity" AS typ FROM crm_activities a WHERE ' + ' AND '.join(where) +
        ' ORDER BY a.happened_at DESC, a.id DESC LIMIT %d' % limit)
    acts = [dict(r) for r in conn.execute(sql, args)]
    tw, targs = [], []
    if contact_id:
        tw.append('t.contact_id=?'); targs.append(contact_id)
    elif opportunity_id:
        tw.append('t.opportunity_id=?'); targs.append(opportunity_id)
    elif company_id:
        tw.append('t.company_id=?'); targs.append(company_id)
    tasks = list(conn.execute(
        "SELECT t.id, t.title AS body, COALESCE(t.done_at, t.created_at) AS at, t.created_by, t.done, t.due, "
        "'task' AS typ FROM crm_tasks t WHERE " + " AND ".join(tw) +
        " ORDER BY COALESCE(t.done_at, t.created_at) DESC LIMIT %d" % limit, targs))
    items = []
    for r in acts:
        items.append({'typ': 'activity', 'kind': r['kind'], 'body': r['body'], 'at': r['at'], 'who': r['created_by'], 'id': r['id']})
    for r in tasks:
        items.append({'typ': 'task', 'kind': 'done' if r['done'] else 'task', 'body': r['body'],
                      'at': r['at'], 'who': r['created_by'], 'due': r['due'], 'done': r['done'], 'id': r['id']})
    items.sort(key=lambda x: x.get('at') or '', reverse=True)
    return items[:limit]


def register(fo):
    from flask import request, redirect, session, jsonify
    import urllib.parse

    app = fo.app
    h = fo.h

    def office_only():
        if not fo.me():
            return redirect('/login')
        if not fo.is_office():
            return redirect('/')
        return None

    def page(title, body, back=None, kicker='Sales', actions='', msg=''):
        session['fo_area'] = 'sales'
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

    def kind_label(k):
        return dict(ACTIVITY_KINDS).get(k, (k or 'Note').title())

    def when_label(s):
        s = (s or '').strip()
        if not s:
            return ''
        try:
            d = datetime.fromisoformat(s.replace('Z', '')[:19])
            if 'T' in s or len(s) > 10:
                return d.strftime('%b %d, %I:%M %p').replace(' 0', ' ')
            return d.strftime('%b %d')
        except ValueError:
            return s[:16].replace('T', ' ')

    def ids_from(prefix=''):
        def num(key):
            try:
                v = int(request.form.get(prefix + key) or request.args.get(key) or 0)
            except ValueError:
                v = 0
            return v or None
        return num('company_id'), num('contact_id'), num('opportunity_id')

    def crm_block(subject, subject_id, company_id=None, contact_id=None, opportunity_id=None):
        """HTML panel for a record page."""
        conn = fo.get_db()
        cid, tid, oid = company_id, contact_id, opportunity_id
        items = timeline(conn, company_id=cid, contact_id=tid, opportunity_id=oid, limit=30)
        open_tasks = list(conn.execute(
            '''SELECT * FROM crm_tasks WHERE done=0 AND %s ORDER BY IFNULL(due,'9999'), id'''
            % ('contact_id=?' if tid else ('opportunity_id=?' if oid else 'company_id=?')),
            (tid or oid or cid,)))
        conn.close()
        hidden = ''.join(
            '<input type="hidden" name="%s" value="%s">' % (k, v)
            for k, v in (('company_id', cid), ('contact_id', tid), ('opportunity_id', oid)) if v)
        back = request.path
        body = '<div class="plat-panel"><h2>Follow-up tasks</h2>'
        if open_tasks:
            body += '<ul class="plat-list">'
            for tsk in open_tasks:
                due = tsk['due'] or 'No date'
                body += (
                    '<li><b>%s</b> <span class="muted">%s</span>'
                    '<form method="post" action="/tasks/%d/done" style="display:inline;margin-left:8px">'
                    '<input type="hidden" name="next" value="%s">'
                    '<button class="plat-link-btn" type="submit">Mark done</button></form></li>'
                    % (h(tsk['title']), h(due), tsk['id'], h(back)))
            body += '</ul>'
        else:
            body += '<div class="muted">No open tasks.</div>'
        today = ''
        try:
            today = fo.today_local().isoformat()
        except Exception:
            today = now_iso()[:10]
        body += (
            '<form class="plat-form" method="post" action="/tasks" style="margin-top:12px">'
            + hidden + '<input type="hidden" name="next" value="%s">'
            '<label>New task</label><input name="title" required maxlength="200" placeholder="Follow up">'
            '<label>Due</label><input name="due" type="date" value="%s">'
            '<button class="plat-btn plat-btn-ghost" type="submit">Add task</button></form></div>'
        ) % (h(back), today)
        title = 'Timeline' if tid else 'Activity'
        body += '<div class="plat-panel"><h2>%s</h2>' % title
        body += (
            '<form class="plat-form" method="post" action="/activities" style="margin-bottom:14px">'
            + hidden + '<input type="hidden" name="next" value="%s">'
            '<div class="plat-row"><div><label>Type</label><select name="kind">%s</select></div></div>'
            '<label>What happened</label><textarea name="body" rows="2" required placeholder="Short note"></textarea>'
            '<button class="plat-btn plat-btn-gold" type="submit">Log activity</button></form>'
        ) % (h(back), ''.join('<option value="%s">%s</option>' % (k, lab) for k, lab in ACTIVITY_KINDS))
        if items:
            body += '<ul class="plat-list crm-timeline">'
            for it in items:
                if it['typ'] == 'task':
                    flag = 'Task done' if it.get('done') else 'Task'
                    extra = ('Due ' + h(it.get('due') or '')) if it.get('due') else ''
                else:
                    flag = kind_label(it['kind']); extra = ''
                body += (
                    '<li><span class="plat-pill">%s</span> <span class="muted">%s%s</span>'
                    '<div>%s</div></li>' % (
                        h(flag), h(when_label(it.get('at'))),
                        (' · ' + h(it['who'])) if it.get('who') else '',
                        h(it['body'] or '')))
                if extra:
                    body = body[:-5] + '<div class="muted">%s</div></li>' % extra
            body += '</ul>'
        else:
            body += '<div class="muted">Nothing logged yet.</div>'
        body += '</div>'
        return body

    fo.crm_block = crm_block

    def crm_today_actions(conn, day):
        rows = due_tasks(conn, day)
        if not rows:
            return []
        overdue = sum(1 for r in rows if (r['due'] or '') < day)
        detail = ', '.join((r['title'] or '') for r in rows[:3])
        if overdue:
            detail = '%d overdue. ' % overdue + detail
        icon = fo.ICONS.get('bell') or fo.ICONS.get('note') or '&#128197;'
        return [{'n': len(rows), 'title': 'CRM tasks due', 'detail': detail,
                 'icon': icon, 'href': '/tasks', 'btn': 'Open', 'tone': 'red' if overdue else ''}]

    fo.crm_today_actions = crm_today_actions

    def sales_extra(conn):
        today = fo.today_local()
        ti = today.isoformat()
        week = (today - timedelta(days=today.weekday())).isoformat()
        month = ti[:7]
        open_tasks = conn.execute('SELECT COUNT(*) FROM crm_tasks WHERE done=0').fetchone()[0]
        due = conn.execute('SELECT COUNT(*) FROM crm_tasks WHERE done=0 AND due IS NOT NULL AND due<=?', (ti,)).fetchone()[0]
        week_acts = conn.execute('SELECT COUNT(*) FROM crm_activities WHERE happened_at>=?', (week,)).fetchone()[0]
        quotes_sent = 0
        quotes_won = 0
        try:
            quotes_sent = conn.execute("SELECT COUNT(*) FROM quotes WHERE status IN ('sent','accepted') AND IFNULL(updated_at,'')>=?", (month + '-01',)).fetchone()[0]
            quotes_won = conn.execute("SELECT COUNT(*) FROM quotes WHERE status='accepted' AND IFNULL(accepted_at,'')>=?", (month + '-01',)).fetchone()[0]
        except Exception:
            pass
        body = '<div class="plat-stats" style="margin-top:8px">'
        body += (
            '<a class="plat-stat" href="/tasks"><small>Open tasks</small><b>%d</b><div class="muted">%d due today or earlier</div></a>'
            '<a class="plat-stat" href="/tasks"><small>This week</small><b>%d</b><div class="muted">Calls, meetings and notes</div></a>'
            '<a class="plat-stat" href="/quote"><small>Quotes this month</small><b>%d</b><div class="muted">%d accepted</div></a>'
        ) % (open_tasks, due, week_acts, quotes_sent, quotes_won)
        body += '</div>'
        recent = list(conn.execute(
            'SELECT kind,body,happened_at,created_by FROM crm_activities ORDER BY happened_at DESC, id DESC LIMIT 5'))
        body += '<div class="plat-panel"><div class="plat-panel-h"><h2>Recent activity</h2><a class="plat-btn plat-btn-ghost" href="/tasks">Tasks</a></div>'
        if recent:
            body += '<ul class="plat-list">' + ''.join(
                '<li><span class="plat-pill">%s</span> <span class="muted">%s</span><div>%s</div></li>' % (
                    h(kind_label(r['kind'])), h(when_label(r['happened_at'])), h((r['body'] or '')[:200]))
                for r in recent) + '</ul>'
        else:
            body += '<div class="muted">Log a call, meeting or note on a company, contact or opportunity. Signing a quote moves that opportunity to Won.</div>'
        body += '</div>'
        return body

    fo.crm_sales_extra = sales_extra

    @app.route('/activities', methods=['POST'])
    def activities_new():
        gate = office_only()
        if gate:
            return gate
        company_id, contact_id, opportunity_id = ids_from()
        conn = fo.get_db()
        _, err = add_activity(
            conn, request.form.get('kind') or 'note', request.form.get('body'),
            company_id, contact_id, opportunity_id, created_by=fo.me())
        if not err:
            conn.commit()
        conn.close()
        nxt = request.form.get('next') or '/sales'
        if not nxt.startswith('/'):
            nxt = '/sales'
        return redirect(nxt)

    @app.route('/tasks', methods=['GET', 'POST'])
    def tasks_page():
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        msg = ''
        if request.method == 'POST':
            company_id, contact_id, opportunity_id = ids_from()
            _, err = add_task(
                conn, request.form.get('title'), request.form.get('due'),
                request.form.get('notes') or '', company_id, contact_id, opportunity_id, fo.me())
            if err:
                msg = err
            else:
                conn.commit()
                nxt = request.form.get('next') or '/tasks'
                if not nxt.startswith('/'):
                    nxt = '/tasks'
                conn.close()
                return redirect(nxt)
        show = request.args.get('show') or 'open'
        sql = '''SELECT t.*, c.name AS company, ct.name AS contact, o.title AS opportunity
                 FROM crm_tasks t
                 LEFT JOIN companies c ON c.id=t.company_id
                 LEFT JOIN contacts ct ON ct.id=t.contact_id
                 LEFT JOIN opportunities o ON o.id=t.opportunity_id'''
        if show == 'done':
            sql += ' WHERE t.done=1 ORDER BY t.done_at DESC, t.id DESC'
        else:
            sql += ' WHERE t.done=0 ORDER BY IFNULL(t.due,\'9999\'), t.id'
        rows = conn.execute(sql).fetchall()
        companies = conn.execute('SELECT id,name FROM companies WHERE IFNULL(archived,0)=0 ORDER BY name').fetchall()
        today = fo.today_local().isoformat()
        conn.close()
        body = '<div class="plat-pills">'
        body += '<a class="plat-pill-link%s" href="/tasks">Open</a>' % (' on' if show != 'done' else '')
        body += '<a class="plat-pill-link%s" href="/tasks?show=done">Done</a></div>' % (' on' if show == 'done' else '')
        if rows:
            body += '<ul class="plat-list">'
            for r in rows:
                link = ''
                if r['opportunity']:
                    link = '<a href="/opportunities/%d">%s</a>' % (r['opportunity_id'], h(r['opportunity']))
                elif r['contact']:
                    link = '<a href="/contacts/%d">%s</a>' % (r['contact_id'], h(r['contact']))
                elif r['company']:
                    link = '<a href="/customers/%d">%s</a>' % (r['company_id'], h(r['company']))
                due = r['due'] or 'No date'
                tone = ' style="color:#b00020"' if (not r['done'] and r['due'] and r['due'] < today) else ''
                body += '<li><b>%s</b> <span class="muted"%s>%s</span>' % (h(r['title']), tone, h(due))
                if link:
                    body += '<div class="muted">%s</div>' % link
                if r['notes']:
                    body += '<div class="muted">%s</div>' % h(r['notes'])
                if not r['done']:
                    body += ('<form method="post" action="/tasks/%d/done" style="display:inline">'
                             '<button class="plat-link-btn" type="submit">Mark done</button></form>' % r['id'])
                body += '</li>'
            body += '</ul>'
        else:
            body += '<div class="muted">No tasks in this list.</div>'
        opts = ''.join('<option value="%d">%s</option>' % (c['id'], h(c['name'])) for c in companies)
        body += (
            '<div class="plat-panel" style="margin-top:18px"><h2>Add task</h2>'
            '<form class="plat-form" method="post">'
            '<label>Title</label><input name="title" required maxlength="200">'
            '<div class="plat-row"><div><label>Due</label><input name="due" type="date" value="%s"></div>'
            '<div><label>Company (optional)</label><select name="company_id"><option value="">None</option>%s</select></div></div>'
            '<label>Notes</label><textarea name="notes" rows="2"></textarea>'
            '<button class="plat-btn plat-btn-gold" type="submit">Add task</button></form></div>'
        ) % (today, opts)
        return page('Tasks', body, kicker='Sales', msg=msg)

    @app.route('/tasks/<int:tid>/done', methods=['POST'])
    def tasks_done(tid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        complete_task(conn, tid, fo.me())
        conn.commit(); conn.close()
        nxt = request.form.get('next') or '/tasks'
        if not nxt.startswith('/'):
            nxt = '/tasks'
        return redirect(nxt)
