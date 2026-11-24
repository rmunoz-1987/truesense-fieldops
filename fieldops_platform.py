"""Phase 1 platform backbone and office pages for Field Ops.

Companies, contacts, sites and opportunities, plus Sales and Quote home
pages. The office chrome (product switcher) lives in fieldops_app.py.
This module does not send email, texts or any message to clients or vendors.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime

# Stages and roles are fixed labels for Phase 1. UI copy must not use em dashes.
OPP_STAGES = [
    ('lead', 'Lead'),
    ('qualified', 'Qualified'),
    ('estimating', 'Estimating'),
    ('proposal', 'Proposal sent'),
    ('won', 'Won'),
    ('lost', 'Lost'),
]
OPP_STAGE_SET = {k for k, _ in OPP_STAGES}
CONTACT_ROLES = [
    ('bill_to', 'Bill-to'),
    ('site', 'Site contact'),
    ('pm', 'Project manager'),
    ('other', 'Other'),
]
CONTACT_ROLE_SET = {k for k, _ in CONTACT_ROLES}

_OPP_DND_JS = r"""<div id="fo-lost-modal" hidden>
<div class="fo-lost-box" role="dialog" aria-labelledby="fo-lost-title">
<h2 id="fo-lost-title">Why was this lost?</h2>
<p class="muted">Pick a reason. This stays on the opportunity. Nothing is sent to the client.</p>
<label>Lost reason</label>
<select id="fo-lost-reason">
<option value="">Pick a reason</option>
<option value="price">Price</option>
<option value="timing">Timing</option>
<option value="no_fit">No fit</option>
<option value="no_response">No response</option>
<option value="competitor">Lost to competitor</option>
<option value="other">Other</option>
</select>
<label>Note</label>
<textarea id="fo-lost-note" rows="3" placeholder="Required if you pick Other"></textarea>
<div class="fo-lost-err" id="fo-lost-err"></div>
<div class="fo-lost-actions">
<button type="button" class="big-btn" id="fo-lost-ok">Save</button>
<button type="button" class="big-btn gray" id="fo-lost-cancel">Cancel</button>
</div></div></div>
<script>
(function(){
  const board=document.getElementById('plat-board');
  if(!board) return;
  window.foAskLost=function(){
    return new Promise(function(resolve){
      var box=document.getElementById('fo-lost-modal');
      if(!box){resolve(null);return;}
      box.hidden=false;
      var ok=document.getElementById('fo-lost-ok');
      var cancel=document.getElementById('fo-lost-cancel');
      var reason=document.getElementById('fo-lost-reason');
      var note=document.getElementById('fo-lost-note');
      var err=document.getElementById('fo-lost-err');
      if(err) err.textContent='';
      if(note) note.value='';
      function close(val){box.hidden=true;if(ok)ok.onclick=null;if(cancel)cancel.onclick=null;resolve(val);}
      if(cancel) cancel.onclick=function(){close(null)};
      if(ok) ok.onclick=function(){
        var r=(reason&&reason.value)||'';
        var n=(note&&note.value)||'';
        if(!r){if(err)err.textContent='Pick a lost reason.';return;}
        if(r==='other' && !String(n).trim()){if(err)err.textContent='Add a short note for Other.';return;}
        close({lost_reason:r,lost_note:n});
      };
    });
  };
  window.foOppStage=async function(id,stage,extra){
    extra=extra||{};
    if(stage==='lost' && !extra.lost_reason){
      var info=await window.foAskLost();
      if(!info) return;
      extra=info;
    }
    var body=Object.assign({stage:stage},extra);
    const r=await fetch('/api/opportunities/'+id+'/stage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok){const e=await r.json().catch(()=>({}));alert(e.error||'Could not move');return}
    location.reload();
  };
  function colFromPoint(x,y){
    const el=document.elementFromPoint(x,y);
    return el && el.closest ? el.closest('.plat-col') : null;
  }
  let drag=null;
  board.querySelectorAll('.plat-opp').forEach(card=>{
    card.addEventListener('dragstart',e=>{
      drag=card; card.classList.add('dragging');
      try{e.dataTransfer.setData('text/plain',card.dataset.id);e.dataTransfer.effectAllowed='move'}catch(err){}
    });
    card.addEventListener('dragend',()=>{card.classList.remove('dragging');board.querySelectorAll('.plat-col').forEach(c=>c.classList.remove('over'));drag=null});
  });
  board.querySelectorAll('.plat-col').forEach(col=>{
    col.addEventListener('dragover',e=>{e.preventDefault();col.classList.add('over')});
    col.addEventListener('dragleave',()=>col.classList.remove('over'));
    col.addEventListener('drop',e=>{
      e.preventDefault();col.classList.remove('over');
      const id=(drag&&drag.dataset.id)||(e.dataTransfer&&e.dataTransfer.getData('text/plain'));
      const stage=col.dataset.stage;
      if(id&&stage) foOppStage(id,stage);
    });
  });
  /* Touch / pointer fallback (HTML5 drag is unreliable on phones). */
  let hold=null, ghost=null, start=null;
  function clearPtr(){
    if(ghost) ghost.remove();
    if(hold) hold.classList.remove('dragging');
    board.querySelectorAll('.plat-col').forEach(c=>c.classList.remove('over'));
    hold=ghost=start=null;
  }
  board.addEventListener('pointerdown',e=>{
    if(e.pointerType==='mouse' && e.button!==0) return;
    if(e.target.closest('select,a,button,label,input')) return;
    const card=e.target.closest('.plat-opp');
    if(!card) return;
    start={x:e.clientX,y:e.clientY,card:card,pid:e.pointerId};
  });
  board.addEventListener('pointermove',e=>{
    if(!start||e.pointerId!==start.pid) return;
    const dx=e.clientX-start.x, dy=e.clientY-start.y;
    if(!hold && Math.hypot(dx,dy)<8) return;
    if(!hold){
      hold=start.card; hold.classList.add('dragging');
      try{hold.setPointerCapture(e.pointerId)}catch(err){}
      ghost=hold.cloneNode(true); ghost.classList.add('plat-ghost');
      ghost.style.width=hold.offsetWidth+'px'; document.body.appendChild(ghost);
    }
    ghost.style.left=(e.clientX-40)+'px'; ghost.style.top=(e.clientY-20)+'px';
    board.querySelectorAll('.plat-col').forEach(c=>c.classList.remove('over'));
    const col=colFromPoint(e.clientX,e.clientY); if(col) col.classList.add('over');
    e.preventDefault();
  },{passive:false});
  function endPtr(e){
    if(!start||e.pointerId!==start.pid) return;
    if(hold){
      const col=colFromPoint(e.clientX,e.clientY);
      const id=hold.dataset.id, stage=col&&col.dataset.stage;
      const same=stage&&stage===hold.dataset.stage;
      clearPtr();
      if(id&&stage&&!same) foOppStage(id,stage);
      return;
    }
    start=null;
  }
  board.addEventListener('pointerup',endPtr);
  board.addEventListener('pointercancel',()=>{clearPtr();});
})();
</script>
"""


PRE_MIGRATE_BACKUP = 'fieldops-pre-platform-v2.db'


def nkey(name):
    return ' '.join((name or '').split()).lower()


def now_iso():
    return datetime.utcnow().isoformat()


def _cols(conn, table):
    return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}


def _add_col(conn, table, coldef):
    name = coldef.split()[0]
    if name in _cols(conn, table):
        return
    conn.execute('ALTER TABLE %s ADD COLUMN %s' % (table, coldef))


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        billing_address TEXT,
        notes TEXT,
        archived INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        role TEXT DEFAULT 'other',
        title TEXT,
        phone TEXT,
        email TEXT,
        notes TEXT,
        created_at TEXT,
        updated_at TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT,
        address TEXT,
        address_key TEXT,
        phone TEXT,
        notes TEXT,
        created_at TEXT,
        updated_at TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS opportunities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        contact_id INTEGER,
        site_id INTEGER,
        title TEXT NOT NULL,
        stage TEXT DEFAULT 'lead',
        amount REAL DEFAULT 0,
        notes TEXT,
        job_id TEXT,
        created_by TEXT,
        created_at TEXT,
        updated_at TEXT,
        won_at TEXT,
        lost_at TEXT
    )''')
    for ix in (
        'CREATE UNIQUE INDEX IF NOT EXISTS ix_companies_name_key ON companies(name_key)',
        'CREATE INDEX IF NOT EXISTS ix_contacts_company ON contacts(company_id)',
        'CREATE INDEX IF NOT EXISTS ix_sites_company ON sites(company_id)',
        'CREATE UNIQUE INDEX IF NOT EXISTS ix_sites_addr ON sites(company_id, address_key)',
        'CREATE INDEX IF NOT EXISTS ix_opp_company ON opportunities(company_id)',
        'CREATE INDEX IF NOT EXISTS ix_opp_stage ON opportunities(stage)',
        'CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id)',
        'CREATE INDEX IF NOT EXISTS ix_jobs_site ON jobs(site_id)',
    ):
        try:
            conn.execute(ix)
        except sqlite3.OperationalError:
            # jobs columns may not exist yet; added below, then retry indexes
            pass
    _add_col(conn, 'jobs', 'company_id INTEGER')
    _add_col(conn, 'jobs', 'site_id INTEGER')
    _add_col(conn, 'jobs', 'contact_id INTEGER')
    for ix in (
        'CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id)',
        'CREATE INDEX IF NOT EXISTS ix_jobs_site ON jobs(site_id)',
    ):
        conn.execute(ix)


def backup_before_migrate(backup_dir, db_path):
    """Write one pre-migration copy. Safe to call again; will not overwrite."""
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, PRE_MIGRATE_BACKUP)
    if os.path.exists(dst) or not os.path.exists(db_path):
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
    return dst


def get_company_by_key(conn, key):
    if not key:
        return None
    return conn.execute('SELECT * FROM companies WHERE name_key=?', (key,)).fetchone()


def upsert_company(conn, name, phone='', email='', billing_address='', notes='', when=None):
    name = (name or '').strip()
    if not name:
        return None
    key = nkey(name)
    when = when or now_iso()
    row = get_company_by_key(conn, key)
    if row:
        sets, args = [], []
        if phone and not (row['phone'] or '').strip():
            sets.append('phone=?'); args.append(phone.strip()[:40])
        if email and not (row['email'] or '').strip():
            sets.append('email=?'); args.append(email.strip()[:120])
        if billing_address and not (row['billing_address'] or '').strip():
            sets.append('billing_address=?'); args.append(billing_address.strip()[:300])
        if notes and not (row['notes'] or '').strip():
            sets.append('notes=?'); args.append(notes.strip()[:2000])
        # Prefer the first-seen casing of the name; do not overwrite name.
        if sets:
            sets.append('updated_at=?'); args.append(when)
            args.append(row['id'])
            conn.execute('UPDATE companies SET %s WHERE id=?' % ','.join(sets), args)
            row = conn.execute('SELECT * FROM companies WHERE id=?', (row['id'],)).fetchone()
        return row
    cur = conn.execute(
        'INSERT INTO companies (name,name_key,phone,email,billing_address,notes,archived,created_at,updated_at) VALUES (?,?,?,?,?,?,0,?,?)',
        (name[:200], key, (phone or '').strip()[:40], (email or '').strip()[:120],
         (billing_address or '').strip()[:300], (notes or '').strip()[:2000], when, when))
    return conn.execute('SELECT * FROM companies WHERE id=?', (cur.lastrowid,)).fetchone()


def upsert_site(conn, company_id, address='', name='', phone='', when=None):
    if not company_id:
        return None
    address = (address or '').strip()
    name = (name or '').strip()
    key = nkey(address) or nkey(name) or '-'
    when = when or now_iso()
    row = conn.execute(
        'SELECT * FROM sites WHERE company_id=? AND address_key=?',
        (company_id, key)).fetchone()
    if row:
        sets, args = [], []
        if phone and not (row['phone'] or '').strip():
            sets.append('phone=?'); args.append(phone.strip()[:40])
        if name and not (row['name'] or '').strip():
            sets.append('name=?'); args.append(name[:200])
        if address and not (row['address'] or '').strip():
            sets.append('address=?'); args.append(address[:300])
        if sets:
            sets.append('updated_at=?'); args.append(when)
            args.append(row['id'])
            conn.execute('UPDATE sites SET %s WHERE id=?' % ','.join(sets), args)
            row = conn.execute('SELECT * FROM sites WHERE id=?', (row['id'],)).fetchone()
        return row
    label = name or (address.split(',')[0].strip() if address else 'Main site')
    cur = conn.execute(
        'INSERT INTO sites (company_id,name,address,address_key,phone,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
        (company_id, label[:200], address[:300], key, (phone or '').strip()[:40], when, when))
    return conn.execute('SELECT * FROM sites WHERE id=?', (cur.lastrowid,)).fetchone()


def ensure_contact(conn, company_id, name, role='site', phone='', email='', title='', when=None):
    name = (name or '').strip()
    if not company_id or not name:
        return None
    role = role if role in CONTACT_ROLE_SET else 'other'
    phone = (phone or '').strip()[:40]
    email = (email or '').strip()[:120]
    when = when or now_iso()
    # Match an existing contact by phone, else email, else same name+role.
    row = None
    if phone:
        row = conn.execute(
            'SELECT * FROM contacts WHERE company_id=? AND TRIM(IFNULL(phone,""))!=\'\' AND LOWER(TRIM(phone))=?',
            (company_id, phone.lower())).fetchone()
    if not row and email:
        row = conn.execute(
            'SELECT * FROM contacts WHERE company_id=? AND LOWER(TRIM(IFNULL(email,"")))=?',
            (company_id, email.lower())).fetchone()
    if not row:
        row = conn.execute(
            'SELECT * FROM contacts WHERE company_id=? AND LOWER(TRIM(name))=? AND role=?',
            (company_id, nkey(name), role)).fetchone()
    if row:
        return row
    cur = conn.execute(
        'INSERT INTO contacts (company_id,name,role,title,phone,email,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
        (company_id, name[:120], role, (title or '').strip()[:80], phone, email, when, when))
    return conn.execute('SELECT * FROM contacts WHERE id=?', (cur.lastrowid,)).fetchone()


def link_job(conn, job_row, company_id=None):
    """Attach a job to a company and site. Prefer company_id; do not rematch by name.

    If the job already has a company_id (or one is passed in), that company is
    kept and jobs.customer is set to the company's canonical name. A typo in
    the customer text cannot split off a second company. Name matching is only
    used when the job is not yet linked.
    """
    if job_row is None:
        return None
    if not isinstance(job_row, sqlite3.Row) and not hasattr(job_row, 'keys'):
        return None
    phone = (job_row['phone'] or '').strip()
    address = (job_row['address'] or '').strip()
    customer = (job_row['customer'] or '').strip()
    existing = company_id or (job_row['company_id'] if 'company_id' in job_row.keys() else None)
    co = None
    if existing:
        co = conn.execute('SELECT * FROM companies WHERE id=?', (existing,)).fetchone()
    if co is None and customer:
        co = upsert_company(conn, customer, phone=phone, billing_address=address)
    if not co:
        return None
    site = upsert_site(conn, co['id'], address=address, phone=phone) if address else None
    if site is None and job_row['site_id'] if 'site_id' in job_row.keys() else None:
        site = conn.execute('SELECT * FROM sites WHERE id=? AND company_id=?', (job_row['site_id'], co['id'])).fetchone()
    contact = None
    if phone:
        contact = ensure_contact(conn, co['id'], customer or co['name'], role='site', phone=phone)
    cid = co['id']
    sid = site['id'] if site else (job_row['site_id'] if 'site_id' in job_row.keys() else None)
    tid = contact['id'] if contact else (job_row['contact_id'] if 'contact_id' in job_row.keys() else None)
    conn.execute(
        'UPDATE jobs SET company_id=?, site_id=?, contact_id=COALESCE(?, contact_id), customer=? WHERE id=?',
        (cid, sid, tid, co['name'], job_row['id']))
    return cid, sid


def archive_company(conn, company_id):
    """Hide a company from default lists. Jobs, contacts, sites and opportunities stay."""
    row = conn.execute('SELECT * FROM companies WHERE id=?', (company_id,)).fetchone()
    if not row:
        return None, 'Company not found'
    conn.execute('UPDATE companies SET archived=1, updated_at=? WHERE id=?', (now_iso(), company_id))
    return conn.execute('SELECT * FROM companies WHERE id=?', (company_id,)).fetchone(), None


def merge_companies(conn, keep_id, drop_id):
    """Move records from drop_id into keep_id, then remove the leftover company.

    Jobs are re-linked by company_id. Sites with the same address are combined.
    """
    if not keep_id or not drop_id or int(keep_id) == int(drop_id):
        return None, 'Pick two different companies'
    keep = conn.execute('SELECT * FROM companies WHERE id=?', (keep_id,)).fetchone()
    drop = conn.execute('SELECT * FROM companies WHERE id=?', (drop_id,)).fetchone()
    if not keep or not drop:
        return None, 'Company not found'
    when = now_iso()
    # Contacts
    conn.execute('UPDATE contacts SET company_id=?, updated_at=? WHERE company_id=?', (keep_id, when, drop_id))
    # Sites: reuse keep site when the address key already exists
    for s in conn.execute('SELECT * FROM sites WHERE company_id=?', (drop_id,)).fetchall():
        twin = None
        if s['address_key']:
            twin = conn.execute(
                'SELECT * FROM sites WHERE company_id=? AND address_key=?',
                (keep_id, s['address_key'])).fetchone()
        if twin:
            conn.execute('UPDATE jobs SET site_id=? WHERE site_id=?', (twin['id'], s['id']))
            conn.execute('UPDATE opportunities SET site_id=? WHERE site_id=?', (twin['id'], s['id']))
            conn.execute('DELETE FROM sites WHERE id=?', (s['id'],))
        else:
            # unique (company_id, address_key) is free after the move
            conn.execute(
                'UPDATE sites SET company_id=?, updated_at=? WHERE id=?',
                (keep_id, when, s['id']))
    conn.execute('UPDATE opportunities SET company_id=?, updated_at=? WHERE company_id=?', (keep_id, when, drop_id))
    conn.execute(
        'UPDATE jobs SET company_id=?, customer=? WHERE company_id=?',
        (keep_id, keep['name'], drop_id))
    # Leftover jobs still keyed only by the old name
    conn.execute(
        "UPDATE jobs SET company_id=?, customer=? WHERE company_id IS NULL AND LOWER(TRIM(customer))=?",
        (keep_id, keep['name'], drop['name_key']))
    conn.execute('DELETE FROM clients WHERE LOWER(TRIM(name))=?', (drop['name_key'],))
    if not conn.execute('SELECT 1 FROM clients WHERE LOWER(TRIM(name))=?', (keep['name_key'],)).fetchone():
        conn.execute(
            'INSERT INTO clients (name,address,phone,created_at) VALUES (?,?,?,?)',
            (keep['name'], keep['billing_address'] or '', keep['phone'] or '', when))
    conn.execute('DELETE FROM companies WHERE id=?', (drop_id,))
    return keep, None


def migrate_existing(conn):
    """Create companies and sites from clients and jobs. Never deletes. Safe to rerun."""
    # Clients table first so the official name and phone are preferred.
    for r in conn.execute('SELECT name,address,phone FROM clients').fetchall():
        name = (r['name'] or '').strip()
        if not name:
            continue
        co = upsert_company(conn, name, phone=r['phone'] or '', billing_address=r['address'] or '')
        if co:
            upsert_site(conn, co['id'], address=r['address'] or '', phone=r['phone'] or '')
            if (r['phone'] or '').strip():
                ensure_contact(conn, co['id'], name, role='bill_to', phone=r['phone'] or '')
    # Jobs (including ones with no clients row)
    for r in conn.execute('SELECT * FROM jobs').fetchall():
        link_job(conn, r)
    return {
        'companies': conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0],
        'contacts': conn.execute('SELECT COUNT(*) FROM contacts').fetchone()[0],
        'sites': conn.execute('SELECT COUNT(*) FROM sites').fetchone()[0],
        'jobs_linked': conn.execute('SELECT COUNT(*) FROM jobs WHERE company_id IS NOT NULL').fetchone()[0],
    }


def init_and_migrate(conn, backup_dir=None, db_path=None):
    init_schema(conn)
    if backup_dir and db_path:
        # Schema is in place; back up the current file once before first data move.
        flag = conn.execute("SELECT value FROM settings WHERE key='platform_v2_migrated'").fetchone()
        if not flag:
            backup_before_migrate(backup_dir, db_path)
    stats = migrate_existing(conn)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key,value) VALUES ('platform_v2_migrated',?)",
        (now_iso(),))
    return stats


def rename_company(conn, company_id, new_name):
    """Change the company name and keep job.customer / clients.name in sync."""
    new_name = (new_name or '').strip()
    if not new_name:
        return None, 'Name is required'
    key = nkey(new_name)
    other = conn.execute(
        'SELECT id FROM companies WHERE name_key=? AND id!=?', (key, company_id)).fetchone()
    if other:
        return None, 'Another company already has that name'
    row = conn.execute('SELECT * FROM companies WHERE id=?', (company_id,)).fetchone()
    if not row:
        return None, 'Company not found'
    old_key = row['name_key']
    when = now_iso()
    conn.execute(
        'UPDATE companies SET name=?, name_key=?, updated_at=? WHERE id=?',
        (new_name[:200], key, when, company_id))
    conn.execute(
        'UPDATE jobs SET customer=? WHERE company_id=?',
        (new_name, company_id))
    # Also update leftover jobs that still match the old name but were not linked.
    conn.execute(
        "UPDATE jobs SET customer=? WHERE company_id IS NULL AND LOWER(TRIM(customer))=?",
        (new_name, old_key))
    conn.execute(
        'UPDATE clients SET name=? WHERE LOWER(TRIM(name))=?',
        (new_name, old_key))
    return conn.execute('SELECT * FROM companies WHERE id=?', (company_id,)).fetchone(), None



SHARED_PATHS = ('/customers', '/clients', '/contacts', '/sites', '/search', '/systems')
SKIP_AREA_PATHS = ('/login', '/logout', '/hub/', '/q/', '/s/', '/uploads/', '/healthz', '/static/', '/sw.js', '/offline', '/api/', '/manifest.webmanifest')

def is_estimate_path(path):
    path = path or ''
    if path == '/quote' or path.startswith('/quote/') or path.startswith('/quotes') or path.startswith('/catalog'):
        return True
    # Only the job or portal estimate routes, never a shared path that happens to contain the letters.
    if path.startswith('/job/') or path.startswith('/hub/'):
        return path.rstrip('/').endswith('/estimate') or '/estimate/' in path
    return False

def is_shared_path(path):
    path = path or ''
    return any(path == p or path.startswith(p + '/') for p in SHARED_PATHS)

def area_from_request(path, saved=None, area_arg=None):
    """Return (area, sticky). sticky True means remember this area for shared pages.

    Shared pages (Customers, contacts, sites, search, systems) keep the last
    remembered area and default to Ops. Visiting Today, Jobs, Sales, or Quote
    updates the remembered area so Customers cannot jump to Quote after a later
    Ops page.
    """
    if area_arg in ('ops', 'sales', 'quote'):
        return area_arg, True
    path = path or '/'
    if path.startswith(('/sales', '/opportunities', '/opportunity', '/tasks', '/activities')):
        return 'sales', True
    if is_estimate_path(path):
        return 'quote', True
    if is_shared_path(path):
        return (saved if saved in ('ops', 'sales', 'quote') else 'ops'), False
    if any(path == p or path.startswith(p) for p in SKIP_AREA_PATHS):
        return (saved if saved in ('ops', 'sales', 'quote') else 'ops'), False
    return 'ops', True


# ---------- Flask pages (registered from fieldops_app) ----------

def register(fo):
    """Attach routes and hooks onto the Field Ops Flask app module."""
    from flask import request, redirect, session, url_for, jsonify
    from markupsafe import Markup
    import urllib.parse

    app = fo.app
    h = fo.h
    money = getattr(fo, 'money', lambda n: ('$%s' % ('{:,.2f}'.format(n) if n else '0.00')))

    def office_only():
        if not fo.me():
            return redirect('/login')
        if not fo.may_estimate():
            return redirect('/')
        return None

    def area_of(path=None):
        path = path or (request.path if request else '/')
        saved = (session.get('fo_area') if session else None) or ''
        area_arg = (request.args.get('area') if request else None) or ''
        return area_from_request(path, saved, area_arg)[0]

    def set_area(a):
        if a in ('ops', 'sales', 'quote'):
            session['fo_area'] = a

    @app.before_request
    def _plat_area():
        if not fo.me() or not fo.may_estimate():
            return
        area, sticky = area_from_request(
            request.path, session.get('fo_area'), request.args.get('area'))
        if sticky:
            session['fo_area'] = area

    fo.plat_area_of = area_of

    def page(title, body, back=None, kicker='', actions='', msg='', area=None):
        if area in ('ops', 'sales', 'quote'):
            set_area(area)
        else:
            # Shared record pages keep the remembered area. Do not overwrite it here.
            path = request.path if request else '/'
            if not is_shared_path(path):
                set_area(area_of())
        act = actions or ''
        if back:
            act = ('<a class="plat-btn plat-btn-ghost" href="%s">Back</a>' % h(back)) + act
        head = (
            '<div class="plat-head"><div class="plat-head-text">'
            + ('<div class="plat-kicker">%s</div>' % h(kicker) if kicker else '')
            + '<h1 class="plat-title">%s<span class="plat-dot">.</span></h1></div>'
            % h(title)
            + ('<div class="plat-actions">%s</div>' % act if act else '')
            + '</div>'
        )
        banner = ('<div class="plat-msg">%s</div>' % h(msg)) if msg else ''
        return fo.ops_page(title, head + banner + body, back=False, msg='')

    def stage_label(key):
        return dict(OPP_STAGES).get(key or '', key or 'Lead')

    def role_label(key):
        return dict(CONTACT_ROLES).get(key or '', 'Other')

    def company_or_404(conn, cid):
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return None
        return conn.execute('SELECT * FROM companies WHERE id=?', (cid,)).fetchone()

    def sel(options, current, blank=''):
        if blank is None:
            out = ''
        else:
            out = '<option value="">%s</option>' % h(blank)
        for val, lab in options:
            out += '<option value="%s"%s>%s</option>' % (
                h(val), ' selected' if str(current or '') == str(val) else '', h(lab))
        return out

    def money_amt(n):
        try:
            n = float(n or 0)
        except (TypeError, ValueError):
            n = 0
        return money(n) if n else ''

    # ----- Customers -----
    @app.route('/customers')
    def customers_list():
        gate = office_only()
        if gate:
            return gate
        q = (request.args.get('q') or '').strip()[:80]
        conn = fo.get_db()
        fo.plat_migrate_if_needed = True
        sql = '''SELECT c.*,
            (SELECT COUNT(*) FROM sites s WHERE s.company_id=c.id) AS sites,
            (SELECT COUNT(*) FROM contacts t WHERE t.company_id=c.id) AS contacts,
            (SELECT COUNT(*) FROM jobs j WHERE j.company_id=c.id AND j.status!="completed") AS open_jobs,
            (SELECT COUNT(*) FROM opportunities o WHERE o.company_id=c.id AND o.stage NOT IN ("won","lost")) AS open_opps
            FROM companies c WHERE IFNULL(c.archived,0)=0'''
        args = []
        if q:
            sql += ' AND (LOWER(c.name) LIKE ? OR LOWER(IFNULL(c.phone,"")) LIKE ? OR LOWER(IFNULL(c.billing_address,"")) LIKE ?)'
            like = '%' + q.lower() + '%'
            args.extend([like, like, like])
        sql += ' ORDER BY c.name COLLATE NOCASE'
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        filt = (
            '<form class="plat-filter" action="/customers" method="get">'
            '<input type="search" name="q" value="%s" placeholder="Search companies" aria-label="Search companies">'
            '<button class="plat-btn plat-btn-ghost" type="submit">Search</button></form>' % h(q)
        )
        if not rows:
            body = filt + '<div class="plat-empty">No companies yet. Add one, or they will appear here after the next job is saved.</div>'
        else:
            cards = []
            for r in rows:
                bits = []
                if r['open_jobs']:
                    bits.append('%d open job%s' % (r['open_jobs'], '' if r['open_jobs'] == 1 else 's'))
                if r['open_opps']:
                    bits.append('%d open opportunit%s' % (r['open_opps'], 'y' if r['open_opps'] == 1 else 'ies'))
                bits.append('%d site%s' % (r['sites'], '' if r['sites'] == 1 else 's'))
                bits.append('%d contact%s' % (r['contacts'], '' if r['contacts'] == 1 else 's'))
                cards.append(
                    '<a class="plat-card" href="/customers/%d"><b>%s</b>'
                    '<div class="muted">%s</div><div class="muted">%s</div></a>' % (
                        r['id'], h(r['name']), h(r['billing_address'] or r['phone'] or ''),
                        h(' · '.join(bits))))
            body = filt + '<div class="plat-cards">' + ''.join(cards) + '</div>'
        actions = ('<a class="plat-btn plat-btn-ghost" href="/customers/merge">Merge duplicates</a>'
                   '<a class="plat-btn plat-btn-gold" href="/customers/new">+ Company</a>')
        return page('Customers', body, kicker='Shared across Ops, Sales and Quote', actions=actions)

    @app.route('/customers/new', methods=['GET', 'POST'])
    def customers_new():
        gate = office_only()
        if gate:
            return gate
        msg = ''
        vals = {k: (request.form.get(k) or '').strip() for k in ('name', 'phone', 'email', 'billing_address', 'notes')}
        if request.method == 'POST':
            if not vals['name']:
                msg = 'Name is required.'
            else:
                conn = fo.get_db()
                existing = get_company_by_key(conn, nkey(vals['name']))
                if existing:
                    conn.close()
                    msg = 'A company with that name already exists. Open it from Customers, or use Merge duplicates if these are the same firm.'
                else:
                    co = upsert_company(
                        conn, vals['name'], phone=vals['phone'], email=vals['email'],
                        billing_address=vals['billing_address'], notes=vals['notes'])
                    if vals['billing_address']:
                        upsert_site(conn, co['id'], address=vals['billing_address'], phone=vals['phone'])
                    # Keep the legacy clients table in sync so old pages still work.
                    if not conn.execute('SELECT 1 FROM clients WHERE LOWER(TRIM(name))=?', (nkey(vals['name']),)).fetchone():
                        conn.execute(
                            'INSERT INTO clients (name,address,phone,created_at) VALUES (?,?,?,?)',
                            (vals['name'], vals['billing_address'], vals['phone'], now_iso()))
                    conn.commit()
                    cid = co['id']
                    conn.close()
                    return redirect('/customers/%d' % cid)
        form = _company_form(vals, '/customers/new', 'Add company')
        if msg:
            form = '<div class="plat-msg">%s</div>' % h(msg) + form
        return page('New company', form, back='/customers', kicker='Customers')

    def _company_form(vals, action, submit, cid=None):
        v = lambda k: h(vals.get(k) or '')
        return (
            '<form class="plat-form" method="post" action="%s">'
            '<label>Company name</label><input name="name" required maxlength="200" value="%s">'
            '<div class="plat-row"><div><label>Phone</label><input name="phone" type="tel" value="%s"></div>'
            '<div><label>Email</label><input name="email" type="email" value="%s"></div></div>'
            '<label>Billing address</label><input name="billing_address" value="%s">'
            '<label>Notes (office only)</label><textarea name="notes" rows="3">%s</textarea>'
            '<button class="plat-btn plat-btn-gold" type="submit">%s</button></form>'
            % (h(action), v('name'), v('phone'), v('email'), v('billing_address'), v('notes'), h(submit))
        )

    @app.route('/customers/<int:cid>', methods=['GET', 'POST'])
    def customers_detail(cid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        co = company_or_404(conn, cid)
        if not co:
            conn.close()
            return redirect('/customers')
        msg = ''
        if request.method == 'POST':
            action = request.form.get('action') or 'save'
            if action == 'save':
                new_name = (request.form.get('name') or '').strip()
                if new_name and nkey(new_name) != co['name_key']:
                    _, err = rename_company(conn, cid, new_name)
                    if err:
                        msg = err
                if not msg:
                    conn.execute(
                        'UPDATE companies SET phone=?, email=?, billing_address=?, notes=?, updated_at=? WHERE id=?',
                        ((request.form.get('phone') or '').strip()[:40],
                         (request.form.get('email') or '').strip()[:120],
                         (request.form.get('billing_address') or '').strip()[:300],
                         (request.form.get('notes') or '').strip()[:2000],
                         now_iso(), cid))
                    msg = 'Saved.'
                conn.commit()
                co = conn.execute('SELECT * FROM companies WHERE id=?', (cid,)).fetchone()
        contacts = conn.execute(
            'SELECT * FROM contacts WHERE company_id=? ORDER BY name COLLATE NOCASE', (cid,)).fetchall()
        sites = conn.execute(
            'SELECT * FROM sites WHERE company_id=? ORDER BY name COLLATE NOCASE, id', (cid,)).fetchall()
        opps = conn.execute(
            'SELECT * FROM opportunities WHERE company_id=? ORDER BY (stage IN ("won","lost")), id DESC',
            (cid,)).fetchall()
        jobs = conn.execute(
            "SELECT id,title,status,job_type,scheduled_date,address FROM jobs WHERE company_id=? ORDER BY (status='completed'), COALESCE(scheduled_date, created_at) DESC",
            (cid,)).fetchall()
        conn.close()

        vals = {
            'name': co['name'], 'phone': co['phone'] or '', 'email': co['email'] or '',
            'billing_address': co['billing_address'] or '', 'notes': co['notes'] or '',
        }
        body = ''
        if msg:
            body += '<div class="plat-msg">%s</div>' % h(msg)
        body += '<div class="plat-grid-2">'
        body += '<div class="plat-panel"><h2>Company</h2>' + _company_form(vals, '/customers/%d' % cid, 'Save company') + '</div>'
        body += '<div>'
        # Contacts
        body += '<div class="plat-panel"><div class="plat-panel-h"><h2>Contacts</h2><a class="plat-btn plat-btn-ghost" href="/customers/%d/contacts/new">+ Contact</a></div>' % cid
        if contacts:
            body += '<ul class="plat-list">' + ''.join(
                '<li><a href="/contacts/%d"><b>%s</b></a> <span class="plat-pill">%s</span>'
                '<div class="muted">%s</div></li>' % (
                    t['id'], h(t['name']), h(role_label(t['role'])),
                    h(' · '.join(x for x in (t['title'] or '', t['phone'] or '', t['email'] or '') if x)))
                for t in contacts
            ) + '</ul>'
        else:
            body += '<div class="muted">No contacts yet.</div>'
        body += '</div>'
        # Sites
        body += '<div class="plat-panel"><div class="plat-panel-h"><h2>Sites</h2><a class="plat-btn plat-btn-ghost" href="/customers/%d/sites/new">+ Site</a></div>' % cid
        if sites:
            body += '<ul class="plat-list">' + ''.join(
                '<li><a href="/sites/%d"><b>%s</b></a><div class="muted">%s</div>'
                '<div class="plat-mini-actions"><a href="/systems?client=%s">Installed systems</a></div></li>' % (
                    s['id'], h(s['name'] or s['address'] or 'Site'),
                    h(s['address'] or ''),
                    urllib.parse.quote(co['name']))
                for s in sites
            ) + '</ul>'
        else:
            body += '<div class="muted">No sites yet.</div>'
        body += '</div></div></div>'

        body += '<div class="plat-panel"><div class="plat-panel-h"><h2>Opportunities</h2><a class="plat-btn plat-btn-ghost" href="/opportunities/new?company_id=%d">+ Opportunity</a></div>' % cid
        if opps:
            body += '<ul class="plat-list">' + ''.join(
                '<li><a href="/opportunities/%d"><b>%s</b></a> <span class="plat-pill">%s</span>'
                '<div class="muted">%s</div></li>' % (
                    o['id'], h(o['title']), h(stage_label(o['stage'])),
                    h(money_amt(o['amount']) or ''))
                for o in opps
            ) + '</ul>'
        else:
            body += '<div class="muted">None yet. Add an opportunity when a lead comes in.</div>'
        body += '</div>'

        body += '<div class="plat-panel"><div class="plat-panel-h"><h2>Jobs</h2><a class="plat-btn plat-btn-ghost" href="/new-job?company_id=%d&customer=%s">+ Job</a></div>' % (cid, urllib.parse.quote(co['name']))
        if jobs:
            body += '<ul class="plat-list">' + ''.join(
                '<li><a href="/?job=%s"><b>%s</b> %s</a> <span class="plat-pill">%s</span>'
                '<div class="muted">%s</div></li>' % (
                    urllib.parse.quote(j['id']), h(j['id']), h(j['title'] or ''),
                    h(j['job_type'] or j['status'] or ''),
                    h(' · '.join(x for x in (j['address'] or '', j['scheduled_date'] or '', j['status'] or '') if x)))
                for j in jobs
            ) + '</ul>'
        else:
            body += '<div class="muted">No jobs linked yet.</div>'
        body += '</div>'
        body += ('<div class="plat-panel"><h2>Company record</h2>'
                 '<p class="muted">Jobs are never deleted from this page. Archiving hides the company from the Customers list. Jobs, photos, contacts, sites and opportunities stay.</p>')
        if co['archived']:
            body += '<p>This company is archived.</p>'
        else:
            msg = 'Archive %s? Jobs will be kept.' % h(co['name'])
            body += (
                '<form method="post" action="/customers/%d/archive">'
                '<button class="plat-btn plat-btn-ghost" type="submit" data-msg="%s" '
                'onclick="return confirm(this.getAttribute(\'data-msg\'))">Archive company</button></form>'
                % (cid, msg)
            )
        body += '<div class="plat-mini-actions" style="margin-top:10px"><a href="/customers/merge?drop=%d">Merge into another company</a></div></div>' % cid
        if hasattr(fo, 'crm_block'):
            try:
                body += fo.crm_block('company', cid, company_id=cid)
            except Exception:
                fo.app.logger.exception('crm company block failed')
        if hasattr(fo, 'portal_company_html'):
            try:
                body += fo.portal_company_html(conn, cid)
            except Exception:
                fo.app.logger.exception('portal company block failed')
        return page(co['name'], body, back='/customers', kicker='Customer',
                    actions='<a class="plat-btn plat-btn-ghost" href="/systems?client=%s">Installed systems</a>' % urllib.parse.quote(co['name']))


    @app.route('/customers/<int:cid>/archive', methods=['POST'])
    def customers_archive(cid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        _, err = archive_company(conn, cid)
        if not err:
            conn.commit()
        conn.close()
        return redirect('/customers')

    @app.route('/customers/merge', methods=['GET', 'POST'])
    def customers_merge():
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        companies = conn.execute(
            'SELECT id,name,archived FROM companies ORDER BY name COLLATE NOCASE').fetchall()
        msg = ''
        if request.method == 'POST':
            try:
                keep_id = int(request.form.get('keep') or 0)
                drop_id = int(request.form.get('drop') or 0)
            except ValueError:
                keep_id = drop_id = 0
            if request.form.get('confirm') != 'yes':
                keep = conn.execute('SELECT * FROM companies WHERE id=?', (keep_id,)).fetchone()
                drop = conn.execute('SELECT * FROM companies WHERE id=?', (drop_id,)).fetchone()
                conn.close()
                if not keep or not drop or keep_id == drop_id:
                    return redirect('/customers/merge')
                body = (
                    '<div class="plat-panel"><p>Merge <b>%s</b> into <b>%s</b>.</p>'
                    '<p>Jobs, contacts, sites and opportunities move to %s. The other company record is removed. Jobs are not deleted.</p>'
                    '<form method="post" action="/customers/merge">'
                    '<input type="hidden" name="keep" value="%d"><input type="hidden" name="drop" value="%d">'
                    '<input type="hidden" name="confirm" value="yes">'
                    '<button class="plat-btn plat-btn-gold" type="submit">Merge now</button> '
                    '<a class="plat-btn plat-btn-ghost" href="/customers/merge">Cancel</a></form></div>'
                    % (h(drop['name']), h(keep['name']), h(keep['name']), keep_id, drop_id))
                return page('Merge companies', body, back='/customers', kicker='Customers')
            row, err = merge_companies(conn, keep_id, drop_id)
            if err:
                msg = err
            else:
                conn.commit()
                cid = row['id']
                conn.close()
                return redirect('/customers/%d' % cid)
        def sel_for(flag, label):
            return '<label>%s</label><select name="%s" required><option value="">Pick a company</option>%s</select>' % (
                label, flag, ''.join(
                    '<option value="%d"%s>%s%s</option>' % (
                        c['id'],
                        ' selected' if str(c['id']) == str(request.values.get(flag) or '') else '',
                        h(c['name']),
                        ' (archived)' if c['archived'] else '')
                    for c in companies))
        conn.close()
        form = (
            '<div class="plat-panel"><p>Use this when the same customer was entered twice. Jobs stay; they move to the company you keep.</p>'
            '<form class="plat-form" method="post">'
            + sel_for('drop', 'Merge this duplicate')
            + sel_for('keep', 'Into this company')
            + '<button class="plat-btn plat-btn-gold" type="submit">Review merge</button></form></div>')
        if msg:
            form = '<div class="plat-msg">%s</div>' % h(msg) + form
        return page('Merge duplicates', form, back='/customers', kicker='Customers')

    # ----- Contacts -----
    def _contact_form(vals, action, submit, companies, company_locked=False):
        co_opts = [(str(c['id']), c['name']) for c in companies]
        co_sel = ('<input type="hidden" name="company_id" value="%s"><div class="muted">Company is set.</div>' % h(str(vals.get('company_id') or ''))
                  if company_locked else
                  '<label>Company</label><select name="company_id" required>%s</select>' % sel(co_opts, vals.get('company_id'), blank='Pick a company'))
        return (
            '<form class="plat-form" method="post" action="%s">%s'
            '<label>Name</label><input name="name" required maxlength="120" value="%s">'
            '<div class="plat-row"><div><label>Role</label><select name="role">%s</select></div>'
            '<div><label>Title</label><input name="title" maxlength="80" value="%s"></div></div>'
            '<div class="plat-row"><div><label>Phone</label><input name="phone" type="tel" value="%s"></div>'
            '<div><label>Email</label><input name="email" type="email" value="%s"></div></div>'
            '<label>Notes (office only)</label><textarea name="notes" rows="3">%s</textarea>'
            '<button class="plat-btn plat-btn-gold" type="submit">%s</button></form>'
            % (h(action), co_sel, h(vals.get('name') or ''),
               sel(CONTACT_ROLES, vals.get('role') or 'site', blank=None),
               h(vals.get('title') or ''), h(vals.get('phone') or ''),
               h(vals.get('email') or ''), h(vals.get('notes') or ''), h(submit))
        )

    def _save_contact(conn, cid=None):
        name = (request.form.get('name') or '').strip()
        try:
            company_id = int(request.form.get('company_id') or 0)
        except ValueError:
            company_id = 0
        if not name or not company_id:
            return None, 'Name and company are required.'
        if not conn.execute('SELECT 1 FROM companies WHERE id=?', (company_id,)).fetchone():
            return None, 'Company not found.'
        role = request.form.get('role') if request.form.get('role') in CONTACT_ROLE_SET else 'other'
        vals = (
            company_id, name[:120], role,
            (request.form.get('title') or '').strip()[:80],
            (request.form.get('phone') or '').strip()[:40],
            (request.form.get('email') or '').strip()[:120],
            (request.form.get('notes') or '').strip()[:2000],
            now_iso(),
        )
        if cid:
            conn.execute(
                'UPDATE contacts SET company_id=?,name=?,role=?,title=?,phone=?,email=?,notes=?,updated_at=? WHERE id=?',
                vals + (cid,))
            return cid, None
        cur = conn.execute(
            'INSERT INTO contacts (company_id,name,role,title,phone,email,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
            vals[:-1] + (vals[-1], vals[-1]))
        return cur.lastrowid, None

    @app.route('/customers/<int:cid>/contacts/new', methods=['GET', 'POST'])
    def contact_new(cid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        co = company_or_404(conn, cid)
        if not co:
            conn.close()
            return redirect('/customers')
        msg = ''
        if request.method == 'POST':
            new_id, err = _save_contact(conn)
            if err:
                msg = err
            else:
                conn.commit(); conn.close()
                return redirect('/customers/%d' % cid)
        companies = [co]
        vals = {k: request.form.get(k) or '' for k in ('name', 'role', 'title', 'phone', 'email', 'notes')}
        vals['company_id'] = str(cid)
        vals.setdefault('role', 'site')
        form = _contact_form(vals, '/customers/%d/contacts/new' % cid, 'Add contact', companies, company_locked=True)
        conn.close()
        if msg:
            form = '<div class="plat-msg">%s</div>' % h(msg) + form
        return page('New contact', form, back='/customers/%d' % cid, kicker=co['name'])

    @app.route('/contacts/<int:tid>', methods=['GET', 'POST'])
    def contact_edit(tid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM contacts WHERE id=?', (tid,)).fetchone()
        if not row:
            conn.close()
            return redirect('/customers')
        msg = ''
        if request.method == 'POST':
            _, err = _save_contact(conn, tid)
            if err:
                msg = err
            else:
                msg = 'Saved.'
                conn.commit()
                row = conn.execute('SELECT * FROM contacts WHERE id=?', (tid,)).fetchone()
        companies = conn.execute('SELECT id,name FROM companies WHERE IFNULL(archived,0)=0 ORDER BY name').fetchall()
        vals = {k: row[k] or '' for k in ('name', 'role', 'title', 'phone', 'email', 'notes', 'company_id')}
        form = _contact_form(vals, '/contacts/%d' % tid, 'Save contact', companies)
        if msg:
            form = '<div class="plat-msg">%s</div>' % h(msg) + form
        co = conn.execute('SELECT name FROM companies WHERE id=?', (row['company_id'],)).fetchone()
        conn.close()
        extra = ''
        if hasattr(fo, 'crm_block'):
            try:
                extra = fo.crm_block('contact', tid, company_id=row['company_id'], contact_id=tid)
            except Exception:
                fo.app.logger.exception('crm contact block failed')
        return page(row['name'], form + extra, back='/customers/%d' % row['company_id'],
                    kicker=(co['name'] if co else 'Contact'))

    # ----- Sites -----
    def _site_form(vals, action, submit):
        return (
            '<form class="plat-form" method="post" action="%s">'
            '<label>Site name</label><input name="name" maxlength="200" value="%s" placeholder="Main building">'
            '<label>Address</label><input name="address" required maxlength="300" value="%s">'
            '<label>Site phone</label><input name="phone" type="tel" value="%s">'
            '<label>Notes (office only)</label><textarea name="notes" rows="3">%s</textarea>'
            '<button class="plat-btn plat-btn-gold" type="submit">%s</button></form>'
            % (h(action), h(vals.get('name') or ''), h(vals.get('address') or ''),
               h(vals.get('phone') or ''), h(vals.get('notes') or ''), h(submit))
        )

    def _save_site(conn, company_id, sid=None):
        address = (request.form.get('address') or '').strip()
        name = (request.form.get('name') or '').strip()
        if not address and not name:
            return None, 'Address or name is required.'
        key = nkey(address) or nkey(name) or '-'
        other = conn.execute(
            'SELECT id FROM sites WHERE company_id=? AND address_key=? AND id!=?',
            (company_id, key, sid or 0)).fetchone()
        if other:
            return None, 'That site already exists for this company.'
        vals = (
            name[:200] or (address.split(',')[0].strip()[:200] if address else 'Main site'),
            address[:300], key,
            (request.form.get('phone') or '').strip()[:40],
            (request.form.get('notes') or '').strip()[:2000],
            now_iso(),
        )
        if sid:
            conn.execute(
                'UPDATE sites SET name=?,address=?,address_key=?,phone=?,notes=?,updated_at=? WHERE id=?',
                vals + (sid,))
            return sid, None
        cur = conn.execute(
            'INSERT INTO sites (company_id,name,address,address_key,phone,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
            (company_id,) + vals[:-1] + (vals[-1], vals[-1]))
        return cur.lastrowid, None

    @app.route('/customers/<int:cid>/sites/new', methods=['GET', 'POST'])
    def site_new(cid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        co = company_or_404(conn, cid)
        if not co:
            conn.close()
            return redirect('/customers')
        msg = ''
        if request.method == 'POST':
            _, err = _save_site(conn, cid)
            if err:
                msg = err
            else:
                conn.commit(); conn.close()
                return redirect('/customers/%d' % cid)
        conn.close()
        vals = {k: request.form.get(k) or '' for k in ('name', 'address', 'phone', 'notes')}
        form = _site_form(vals, '/customers/%d/sites/new' % cid, 'Add site')
        if msg:
            form = '<div class="plat-msg">%s</div>' % h(msg) + form
        return page('New site', form, back='/customers/%d' % cid, kicker=co['name'])

    @app.route('/sites/<int:sid>', methods=['GET', 'POST'])
    def site_edit(sid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM sites WHERE id=?', (sid,)).fetchone()
        if not row:
            conn.close()
            return redirect('/customers')
        msg = ''
        if request.method == 'POST':
            _, err = _save_site(conn, row['company_id'], sid)
            if err:
                msg = err
            else:
                msg = 'Saved.'
                conn.commit()
                row = conn.execute('SELECT * FROM sites WHERE id=?', (sid,)).fetchone()
        co = conn.execute('SELECT * FROM companies WHERE id=?', (row['company_id'],)).fetchone()
        jobs = conn.execute(
            'SELECT id,title,status,job_type FROM jobs WHERE site_id=? ORDER BY id DESC',
            (sid,)).fetchall()
        conn.close()
        vals = {k: row[k] or '' for k in ('name', 'address', 'phone', 'notes')}
        form = _site_form(vals, '/sites/%d' % sid, 'Save site')
        if msg:
            form = '<div class="plat-msg">%s</div>' % h(msg) + form
        if jobs:
            form += '<h2>Jobs at this site</h2><ul class="plat-list">' + ''.join(
                '<li><a href="/?job=%s"><b>%s</b> %s</a> <span class="plat-pill">%s</span></li>' % (
                    urllib.parse.quote(j['id']), h(j['id']), h(j['title'] or ''), h(j['status'] or ''))
                for j in jobs) + '</ul>'
        sys_link = ''
        if co:
            sys_link = '<a class="plat-btn plat-btn-ghost" href="/systems?client=%s">Installed systems</a>' % urllib.parse.quote(co['name'])
        return page(row['name'] or 'Site', form, back='/customers/%d' % row['company_id'],
                    kicker=(co['name'] if co else 'Site'), actions=sys_link)

    # ----- Opportunities -----
    def _opp_form(vals, action, submit, companies, contacts, sites):
        co_opts = [(str(c['id']), c['name']) for c in companies]
        ct_opts = [(str(t['id']), t['name']) for t in contacts]
        st_opts = [(str(s['id']), (s['name'] or s['address'] or 'Site')) for s in sites]
        amt = vals.get('amount') or ''
        return (
            '<form class="plat-form" method="post" action="%s">'
            '<label>Title</label><input name="title" required maxlength="200" value="%s" placeholder="Camera upgrade, warehouse">'
            '<label>Company</label><select name="company_id" required id="opp-co">%s</select>'
            '<div class="plat-row"><div><label>Contact</label><select name="contact_id">%s</select></div>'
            '<div><label>Site</label><select name="site_id">%s</select></div></div>'
            '<div class="plat-row"><div><label>Stage</label><select name="stage" id="opp-stage" onchange="var b=document.getElementById(\'opp-lost-fields\');if(b)b.style.display=this.value===\'lost\'?\'\'\':\'none\'">%s</select></div>'
            '<div><label>Amount $ (office only)</label><input name="amount" type="number" step="0.01" min="0" value="%s"></div></div>'
            '<div class="plat-row"><div><label>Lead source</label><select name="lead_source">%s</select></div>'
            '<div><label>Fit grade</label><select name="fit_grade">%s</select></div></div>'
            '<div id="opp-lost-fields" style="display:%s">'
            '<div class="plat-row"><div><label>Lost reason</label><select name="lost_reason">%s</select></div></div>'
            '<label>Lost note</label><textarea name="lost_note" rows="2" placeholder="Required if you pick Other">%s</textarea></div>'
            '<label>Notes (office only)</label><textarea name="notes" rows="3">%s</textarea>'
            '<button class="plat-btn plat-btn-gold" type="submit">%s</button></form>'
            % (h(action), h(vals.get('title') or ''),
               sel(co_opts, vals.get('company_id'), blank='Pick a company'),
               sel(ct_opts, vals.get('contact_id'), blank='None yet'),
               sel(st_opts, vals.get('site_id'), blank='None yet'),
               sel(OPP_STAGES, vals.get('stage') or 'lead', blank=None),
               h(str(amt)),
               sel(fo.LEAD_SOURCES if hasattr(fo,'LEAD_SOURCES') else [('zack','Zack'),('website','Website'),('referral','Referral'),('google','Google'),('repeat','Repeat'),('other','Other')], vals.get('lead_source') or '', blank='None'),
               sel([('A','A'),('B','B'),('C','C')], vals.get('fit_grade') or '', blank='None'),
               '' if (vals.get('stage')=='lost') else 'none',
               sel(fo.LOST_REASONS if hasattr(fo,'LOST_REASONS') else [('price','Price'),('timing','Timing'),('no_fit','No fit'),('no_response','No response'),('competitor','Lost to competitor'),('other','Other')], vals.get('lost_reason') or '', blank='Pick a reason'),
               h(vals.get('lost_note') or ''),
               h(vals.get('notes') or ''), h(submit))
        )

    def _save_opp(conn, oid=None):
        title = (request.form.get('title') or '').strip()
        try:
            company_id = int(request.form.get('company_id') or 0)
        except ValueError:
            company_id = 0
        if not title or not company_id:
            return None, 'Title and company are required.'
        if not conn.execute('SELECT 1 FROM companies WHERE id=?', (company_id,)).fetchone():
            return None, 'Company not found.'
        def opt_id(key):
            raw = (request.form.get(key) or '').strip()
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                return None
        contact_id, site_id = opt_id('contact_id'), opt_id('site_id')
        if contact_id and not conn.execute(
                'SELECT 1 FROM contacts WHERE id=? AND company_id=?', (contact_id, company_id)).fetchone():
            return None, 'That contact does not belong to this company.'
        if site_id and not conn.execute(
                'SELECT 1 FROM sites WHERE id=? AND company_id=?', (site_id, company_id)).fetchone():
            return None, 'That site does not belong to this company.'
        stage = request.form.get('stage') if request.form.get('stage') in OPP_STAGE_SET else 'lead'
        try:
            amount = max(0, float(request.form.get('amount') or 0))
        except ValueError:
            amount = 0
        when = now_iso()
        won_at = when if stage == 'won' else None
        lost_at = when if stage == 'lost' else None
        notes = (request.form.get('notes') or '').strip()[:4000]
        lead_source = (request.form.get('lead_source') or '').strip().lower()
        if lead_source not in (set(k for k,_ in getattr(fo,'LEAD_SOURCES',())) or set()):
            # allow empty
            if lead_source not in ('zack','website','referral','google','repeat','other',''):
                lead_source = ''
        lead_source = lead_source or None
        fit_grade = (request.form.get('fit_grade') or '').strip().upper()
        if fit_grade not in ('A','B','C'):
            fit_grade = None
        lost_reason = (request.form.get('lost_reason') or '').strip()
        lost_note = (request.form.get('lost_note') or '').strip()[:1000]
        if stage == 'lost':
            apply = getattr(fo, 'leads_apply_lost', None)
            # validate before write; apply_lost also writes stage, so we validate only here
            if lost_reason not in ('price','timing','no_fit','no_response','competitor','other'):
                return None, 'Pick a lost reason.'
            if lost_reason == 'other' and not lost_note:
                return None, 'Add a short note for Other.'
        else:
            lost_reason = None
            lost_note = None
        if oid:
            existing = conn.execute('SELECT * FROM opportunities WHERE id=?', (oid,)).fetchone()
            if existing and existing['stage'] == 'won' and stage == 'won':
                won_at = existing['won_at'] or won_at
            if existing and existing['stage'] == 'lost' and stage == 'lost':
                lost_at = existing['lost_at'] or lost_at
            conn.execute(
                'UPDATE opportunities SET company_id=?,contact_id=?,site_id=?,title=?,stage=?,amount=?,notes=?,updated_at=?,won_at=?,lost_at=?,lead_source=?,fit_grade=?,lost_reason=?,lost_note=? WHERE id=?',
                (company_id, contact_id, site_id, title[:200], stage, amount, notes, when, won_at, lost_at, lead_source, fit_grade, lost_reason, lost_note, oid))
            return oid, None
        cur = conn.execute(
            'INSERT INTO opportunities (company_id,contact_id,site_id,title,stage,amount,notes,created_by,created_at,updated_at,won_at,lost_at,lead_source,fit_grade,lost_reason,lost_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (company_id, contact_id, site_id, title[:200], stage, amount, notes, fo.me(), when, when, won_at, lost_at, lead_source, fit_grade, lost_reason, lost_note))
        return cur.lastrowid, None

    @app.route('/opportunities')
    def opportunities_list():
        gate = office_only()
        if gate:
            return gate
        set_area('sales')
        stage = (request.args.get('stage') or '').strip()
        if stage and stage not in OPP_STAGE_SET:
            stage = ''
        conn = fo.get_db()
        sql = '''SELECT o.*, c.name AS company, s.name AS site_name, s.address AS site_address,
                    t.name AS contact_name
                 FROM opportunities o
                 JOIN companies c ON c.id=o.company_id
                 LEFT JOIN sites s ON s.id=o.site_id
                 LEFT JOIN contacts t ON t.id=o.contact_id'''
        args = []
        if stage:
            sql += ' WHERE o.stage=?'
            args.append(stage)
        sql += ' ORDER BY (o.stage IN ("won","lost")), o.updated_at DESC, o.id DESC'
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        counts = {k: 0 for k, _ in OPP_STAGES}
        totals = {k: 0.0 for k, _ in OPP_STAGES}
        all_rows = conn.execute('SELECT stage, amount FROM opportunities').fetchall()
        for r in all_rows:
            counts[r['stage']] = counts.get(r['stage'], 0) + 1
            totals[r['stage']] = totals.get(r['stage'], 0) + (r['amount'] or 0)
        conn.close()
        pills = '<div class="plat-pills"><a class="plat-pill-link%s" href="/opportunities">All</a>' % (' on' if not stage else '')
        for k, lab in OPP_STAGES:
            pills += '<a class="plat-pill-link%s" href="/opportunities?stage=%s">%s <em>%d</em></a>' % (
                ' on' if stage == k else '', k, h(lab), counts.get(k, 0))
        pills += '</div>'
        # Stat row (office only, includes amounts)
        open_n = sum(counts[k] for k, _ in OPP_STAGES if k not in ('won', 'lost'))
        open_amt = sum(totals[k] for k, _ in OPP_STAGES if k not in ('won', 'lost'))
        stats = (
            '<div class="plat-stats">'
            '<div class="plat-stat"><small>Open pipeline</small><b>%s</b><div class="muted">%d opportunities</div></div>'
            '<div class="plat-stat"><small>Won</small><b>%s</b><div class="muted">%d</div></div>'
            '<div class="plat-stat"><small>Lost</small><b>%s</b><div class="muted">%d</div></div>'
            '</div>' % (
                h(money(open_amt) if open_amt else (str(open_n) if open_n else '0')),
                open_n, h(money(totals.get('won') or 0) if totals.get('won') else str(counts.get('won') or 0)),
                counts.get('won') or 0,
                h(money(totals.get('lost') or 0) if totals.get('lost') else str(counts.get('lost') or 0)),
                counts.get('lost') or 0)
        )

        def card(o):
            where = o.get('site_name') or o.get('site_address') or ''
            src = ''
            try:
                if o.get('lead_source') and hasattr(fo, 'lead_source_label'):
                    src = fo.lead_source_label(o.get('lead_source'))
            except Exception:
                src = o.get('lead_source') or ''
            badge = ''
            try:
                if hasattr(fo, 'leads_fit_badge'):
                    badge = fo.leads_fit_badge(o.get('fit_grade'))
            except Exception:
                badge = ''
            meta = ' · '.join(x for x in (
                o.get('company') or '',
                money_amt(o.get('amount')) if fo.is_office() else '',
                o.get('contact_name') or '',
                where,
                src,
            ) if x)
            stage_opts = ''.join(
                '<option value="%s"%s>%s</option>' % (sk, ' selected' if sk == o['stage'] else '', h(slab))
                for sk, slab in OPP_STAGES)
            return (
                '<div class="plat-card plat-opp" draggable="true" data-id="%d" data-stage="%s">'
                '<a href="/opportunities/%d"><b>%s</b></a> %s'
                '<div class="muted">%s</div>'
                '<div class="plat-opp-move"><label class="muted">Move to</label>'
                '<select aria-label="Move opportunity" onchange="foOppStage(%d,this.value)">%s</select></div></div>'
                % (o['id'], h(o['stage']), o['id'], h(o['title']), badge, h(meta), o['id'], stage_opts)
            )

        show_stages = OPP_STAGES if not stage else [s for s in OPP_STAGES if s[0] == stage]
        by = {}
        for o in rows:
            by.setdefault(o['stage'], []).append(o)
        cols = '<div class="plat-board" id="plat-board">'
        for k, lab in show_stages:
            items = by.get(k, [])
            amt = totals.get(k) or 0
            cols += '<div class="plat-col" data-stage="%s"><div class="plat-col-h"><span>%s</span> <em>%d%s</em></div>' % (
                k, h(lab), len(items), (' · ' + money(amt)) if amt else '')
            cols += ''.join(card(o) for o in items) or '<div class="muted plat-col-empty">None</div>'
            cols += '</div>'
        cols += '</div>'
        cols += _OPP_DND_JS
        body = stats + pills + cols
        actions = '<a class="plat-btn plat-btn-gold" href="/opportunities/new">+ Opportunity</a> <a class="plat-btn plat-btn-ghost" href="/leads">Leads list</a>'
        return page('Opportunities', body, kicker='Sales', actions=actions, area='sales')

    @app.route('/opportunities/new', methods=['GET', 'POST'])
    def opportunity_new():
        gate = office_only()
        if gate:
            return gate
        set_area('sales')
        conn = fo.get_db()
        pre_co = request.args.get('company_id') or request.form.get('company_id') or ''
        try:
            pre_id = int(pre_co) if pre_co else 0
        except ValueError:
            pre_id = 0
        msg = ''
        if request.method == 'POST':
            new_id, err = _save_opp(conn)
            if err:
                msg = err
            else:
                conn.commit(); conn.close()
                return redirect('/opportunities/%d' % new_id)
        companies = conn.execute('SELECT id,name FROM companies WHERE IFNULL(archived,0)=0 ORDER BY name').fetchall()
        contacts = conn.execute(
            'SELECT id,name,company_id FROM contacts WHERE ? IN (0, company_id) ORDER BY name',
            (pre_id,)).fetchall() if pre_id else conn.execute(
            'SELECT id,name,company_id FROM contacts ORDER BY name').fetchall()
        sites = conn.execute(
            'SELECT id,name,address,company_id FROM sites WHERE ? IN (0, company_id) ORDER BY name',
            (pre_id,)).fetchall() if pre_id else conn.execute(
            'SELECT id,name,address,company_id FROM sites ORDER BY name').fetchall()
        vals = {k: request.form.get(k) or '' for k in ('title', 'company_id', 'contact_id', 'site_id', 'stage', 'amount', 'notes', 'lead_source', 'fit_grade', 'lost_reason', 'lost_note')}
        if pre_id and not vals['company_id']:
            vals['company_id'] = str(pre_id)
        form = _opp_form(vals, '/opportunities/new', 'Add opportunity', companies, contacts, sites)
        conn.close()
        if msg:
            form = '<div class="plat-msg">%s</div>' % h(msg) + form
        back = '/customers/%d' % pre_id if pre_id else '/opportunities'
        return page('New opportunity', form, back=back, kicker='Sales', area='sales')

    @app.route('/opportunities/<int:oid>', methods=['GET', 'POST'])
    def opportunity_edit(oid):
        gate = office_only()
        if gate:
            return gate
        set_area('sales')
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM opportunities WHERE id=?', (oid,)).fetchone()
        if not row:
            conn.close()
            return redirect('/opportunities')
        msg = ''
        if request.method == 'POST' and request.form.get('action') != 'start_job':
            _, err = _save_opp(conn, oid)
            if err:
                msg = err
            else:
                msg = 'Saved.'
                conn.commit()
                row = conn.execute('SELECT * FROM opportunities WHERE id=?', (oid,)).fetchone()
        companies = conn.execute('SELECT id,name FROM companies WHERE IFNULL(archived,0)=0 ORDER BY name').fetchall()
        contacts = conn.execute(
            'SELECT id,name FROM contacts WHERE company_id=? ORDER BY name', (row['company_id'],)).fetchall()
        sites = conn.execute(
            'SELECT id,name,address FROM sites WHERE company_id=? ORDER BY name', (row['company_id'],)).fetchall()
        co = conn.execute('SELECT * FROM companies WHERE id=?', (row['company_id'],)).fetchone()
        site = conn.execute('SELECT * FROM sites WHERE id=?', (row['site_id'],)).fetchone() if row['site_id'] else None
        contact = conn.execute('SELECT * FROM contacts WHERE id=?', (row['contact_id'],)).fetchone() if row['contact_id'] else None
        def _col(key):
            try:
                return row[key] if key in row.keys() else ''
            except Exception:
                return ''
        vals = {
            'title': row['title'], 'company_id': row['company_id'], 'contact_id': row['contact_id'] or '',
            'site_id': row['site_id'] or '', 'stage': row['stage'],
            'amount': ('%g' % row['amount']) if row['amount'] else '', 'notes': row['notes'] or '',
            'lead_source': _col('lead_source') or '', 'fit_grade': _col('fit_grade') or '',
            'lost_reason': _col('lost_reason') or '', 'lost_note': _col('lost_note') or '',
        }
        form = _opp_form(vals, '/opportunities/%d' % oid, 'Save opportunity', companies, contacts, sites)
        extra = '<div class="plat-panel"><h2>Next step</h2><div class="muted">Start a quote from this opportunity. Prices stay in the office until you share the client link. Nothing is sent from the app.</div>'
        extra += '<a class="plat-btn plat-btn-gold" href="/quotes/new?opportunity_id=%d">Start quote</a>' % oid
        survey_id = ''
        try:
            survey_id = row['survey_job_id'] if 'survey_job_id' in row.keys() else ''
        except Exception:
            survey_id = ''
        if survey_id:
            extra += ' <a class="plat-btn plat-btn-ghost" href="/?job=%s">Open survey %s</a>' % (urllib.parse.quote(survey_id), h(survey_id))
        else:
            extra += (
                ' <form method="post" action="/opportunities/%d/survey" style="display:inline">'
                '<button class="plat-btn plat-btn-ghost" type="submit">Start survey</button></form>'
            ) % oid
        if row['job_id']:
            extra += ' <a class="plat-btn plat-btn-ghost" href="/?job=%s">Open job %s</a>' % (urllib.parse.quote(row['job_id']), h(row['job_id']))
        else:
            q = urllib.parse.urlencode({
                't': 'Install',
                'customer': (co['name'] if co else ''),
                'address': (site['address'] if site else ''),
                'phone': (contact['phone'] if contact and contact['phone'] else (co['phone'] if co else '')),
                'title': row['title'],
                'company_id': str(row['company_id'] or ''),
                'opp': str(oid),
            })
            extra += ' <a class="plat-btn plat-btn-ghost" href="/new-job?%s">Start a job from this</a>' % h(q)
        extra += '</div>'
        conn.close()
        if msg:
            form = '<div class="plat-msg">%s</div>' % h(msg) + form
        crm = ''
        if hasattr(fo, 'crm_block'):
            try:
                crm = fo.crm_block('opportunity', oid, company_id=row['company_id'],
                                   contact_id=row['contact_id'], opportunity_id=oid)
            except Exception:
                fo.app.logger.exception('crm opportunity block failed')
        if crm:
            crm = '<div id="lead-notes">' + crm + '</div>'
        return page(row['title'], form + extra + crm, back='/opportunities',
                    kicker=(co['name'] if co else 'Opportunity'), area='sales')

    # ----- Sales and Quote homes -----
    @app.route('/api/opportunities/<int:oid>/stage', methods=['POST'])
    def opportunity_stage(oid):
        gate = office_only()
        if gate:
            return gate
        data = request.get_json(silent=True) or request.form
        stage = (data.get('stage') or '').strip()
        if stage not in OPP_STAGE_SET:
            return jsonify({'error': 'Unknown stage'}), 400
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM opportunities WHERE id=?', (oid,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Not found'}), 404
        when = now_iso()
        if stage == 'won':
            won_at = row['won_at'] or when
            lost_at = None
            conn.execute(
                'UPDATE opportunities SET stage=?, updated_at=?, won_at=?, lost_at=?, lost_reason=NULL, lost_note=NULL WHERE id=?',
                (stage, when, won_at, lost_at, oid))
        elif stage == 'lost':
            reason = (data.get('lost_reason') or '').strip()
            note = (data.get('lost_note') or '').strip()[:1000]
            apply = getattr(fo, 'leads_apply_lost', None)
            if callable(apply):
                err = apply(conn, oid, reason, note, when=when)
                if err:
                    conn.close()
                    return jsonify({'error': err, 'need': 'lost_reason'}), 400
            else:
                if not reason:
                    conn.close()
                    return jsonify({'error': 'Pick a lost reason.', 'need': 'lost_reason'}), 400
                conn.execute(
                    'UPDATE opportunities SET stage=?, updated_at=?, lost_at=?, won_at=NULL, lost_reason=?, lost_note=? WHERE id=?',
                    (stage, when, row['lost_at'] or when, reason, note, oid))
        else:
            conn.execute(
                'UPDATE opportunities SET stage=?, updated_at=?, won_at=NULL, lost_at=NULL WHERE id=?',
                (stage, when, oid))
        conn.commit(); conn.close()
        return jsonify({'ok': True, 'id': oid, 'stage': stage})

    @app.route('/sales')
    def sales_home():
        gate = office_only()
        if gate:
            return gate
        set_area('sales')
        conn = fo.get_db()
        rows = conn.execute(
            '''SELECT o.*, c.name AS company FROM opportunities o
               JOIN companies c ON c.id=o.company_id
               ORDER BY o.updated_at DESC LIMIT 12''').fetchall()
        counts = {k: 0 for k, _ in OPP_STAGES}
        totals = {k: 0.0 for k, _ in OPP_STAGES}
        for r in conn.execute('SELECT stage, amount FROM opportunities'):
            counts[r['stage']] = counts.get(r['stage'], 0) + 1
            totals[r['stage']] = totals.get(r['stage'], 0) + (r['amount'] or 0)
        open_n = sum(counts[k] for k, _ in OPP_STAGES if k not in ('won', 'lost'))
        open_amt = sum(totals[k] for k, _ in OPP_STAGES if k not in ('won', 'lost'))
        n_co = conn.execute('SELECT COUNT(*) FROM companies WHERE IFNULL(archived,0)=0').fetchone()[0]
        crm_dash = ''
        if hasattr(fo, 'crm_sales_extra'):
            try:
                crm_dash = fo.crm_sales_extra(conn) or ''
            except Exception:
                fo.app.logger.exception('crm sales dashboard failed')
        conn.close()
        body = (
            '<div class="plat-stats">'
            '<a class="plat-stat" href="/opportunities"><small>Open pipeline</small><b>%s</b><div class="muted">%d opportunities</div></a>'
            '<a class="plat-stat" href="/opportunities?stage=won"><small>Won</small><b>%s</b><div class="muted">%d</div></a>'
            '<a class="plat-stat" href="/opportunities?stage=lost"><small>Lost</small><b>%s</b><div class="muted">%d</div></a>'
            '<a class="plat-stat" href="/customers"><small>Companies</small><b>%d</b><div class="muted">Shared customer list</div></a>'
            '</div>' % (
                h(money(open_amt) if open_amt else str(open_n)), open_n,
                h(str(counts.get('won') or 0)),
                counts.get('won') or 0,
                h(str(counts.get('lost') or 0)),
                counts.get('lost') or 0,
                n_co)
        )
        body += '<div class="plat-panel"><div class="plat-panel-h"><h2>Recent opportunities</h2><a class="plat-btn plat-btn-ghost" href="/opportunities">View all</a></div>'
        if rows:
            body += '<ul class="plat-list">' + ''.join(
                '<li><a href="/opportunities/%d"><b>%s</b></a> <span class="plat-pill">%s</span>'
                '<div class="muted">%s</div></li>' % (
                    r['id'], h(r['title']), h(stage_label(r['stage'])),
                    h(' · '.join(x for x in (r['company'], money_amt(r['amount'])) if x)))
                for r in rows) + '</ul>'
        else:
            body += '<div class="muted">No opportunities yet. When a lead comes in, add one here. A later step will add a full pipeline board, tasks and follow-ups.</div>'
        body += '</div>'
        body += crm_dash
        body += '<p class="muted">This is the Sales home. Customers are the same list you see in Ops and Quote. Signing or accepting a quote moves that opportunity to Won.</p>'
        actions = '<a class="plat-btn plat-btn-gold" href="/leads">Leads</a> <a class="plat-btn plat-btn-gold" href="/opportunities/new">+ Opportunity</a> <a class="plat-btn plat-btn-ghost" href="/tasks">Tasks</a>'
        return page('Sales', body, kicker='Field Ops', actions=actions, area='sales')

    @app.route('/quote')
    def quote_home():
        gate = office_only()
        if gate:
            return gate
        set_area('quote')
        conn = fo.get_db()
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
                # Still list jobs that have a plan, so Quote has somewhere to start.
                plans = []
                try:
                    plans = fo.plan_files(conn, r['id'])
                except Exception:
                    plans = []
                if not plans:
                    continue
                items.append((r, est, plans, 'plan'))
            else:
                plans = []
                try:
                    plans = fo.plan_files(conn, r['id'])
                except Exception:
                    plans = []
                items.append((r, est, plans, fo.est_stage(est) or 'draft'))
        conn.close()
        if not items:
            body = '<div class="plat-empty">No estimates or plans yet. Open a survey or install job and use the plan designer, or write an estimate on the job.</div>'
        else:
            body = '<div class="plat-cards">'
            for r, est, plans, st in items[:80]:
                lab = {'approved': 'Approved', 'lost': 'Lost', 'sent': 'Ready', 'plan': 'Plan only', 'draft': 'Draft'}.get(st, st)
                price = money_amt(est.get('price') or r['sale_amount'] or 0)
                body += (
                    '<a class="plat-card" href="/job/%s/estimate"><b>%s</b> <span class="plat-pill">%s</span>'
                    '<div>%s</div><div class="muted">%s%s</div></a>' % (
                        urllib.parse.quote(r['id']), h(r['id']), h(lab),
                        h(r['title'] or r['customer'] or ''),
                        h(r['customer'] or ''),
                        (' · ' + price) if price else ''))
            body += '</div>'
        body += '<p class="muted">Quote still uses the job estimate and the plan designer. A later step will add a price catalog, options and versions.</p>'
        return page('Estimates', body, kicker='Quote', area='quote')

    # Search hook: companies, contacts, sites, opportunities (office only)
    def plat_search(conn, like, hit, out, office):
        if not office:
            return
        q = like
        def block(title, href, rows):
            if not rows:
                return
            out.append('<h2>%s</h2>' % title + ''.join(
                '<a class="card" href="%s"><b>%s</b><div class="muted">%s</div></a>' % (
                    href % r[0], h(r[1]), h(r[2] or ''))
                for r in rows))
        block('Companies', '/customers/%s', [
            (r['id'], r['name'], r['billing_address'] or r['phone'] or '')
            for r in conn.execute(
                "SELECT id,name,billing_address,phone FROM companies WHERE IFNULL(archived,0)=0 AND LOWER(name||' '||IFNULL(billing_address,'')||' '||IFNULL(phone,'')) LIKE ? LIMIT 20",
                (q,))])
        block('Contacts', '/contacts/%s', [
            (r['id'], r['name'], r['company'])
            for r in conn.execute(
                "SELECT t.id,t.name,t.phone,t.email,c.name AS company FROM contacts t JOIN companies c ON c.id=t.company_id WHERE LOWER(t.name||' '||IFNULL(t.phone,'')||' '||IFNULL(t.email,'')||' '||c.name) LIKE ? LIMIT 20",
                (q,))])
        block('Sites', '/sites/%s', [
            (r['id'], r['name'] or r['address'] or 'Site', r['company'])
            for r in conn.execute(
                "SELECT s.id,s.name,s.address,c.name AS company FROM sites s JOIN companies c ON c.id=s.company_id WHERE LOWER(IFNULL(s.name,'')||' '||IFNULL(s.address,'')||' '||c.name) LIKE ? LIMIT 20",
                (q,))])
        block('Opportunities', '/opportunities/%s', [
            (r['id'], r['title'], r['company'] + ' · ' + stage_label(r['stage']))
            for r in conn.execute(
                "SELECT o.id,o.title,o.stage,c.name AS company FROM opportunities o JOIN companies c ON c.id=o.company_id WHERE LOWER(o.title||' '||c.name) LIKE ? LIMIT 20",
                (q,))])


    fo.SEARCH_HOOKS.append(plat_search)
    fo.plat_link_job = lambda conn, row: link_job(conn, row)
    fo.plat_upsert_company = upsert_company
    fo.plat_upsert_site = upsert_site
    fo.plat_ensure_contact = ensure_contact
    fo.plat_init_and_migrate = init_and_migrate

    # Patch search_page rendering of `out` tuples. The current search_page only
    # uses `out` if hooks put job-related extras; eq_search uses `out` too.
    # We wrap by appending a renderer hook used below.
    fo.PLAT_SEARCH_RENDER = True
