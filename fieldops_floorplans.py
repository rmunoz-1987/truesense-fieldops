"""Floor Plans list: every job or survey with a plan, plus New plan."""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime

import fieldops_favorites as favs
import fieldops_areas as areas


def _h(fo, s):
    return fo.h(s or '')


def _device_count(fo, row):
    try:
        return len(fo.parse_devices(row['survey_devices']))
    except Exception:
        return 0


def _last_updated(conn, row):
    times = [row['created_at'] or '']
    try:
        r = conn.execute(
            "SELECT MAX(uploaded_at) FROM photos WHERE job_id=? AND photo_type='plan'",
            (row['id'],)).fetchone()
        if r and r[0]:
            times.append(r[0])
    except Exception:
        pass
    try:
        r = conn.execute(
            'SELECT MAX(created_at) FROM plan_meta WHERE job_id=?',
            (row['id'],)).fetchone()
        if r and r[0]:
            times.append(r[0])
    except Exception:
        pass
    return max(times) if any(times) else ''


def _nice_when(fo, raw):
    text = (raw or '')[:19].replace('T', ' ')
    if len(text) >= 10:
        day = text[:10]
        try:
            if hasattr(fo, 'nice_day'):
                return fo.nice_day(day) + (text[10:] if len(text) > 10 else '')
        except Exception:
            pass
        return text
    return text or 'Unknown'


def jobs_with_plans(conn, fo, q=''):
    init_ok = True
    try:
        favs.init_schema(conn)
    except Exception:
        init_ok = False
    rows = conn.execute(
        "SELECT * FROM jobs WHERE EXISTS (SELECT 1 FROM photos p WHERE p.job_id=jobs.id AND p.photo_type='plan') "
        "ORDER BY created_at DESC").fetchall()
    key = favs.user_key(fo)
    starred = set(favs.list_for(conn, key, 'job')) if init_ok and key else set()
    q = (q or '').strip().lower()
    out = []
    for row in rows:
        if not fo.can_see(row):
            continue
        rec = {
            'id': row['id'],
            'title': row['title'] or '',
            'customer': row['customer'] or '',
            'address': row['address'] or '',
            'job_type': row['job_type'] or '',
            'device_count': _device_count(fo, row),
            'updated': _last_updated(conn, row),
            'favorite': row['id'] in starred,
        }
        if q:
            hay = ' '.join(str(rec[k]) for k in ('id', 'title', 'customer', 'address', 'job_type')).lower()
            if q not in hay:
                continue
        out.append(rec)
    # favorites first, then most recently updated
    favs_rows = [r for r in out if r['favorite']]
    rest = [r for r in out if not r['favorite']]
    rest.sort(key=lambda r: r['updated'] or '', reverse=True)
    favs_rows.sort(key=lambda r: r['updated'] or '', reverse=True)
    return favs_rows + rest


def next_job_id(conn, fo):
    y = fo.today_local().year
    pre = 'TS-%d-' % y
    top = 0
    for r in conn.execute('SELECT id FROM jobs WHERE id LIKE ?', (pre + '%',)):
        try:
            top = max(top, int(r['id'][len(pre):]))
        except ValueError:
            pass
    return pre + '%04d' % (top + 1)


def create_plan_job(conn, fo, title, customer, address, job_id='', tech=''):
    title = ' '.join((title or '').split())[:200]
    if not title:
        return None, 'Title is required.'
    jid = ' '.join((job_id or '').split())[:40]
    if not jid:
        jid = next_job_id(conn, fo)
    if conn.execute('SELECT 1 FROM jobs WHERE id=?', (jid,)).fetchone():
        return None, 'That Job ID already exists.'
    tech = fo.nice_name(tech) or fo.default_sub() or fo.me()
    if not tech:
        return None, 'Pick a sub for this job.'
    now = datetime.utcnow().isoformat()
    sd = fo.today_local().isoformat()
    conn.execute(
        '''INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,scheduled_date,created_by,job_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (jid, title, (customer or '').strip(), (address or '').strip(), '',
         'assigned', tech, now, sd, fo.me(), 'Survey'))
    try:
        import fieldops_platform as plat
        plat.link_job(conn, conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
    except Exception:
        pass
    areas.add_blank_area(conn, jid, 'Area 1', fo, created_by=fo.me())
    return jid, None


def _star_svg(on):
    if on:
        return ('<svg viewBox="0 0 24 24" width="22" height="22" fill="#a49f2a" stroke="#a49f2a" '
                'stroke-width="1.5" aria-hidden="true"><path d="M12 3.2l2.6 5.4 6 .9-4.3 4.2 1 6-5.3-2.8-5.3 2.8 1-6-4.3-4.2 6-.9z"/></svg>')
    return ('<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="#8a8b7a" '
            'stroke-width="1.8" aria-hidden="true"><path d="M12 3.2l2.6 5.4 6 .9-4.3 4.2 1 6-5.3-2.8-5.3 2.8 1-6-4.3-4.2 6-.9z"/></svg>')


def list_html(fo, rows, q='', office=True):
    h = lambda s: _h(fo, s)
    body = '<div class="fp-page">'
    body += '<form class="fp-search" method="get" action="/floor-plans" role="search">'
    body += '<input type="search" name="q" value="%s" placeholder="Search client, site, or job ID" aria-label="Search floor plans" autocomplete="off">' % h(q)
    body += '<button class="plat-btn" type="submit">Search</button></form>'
    if office:
        body += '<p class="fp-actions"><a class="plat-btn plat-btn-gold" href="/floor-plans/new">New plan</a></p>'
    if not rows:
        body += '<div class="muted">No floor plans match this search.</div>' if q else '<div class="muted">No jobs or surveys have a plan yet.</div>'
        body += '</div>'
        return body
    body += '<div class="fp-list">'
    for r in rows:
        body += '<article class="fp-row%s" data-job="%s">' % (' fav' if r['favorite'] else '', h(r['id']))
        body += '<button type="button" class="fav-btn%s" data-kind="job" data-key="%s" aria-pressed="%s" aria-label="%s">%s</button>' % (
            ' on' if r['favorite'] else '', h(r['id']),
            'true' if r['favorite'] else 'false',
            'Remove from favorites' if r['favorite'] else 'Add to favorites',
            _star_svg(r['favorite']))
        body += '<div class="fp-main">'
        body += '<div class="fp-title"><b>%s</b> <span class="muted">%s</span></div>' % (
            h(r['customer'] or r['title'] or r['id']), h(r['id']))
        if r['title'] and r['title'] != r['customer']:
            body += '<div>%s</div>' % h(r['title'])
        body += '<div class="muted">%s</div>' % h(r['address'] or 'No site yet')
        typ = h(r['job_type'] or 'Job')
        body += '<div class="muted">%s &middot; %s device%s &middot; Updated %s</div>' % (
            typ, r['device_count'], '' if r['device_count'] == 1 else 's', h(_nice_when(fo, r['updated'])))
        body += '</div>'
        body += '<div class="fp-btns">'
        body += '<a class="plat-btn plat-btn-gold" href="/design/%s">Open plan</a>' % urllib.parse.quote(r['id'])
        body += '<a class="plat-btn plat-btn-ghost" href="/?job=%s">Open job</a>' % urllib.parse.quote(r['id'])
        body += '</div></article>'
    body += '</div></div>'
    body += LIST_CSS + LIST_JS
    return body


LIST_CSS = '''
<style>
.fp-page{max-width:980px}
.fp-search{display:flex;gap:8px;margin:0 0 14px}
.fp-search input{flex:1;min-width:0;padding:12px 14px;border:1px solid var(--line,#ecece6);border-radius:12px;font-size:1rem}
.fp-actions{margin:0 0 14px}
.fp-list{display:flex;flex-direction:column;gap:10px}
.fp-row{display:grid;grid-template-columns:44px 1fr auto;gap:10px 12px;align-items:center;background:#fff;border:1px solid var(--line,#ecece6);border-radius:16px;padding:12px 14px}
.fp-row.fav{border-color:#a49f2a;box-shadow:0 0 0 1px rgba(164,159,42,.25)}
.fav-btn{width:44px;height:44px;border:0;background:none;border-radius:10px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;color:#8a8b7a;padding:0}
.fav-btn.on{color:#a49f2a}
.fp-title{font-size:1.05rem}
.fp-btns{display:flex;flex-direction:column;gap:8px;min-width:140px}
.fp-btns a{text-align:center;text-decoration:none}
@media(max-width:700px){
  .fp-row{grid-template-columns:44px 1fr;grid-template-areas:"star main" "btns btns"}
  .fav-btn{grid-area:star}
  .fp-main{grid-area:main;min-width:0}
  .fp-btns{grid-area:btns;flex-direction:row;min-width:0}
  .fp-btns a{flex:1}
  .fp-search{flex-direction:column}
}
</style>
'''

LIST_JS = '''
<script>
(function(){
  function on(btn, yes){
    btn.classList.toggle('on', yes);
    btn.setAttribute('aria-pressed', yes ? 'true' : 'false');
    btn.setAttribute('aria-label', yes ? 'Remove from favorites' : 'Add to favorites');
    btn.innerHTML = yes
      ? '<svg viewBox="0 0 24 24" width="22" height="22" fill="#a49f2a" stroke="#a49f2a" stroke-width="1.5" aria-hidden="true"><path d="M12 3.2l2.6 5.4 6 .9-4.3 4.2 1 6-5.3-2.8-5.3 2.8 1-6-4.3-4.2 6-.9z"/></svg>'
      : '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="#8a8b7a" stroke-width="1.8" aria-hidden="true"><path d="M12 3.2l2.6 5.4 6 .9-4.3 4.2 1 6-5.3-2.8-5.3 2.8 1-6-4.3-4.2 6-.9z"/></svg>';
    var row = btn.closest('.fp-row');
    if(row) row.classList.toggle('fav', yes);
  }
  document.querySelectorAll('.fav-btn').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.preventDefault(); e.stopPropagation();
      var want = !btn.classList.contains('on');
      fetch('/api/favorites', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({kind: btn.getAttribute('data-kind'), item_key: btn.getAttribute('data-key'), on: want})
      }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
        .then(function(res){ if(res.ok){ on(btn, want); location.reload(); } else { alert((res.d && res.d.error) || 'Could not save favorite'); } })
        .catch(function(){ alert('Could not save favorite'); });
    });
  });
})();
</script>
'''


def new_form_html(fo, msg='', vals=None, jobs=None, next_id=''):
    vals = vals or {}
    h = lambda s: _h(fo, s)
    body = ''
    if msg:
        body += '<div class="okf" style="margin-bottom:12px">%s</div>' % h(msg) if 'saved' in (msg or '').lower() else (
            '<div class="flag" style="margin-bottom:12px">%s</div>' % h(msg))
    body += '<form method="post" class="plat-form" style="max-width:640px">'
    body += '<h2>New floor plan</h2>'
    body += '<p class="muted">Start a new survey with a blank plan, or open a plan on a job that already exists.</p>'
    body += '<label>Title</label><input name="title" required maxlength="200" value="%s" placeholder="Lobby cameras">' % h(vals.get('title') or '')
    body += '<label>Client</label><input name="customer" maxlength="200" value="%s" placeholder="Company or contact">' % h(vals.get('customer') or '')
    body += '<label>Site / address</label><input name="address" maxlength="300" value="%s">' % h(vals.get('address') or '')
    body += '<label>Job ID (leave blank to auto-number)</label><input name="job_id" maxlength="40" value="%s" placeholder="%s">' % (
        h(vals.get('job_id') or ''), h(next_id or 'TS-YYYY-0001'))
    body += '<button class="plat-btn plat-btn-gold" type="submit" name="action" value="create" style="margin-top:12px">Create plan</button>'
    body += '</form>'
    if jobs:
        body += '<h2 style="margin-top:28px">Or open an existing job</h2>'
        body += '<div class="fp-list">'
        for r in jobs[:40]:
            body += ('<article class="fp-row"><div class="fp-main"><div class="fp-title"><b>%s</b> <span class="muted">%s</span></div>'
                     '<div class="muted">%s</div></div><div class="fp-btns">'
                     '<a class="plat-btn plat-btn-gold" href="/design/%s">Open plan</a>'
                     '<a class="plat-btn plat-btn-ghost" href="/floor-plans/new?use=%s">Add blank plan</a>'
                     '</div></article>' % (
                         h(r['customer'] or r['title'] or r['id']), h(r['id']),
                         h(r['address'] or r['title'] or ''),
                         urllib.parse.quote(r['id']), urllib.parse.quote(r['id'])))
        body += '</div>' + LIST_CSS
    return body


def attach_blank(conn, fo, job_id):
    row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        return None, 'Job not found.'
    if not fo.can_see(row):
        return None, 'You cannot open that job.'
    areas.add_blank_area(conn, job_id, '', fo, created_by=fo.me())
    return job_id, None


def register(fo):
    app = fo.app
    request = fo.request
    redirect = fo.redirect

    @app.route('/floor-plans')
    def floor_plans_list():
        if not fo.me():
            return redirect('/login')
        q = (request.args.get('q') or '').strip()
        conn = fo.get_db()
        favs.init_schema(conn)
        rows = jobs_with_plans(conn, fo, q)
        conn.close()
        return fo.ops_page('Floor Plans', list_html(fo, rows, q, office=fo.is_office()), back='/')

    @app.route('/floor-plans/new', methods=['GET', 'POST'])
    def floor_plans_new():
        if not fo.me():
            return redirect('/login')
        if not fo.is_office():
            return redirect('/floor-plans')
        conn = fo.get_db()
        msg = ''
        if request.method == 'POST' or request.args.get('use'):
            use = (request.form.get('use') or request.args.get('use') or '').strip()
            if use:
                jid, err = attach_blank(conn, fo, use)
                if err:
                    msg = err
                else:
                    conn.commit(); conn.close()
                    return redirect('/design/' + urllib.parse.quote(jid))
            else:
                jid, err = create_plan_job(
                    conn, fo,
                    request.form.get('title'),
                    request.form.get('customer'),
                    request.form.get('address'),
                    request.form.get('job_id'))
                if err:
                    msg = err
                    vals = {k: request.form.get(k) or '' for k in ('title', 'customer', 'address', 'job_id')}
                    nxt = next_job_id(conn, fo)
                    jobs = [{'id': r['id'], 'title': r['title'] or '', 'customer': r['customer'] or '',
                             'address': r['address'] or ''} for r in conn.execute(
                                 "SELECT id,title,customer,address FROM jobs WHERE status!='completed' ORDER BY created_at DESC LIMIT 40")]
                    conn.close()
                    return fo.ops_page('New plan', new_form_html(fo, msg, vals, jobs, nxt), back='/floor-plans')
                conn.commit(); conn.close()
                return redirect('/design/' + urllib.parse.quote(jid))
        nxt = next_job_id(conn, fo)
        jobs = [{'id': r['id'], 'title': r['title'] or '', 'customer': r['customer'] or '',
                 'address': r['address'] or ''} for r in conn.execute(
                     "SELECT id,title,customer,address FROM jobs WHERE status!='completed' ORDER BY created_at DESC LIMIT 40")]
        conn.close()
        return fo.ops_page('New plan', new_form_html(fo, msg, {}, jobs, nxt), back='/floor-plans')
