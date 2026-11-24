"""Sub compliance (office only): COI, W-9, signed agreement, and a 1099 report.

Expiring and expired items show on Today. Scheduling a non-compliant sub shows a
warning but is not a hard block. 1099 totals come from timesheet hours and rates.
Money stays in the office. The app does not send anything.
"""
from __future__ import annotations

import csv
import io
import os
import uuid
from datetime import datetime, timedelta

COI_KIND = 'insurance'  # existing sub_docs kind; labeled COI
W9_KIND = 'w9'
SOON_DAYS = 30


def now_iso():
    return datetime.utcnow().isoformat()


def ckey(name):
    return ' '.join((name or '').split()).lower()


def _expires(row):
    return (row['expires'] if row else '') or ''


def coi_state(row, today, soon):
    if not row or not row.get('filename'):
        return 'missing', 'Not on file'
    exp = _expires(row)
    if not exp:
        return 'ok', 'On file (no expiration date)'
    if exp < today:
        return 'expired', 'Expired %s' % exp
    if exp <= soon:
        return 'expiring', 'Expires %s' % exp
    return 'ok', 'Expires %s' % exp


def w9_state(row):
    if not row:
        return 'missing', 'W-9 is not on file'
    fn = (row['filename'] or '').strip()
    orig = (row['orig_name'] or '').strip().lower().replace('_', '-')
    if fn or orig == 'on-file':
        return 'ok', 'W-9 is on file'
    return 'missing', 'W-9 is not on file'


def agreement_state(fo, conn, sub):
    try:
        if fo.signed_current(conn, sub):
            return 'ok', 'Current agreement is signed'
    except Exception:
        pass
    return 'missing', 'Current agreement is not signed'


def compliance_for(fo, conn, sub, today=None):
    today = today or fo.today_local().isoformat()
    soon = (fo.today_local() + timedelta(days=SOON_DAYS)).isoformat()
    key = ckey(sub)
    docs = {r['kind']: dict(r) for r in conn.execute('SELECT * FROM sub_docs WHERE sub=?', (key,))}
    coi_st, coi_msg = coi_state(docs.get(COI_KIND) or docs.get('coi'), today, soon)
    w9_st, w9_msg = w9_state(docs.get(W9_KIND))
    ag_st, ag_msg = agreement_state(fo, conn, sub)
    parts = [coi_msg, w9_msg, ag_msg]
    ok = coi_st == 'ok' and w9_st == 'ok' and ag_st == 'ok'
    severe = coi_st in ('expired', 'missing') or w9_st == 'missing' or ag_st == 'missing'
    return {
        'sub': sub, 'ok': ok, 'severe': severe,
        'coi': coi_st, 'w9': w9_st, 'agreement': ag_st,
        'messages': parts,
        'warning': None if ok else ('%s: %s.' % (sub, '; '.join(p for p in parts if 'not' in p or 'expired' in p or 'expires' in p))),
        'docs': docs,
    }


def list_subs(fo, conn):
    names = list(fo.sub_logins())
    seen = {ckey(n) for n in names}
    for r in conn.execute("SELECT DISTINCT TRIM(tech) t FROM jobs WHERE IFNULL(TRIM(tech),'')!=''"):
        if ckey(r['t']) and ckey(r['t']) not in seen and ckey(r['t']) not in getattr(fo, 'OFFICE_NAMES', ()):
            names.append(fo.nice_name(r['t'])); seen.add(ckey(r['t']))
    for r in conn.execute("SELECT DISTINCT sub FROM sub_docs"):
        if ckey(r['sub']) not in seen:
            names.append(fo.nice_name(r['sub'])); seen.add(ckey(r['sub']))
    return names


def alerts(fo, conn):
    today = fo.today_local().isoformat()
    soon = (fo.today_local() + timedelta(days=SOON_DAYS)).isoformat()
    out = []
    for name in list_subs(fo, conn):
        c = compliance_for(fo, conn, name, today)
        if c['ok']:
            continue
        if c['coi'] == 'expired':
            out.append({'sub': name, 'kind': 'COI', 'expires': '', 'expired': True, 'missing': False})
        elif c['coi'] == 'expiring':
            row = c['docs'].get(COI_KIND) or {}
            out.append({'sub': name, 'kind': 'COI', 'expires': row.get('expires') or '', 'expired': False, 'missing': False})
        elif c['coi'] == 'missing':
            out.append({'sub': name, 'kind': 'COI', 'expires': '', 'expired': False, 'missing': True})
        if c['w9'] == 'missing':
            out.append({'sub': name, 'kind': 'W-9', 'expires': '', 'expired': False, 'missing': True})
        if c['agreement'] == 'missing':
            out.append({'sub': name, 'kind': 'Agreement', 'expires': '', 'expired': False, 'missing': True})
    return out


def warning_for(fo, conn, sub):
    if not sub:
        return None
    c = compliance_for(fo, conn, sub)
    if c['ok']:
        return None
    return c['warning'] + ' You can still schedule. This is a warning only.'


def year_hours(conn, sub, year):
    start = '%d-01-01' % year
    end = '%d-01-01' % (year + 1)
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


def year_report(fo, conn, year):
    rates = {r['key']: r for r in conn.execute('SELECT * FROM subs')}
    rows = []
    for name in list_subs(fo, conn):
        mins = year_hours(conn, name, year)
        if mins <= 0:
            continue
        rate = rates.get(ckey(name)) or {}
        rh = rate['rate_hourly'] if rate and rate['rate_hourly'] is not None else None
        hours = round(mins / 60, 2)
        pay = round(hours * (rh or 0), 2) if rh is not None else None
        rows.append({'sub': name, 'year': year, 'hours': hours, 'rate_hourly': rh, 'pay': pay})
    return rows


def register(fo):
    from flask import request, redirect, session, Response
    from werkzeug.utils import secure_filename

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

    fo.compliance_alerts = lambda conn: alerts(fo, conn)
    fo.compliance_warning = lambda conn, sub: warning_for(fo, conn, sub)

    def today_actions(conn, day):
        rows = alerts(fo, conn)
        if not rows:
            return []
        expired = sum(1 for r in rows if r.get('expired'))
        missing = sum(1 for r in rows if r.get('missing'))
        detail = ', '.join('%s %s%s' % (r['sub'], r['kind'], ' expired' if r.get('expired') else (' missing' if r.get('missing') else ' expiring')) for r in rows[:4])
        icon = fo.ICONS.get('alert') or fo.ICONS.get('doc') or '!'
        return [{'n': len(rows), 'title': 'Sub compliance', 'detail': detail,
                 'icon': icon, 'href': '/compliance', 'btn': 'Check', 'tone': 'red' if expired or missing else ''}]

    fo.compliance_today_actions = today_actions

    def save_doc(conn, sub, kind, files, expires=None):
        key = ckey(sub)
        if not key or kind not in (COI_KIND, 'coi', W9_KIND):
            return 'Bad request.'
        kind = COI_KIND if kind == 'coi' else kind
        f = files.get('file') if files else None
        old = conn.execute('SELECT * FROM sub_docs WHERE sub=? AND kind=?', (key, kind)).fetchone()
        exp = expires
        if exp:
            try:
                exp = datetime.strptime(exp, '%Y-%m-%d').date().isoformat()
            except ValueError:
                return 'Expiration date must be YYYY-MM-DD.'
        elif kind == COI_KIND and not old:
            return 'COI needs an expiration date.'
        else:
            exp = (old['expires'] if old else None)
        fn = old['filename'] if old else None
        orig = old['orig_name'] if old else None
        uploaded = old['uploaded_at'] if old else None
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ('.pdf', '.jpg', '.jpeg', '.png', '.heic', '.webp'):
                return 'PDF or photo only.'
            folder = fo.app.config['UPLOAD_FOLDER']
            os.makedirs(folder, exist_ok=True)
            fn = secure_filename('subdoc_%s_%s_%s%s' % (key, kind, uuid.uuid4().hex[:8], ext))
            f.save(os.path.join(folder, fn))
            if old and old['filename']:
                try:
                    os.remove(os.path.join(folder, secure_filename(old['filename'])))
                except OSError:
                    pass
            orig = secure_filename(f.filename)[:100]
            uploaded = now_iso()
        elif kind == W9_KIND and request.form.get('on_file') == '1' and not fn:
            # marked on file without a scan: store a placeholder so the flag is explicit
            fn = ''; orig = orig or 'on-file'; uploaded = now_iso()
        if kind == W9_KIND and not fn and request.form.get('on_file') != '1' and not (old and old['filename']):
            return 'Upload the W-9 or mark it on file.'
        # For W-9 marked on file without upload, keep filename empty but set orig_name
        if kind == W9_KIND and not fn and request.form.get('on_file') == '1':
            fn = old['filename'] if old and old['filename'] else ''
        conn.execute(
            'INSERT OR REPLACE INTO sub_docs (sub,kind,filename,orig_name,expires,uploaded_at) VALUES (?,?,?,?,?,?)',
            (key, kind, fn or (old['filename'] if old else '') or '', orig or '', exp, uploaded or now_iso()))
        return None

    # Treat W-9 on file: w9_state checks filename OR we'll also treat orig_name=='on-file'
    # Patch w9_state usage: if orig_name and not filename, still ok.

    @app.route('/compliance', methods=['GET', 'POST'])
    def compliance_page():
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        msg = ''
        if request.method == 'POST':
            err = save_doc(conn, request.form.get('name'), request.form.get('kind'), request.files, request.form.get('expires'))
            if err:
                msg = err
            else:
                conn.commit(); msg = 'Saved.'
        names = list_subs(fo, conn)
        rows = [compliance_for(fo, conn, n) for n in names]
        year = fo.today_local().year
        body = '<p class="muted">Office only. COI expiration, W-9, and the signed sub agreement (the same countersign flow as Agreements). Scheduling a sub who is short on any of these shows a warning, but you can still book them.</p>'
        for c in rows:
            flag = 'ok' if c['ok'] else 'warn'
            body += '<div class="plat-panel"><div class="plat-panel-h"><h2>%s</h2><span class="plat-pill">%s</span></div>' % (
                h(c['sub']), 'All set' if c['ok'] else 'Needs attention')
            body += '<ul class="plat-list">'
            body += '<li><b>COI</b> <span class="muted">%s</span></li>' % h(c['messages'][0])
            body += '<li><b>W-9</b> <span class="muted">%s</span></li>' % h(c['messages'][1])
            body += '<li><b>Agreement</b> <span class="muted">%s</span> <a href="/agreement">Open agreements</a></li>' % h(c['messages'][2])
            body += '</ul>'
            body += (
                '<form class="plat-form" method="post" enctype="multipart/form-data" style="margin-top:10px">'
                '<input type="hidden" name="name" value="%s"><input type="hidden" name="kind" value="insurance">'
                '<label>COI file and expiration</label>'
                '<div class="plat-row"><div><input type="file" name="file" accept=".pdf,.png,.jpg,.jpeg,.webp,.heic"></div>'
                '<div><input type="date" name="expires"></div>'
                '<div><button class="plat-btn plat-btn-ghost" type="submit">Save COI</button></div></div></form>'
            ) % h(c['sub'])
            body += (
                '<form class="plat-form" method="post" enctype="multipart/form-data">'
                '<input type="hidden" name="name" value="%s"><input type="hidden" name="kind" value="w9">'
                '<label>W-9</label>'
                '<div class="plat-row"><div><input type="file" name="file" accept=".pdf,.png,.jpg,.jpeg,.webp,.heic"></div>'
                '<div style="display:flex;align-items:center;gap:8px"><label style="margin:0"><input type="checkbox" name="on_file" value="1"> On file</label></div>'
                '<div><button class="plat-btn plat-btn-ghost" type="submit">Save W-9</button></div></div></form>'
            ) % h(c['sub'])
            body += '</div>'
        if not rows:
            body += '<div class="muted">No subs to review yet.</div>'
        body += '<p><a class="plat-btn plat-btn-ghost" href="/compliance/1099?year=%d">1099 totals for %d</a></p>' % (year, year)
        conn.close()
        return page('Sub compliance', body, msg=msg)

    @app.route('/compliance/1099')
    def compliance_1099():
        gate = office_only()
        if gate:
            return gate
        try:
            year = int(request.args.get('year') or fo.today_local().year)
        except ValueError:
            year = fo.today_local().year
        conn = fo.get_db()
        rows = year_report(fo, conn, year)
        conn.close()
        body = '<p class="muted">Totals from clock-in hours times each sub\'s hourly rate this calendar year. Flat job rates are not included. This is for the office. Nothing is sent.</p>'
        body += '<div class="sked-nav" style="margin:0 0 16px"><a class="plat-btn plat-btn-ghost" href="/compliance/1099?year=%d">%d</a>' % (year-1, year-1)
        body += '<a class="plat-btn plat-btn-gold" href="/compliance/1099?year=%d">%d</a>' % (year, year)
        body += '<a class="plat-btn plat-btn-ghost" href="/compliance/1099?year=%d">%d</a></div>' % (year+1, year+1)
        body += '<div class="plat-panel" style="overflow:auto"><table class="cost-table"><thead><tr>'
        body += ''.join('<th>%s</th>' % x for x in ('Sub', 'Hours', 'Hourly rate', 'Total'))
        body += '</tr></thead><tbody>'
        grand = 0
        for r in rows:
            pay = r['pay']
            if pay is not None:
                grand += pay
            body += '<tr><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                h(r['sub']), r['hours'], money(r['rate_hourly'] or 0) if r['rate_hourly'] is not None else 'No rate',
                money(pay) if pay is not None else 'No rate')
        if not rows:
            body += '<tr><td colspan="4" class="muted">No clock-ins this year.</td></tr>'
        else:
            body += '<tr><th>Total</th><th></th><th></th><th>%s</th></tr>' % money(grand)
        body += '</tbody></table></div>'
        body += '<p><a class="plat-btn plat-btn-ghost" href="/compliance/1099.csv?year=%d">Download CSV</a></p>' % year
        return page('1099 totals, %d' % year, body, back='/compliance')

    @app.route('/compliance/1099.csv')
    def compliance_1099_csv():
        gate = office_only()
        if gate:
            return gate
        try:
            year = int(request.args.get('year') or fo.today_local().year)
        except ValueError:
            year = fo.today_local().year
        conn = fo.get_db()
        rows = year_report(fo, conn, year)
        conn.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['sub', 'year', 'hours', 'rate_hourly', 'total'])
        for r in rows:
            w.writerow([r['sub'], year, r['hours'], r['rate_hourly'] if r['rate_hourly'] is not None else '', r['pay'] if r['pay'] is not None else ''])
        return Response(buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=1099-%d.csv' % year})

