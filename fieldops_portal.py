"""Client portal: a view-only tokenized link per company or site.

Shows installed systems, job history, quotes, and invoice status. Clients can
request service (creates a ticket). Pay now appears only for invoices that were
created or paid in QuickBooks and have an https payment link. The app never
sends the link or the invoice. Equipment passwords never appear.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

PORTAL_KIND = 'portal'
DEFAULT_DAYS = 365
LICENSE_NO = 'B31163401'

def portal_brand(conn, row=None):
    oid = 1
    try:
        import fieldops_orgs as _orgs
        if row is not None:
            try:
                if 'org_id' in row.keys() and row['org_id']:
                    oid = int(row['org_id'])
            except Exception:
                pass
            cid = row['company_id'] if row['company_id'] else None
            if cid:
                co = conn.execute('SELECT * FROM companies WHERE id=?', (cid,)).fetchone()
                if co:
                    try:
                        if 'org_id' in co.keys() and co['org_id']:
                            oid = int(co['org_id'])
                    except Exception:
                        pass
        else:
            oid = _orgs.current_org_id()
        return _orgs.branding(oid)
    except Exception:
        return {'name': 'True Sense Security', 'license': LICENSE_NO, 'logo_url': '/static/logo.png'}



def now_iso():
    return datetime.utcnow().isoformat(timespec='seconds')


def _cols(conn, table):
    return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}


def _add_col(conn, table, coldef):
    name = coldef.split()[0]
    if name in _cols(conn, table):
        return
    conn.execute('ALTER TABLE %s ADD COLUMN %s' % (table, coldef))


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS share_links (
        token TEXT PRIMARY KEY, job_id TEXT, kind TEXT, created_by TEXT, created_at TEXT,
        expires_at TEXT, revoked_at TEXT, revoked_by TEXT, views INTEGER DEFAULT 0, last_view TEXT,
        company_id INTEGER, site_id INTEGER)''')
    _add_col(conn, 'share_links', 'company_id INTEGER')
    _add_col(conn, 'share_links', 'site_id INTEGER')
    try:
        _add_col(conn, 'qbo_drafts', 'pay_link TEXT')
    except Exception:
        pass
    conn.execute('CREATE INDEX IF NOT EXISTS ix_share_company ON share_links(company_id)')


def pick_pay_link(data):
    if not data:
        return ''
    link = str(data.get('pay_link') or data.get('invoiceLink') or data.get('InvoiceLink') or '').strip()[:500]
    if link and not link.startswith('https://'):
        return None  # invalid
    return link


def store_pay_link(conn, job_id=None, draft_id=None, link='', status=''):
    if not link:
        return
    if draft_id:
        conn.execute('UPDATE qbo_drafts SET pay_link=? WHERE id=?', (link, draft_id))
        if not job_id:
            row = conn.execute('SELECT job_id FROM qbo_drafts WHERE id=?', (draft_id,)).fetchone()
            job_id = row['job_id'] if row else None
    if job_id and status in ('created', 'paid'):
        conn.execute('UPDATE jobs SET pay_link=? WHERE id=?', (link, job_id))


def link_active(row):
    if not row or (row['kind'] or '') != PORTAL_KIND:
        return False
    if row['revoked_at']:
        return False
    exp = row['expires_at'] or ''
    if exp and exp[:19] < now_iso()[:19]:
        return False
    return True


def create_link(conn, company_id, site_id=None, who='', days=DEFAULT_DAYS):
    try:
        import fieldops_orgs as _orgs
        co = _orgs.get_company(conn, company_id)
    except Exception:
        co = conn.execute('SELECT * FROM companies WHERE id=?', (company_id,)).fetchone()
    if not co:
        return None, 'Company not found.'
    if site_id:
        site = conn.execute('SELECT * FROM sites WHERE id=? AND company_id=?', (site_id, company_id)).fetchone()
        if not site:
            return None, 'Site not found.'
    token = secrets.token_urlsafe(24)
    when = now_iso()
    exp = (datetime.utcnow() + timedelta(days=max(1, int(days or DEFAULT_DAYS)))).isoformat(timespec='seconds')
    conn.execute(
        '''INSERT INTO share_links (token,job_id,kind,created_by,created_at,expires_at,company_id,site_id,org_id)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (token, None, PORTAL_KIND, who or '', when, exp, company_id, site_id, (co['org_id'] if co and 'org_id' in co.keys() and co['org_id'] else 1)))
    return conn.execute('SELECT * FROM share_links WHERE token=?', (token,)).fetchone(), None


def revoke_link(conn, token, who=''):
    n = conn.execute(
        "UPDATE share_links SET revoked_at=?, revoked_by=? WHERE token=? AND kind=? AND revoked_at IS NULL",
        (now_iso(), who or '', token, PORTAL_KIND)).rowcount
    return n > 0


def get_link(conn, token):
    return conn.execute('SELECT * FROM share_links WHERE token=?', (token,)).fetchone()


def record_view(conn, token):
    conn.execute(
        'UPDATE share_links SET views=COALESCE(views,0)+1, last_view=? WHERE token=?',
        (now_iso(), token))


def invoices_for(conn, company_id, customer=''):
    """Sent invoices only (created or paid). Drafts and unsent stay hidden from Pay now."""
    out = []
    seen = set()
    jobs = list(conn.execute(
        'SELECT * FROM jobs WHERE company_id=? OR lower(trim(customer))=lower(trim(?)) ORDER BY created_at DESC',
        (company_id, customer or '')))
    job_ids = [j['id'] for j in jobs]
    by_id = {j['id']: dict(j) for j in jobs}

    def add(status, title, doc, amount, link, job_id, paid=False):
        key = (job_id or '') + '|' + (doc or '') + '|' + (title or '')
        if key in seen:
            return
        seen.add(key)
        st = (status or '').lower()
        https = (link or '').startswith('https://')
        show_pay = https and st in ('created', 'paid') and not paid
        # if paid, hide Pay now and show Paid
        if st == 'paid':
            show_pay = False
            paid = True
        lab = 'Paid' if paid or st == 'paid' else ('Sent' if st in ('created',) else st or 'Invoice')
        if st in ('draft', 'queued', 'error', ''):
            return  # never show pay for unsent
        if st not in ('created', 'paid'):
            return
        out.append({
            'title': title or (by_id.get(job_id) or {}).get('title') or (doc or job_id or 'Invoice'),
            'doc': doc or '', 'status': lab, 'amount': amount, 'pay_link': link if show_pay else '',
            'paid': paid, 'job_id': job_id or '',
        })

    if job_ids:
        qmarks = ','.join('?' * len(job_ids))
        for r in conn.execute('SELECT * FROM qbo_queue WHERE job_id IN (%s) ORDER BY updated_at DESC' % qmarks, job_ids):
            job = by_id.get(r['job_id']) or {}
            link = r['pay_link'] if 'pay_link' in r.keys() and r['pay_link'] else (job.get('pay_link') if job else '')
            # qbo_queue may not have pay_link col; use job
            if 'pay_link' not in r.keys():
                link = job.get('pay_link') if job else ''
            else:
                link = r['pay_link'] or (job.get('pay_link') if job else '')
            add(r['status'], job.get('title') if job else '', r['doc_number'], r['total'], link, r['job_id'],
                paid=bool(job.get('paid_at') if job else False) or r['status'] == 'paid')
        try:
            for r in conn.execute('SELECT * FROM qbo_drafts WHERE job_id IN (%s) ORDER BY id DESC' % qmarks, job_ids):
                job = by_id.get(r['job_id']) or {}
                link = ''
                if 'pay_link' in r.keys():
                    link = r['pay_link'] or ''
                if not link:
                    link = job.get('pay_link') if job else ''
                add(r['status'], job.get('title') if job else (r['memo'] or ''), r['doc_number'], r['amount'], link, r['job_id'],
                    paid=r['status'] == 'paid')
        except Exception:
            pass
    # plan drafts tied to this company via payload customer name or plan
    try:
        for r in conn.execute("SELECT * FROM qbo_drafts WHERE IFNULL(job_id,'') LIKE 'PLAN-%' ORDER BY id DESC LIMIT 50"):
            payload = {}
            try:
                payload = json.loads(r['payload'] or '{}')
            except Exception:
                payload = {}
            name = ((payload.get('customer') or {}).get('name') or '')
            if customer and name.strip().lower() != (customer or '').strip().lower():
                continue
            link = r['pay_link'] if 'pay_link' in r.keys() else ''
            add(r['status'], r['memo'] or 'Service plan', r['doc_number'], r['amount'], link, r['job_id'],
                paid=r['status'] == 'paid')
    except Exception:
        pass
    return out


def register(fo):
    from flask import request, redirect, render_template_string
    import fieldops_tickets as tix
    import fieldops_platform as plat

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
        from flask import session
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

    def company_card(conn, cid):
        conn = fo.get_db()
        try:
            return _company_card_body(conn, cid)
        finally:
            conn.close()

    def _company_card_body(conn, cid):
        rows = list(conn.execute(
            "SELECT * FROM share_links WHERE kind=? AND company_id=? ORDER BY created_at DESC",
            (PORTAL_KIND, cid)))
        html = '<div class="plat-panel"><div class="plat-panel-h"><h2>Client portal</h2></div>'
        html += '<p class="muted">A view-only link for this company. Copy it and send it yourself. The app does not send anything. Equipment passwords are never shown.</p>'
        active = [r for r in rows if link_active(r)]
        if active:
            for r in active:
                url = request.host_url.rstrip('/') + '/p/' + r['token']
                html += '<div class="plat-card" style="margin:8px 0"><div class="muted">Expires %s</div>' % h((r['expires_at'] or '')[:10])
                html += '<input value="%s" readonly style="width:100%%;margin:6px 0" id="pl-%s">' % (h(url), h(r['token'][:8]))
                html += '<button type="button" class="plat-btn plat-btn-gold" onclick="navigator.clipboard.writeText(document.getElementById(\'pl-%s\').value)">Copy link</button>' % h(r['token'][:8])
                html += ' <a class="plat-btn plat-btn-ghost" href="/p/%s" target="_blank">Open</a>' % h(r['token'])
                html += ('<form method="post" action="/portal/%s/revoke" style="display:inline">'
                         '<button class="plat-btn plat-btn-ghost" type="submit">Revoke</button></form></div>') % h(r['token'])
        html += ('<form method="post" action="/customers/%d/portal">'
                 '<button class="plat-btn plat-btn-ghost" type="submit">Make a new portal link</button></form>' % cid)
        html += '</div>'
        return html

    fo.portal_company_html = lambda conn, cid: company_card(conn, cid)

    @app.route('/customers/<int:cid>/portal', methods=['POST'])
    def portal_create(cid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        row, err = create_link(conn, cid, who=fo.me())
        if err:
            conn.close()
            return redirect('/customers/%d' % cid)
        conn.commit(); conn.close()
        return redirect('/customers/%d' % cid)

    @app.route('/portal/<token>/revoke', methods=['POST'])
    def portal_revoke(token):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        row = get_link(conn, token)
        cid = row['company_id'] if row else 0
        revoke_link(conn, token, fo.me())
        conn.commit(); conn.close()
        return redirect('/customers/%d' % cid if cid else '/customers')

    PORTAL_HTML = r'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>{{ brand_name }}: {{ company }}</title>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#f7f6ef;color:#1d1f14;margin:0}
.wrap{max-width:880px;margin:0 auto;padding:24px 20px 48px}
.hdr{display:flex;align-items:center;gap:16px;border-bottom:2px solid #a49f2a;padding-bottom:16px;margin-bottom:20px}
.hdr img{height:52px}
h1{font-size:1.7rem;margin:8px 0 4px}
h2{font-size:1.05rem;color:#5f5b16;margin:22px 0 8px;border-bottom:2px solid #a49f2a;padding-bottom:4px}
.muted{color:#6b6d60}
.card{background:#fff;border:1px solid #ecece6;border-radius:14px;padding:14px 16px;margin:8px 0}
.pill{display:inline-block;background:#f3f0e0;color:#6b6a16;border-radius:8px;padding:2px 8px;font-size:.8rem;font-weight:700}
.btn{display:inline-block;background:linear-gradient(145deg,#baa205,#7a7a12);color:#fff;border:0;padding:12px 18px;border-radius:10px;font-weight:700;text-decoration:none;font-size:1rem}
.btn.ghost{background:#fff;color:#1d1f14;border:1px solid #ecece6}
input,textarea,select{width:100%;padding:12px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:6px 0;font-family:inherit}
label{font-weight:650;display:block;margin-top:8px}
.ok{background:#e8f5e9;color:#1b5e20;border-radius:12px;padding:12px;font-weight:700}
.foot{margin-top:28px;text-align:center;color:#6b6d60;font-size:.9rem}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #eee;padding:8px 6px;text-align:left}
</style></head><body>
<div class="wrap">
<div class="hdr">
<img src="{{ logo_url }}" alt="{{ brand_name }}">
<div><div style="font-weight:800">{{ brand_name }}</div><div class="muted">License {{ license }}</div></div>
</div>
{% if msg %}<div class="ok">{{ msg }}</div>{% endif %}
<h1>{{ company }}</h1>
{% if site %}<div class="muted">{{ site }}</div>{% endif %}
<p class="muted">This is a view-only portal. Request service below if you need a visit.</p>

<h2>Installed systems</h2>
{% if equipment %}
<table><tr><th>Device</th><th>Model</th><th>Installed</th><th>Warranty</th></tr>
{% for e in equipment %}
<tr><td>{{ e.device }}</td><td>{{ e.model }}</td><td>{{ e.install_date }}</td><td>{{ e.warranty_end }}</td></tr>
{% endfor %}
</table>
{% else %}
<div class="card muted">No installed equipment is on file for this site yet.</div>
{% endif %}

<h2>Job history</h2>
{% if jobs %}
{% for j in jobs %}
<div class="card"><b>{{ j.title }}</b> <span class="pill">{{ j.status }}</span>
<div class="muted">{{ j.id }} · {{ j.job_type }} · {{ j.when }}</div></div>
{% endfor %}
{% else %}
<div class="card muted">No jobs on file yet.</div>
{% endif %}

<h2>Quotes</h2>
{% if quotes %}
{% for q in quotes %}
<div class="card"><b>{{ q.title }}</b> <span class="pill">{{ q.status }}</span>
<div class="muted">{{ q.number }} · {{ q.total }}</div>
<a class="btn ghost" href="/q/{{ q.token }}">View estimate</a></div>
{% endfor %}
{% else %}
<div class="card muted">No estimates have been shared yet.</div>
{% endif %}

<h2>Invoices</h2>
{% if invoices %}
{% for inv in invoices %}
<div class="card"><b>{{ inv.title }}</b> <span class="pill">{{ inv.status }}</span>
{% if inv.doc %}<div class="muted">{{ inv.doc }}</div>{% endif %}
{% if inv.amount %}<div>{{ money(inv.amount) }}</div>{% endif %}
{% if inv.paid %}<div class="muted">Paid</div>{% endif %}
{% if inv.pay_link %}<p><a class="btn" href="{{ inv.pay_link }}" target="_blank" rel="noopener">Pay now</a></p>{% endif %}
</div>
{% endfor %}
{% else %}
<div class="card muted">No sent invoices to show.</div>
{% endif %}

<h2>Request service</h2>
<div class="card">
<form method="post" action="/p/{{ token }}/service">
<label>What needs attention</label>
<textarea name="issue" rows="4" required maxlength="2000" placeholder="Describe the issue"></textarea>
<label>Your name</label>
<input name="contact_name" maxlength="100" placeholder="Optional">
<label>Phone or email</label>
<input name="phone" maxlength="80" placeholder="How should we reach you">
<button class="btn" type="submit">Submit request</button>
</form>
<p class="muted">This opens a service ticket in our office. The portal does not send email.</p>
</div>
<div class="foot">{{ brand_name }} · License {{ license }}</div>
</div></body></html>'''

    def _portal_context(conn, link):
        co = conn.execute('SELECT * FROM companies WHERE id=?', (link['company_id'],)).fetchone()
        site = None
        if link['site_id']:
            site = conn.execute('SELECT * FROM sites WHERE id=?', (link['site_id'],)).fetchone()
        company = co['name'] if co else 'Your account'
        site_label = (site['address'] or site['name']) if site else ''
        customer = company
        # equipment: never include password or notes
        if site:
            eq = list(conn.execute(
                'SELECT device,model,install_date,warranty_end FROM equipment WHERE lower(trim(client))=lower(trim(?)) AND lower(trim(site))=lower(trim(?)) ORDER BY id DESC',
                (customer, site['address'] or site['name'] or '')))
        else:
            eq = list(conn.execute(
                'SELECT device,model,install_date,warranty_end FROM equipment WHERE lower(trim(client))=lower(trim(?)) ORDER BY id DESC',
                (customer,)))
        equipment = [{'device': (r['device'] or '-') if r['device'] else '-',
                      'model': r['model'] or '-',
                      'install_date': r['install_date'] or '-',
                      'warranty_end': r['warranty_end'] or '-'} for r in eq]
        jobs_q = 'SELECT id,title,job_type,status,scheduled_date,completed_at,created_at,address FROM jobs WHERE company_id=?'
        args = [link['company_id']]
        if site:
            jobs_q += ' AND (site_id=? OR lower(trim(IFNULL(address,\'\')))=?)'
            args.extend([site['id'], (site['address'] or '').strip().lower()])
        jobs_q += " ORDER BY COALESCE(completed_at,scheduled_date,created_at) DESC LIMIT 40"
        jobs = []
        for j in conn.execute(jobs_q, args):
            when = (j['completed_at'] or j['scheduled_date'] or j['created_at'] or '')[:10]
            jobs.append({'id': j['id'], 'title': j['title'] or j['id'], 'job_type': j['job_type'] or '',
                         'status': j['status'] or '', 'when': when})
        quotes = []
        qsql = "SELECT * FROM quotes WHERE company_id=? AND token IS NOT NULL AND token!='' AND status IN ('sent','accepted') ORDER BY id DESC"
        for q in conn.execute(qsql, (link['company_id'],)):
            if 'site_id' in q.keys() and link['site_id'] and q['site_id'] and q['site_id'] != link['site_id']:
                continue
            quotes.append({
                'title': q['title'] or 'Estimate', 'status': 'Accepted' if q['status']=='accepted' else 'Ready',
                'number': 'Q-%d' % q['id'], 'total': money(q['total'] or 0), 'token': q['token'],
            })
        invoices = invoices_for(conn, link['company_id'], customer)
        brand = portal_brand(conn, link)
        return dict(company=company, site=site_label, equipment=equipment, jobs=jobs, quotes=quotes,
                    invoices=invoices, token=link['token'], license=brand['license'], brand_name=brand['name'], logo_url=brand['logo_url'], money=money)

    @app.route('/p/<token>')
    def portal_view(token):
        conn = fo.get_db()
        link = get_link(conn, token)
        if not link or not link_active(link):
            conn.close()
            return ('This portal link is not valid.', 404)
        record_view(conn, token)
        ctx = _portal_context(conn, link)
        conn.commit(); conn.close()
        return render_template_string(PORTAL_HTML, msg='', **ctx)

    @app.route('/p/<token>/service', methods=['POST'])
    def portal_service(token):
        conn = fo.get_db()
        link = get_link(conn, token)
        if not link or not link_active(link):
            conn.close()
            return ('This portal link is not valid.', 404)
        issue = (request.form.get('issue') or '').strip()
        phone = (request.form.get('phone') or '').strip()[:80]
        name = (request.form.get('contact_name') or '').strip()[:100]
        msg = ''
        if not issue:
            msg = 'Describe the issue first.'
        else:
            if name and issue:
                issue = '%s: %s' % (name, issue)
            row, err = tix.create_ticket(conn, {
                'company_id': link['company_id'], 'site_id': link['site_id'],
                'phone': phone, 'issue': issue, 'priority': 'normal',
            }, created_by=name or 'Client portal', source='portal')
            if err:
                msg = err
            else:
                conn.commit()
                msg = 'Thanks. Your request was sent to our office as %s.' % row['number']
        ctx = _portal_context(conn, link)
        conn.close()
        return render_template_string(PORTAL_HTML, msg=msg, **ctx)

    fo.pick_pay_link = pick_pay_link
    fo.store_pay_link = store_pay_link

