from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect, g, Response
import sqlite3, os, uuid, hashlib, hmac, time, json
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Render sits behind one proxy

# ---- Users and login (no secrets in code; everything comes from env) ----
# FIELDOPS_USERS="Name:role:password;Name:role:password"  role = office | tech
def parse_users(raw):
    users = {}
    for part in (raw or '').split(';'):
        bits = [b.strip() for b in part.split(':', 2)]
        if len(bits) != 3: continue
        name, role, pw = bits
        role = role.lower()
        if name and pw and role in ('office', 'tech'):
            users[name.lower()] = {'name': name, 'role': role, 'password': pw}
    return users

_USERS_RAW = os.environ.get('FIELDOPS_USERS', '')
USERS = parse_users(_USERS_RAW)
app.secret_key = os.environ.get('FIELDOPS_SECRET_KEY') or (
    hashlib.sha256(('fieldops-session:' + _USERS_RAW).encode()).hexdigest() if USERS else os.urandom(32).hex())
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
DB_PATH = 'fieldops.db'
ACTIVE = ('assigned', 'in progress')

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo('America/Chicago')
except Exception:
    TZ = timezone(timedelta(hours=-6))

timezone_utc = timezone.utc
def utcnow(): return datetime.now(timezone_utc).replace(tzinfo=None).isoformat(timespec='seconds')
def today_local(offset=0): return (datetime.now(TZ) + timedelta(days=offset)).strftime('%Y-%m-%d')
def local_time(iso):
    try: return datetime.fromisoformat(iso).replace(tzinfo=timezone_utc).astimezone(TZ).strftime('%b %d, %Y %I:%M %p CT')
    except Exception: return iso or ''

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def make_checklist(text):
    items = [l.strip().lstrip('-*').strip() for l in (text or '').splitlines()]
    return json.dumps([{'text': t[:200], 'done': False} for t in items if t][:50])

SEED = [
    ('TS-2026-0142', 'Access Control - Main Entrance', 'ABC Warehouse', '1234 Industrial Blvd, Houston, TX',
     'Install 2 Maglocks, 1 Aiphone, 1 Card Reader.', 'Mike', '713-555-0142', 0,
     'Mount and wire 2 maglocks\nInstall Aiphone intercom\nInstall card reader\nPower from existing panel and test\nProgram users per list\nShow customer how it works'),
    ('TS-2026-0143', 'Camera Service Call', 'XYZ Retail', '567 Commerce St, Houston, TX',
     'Troubleshoot offline NVR.', 'Mike', '713-555-0143', 1,
     'Check NVR power and network\nCheck PoE switch ports\nVerify every camera is online\nReplace failed camera if needed\nConfirm recording and playback'),
]

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, title TEXT, customer TEXT, address TEXT, scope TEXT,
        status TEXT DEFAULT 'assigned', tech TEXT, notes TEXT, completed_at TEXT,
        created_at TEXT, total_minutes INTEGER DEFAULT 0, total_miles REAL DEFAULT 0,
        signature TEXT, materials TEXT, phone TEXT, checklist TEXT, scheduled_date TEXT)''')
    for col in ['signature TEXT', 'materials TEXT', 'phone TEXT', 'checklist TEXT', 'scheduled_date TEXT']:
        try: conn.execute(f'ALTER TABLE jobs ADD COLUMN {col}')
        except Exception: pass
    conn.execute('''CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, filename TEXT,
        photo_type TEXT, uploaded_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS invoice_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, title TEXT, customer TEXT, address TEXT, phone TEXT,
        tech TEXT, materials TEXT, hours REAL, miles REAL, notes TEXT, photo_urls TEXT,
        completed_at TEXT, status TEXT DEFAULT 'pending', created_at TEXT, drafted_at TEXT, qbo_ref TEXT)''')
    if conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0:
        now = utcnow()
        for jid, title, cust, addr, scope, tech, phone, day, checks in SEED:
            conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,phone,checklist,scheduled_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         (jid, title, cust, addr, scope, 'assigned', tech, now, phone, make_checklist(checks), today_local(day)))
    else:  # older databases: fill the new fields on the sample jobs
        for jid, title, cust, addr, scope, tech, phone, day, checks in SEED:
            conn.execute('UPDATE jobs SET phone=COALESCE(phone,?), checklist=COALESCE(checklist,?), scheduled_date=COALESCE(scheduled_date,?) WHERE id=?',
                         (phone, make_checklist(checks), today_local(day), jid))
    conn.execute("UPDATE jobs SET scheduled_date=substr(created_at,1,10) WHERE scheduled_date IS NULL OR scheduled_date=''")
    conn.commit(); conn.close()

init_db()

# ---- Access control ----
SETUP_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Setup required</title></head>
<body style="font-family:-apple-system,sans-serif;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:24px;text-align:center">
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<div style="color:#a49f2a;font-weight:600;margin-bottom:24px">Field Ops</div>
<p>Setup required: add FIELDOPS_USERS in Render, Environment</p></body></html>"""

LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>True Sense Field Ops - Sign in</title>
<style>body{font-family:-apple-system,sans-serif;background:#fcfcfc;color:#111;max-width:420px;margin:auto;padding:24px}
input{width:100%;box-sizing:border-box;padding:14px;border:1px solid #e5e5e5;border-radius:10px;margin:8px 0;font-size:1rem}
button{width:100%;background:#a49f2a;color:#fff;border:none;padding:16px;border-radius:10px;font-size:1.05rem;font-weight:600;margin-top:8px}
.err{color:#b00020;margin:8px 0}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<div style="color:#a49f2a;font-weight:600;margin-bottom:24px;text-align:center">Field Ops</div>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post" action="/login">
<input name="name" placeholder="Name" autocomplete="username" autocapitalize="none" required value="{{ name }}">
<input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form></body></html>"""

_failed = {}  # ip -> [timestamps] simple brute-force throttle
def _client_ip():
    return request.remote_addr or ''

def current_user():
    u = USERS.get((session.get('u') or '').lower())
    return u

def is_office():
    u = getattr(g, 'user', None)
    return bool(u and u['role'] == 'office')

def api_token_ok():
    token = os.environ.get('FIELDOPS_API_TOKEN', '')
    auth = request.headers.get('Authorization', '')
    if not token or not auth.startswith('Bearer '): return False
    return hmac.compare_digest(auth[7:].strip().encode(), token.encode())

OPEN_ENDPOINTS = {'healthz', 'login', 'logout'}

@app.before_request
def require_login():
    g.user = None; g.token = False
    if request.endpoint == 'healthz': return
    api = request.path.startswith('/api/') or request.path.startswith('/uploads/')
    if not USERS:
        if api: return jsonify({'error': 'Setup required: add FIELDOPS_USERS in Render, Environment'}), 503
        return SETUP_HTML, 503
    if request.endpoint in OPEN_ENDPOINTS: return
    g.user = current_user()
    if g.user: return
    if request.path.startswith('/api/invoice-queue') and api_token_ok():
        g.token = True; return
    if api: return jsonify({'error': 'login required'}), 401
    return redirect('/login')

@app.route('/healthz')
def healthz(): return jsonify({'ok': True, 'configured': bool(USERS)})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if current_user(): return redirect('/')
        return render_template_string(LOGIN_HTML, error=None, name='')
    ip = _client_ip(); now = time.time()
    recent = [t for t in _failed.get(ip, []) if now - t < 900]
    name = (request.form.get('name') or '').strip()
    if len(recent) >= 10:
        return render_template_string(LOGIN_HTML, error='Too many tries. Wait 15 minutes.', name=name), 429
    pw = request.form.get('password') or ''
    u = USERS.get(name.lower())
    ok = hmac.compare_digest(pw.encode(), (u['password'] if u else 'x' * 24).encode()) and u is not None
    if not ok:
        recent.append(now); _failed[ip] = recent
        return render_template_string(LOGIN_HTML, error='Wrong name or password.', name=name), 401
    _failed.pop(ip, None)
    session.clear(); session.permanent = True; session['u'] = name.lower()
    return redirect('/')

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.clear(); return redirect('/login')


LOGO = 'https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w'
HTML = r'''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>True Sense Field Ops</title>
<style>
:root{--bg:#fcfcfc;--card:#fff;--accent:#a49f2a;--text:#111;--muted:#666;--border:#e5e5e5}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);padding:12px;max-width:480px;margin:auto}
.header{text-align:center;padding:12px 0;border-bottom:1px solid var(--border);margin-bottom:12px}
.header .sub{color:var(--accent);font-weight:600}
.who{font-size:.85rem;color:var(--muted);margin-top:4px}.who a{color:var(--accent)}
.tabs{display:flex;gap:6px;margin-bottom:12px;overflow-x:auto}
.tabs button{flex:1;white-space:nowrap;background:#f0f0f0;color:var(--text);border:none;padding:10px 8px;border-radius:8px;font-weight:600;font-size:.9rem}
.tabs button.on{background:var(--accent);color:#fff}
.job{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:12px}
.job-id{color:var(--accent);font-weight:700;font-size:0.8rem}
.muted{color:var(--muted)}.small{font-size:.85rem}
.badge{display:inline-block;font-size:.75rem;padding:2px 8px;border-radius:99px;background:#f0f0f0;margin-left:6px}
.badge.prog{background:#fff6d6}.badge.done{background:#e3f2ea}
.big-btn{display:block;width:100%;background:var(--accent);color:#fff;border:none;padding:16px;border-radius:10px;font-size:1.05rem;font-weight:600;margin:8px 0;text-align:center;text-decoration:none;cursor:pointer}
.big-btn.gray{background:#f0f0f0;color:var(--text)}
.big-btn.green{background:#2d6a4f}
.big-btn.sm{padding:10px;font-size:.95rem}
.hidden{display:none!important}
textarea,input,select{width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff;font-family:inherit}
label.f{display:block;font-size:.85rem;color:var(--muted);margin-top:6px}
.check{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);font-size:1rem}
.check input{width:24px;height:24px;margin:0;flex:none;accent-color:var(--accent)}
.check.done span{text-decoration:line-through;color:var(--muted)}
.thumbs{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}
.thumbs img{width:70px;height:70px;object-fit:cover;border-radius:8px;border:1px solid var(--border)}
.sigimg{max-width:100%;height:90px;border:1px solid var(--border);border-radius:8px;background:#fafafa}
.week{display:flex;align-items:center;gap:4px;margin-bottom:12px}
.week .nav{flex:none;width:36px;background:#f0f0f0;border:none;border-radius:8px;padding:12px 0;font-size:1.1rem}
.week .days{display:flex;flex:1;gap:4px}
.day{flex:1;background:#fff;border:1px solid var(--border);border-radius:8px;padding:6px 0;text-align:center;font-size:.75rem;color:var(--muted)}
.day b{display:block;font-size:1rem;color:var(--text)}
.day.sel{background:var(--accent);border-color:var(--accent);color:#fff}.day.sel b{color:#fff}
.day.today{border-color:var(--accent)}
.dot{display:block;height:6px;line-height:6px;font-size:.7rem}
pre.inv{white-space:pre-wrap;font-family:inherit;font-size:.9rem;margin:8px 0;background:#fafafa;border:1px solid var(--border);border-radius:8px;padding:10px;word-break:break-word}
canvas#sig{display:block;border:2px dashed var(--border);border-radius:10px;background:#fafafa;width:100%;height:160px;touch-action:none;-ms-touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;-webkit-tap-highlight-color:transparent;overscroll-behavior:contain}
</style></head><body>
<div class="header">
  <img src="__LOGO__" alt="True Sense Security" style="height:48px; margin:0 auto 4px; display:block;">
  <div class="sub">Field Ops</div>
  <div class="who" id="who"></div>
</div>
<div class="tabs" id="tabs">
  <button data-tab="jobs" class="on">Jobs</button>
  <button data-tab="day">By day</button>
  <button data-tab="completed" class="office-only hidden">Completed</button>
  <button data-tab="invoices" class="office-only hidden">Invoices to draft</button>
</div>

<div id="v-jobs" class="view">
  <div id="list"></div>
  <div id="office" class="job office-only hidden"><div style="font-weight:600;margin-bottom:4px">Add New Job (Office)</div>
    <input type="text" id="n-id" placeholder="Job ID (e.g. TS-2026-0144)">
    <input type="text" id="n-title" placeholder="Title">
    <input type="text" id="n-customer" placeholder="Customer">
    <input type="tel" id="n-phone" placeholder="Customer phone">
    <input type="text" id="n-address" placeholder="Address">
    <label class="f">Tech</label><select id="n-tech"></select>
    <label class="f">Scheduled date</label><input type="date" id="n-date">
    <textarea id="n-scope" placeholder="Scope" rows="3"></textarea>
    <textarea id="n-checklist" placeholder="Checklist (one item per line)" rows="4"></textarea>
    <button class="big-btn" id="add-job">Add Job</button>
  </div>
</div>

<div id="v-day" class="view hidden">
  <div class="week"><button class="nav" id="wk-prev">&lsaquo;</button><div class="days" id="days"></div><button class="nav" id="wk-next">&rsaquo;</button></div>
  <div id="day-title" style="font-weight:600;margin-bottom:8px"></div>
  <div id="day-list"></div>
</div>

<div id="v-completed" class="view hidden"><div id="completed-list"></div></div>
<div id="v-invoices" class="view hidden">
  <div class="small muted" style="margin-bottom:8px">Finished jobs waiting for a QuickBooks invoice. Copy the details into a QuickBooks draft, then tap Mark drafted.</div>
  <div id="invoice-list"></div>
</div>

<div id="detail" class="hidden">
  <div class="job"><div class="job-id" id="d-id"></div><div id="d-title" style="font-weight:600;font-size:1.15rem"></div>
    <div id="d-customer" class="muted"></div><div id="d-address" class="muted"></div><div id="d-phone" class="muted"></div>
    <div id="d-meta" class="muted small" style="margin-top:4px"></div>
    <div id="d-scope" style="white-space:pre-wrap;margin-top:8px"></div></div>
  <a id="directions" class="big-btn" target="_blank" rel="noopener">Directions</a>
  <a id="call" class="big-btn gray">Call Customer</a>
  <div id="checklist-box" class="job hidden"><div style="font-weight:600">Checklist</div><div id="checklist"></div></div>
  <div id="done-note" class="job hidden">This job is finished.</div>
  <div id="workform">
    <div class="big-btn" onclick="document.getElementById('before').click()">Before Photos</div>
    <input type="file" id="before" accept="image/*" multiple capture="environment" style="display:none">
    <div id="before-preview" class="thumbs"></div>
    <div class="big-btn" onclick="document.getElementById('after').click()">After Photos</div>
    <input type="file" id="after" accept="image/*" multiple capture="environment" style="display:none">
    <div id="after-preview" class="thumbs"></div>
    <textarea id="notes" placeholder="Notes" rows="3"></textarea>
    <input type="text" id="materials" placeholder="Materials used">
    <div>Customer Signature</div><canvas id="sig"></canvas>
    <button class="big-btn gray sm" onclick="clearSig()">Clear Signature</button>
    <input type="number" id="hours" placeholder="Hours" step="0.25" min="0" inputmode="decimal">
    <input type="number" id="miles" placeholder="Miles" step="0.1" min="0" inputmode="decimal">
    <button class="big-btn green" id="finish">Finish Job</button>
  </div>
  <button class="big-btn gray" onclick="back()">Back</button>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let me=null, currentJob=null, tab='jobs';
async function api(url,opts){const r=await fetch(url,opts);if(r.status===401){location.href='/login';throw new Error('login')}
 const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(data.error||('Error '+r.status));return data}

// ---- Signature pad: touch on the canvas only draws, never scrolls ----
const canvas=$('sig'); const ctx=canvas.getContext('2d');
let drawing=false, sigDrawn=false, last=null, sigW=0;
function sizeSig(force){const r=canvas.getBoundingClientRect();if(!r.width)return;
 if(!force&&Math.round(r.width)===sigW)return; sigW=Math.round(r.width);
 const dpr=window.devicePixelRatio||1;canvas.width=Math.round(r.width*dpr);canvas.height=Math.round(r.height*dpr);
 ctx.setTransform(dpr,0,0,dpr,0,0);ctx.strokeStyle='#111';ctx.fillStyle='#111';ctx.lineWidth=2.5;ctx.lineCap='round';ctx.lineJoin='round';sigDrawn=false}
function pt(e){const r=canvas.getBoundingClientRect();const dpr=window.devicePixelRatio||1;
 return {x:(e.clientX-r.left)*(canvas.width/dpr)/r.width, y:(e.clientY-r.top)*(canvas.height/dpr)/r.height}}
canvas.addEventListener('pointerdown',e=>{e.preventDefault();try{canvas.setPointerCapture(e.pointerId)}catch(_){}
 drawing=true;last=pt(e);ctx.beginPath();ctx.arc(last.x,last.y,1.25,0,Math.PI*2);ctx.fill();sigDrawn=true});
canvas.addEventListener('pointermove',e=>{if(!drawing)return;e.preventDefault();
 const evs=e.getCoalescedEvents?e.getCoalescedEvents():[e];
 for(const ev of (evs.length?evs:[e])){const p=pt(ev);ctx.beginPath();ctx.moveTo(last.x,last.y);ctx.lineTo(p.x,p.y);ctx.stroke();last=p}
 sigDrawn=true});
function endSig(e){if(!drawing)return;drawing=false;try{canvas.releasePointerCapture(e.pointerId)}catch(_){}}
canvas.addEventListener('pointerup',endSig);canvas.addEventListener('pointercancel',endSig);
['touchstart','touchmove','touchend'].forEach(t=>canvas.addEventListener(t,e=>{if(e.cancelable)e.preventDefault()},{passive:false}));
window.addEventListener('resize',()=>sizeSig(false));
function clearSig(){sizeSig(true);ctx.clearRect(0,0,canvas.width,canvas.height);sigDrawn=false}

// ---- Rendering ----
const badge=s=>s==='in progress'?'<span class="badge prog">In progress</span>':s==='completed'?'<span class="badge done">Done</span>':'';
const fmtDay=d=>{const [y,m,dd]=d.split('-').map(Number);return new Date(Date.UTC(y,m-1,dd)).toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric',timeZone:'UTC'})};
function card(j){return `<div class="job"><div class="job-id">${esc(j.id)}${badge(j.status)}</div><div style="font-weight:600">${esc(j.title)}</div>
 <div class="muted">${esc(j.customer)}</div><div class="muted">${esc(j.address)}</div>
 <div class="muted small">${j.scheduled_date?esc(fmtDay(j.scheduled_date)):''}${me&&me.role==='office'?' &middot; '+esc(j.tech):''}</div>
 <button class="big-btn" data-open="${esc(j.id)}">Open Job</button></div>`}
async function loadJobs(){const jobs=await api('/api/jobs?view=active');$('list').innerHTML=jobs.map(card).join('')||'<div class="job">No active jobs</div>'}

// By day
const addDays=(d,n)=>{const [y,m,dd]=d.split('-').map(Number);return new Date(Date.UTC(y,m-1,dd+n)).toISOString().slice(0,10)};
const monday=d=>{const [y,m,dd]=d.split('-').map(Number);const w=new Date(Date.UTC(y,m-1,dd)).getUTCDay();return addDays(d,-((w+6)%7))};
let selDay=null, weekStart=null;
async function loadDay(){const from=weekStart,to=addDays(weekStart,6);
 const jobs=await api(`/api/jobs?view=range&from=${from}&to=${to}`);
 const by={};jobs.forEach(j=>(by[j.scheduled_date]=by[j.scheduled_date]||[]).push(j));
 let h='';for(let i=0;i<7;i++){const d=addDays(weekStart,i);const [y,m,dd]=d.split('-').map(Number);
  const wd=new Date(Date.UTC(y,m-1,dd)).toLocaleDateString('en-US',{weekday:'short',timeZone:'UTC'});const n=(by[d]||[]).length;
  h+=`<button class="day${d===selDay?' sel':''}${d===me.today?' today':''}" data-day="${d}">${wd}<b>${dd}</b><span class="dot">${n?n+' job'+(n>1?'s':''):''}</span></button>`}
 $('days').innerHTML=h;$('day-title').textContent=fmtDay(selDay)+(selDay===me.today?' (today)':'');
 $('day-list').innerHTML=(by[selDay]||[]).map(card).join('')||'<div class="job muted">No jobs this day</div>'}

// Completed (office)
async function loadCompleted(){const jobs=await api('/api/jobs?view=completed');
 $('completed-list').innerHTML=jobs.map(j=>{const ph=t=>j.photos.filter(p=>p.photo_type===t).map(p=>`<a href="/uploads/${encodeURIComponent(p.filename)}" target="_blank"><img src="/uploads/${encodeURIComponent(p.filename)}" alt="${t} photo"></a>`).join('');
  const b=ph('before'),a=ph('after');
  return `<div class="job"><div class="job-id">${esc(j.id)}<span class="badge done">Done</span></div><div style="font-weight:600">${esc(j.title)}</div>
  <div class="muted">${esc(j.customer)} &middot; ${esc(j.tech)}</div><div class="muted small">Finished ${esc(j.completed_local)}</div>
  <div style="margin-top:8px"><b>Hours:</b> ${j.hours} &nbsp; <b>Miles:</b> ${j.total_miles||0}</div>
  <div><b>Materials:</b> ${esc(j.materials||'-')}</div><div style="white-space:pre-wrap"><b>Notes:</b> ${esc(j.notes||'-')}</div>
  ${j.checklist.length?`<div class="small muted">Checklist: ${j.checklist.filter(c=>c.done).length}/${j.checklist.length} done</div>`:''}
  ${b?`<div class="small muted">Before</div><div class="thumbs">${b}</div>`:''}${a?`<div class="small muted">After</div><div class="thumbs">${a}</div>`:''}
  ${j.signature?`<div class="small muted">Signature</div><img class="sigimg" src="${esc(j.signature)}" alt="Customer signature">`:'<div class="small muted">No signature</div>'}
  </div>`}).join('')||'<div class="job">No completed jobs yet</div>'}

// Invoices to draft (office)
let invoices=[];
async function loadInvoices(){invoices=await api('/api/invoice-queue');
 $('invoice-list').innerHTML=invoices.map((q,i)=>`<div class="job"><div class="job-id">${esc(q.job_id)}</div><div style="font-weight:600">${esc(q.customer)}</div>
  <pre class="inv">${esc(q.details)}</pre><button class="big-btn sm" data-copy="${i}">Copy details</button><button class="big-btn gray sm" data-drafted="${q.id}">Mark drafted</button></div>`).join('')||'<div class="job">Nothing waiting</div>'}
async function copyText(t){try{await navigator.clipboard.writeText(t);return true}catch(_){const ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);ta.select();
 const ok=document.execCommand('copy');ta.remove();return ok}}

// Tabs
function show(t){tab=t;document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.tab===t));
 document.querySelectorAll('.view').forEach(v=>v.classList.toggle('hidden',v.id!=='v-'+t));$('detail').classList.add('hidden');$('tabs').classList.remove('hidden');
 ({jobs:loadJobs,day:loadDay,completed:loadCompleted,invoices:loadInvoices})[t]().catch(e=>alert(e.message))}
function back(){show(tab)}

// Job screen
function renderChecklist(){const items=currentJob.checklist||[];$('checklist-box').classList.toggle('hidden',!items.length);
 const ro=currentJob.status==='completed'&&me.role!=='office';
 $('checklist').innerHTML=items.map((c,i)=>`<label class="check${c.done?' done':''}"><input type="checkbox" data-check="${i}"${c.done?' checked':''}${ro?' disabled':''}><span>${esc(c.text)}</span></label>`).join('')}
async function openJob(id){try{currentJob=await api('/api/jobs/'+encodeURIComponent(id))}catch(e){alert(e.message);return}
 const j=currentJob;$('d-id').innerHTML=esc(j.id)+badge(j.status);$('d-title').textContent=j.title;$('d-customer').textContent=j.customer||'';
 $('d-address').textContent=j.address||'';$('d-phone').textContent=j.phone||'';$('d-scope').textContent=j.scope||'';
 $('d-meta').textContent=[j.scheduled_date?fmtDay(j.scheduled_date):'',j.tech?'Tech: '+j.tech:''].filter(Boolean).join(' · ');
 $('directions').href='https://maps.google.com/?q='+encodeURIComponent(j.address||'');$('directions').classList.toggle('hidden',!j.address);
 const tel=(j.phone||'').replace(/[^0-9+]/g,'');$('call').href='tel:'+tel;$('call').classList.toggle('hidden',!tel);
 if(tel)$('call').textContent='Call Customer ('+j.phone+')';
 renderChecklist();const done=j.status==='completed';$('workform').classList.toggle('hidden',done);$('done-note').classList.toggle('hidden',!done);
 ['notes','materials','hours','miles'].forEach(k=>$(k).value='');['before','after'].forEach(k=>{$(k).value='';$(k+'-preview').innerHTML=''});
 document.querySelectorAll('.view').forEach(v=>v.classList.add('hidden'));$('tabs').classList.add('hidden');$('detail').classList.remove('hidden');
 window.scrollTo(0,0);clearSig()}
function preview(inp,el){$(el).innerHTML='';[...inp.files].forEach(f=>{const i=document.createElement('img');i.src=URL.createObjectURL(f);$(el).appendChild(i)})}
$('before').onchange=e=>preview(e.target,'before-preview');$('after').onchange=e=>preview(e.target,'after-preview');
// Shrink phone photos before upload so big batches stay under the size limit
function shrink(file,max=1600){return new Promise(res=>{if(!file.type.startsWith('image/'))return res(file);const img=new Image();const url=URL.createObjectURL(file);
 img.onload=()=>{const s=Math.min(1,max/Math.max(img.width,img.height));const c=document.createElement('canvas');c.width=Math.round(img.width*s);c.height=Math.round(img.height*s);
  c.getContext('2d').drawImage(img,0,0,c.width,c.height);URL.revokeObjectURL(url);c.toBlob(b=>res(b&&b.size<file.size?b:file),'image/jpeg',0.82)};
 img.onerror=()=>{URL.revokeObjectURL(url);res(file)};img.src=url})}
$('finish').onclick=async()=>{const btn=$('finish');if(btn.disabled)return;if(!confirm('Finish this job?'))return;btn.disabled=true;btn.textContent='Saving...';
 try{const fd=new FormData();fd.append('notes',$('notes').value);fd.append('materials',$('materials').value);
  fd.append('hours',$('hours').value||0);fd.append('miles',$('miles').value||0);
  if(sigDrawn)fd.append('signature',canvas.toDataURL('image/png'));
  for(const k of ['before','after'])for(const f of $(k).files)fd.append(k,await shrink(f),(f.name||'photo').replace(/\.\w+$/,'')+'.jpg');
  await api('/api/jobs/'+encodeURIComponent(currentJob.id)+'/complete',{method:'POST',body:fd});alert('Job finished');show(tab)}
 catch(e){alert(e.message)}finally{btn.disabled=false;btn.textContent='Finish Job'}};
async function addJob(){const v=k=>$('n-'+k).value.trim();
 const body={id:v('id'),title:v('title'),customer:v('customer'),phone:v('phone'),address:v('address'),tech:v('tech'),scheduled_date:v('date'),scope:v('scope'),checklist:$('n-checklist').value};
 if(!body.id||!body.title){alert('Job ID and title are required');return}
 try{await api('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  ['id','title','customer','phone','address','scope','checklist'].forEach(k=>$('n-'+k).value='');$('n-date').value=me.today;loadJobs();alert('Job added')}catch(e){alert(e.message)}}
$('add-job').onclick=addJob;

document.addEventListener('click',async e=>{const t=e.target.closest('[data-open],[data-tab],[data-day],[data-copy],[data-drafted],#wk-prev,#wk-next');if(!t)return;
 if(t.dataset.open)openJob(t.dataset.open);
 else if(t.dataset.tab)show(t.dataset.tab);
 else if(t.dataset.day){selDay=t.dataset.day;loadDay()}
 else if(t.id==='wk-prev'||t.id==='wk-next'){const n=t.id==='wk-prev'?-7:7;weekStart=addDays(weekStart,n);selDay=addDays(selDay,n);loadDay()}
 else if(t.dataset.copy!==undefined){const ok=await copyText(invoices[+t.dataset.copy].details);t.textContent=ok?'Copied':'Copy failed';setTimeout(()=>t.textContent='Copy details',1500)}
 else if(t.dataset.drafted){try{await api('/api/invoice-queue/'+t.dataset.drafted+'/mark-drafted',{method:'POST'});loadInvoices()}catch(err){alert(err.message)}}});
document.addEventListener('change',async e=>{const c=e.target.closest('[data-check]');if(!c||!currentJob)return;
 try{const r=await api('/api/jobs/'+encodeURIComponent(currentJob.id)+'/checklist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:+c.dataset.check,done:c.checked})});
  currentJob.checklist=r.checklist;currentJob.status=r.status;$('d-id').innerHTML=esc(currentJob.id)+badge(r.status);renderChecklist()}catch(err){c.checked=!c.checked;alert(err.message)}});

(async()=>{me=await api('/api/me');selDay=me.today;weekStart=monday(me.today);
 $('who').innerHTML='Signed in as '+esc(me.name)+' &middot; <a href="/logout">Log out</a>';
 if(me.role==='office'){document.querySelectorAll('.office-only').forEach(x=>x.classList.remove('hidden'));
  $('n-tech').innerHTML=(me.techs||[]).map(t=>`<option>${esc(t)}</option>`).join('')||'<option value="">No techs in FIELDOPS_USERS</option>';$('n-date').value=me.today}
 show('jobs')})();
</script></body></html>'''.replace('__LOGO__', LOGO)

@app.after_request
def headers(resp):
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    if request.path.startswith('/api/') or request.path == '/': resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.errorhandler(413)
def too_big(e): return jsonify({'error': 'Upload too large (32 MB max). Try fewer photos.'}), 413

def tech_names(): return sorted(u['name'] for u in USERS.values() if u['role'] == 'tech')

def can_see(job):
    return is_office() or (job['tech'] or '').strip().lower() == g.user['name'].lower()

def job_dict(row, conn, full=False):
    d = dict(row)
    try: d['checklist'] = json.loads(d.get('checklist') or '[]')
    except Exception: d['checklist'] = []
    if not full: d.pop('signature', None)
    else:
        d['photos'] = [dict(p) for p in conn.execute('SELECT filename,photo_type FROM photos WHERE job_id=? ORDER BY id', (d['id'],))]
    d['completed_local'] = local_time(d.get('completed_at')) if d.get('completed_at') else ''
    d['hours'] = round((d.get('total_minutes') or 0) / 60, 2)
    return d

def load_job(conn, job_id):
    row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row: return None, (jsonify({'error': 'not found'}), 404)
    if not can_see(row): return None, (jsonify({'error': 'not your job'}), 403)
    return row, None

@app.route('/api/me')
def me():
    d = {'name': g.user['name'], 'role': g.user['role'], 'today': today_local()}
    if is_office(): d['techs'] = tech_names()
    return jsonify(d)

@app.route('/api/jobs')
def list_jobs():
    view = request.args.get('view') or {'completed': 'completed'}.get(request.args.get('status'), 'active')
    where, args = [], []
    if view == 'completed':
        where.append("status='completed'")
    elif view == 'range':
        where.append('scheduled_date BETWEEN ? AND ?'); args += [request.args.get('from', ''), request.args.get('to', '')]
    else:
        where.append("status IN ('assigned','in progress')")
    if not is_office():
        where.append('lower(trim(tech))=?'); args.append(g.user['name'].lower())
    order = 'completed_at DESC' if view == 'completed' else 'scheduled_date, created_at'
    conn = get_db()
    rows = conn.execute(f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 500", args).fetchall()
    out = [job_dict(r, conn, full=(view == 'completed')) for r in rows]
    conn.close()
    return jsonify(out)

@app.route('/api/jobs', methods=['POST'])
def create_job():
    if not is_office(): return jsonify({'error': 'office only'}), 403
    d = request.get_json(silent=True) or request.form
    f = lambda k, n=500: (d.get(k) or '').strip()[:n]
    jid, title, tech = f('id', 40), f('title', 200), f('tech', 60)
    if not jid or not title: return jsonify({'error': 'Job ID and title are required'}), 400
    if tech.lower() not in USERS: return jsonify({'error': 'Pick a tech from the list'}), 400
    tech = USERS[tech.lower()]['name']
    date = f('scheduled_date', 10) or today_local()
    try: datetime.strptime(date, '%Y-%m-%d')
    except ValueError: return jsonify({'error': 'Date must be YYYY-MM-DD'}), 400
    conn = get_db()
    if conn.execute('SELECT 1 FROM jobs WHERE id=?', (jid,)).fetchone():
        conn.close(); return jsonify({'error': 'That Job ID already exists'}), 409
    conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,phone,checklist,scheduled_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (jid, title, f('customer', 200), f('address', 300), f('scope', 4000), 'assigned', tech, utcnow(),
                  f('phone', 40), make_checklist(f('checklist', 4000)), date))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'id': jid}), 201

@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    conn = get_db(); row, err = load_job(conn, job_id)
    if err: conn.close(); return err
    d = job_dict(row, conn, full=True); conn.close()
    return jsonify(d)

@app.route('/api/jobs/<job_id>/checklist', methods=['POST'])
def tick(job_id):
    d = request.get_json(silent=True) or {}
    conn = get_db(); row, err = load_job(conn, job_id)
    if err: conn.close(); return err
    if row['status'] == 'completed' and not is_office():
        conn.close(); return jsonify({'error': 'job already finished'}), 409
    items = json.loads(row['checklist'] or '[]')
    try:
        i = int(d.get('index')); items[i]['done'] = bool(d.get('done'))
    except (TypeError, ValueError, IndexError):
        conn.close(); return jsonify({'error': 'bad checklist item'}), 400
    status = 'in progress' if row['status'] == 'assigned' else row['status']
    conn.execute('UPDATE jobs SET checklist=?, status=? WHERE id=?', (json.dumps(items), status, job_id))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'checklist': items, 'status': status})

@app.route('/api/jobs/<job_id>/complete', methods=['POST'])
def complete(job_id):
    conn = get_db(); row, err = load_job(conn, job_id)
    if err: conn.close(); return err
    if row['status'] == 'completed':
        conn.close(); return jsonify({'error': 'job already finished'}), 409
    try:
        hours = max(0.0, float(request.form.get('hours') or 0)); miles = max(0.0, float(request.form.get('miles') or 0))
    except ValueError:
        conn.close(); return jsonify({'error': 'Hours and miles must be numbers'}), 400
    notes = request.form.get('notes', '')[:5000]; materials = request.form.get('materials', '')[:5000]
    sig = request.form.get('signature', '')
    if not sig.startswith('data:image/png;base64,') or len(sig) > 2_000_000: sig = None
    now = utcnow()
    conn.execute('UPDATE jobs SET status=?,notes=?,materials=?,signature=COALESCE(?,signature),total_minutes=total_minutes+?,total_miles=total_miles+?,completed_at=? WHERE id=?',
                 ('completed', notes, materials, sig, int(round(hours * 60)), miles, now, job_id))
    for ptype in ('before', 'after'):
        for f in request.files.getlist(ptype):
            if f and f.filename:
                fn = secure_filename(f"{job_id}_{ptype}_{uuid.uuid4().hex[:8]}.jpg")
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
                conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at) VALUES (?,?,?,?)', (job_id, fn, ptype, now))
    job = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    urls = [request.host_url.rstrip('/') + '/uploads/' + p['filename']
            for p in conn.execute('SELECT filename FROM photos WHERE job_id=? ORDER BY id', (job_id,))]
    conn.execute('INSERT INTO invoice_queue (job_id,title,customer,address,phone,tech,materials,hours,miles,notes,photo_urls,completed_at,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (job_id, job['title'], job['customer'], job['address'], job['phone'], job['tech'], materials,
                  round((job['total_minutes'] or 0) / 60, 2), job['total_miles'] or 0, notes, json.dumps(urls), now, 'pending', now))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    conn = get_db()
    p = conn.execute('SELECT job_id FROM photos WHERE filename=?', (filename,)).fetchone()
    row = conn.execute('SELECT * FROM jobs WHERE id=?', (p['job_id'],)).fetchone() if p else None
    conn.close()
    if not row: return ('', 404)
    if not can_see(row): return ('', 403)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ---- QuickBooks invoice-draft queue (office session or FIELDOPS_API_TOKEN bearer) ----
def invoice_details(d):
    lines = [f"Customer: {d['customer'] or ''}", f"Job: {d['job_id']} - {d['title'] or ''}",
             f"Address: {d['address'] or ''}"]
    if d.get('phone'): lines.append(f"Phone: {d['phone']}")
    lines += [f"Tech: {d['tech'] or ''}", f"Completed: {local_time(d['completed_at'])}",
              f"Labor hours: {d['hours']:g}", f"Miles: {d['miles']:g}",
              f"Materials: {d['materials'] or '-'}", f"Notes: {d['notes'] or '-'}"]
    if d['photo_urls']: lines.append('Photos:\n' + '\n'.join(d['photo_urls']))
    return '\n'.join(lines)

@app.route('/api/invoice-queue')
def invoice_queue():
    if not (is_office() or g.token): return jsonify({'error': 'office only'}), 403
    status = request.args.get('status', 'pending')
    conn = get_db()
    q = 'SELECT * FROM invoice_queue' + ('' if status == 'all' else ' WHERE status=?') + ' ORDER BY id DESC LIMIT 500'
    rows = conn.execute(q, () if status == 'all' else (status,)).fetchall(); conn.close()
    out = []
    for r in rows:
        d = dict(r); d['photo_urls'] = json.loads(d['photo_urls'] or '[]')
        d['completed_local'] = local_time(d['completed_at']); d['details'] = invoice_details(d)
        out.append(d)
    return jsonify(out)

@app.route('/api/invoice-queue/<int:qid>/mark-drafted', methods=['POST'])
def mark_drafted(qid):
    if not (is_office() or g.token): return jsonify({'error': 'office only'}), 403
    d = request.get_json(silent=True) or {}
    conn = get_db()
    cur = conn.execute("UPDATE invoice_queue SET status='drafted', drafted_at=?, qbo_ref=COALESCE(?,qbo_ref) WHERE id=?",
                       (utcnow(), (str(d.get('qbo_ref') or '')[:100] or None), qid))
    conn.commit(); conn.close()
    if not cur.rowcount: return jsonify({'error': 'not found'}), 404
    return jsonify({'ok': True})

@app.route('/')
def index(): return Response(HTML, mimetype='text/html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
