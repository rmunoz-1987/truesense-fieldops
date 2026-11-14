from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect
import sqlite3, os, uuid, hmac, hashlib, json, threading, urllib.request
from datetime import timedelta
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
# Each user gets a Render env var NAME_PASSWORD (e.g. ROLAND_PASSWORD, MIKE_PASSWORD); the login name is NAME.
USERS = {k[:-len('_PASSWORD')].replace('_', ' ').strip().lower(): v for k, v in os.environ.items()
         if k.endswith('_PASSWORD') and k != 'APP_PASSWORD' and v and k[:-len('_PASSWORD')]}
OFFICE_NAMES = [n.strip().lower() for n in os.environ.get('OFFICE_NAMES', 'Roland').split(',') if n.strip()]
app.secret_key = os.environ.get('SECRET_KEY') or hashlib.sha256(('fieldops:' + json.dumps(sorted(USERS.items()))).encode()).hexdigest()
app.permanent_session_lifetime = timedelta(days=30)

DEFAULT_CHECKLIST = ['Cleaned up', 'Checked in with the client', 'Walked the finished work with the client']
JOB_TYPES = ['Install', 'Service', 'Survey', 'Office Task']
SURVEY_TEXT = ['want_desc', 'contact_name', 'contact_phone', 'current_system', 'power', 'network', 'conduit', 'obstacle_notes', 'timeline', 'budget', 'decision_maker']
SURVEY_OBSTACLES = ['Lift needed', 'Concrete', 'Long runs', 'Access issues']

def clean_survey(raw):
    try: d = json.loads(raw) if isinstance(raw, str) and raw else raw
    except Exception: d = None
    d = d if isinstance(d, dict) else {}
    out = {k: str(d.get(k) or '').strip()[:2000] for k in SURVEY_TEXT}
    ob = d.get('obstacles') if isinstance(d.get('obstacles'), list) else []
    out['obstacles'] = [o for o in SURVEY_OBSTACLES if o in ob]
    return out

def parse_ticks(raw):
    try: items = json.loads(raw) if isinstance(raw, str) and raw else raw
    except Exception: items = []
    return [{'text': str(i.get('text', ''))[:200], 'done': bool(i.get('done'))} for i in (items if isinstance(items, list) else []) if isinstance(i, dict) and str(i.get('text', '')).strip()]


def today_local():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('America/Chicago')).date()
    except Exception:
        return (datetime.utcnow() - timedelta(hours=6)).date()

def now_local_str():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('America/Chicago')).strftime('%Y-%m-%d %I:%M %p CT')
    except Exception:
        return (datetime.utcnow() - timedelta(hours=6)).strftime('%Y-%m-%d %I:%M %p CT')

def parse_checklist(raw):
    try:
        items = json.loads(raw) if raw else None
    except Exception:
        items = None
    if not items:
        items = [{'text': t, 'done': False} for t in DEFAULT_CHECKLIST]
    out = [{'text': str(i.get('text', '')), 'done': bool(i.get('done'))} for i in items if isinstance(i, dict) and i.get('text')]
    have = {i['text'] for i in out}
    return out + [{'text': t, 'done': False} for t in DEFAULT_CHECKLIST if t not in have]

STATUSES = [('assigned', 'Open'), ('in_progress', 'In Progress'), ('waiting_parts', 'Waiting on Parts'), ('completed', 'Complete')]
STATUS_LABEL = dict(STATUSES)
PHOTO_TYPES = ['before', 'during', 'after']
SURVEY_PHOTO_TYPES = ['survey']
BLUEPRINT_EXTS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.heic'}

def parse_materials(raw):
    try: items = json.loads(raw) if raw else []
    except Exception: items = []
    return [{'text': str(i.get('text', ''))[:200], 'used': bool(i.get('used'))} for i in (items if isinstance(items, list) else []) if isinstance(i, dict) and str(i.get('text', '')).strip()]

def job_dict(row):
    d = dict(row); d['checklist'] = parse_checklist(d.get('checklist'))
    d['materials_list'] = parse_materials(d.get('materials_list'))
    d['status_label'] = STATUS_LABEL.get(d.get('status') or 'assigned', (d.get('status') or 'Open').replace('_', ' ').title())
    d['hours'] = round((d.get('total_minutes') or 0) / 60, 2); d['total_miles'] = d.get('total_miles') or 0
    d['job_type'] = d.get('job_type') or ''
    d.pop('survey_list', None); d['survey'] = clean_survey(d.pop('survey_form', None))
    d['survey']['contact_tel'] = ''.join(c for c in d['survey']['contact_phone'] if c.isdigit() or c == '+')
    if not is_office(): d.pop('survey_office', None)
    else: d['survey_office'] = d.get('survey_office') or ''
    d['punch_list'] = parse_ticks(d.get('punch_list')); d['quick_notes'] = d.get('quick_notes') or ''
    d['help_open'] = d.get('help_flag') == 'open'
    if not is_office(): d.pop('private_notes', None)
    return d

def me():
    n = (session.get('name') or '').strip()
    return n if n.lower() in USERS else ''
def is_office(): return me().lower() in OFFICE_NAMES
def can_see(job): return is_office() or (job['tech'] or '').strip().lower() == me().lower()

LOGIN_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Field Ops"><title>True Sense Field</title>
<style>body{font-family:-apple-system,sans-serif;background:#fcfcfc;color:#111;max-width:420px;margin:auto;padding:24px}
input{width:100%;padding:14px;border:1px solid #e5e5e5;border-radius:10px;margin:8px 0;font-size:1rem;box-sizing:border-box}
button{width:100%;background:#a49f2a;color:#fff;border:none;padding:16px;border-radius:10px;font-size:1.05rem;font-weight:600}
.err{color:#b00020}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<div style="text-align:center;color:#a49f2a;font-weight:600;margin-bottom:16px">Field Ops</div>
{% if setup %}<p>Add user passwords in Render, e.g. ROLAND_PASSWORD and MIKE_PASSWORD</p>{% else %}
<form method="post" action="/login"><input name="name" placeholder="Your name" autocomplete="username" required>
<input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
{% if error %}<div class="err">{{ error }}</div>{% endif %}<button type="submit">Log In</button></form>{% endif %}
</body></html>'''

@app.before_request
def require_login():
    if request.path in ('/login', '/healthz') or request.path.startswith('/help-photo/') or request.path=='/api/tech-support/reply': return
    if not USERS:
        return (render_template_string(LOGIN_HTML, setup=True), 503) if not request.path.startswith('/api') else (jsonify({'error':'Add user passwords in Render'}), 503)
    if not me():
        return (jsonify({'error':'login required'}), 401) if request.path.startswith(('/api','/uploads')) else redirect('/login')

@app.route('/healthz')
def healthz(): return 'ok'

@app.route('/login', methods=['GET','POST'])
def login():
    if not USERS: return render_template_string(LOGIN_HTML, setup=True), 503
    error = None
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        pw = request.form.get('password') or ''
        expected = USERS.get(name.lower())
        if expected and hmac.compare_digest(pw.encode(), expected.encode()):
            session.permanent = True; session['name'] = name.title()
            return redirect('/')
        error = 'Wrong name or password'
    return render_template_string(LOGIN_HTML, setup=False, error=error)

@app.route('/logout')
def logout():
    session.clear(); return redirect('/login')
DB_PATH = 'fieldops.db'

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, title TEXT, customer TEXT, address TEXT, scope TEXT,
        status TEXT DEFAULT 'assigned', tech TEXT, notes TEXT, completed_at TEXT,
        created_at TEXT, total_minutes INTEGER DEFAULT 0, total_miles REAL DEFAULT 0,
        signature TEXT, materials TEXT, phone TEXT)''')
    for col in ['signature TEXT', 'materials TEXT', 'phone TEXT', 'checklist TEXT', 'scheduled_date TEXT', 'created_by TEXT', 'materials_list TEXT', 'private_notes TEXT', 'job_type TEXT', 'survey_list TEXT', 'quick_notes TEXT', 'punch_list TEXT', 'survey_form TEXT', 'survey_office TEXT', 'help_flag TEXT', 'help_note TEXT', 'help_at TEXT', 'help_by TEXT', 'help_photo TEXT', 'kelly_at TEXT', 'kelly_by TEXT']:
        try: conn.execute(f'ALTER TABLE jobs ADD COLUMN {col}')
        except: pass
    conn.execute('''CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, filename TEXT,
        photo_type TEXT, uploaded_at TEXT)''')
    try: conn.execute('ALTER TABLE photos ADD COLUMN orig_name TEXT')
    except: pass
    conn.execute('''CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, address TEXT, phone TEXT, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS support_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, note TEXT, created_at TEXT)''')
    for col in ["author TEXT DEFAULT 'Tech Support'", "kind TEXT"]:
        try: conn.execute(f"ALTER TABLE support_replies ADD COLUMN {col}")
        except: pass
    conn.execute('''CREATE TABLE IF NOT EXISTS job_updates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, author TEXT, note TEXT, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS invoice_drafts (
        job_id TEXT PRIMARY KEY, package TEXT, status TEXT DEFAULT 'ready_for_review', created_at TEXT)''')
    count = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    if count == 0:
        now = datetime.utcnow().isoformat()
        conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at) VALUES (?,?,?,?,?,?,?,?)',
                     ('TS-2026-0142','Access Control - Main Entrance','ABC Warehouse','1234 Industrial Blvd, Houston, TX','Install 2 Maglocks, 1 Aiphone, 1 Card Reader.','assigned','Mike',now))
        conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at) VALUES (?,?,?,?,?,?,?,?)',
                     ('TS-2026-0143','Camera Service Call','XYZ Retail','567 Commerce St, Houston, TX','Troubleshoot offline NVR.','assigned','Mike',now))
        conn.execute("UPDATE jobs SET phone='713-555-0142' WHERE id='TS-2026-0142'")
        conn.execute("UPDATE jobs SET phone='713-555-0143' WHERE id='TS-2026-0143'")
        t = today_local()
        c1 = ['Confirm door hardware and power', 'Mount maglocks', 'Mount Aiphone and card reader', 'Program users per list', 'Test every door', 'Customer walkthrough']
        c2 = ['Check NVR power and status', 'Check PoE switch and ports', 'Replace failed camera if needed', 'Verify all cameras recording', 'Customer walkthrough']
        conn.execute('UPDATE jobs SET checklist=?, scheduled_date=? WHERE id=?', (json.dumps([{'text': x, 'done': False} for x in c1]), t.isoformat(), 'TS-2026-0142'))
        conn.execute('UPDATE jobs SET checklist=?, scheduled_date=? WHERE id=?', (json.dumps([{'text': x, 'done': False} for x in c2]), (t + timedelta(days=1)).isoformat(), 'TS-2026-0143'))
    for x in conn.execute("SELECT id,job_id,note,created_at FROM job_updates WHERE author='Tech Support'").fetchall():
        conn.execute('INSERT INTO support_replies (job_id,note,created_at) VALUES (?,?,?)',(x['job_id'],x['note'],x['created_at']))
        conn.execute('DELETE FROM job_updates WHERE id=?',(x['id'],))
    conn.commit(); conn.close()

init_db()

HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Field Ops"><title>True Sense Field</title>
<style>
:root{--bg:#fcfcfc;--card:#fff;--accent:#a49f2a;--text:#111;--muted:#666;--border:#e5e5e5}
body{margin:0;font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);padding:12px;max-width:480px;margin:auto}
.header{text-align:center;padding:12px 0;border-bottom:1px solid var(--border);margin-bottom:16px}
.header h1{margin:0;font-size:1.5rem}
.header .sub{color:var(--accent);font-weight:600}
.job{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:12px}
.job-id{color:var(--accent);font-weight:700;font-size:0.8rem}
.big-btn{display:block;width:100%;background:var(--accent);color:#fff;border:none;padding:16px;border-radius:10px;font-size:1.05rem;font-weight:600;margin:8px 0;text-align:center;text-decoration:none}
.big-btn.gray{background:#f0f0f0;color:var(--text)}
.big-btn.green{background:#2d6a4f}
.hidden{display:none}
.chk{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border);font-size:1rem}
.chk input{width:24px;height:24px;margin:0;accent-color:var(--accent)}
.day{font-weight:700;color:var(--accent);margin:16px 0 6px}
.badge{display:inline-block;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;background:#f0f0f0;color:#333;margin-left:6px}
.row{display:flex;gap:8px}.row>*{flex:1}.muted{color:#666}.ptitle{font-weight:600;font-size:.9rem;margin-top:8px}
.thumbs{display:flex;gap:8px;flex-wrap:wrap}.thumbs img{width:70px;height:70px;object-fit:cover;border-radius:8px}
.mat{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)}.mat input{width:24px;height:24px;margin:0;accent-color:var(--accent)}.mat span{flex:1}.mat button{background:none;border:none;color:#b00020;font-size:1.2rem}
textarea,input{width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem}
canvas{touch-action:none;-webkit-user-select:none;user-select:none;border:2px dashed var(--border);border-radius:10px;background:#fafafa;width:100%;height:160px}
</style></head><body>
<div class="header">
  <img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px; margin:0 auto 4px; display:block;">
  <div class="sub">Field Ops</div>
  <div style="font-size:.85rem;color:#666;margin-top:4px">{{ name }} &middot; <a href="/logout" style="color:#666" onclick="return confirm('Log out?')">Log out</a></div>
</div>
{% if office %}<div class="row"><a href="/clients" class="big-btn gray">Clients</a><a href="/calendar" class="big-btn gray">Calendar</a><a href="/to-order" class="big-btn gray">To Order</a></div>{% endif %}
<div id="search-box" class="row" style="align-items:center"><input type="search" id="q" placeholder="Search client, address, or job ID" onkeydown="if(event.key==='Enter')load()"><button class="big-btn gray" style="flex:0 0 90px" onclick="load()">Search</button></div>
<div id="list"></div>
{% if office %}<div id="office" class="job"><div style="font-weight:600;margin-bottom:4px">Add New Job (Office)</div>
<input type="text" id="n-id" placeholder="Job ID (e.g. TS-2026-0144)">
<input type="text" id="n-title" placeholder="Title">
<input type="text" id="n-customer" placeholder="Customer">
<input type="text" id="n-address" placeholder="Address">
<input type="tel" id="n-phone" placeholder="Customer phone">
<select id="n-tech" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff">{% for v,l in techs %}<option value="{{ v }}">{{ l }}</option>{% else %}<option value="">No techs set in Render</option>{% endfor %}</select>
<select id="n-type" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff">{% for t in job_types %}<option value="{{ t }}">{{ t }}</option>{% endfor %}</select>
<textarea id="n-scope" placeholder="Scope" rows="3"></textarea>
<div style="font-size:.85rem;color:#666">Scheduled date</div><input type="date" id="n-date">
<textarea id="n-checklist" placeholder="Checklist, one item per line (optional)" rows="3"></textarea>
<button class="big-btn" onclick="addJob()">Add Job</button></div>{% else %}<div id="office" class="hidden"></div>{% endif %}
<div id="detail" class="hidden">
<button class="big-btn gray" style="font-size:1.3rem;margin-bottom:8px" onclick="showList()">&larr; Back</button>
<div class="job"><div class="job-id" id="d-id"></div><div id="d-title" style="font-weight:600;font-size:1.15rem"></div><div id="d-customer" style="color:#666"></div><div id="d-address" style="color:#666"></div><div id="d-scope" style="white-space:pre-wrap;margin-top:8px"></div><div id="d-created" style="color:#666;font-size:.85rem;margin-top:6px"></div></div>
{% if office %}<div id="help-banner" class="job hidden" style="border:2px solid #b00020;background:#fff5f5"><div style="color:#b00020;font-weight:700;font-size:1.1rem">&#9888; Needs Help</div>
<div id="help-meta" class="muted" style="font-size:.85rem"></div><div id="help-text" style="white-space:pre-wrap;margin:6px 0"></div><div id="help-img"></div>
</div>{% endif %}
<button class="big-btn" style="background:#b00020;font-size:1.5rem;padding:22px" onclick="document.getElementById('help-form').classList.toggle('hidden');document.getElementById('help-note').focus()">Need Help</button>
<div style="text-align:center;font-size:1.1rem;font-weight:600;margin:4px 0 10px">Pull down to see the answer.</div>
<div id="help-form" class="job hidden"><textarea id="help-note" rows="4" style="font-size:1.25rem" autocapitalize="sentences" autocorrect="on" spellcheck="true" enterkeyhint="send" placeholder="Say or type what you need help with"></textarea>
<button class="big-btn" style="background:#b00020;font-size:1.3rem" onclick="sendHelp()">Send</button></div>
<div class="job" style="border:2px solid #1565c0;overflow-wrap:anywhere;word-break:break-word;min-width:0;max-width:100%;box-sizing:border-box"><div style="font-weight:800;font-size:1.5rem;color:#1565c0">Tech Support</div>
<div id="support-open"><textarea id="support-note" rows="3" style="font-size:1.25rem" autocapitalize="sentences" autocorrect="on" spellcheck="true" enterkeyhint="send" placeholder="Say or type a follow-up"></textarea>
<button class="big-btn" style="background:#1565c0;font-size:1.3rem" onclick="sendSupport()">Send Follow-up</button>
<button class="big-btn" style="background:#2e7d32;font-size:1.3rem" onclick="resolveSupport()">Issue Resolved</button></div><div id="support"></div></div>
<a id="directions" class="big-btn" target="_blank">Directions</a>
<a id="call" class="big-btn gray">Call Customer</a>
<a id="history" class="big-btn gray">Client History</a>
<a id="print" class="big-btn gray" target="_blank">Print Summary</a>
<div class="job"><div style="font-weight:600">Job Type</div>{% if office %}<select id="jtype" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff" onchange="setType(this.value)"><option value="">(not set)</option>{% for t in job_types %}<option value="{{ t }}">{{ t }}</option>{% endfor %}</select>{% else %}<div id="jtype-label" style="margin:6px 0"></div>{% endif %}</div>
<div class="job hidden" id="survey-card"><div style="font-weight:800;font-size:1.4rem">Site Survey for Estimate</div>
<div class="ptitle" style="font-size:1.2rem">1. Photos</div><div class="big-btn" id="sv-add" onclick="document.getElementById('survey').click()">Add Photos</div>
<input type="file" id="survey" accept="image/*" multiple capture="environment" style="display:none"><div id="survey-photos" class="thumbs"></div>
<div class="ptitle" style="font-size:1.2rem">2. What They Want</div><textarea id="sv-want_desc" rows="3" style="font-size:1.1rem" placeholder="Short description of what the client wants (tap the mic to talk)"></textarea>
<div class="ptitle" style="font-size:1.2rem">3. Existing Conditions</div><input type="text" id="sv-current_system" placeholder="Current system"><input type="text" id="sv-power" placeholder="Power"><input type="text" id="sv-network" placeholder="Network"><input type="text" id="sv-conduit" placeholder="Conduit / pathways">
<div class="ptitle" style="font-size:1.2rem">4. Obstacles</div>{% for o in obstacles %}<label class="chk"><input type="checkbox" class="sv-ob" value="{{ o }}"><span>{{ o }}</span></label>{% endfor %}
<textarea id="sv-obstacle_notes" rows="3" placeholder="Other obstacles / notes"></textarea>
<div class="ptitle">Site Contact</div><input type="text" id="sv-contact_name" placeholder="Contact name"><input type="tel" id="sv-contact_phone" placeholder="Contact phone"><a id="sv-call" class="big-btn gray" style="display:none">Call Contact</a>
<div class="ptitle">Client Expectations</div><input type="text" id="sv-timeline" placeholder="Timeline"><input type="text" id="sv-budget" placeholder="Budget range (if given)"><input type="text" id="sv-decision_maker" placeholder="Decision maker">
{% if office %}<div class="ptitle">Rough Materials &amp; Labor (office only)</div><textarea id="sv-office" rows="3" placeholder="Rough materials and labor notes"></textarea>{% endif %}
<button class="big-btn hidden" id="sv-save" onclick="saveSurvey()">Save Changes</button>
{% if office %}<button id="sv-edit" class="big-btn hidden" style="background:#6a1b9a;font-size:1.3rem" onclick="svEditing=true;renderKelly()">Edit Survey</button>{% endif %}
{% if office %}<div id="kelly-box"><div id="kelly-note" style="font-weight:700;font-size:1.1rem;color:#6a1b9a;text-align:center"></div></div>{% endif %}</div>
<div class="job ns"><div style="font-weight:600">Status</div><select id="status" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff" onchange="setStatus(this.value)">{% for v,l in statuses %}<option value="{{ v }}">{{ l }}</option>{% endfor %}</select></div>
<div class="job hidden" id="bp-card"><div style="font-weight:600;margin-bottom:6px">Blueprints</div><div id="bp-list"></div>
<div class="big-btn gray" onclick="document.getElementById('bp-file').click()">Upload Blueprint (PDF or image)</div>
<input type="file" id="bp-file" accept=".pdf,.png,.jpg,.jpeg,.webp,.heic,application/pdf,image/*" multiple style="display:none"></div>
<div class="job ns"><div style="font-weight:600;margin-bottom:6px">Photos</div>
<div class="row"><div class="big-btn" onclick="document.getElementById('before').click()">Before</div><div class="big-btn" onclick="document.getElementById('during').click()">During</div><div class="big-btn" onclick="document.getElementById('after').click()">After</div></div>
<input type="file" id="before" accept="image/*" multiple capture="environment" style="display:none">
<input type="file" id="during" accept="image/*" multiple capture="environment" style="display:none">
<input type="file" id="after" accept="image/*" multiple capture="environment" style="display:none">
<div id="photos"></div></div>
<div class="job ns"><div style="font-weight:600;margin-bottom:6px">Materials</div><div id="matlist"></div>
<div class="row" style="align-items:center"><input type="text" id="mat-new" placeholder="Add material (e.g. 2x Maglock)" onkeydown="if(event.key==='Enter')addMat()"><button class="big-btn" style="flex:0 0 70px" onclick="addMat()">Add</button></div></div>
<div class="job ns"><div style="font-weight:600">Time &amp; Miles</div><div id="totals" style="font-size:1.1rem;margin:6px 0"></div>
<div class="row"><input type="number" id="hours" placeholder="Hours" step="0.25" inputmode="decimal"><input type="number" id="miles" placeholder="Miles" step="0.1" inputmode="decimal"></div>
<div class="row"><button class="big-btn" onclick="saveTime('add')">Add</button><button class="big-btn gray" onclick="saveTime('set')">Set Totals</button></div></div>
{% if office %}<div class="job"><div style="font-weight:600">Private Office Notes</div><div class="muted" style="font-size:.85rem">Only office can see these</div>
<textarea id="private-notes" rows="3" placeholder="Private notes"></textarea><button class="big-btn gray" onclick="savePrivate()">Save Private Notes</button></div>{% endif %}
<div class="job ns"><div style="font-weight:600;margin-bottom:6px">Checklist</div><div id="checklist"></div></div>
<div class="job ns"><div style="font-weight:600;margin-bottom:6px">Internal Punch List</div><div class="muted" style="font-size:.85rem">Office and assigned tech only. Not on client printouts.</div><div id="punch"></div>
<div class="row" style="align-items:center"><input type="text" id="punch-new" placeholder="Add punch item" onkeydown="if(event.key==='Enter')addPunch()"><button class="big-btn" style="flex:0 0 70px" onclick="addPunch()">Add</button></div></div>
<div class="job"><div style="font-weight:600">Quick Notes</div><textarea id="quick-notes" rows="3" placeholder="Quick notes (tap the mic on your keyboard to talk)"></textarea><button class="big-btn gray" onclick="saveQuick()">Save Quick Notes</button></div>
<div class="job ns"><div style="font-weight:600;margin-bottom:6px">Daily Updates</div><div id="updates"></div>
<textarea id="update-note" placeholder="Today's progress, issues, materials needed" rows="3"></textarea>
<button class="big-btn" onclick="saveUpdate()">Save Daily Update</button></div>
<textarea id="notes" placeholder="Notes" rows="3"></textarea>
<div class="ns"><input type="text" id="materials" placeholder="Materials used">
<div>Customer Signature</div><canvas id="sig"></canvas>
<button class="big-btn gray" onclick="clearSig()" style="padding:12px">Clear Signature</button></div>
<button class="big-btn green" id="finish">Finish Job</button>
{% if office %}<select id="r-tech" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff">{% for v,l in techs %}<option value="{{ v }}">{{ l }}</option>{% endfor %}</select>
<div class="row"><button class="big-btn gray" onclick="reassignJob()">Reassign Tech</button><button class="big-btn gray" onclick="assignMe()">Assign to Me</button></div>
<button class="big-btn gray" onclick="copyJob()">Copy Job</button>
<button class="big-btn" style="background:#b00020" onclick="deleteJob()">Delete Job</button>{% endif %}
<button class="big-btn gray" onclick="showList()">Back</button>
</div>
<script>
let currentJob=null; const canvas=document.getElementById('sig'); const ctx=canvas.getContext('2d'); let drawing=false;
function resize(){const r=canvas.getBoundingClientRect();canvas.width=r.width;canvas.height=160;ctx.strokeStyle='#111';ctx.lineWidth=2;ctx.lineCap='round'}
resize(); window.addEventListener('resize',resize);
function pos(e){const r=canvas.getBoundingClientRect();return{x:(e.clientX-r.left)*(canvas.width/r.width),y:(e.clientY-r.top)*(canvas.height/r.height)}}
canvas.addEventListener('pointerdown',e=>{e.preventDefault();drawing=true;try{canvas.setPointerCapture(e.pointerId)}catch(_){}const p=pos(e);ctx.beginPath();ctx.moveTo(p.x,p.y)});
canvas.addEventListener('pointermove',e=>{if(!drawing)return;e.preventDefault();const p=pos(e);ctx.lineTo(p.x,p.y);ctx.stroke()});
canvas.style.touchAction='none';
['pointerup','pointercancel','pointerleave'].forEach(t=>canvas.addEventListener(t,e=>{e.preventDefault();drawing=false}));
['touchstart','touchmove'].forEach(t=>canvas.addEventListener(t,e=>e.preventDefault(),{passive:false}));
function clearSig(){ctx.clearRect(0,0,canvas.width,canvas.height)}
function esc(v){const d=document.createElement('div');d.textContent=v==null?'':String(v);return d.innerHTML}
async function load(){const q=(document.getElementById('q').value||'').trim();const r=await fetch(q?'/api/jobs?q='+encodeURIComponent(q):'/api/jobs?status=assigned');const jobs=await r.json();
const card=j=>`<div class="job"><div class="job-id">${esc(j.id)}${j.help_open?'<span class="badge" style="background:#b00020;color:#fff;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px">Needs Help</span>':''}<span class="badge">${esc(j.status_label)}</span>${j.job_type?'<span class="badge">'+esc(j.job_type)+'</span>':''}</div><div style="font-weight:600">${esc(j.title)}</div><div style="color:#666">${esc(j.customer)}</div><div style="color:#666">${esc(j.address)}</div><button class="big-btn" data-id="${esc(j.id)}" onclick="openJob(this.dataset.id)">Open Job</button></div>`;
const groups={};jobs.forEach(j=>{const k=j.scheduled_date||'';(groups[k]=groups[k]||[]).push(j)});
const keys=Object.keys(groups).sort((a,b)=>a===''?1:b===''?-1:a.localeCompare(b));
document.getElementById('list').innerHTML=(keys.map(k=>`<div class="day">${dayLabel(k)}</div>`+groups[k].map(card).join('')).join('')||'<div class="job">'+(q?'No matching jobs':'No jobs')+'</div>')}
function ymd(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function dayLabel(k){if(!k)return 'No date';const t=new Date();const tm=new Date();tm.setDate(t.getDate()+1);
const d=new Date(k+'T12:00:00');const nice=d.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'});
if(k===ymd(t))return 'Today, '+nice;if(k===ymd(tm))return 'Tomorrow, '+nice;if(k<ymd(t))return 'Overdue, '+nice;return nice}
function renderChecklist(){document.getElementById('checklist').innerHTML=(currentJob.checklist||[]).map((c,i)=>`<label class="chk"><input type="checkbox" ${c.done?'checked':''} onchange="tick(${i},this.checked)"><span></span></label>`).join('');
[...document.querySelectorAll('#checklist .chk span')].forEach((el,i)=>el.textContent=currentJob.checklist[i].text)}
async function tick(i,v){currentJob.checklist[i].done=v;await fetch('/api/jobs/'+currentJob.id+'/checklist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({checklist:currentJob.checklist})})}
async function sendSupport(){const n=document.getElementById('support-note');if(!n.value.trim())return;const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/support',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:n.value})});if(r.ok){n.value='';loadSupport();alert('Sent. Pull down to see the answer.')}else alert('Error')}
async function resolveSupport(){if(!confirm('Mark this issue resolved?'))return;const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/support/resolve',{method:'POST'});if(r.ok){currentJob.help_open=false;renderHelp();loadSupport()}else alert('Error')}
async function loadSupport(){const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/support');const u=r.ok?await r.json():[];const el=document.getElementById('support');el.innerHTML='';
const entry=x=>{const d=document.createElement('div');d.style.cssText='padding:10px 0;border-bottom:1px solid #e5e5e5';const m=document.createElement('div');m.style.cssText='font-size:1rem;font-weight:700;color:'+(x.author==='Tech Support'?'#1565c0':'#333');m.textContent=x.author+' \u00b7 '+x.created_at;const t=document.createElement('div');t.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font-size:1.25rem;line-height:1.5';t.textContent=x.note;d.append(m,t);return d};
const threads=[];let cur=[];u.slice().reverse().forEach(x=>{if(x.author==='Resolved'){threads.push({items:cur,done:x});cur=[]}else{if(x.kind==='help'&&cur.length){threads.push({items:cur,done:null});cur=[]}cur.push(x)}});
document.getElementById('support-open').classList.toggle('hidden',!cur.length);cur.slice().reverse().forEach(x=>el.appendChild(entry(x)));
if(!cur.length)el.innerHTML='<div style="color:#666;font-size:1.1rem;padding:6px 0">No open help request</div>';
threads.reverse().forEach(th=>{const det=document.createElement('details');det.style.cssText='margin-top:6px;background:#e8f5e9;border-radius:8px;padding:6px 8px';const sm=document.createElement('summary');sm.style.cssText='font-size:.9rem;color:#2e7d32;cursor:pointer';
const first=th.items.find(x=>x.author!=='Tech Support');let desc=(first?first.note:'').replace(/\s+/g,' ');if(desc.length>40)desc=desc.slice(0,40)+'\u2026';sm.textContent=(th.done?'Resolved':'Earlier')+' \u00b7 '+(desc||'help request')+' \u00b7 '+(th.done?th.done.created_at:th.items[th.items.length-1].created_at);if(!th.done){det.style.background='#f0f0f0';sm.style.color='#555'}det.appendChild(sm);
th.items.slice().reverse().forEach(x=>det.appendChild(entry(x)));el.appendChild(det)})}
async function loadUpdates(){const r=await fetch('/api/jobs/'+currentJob.id+'/updates');const u=r.ok?await r.json():[];const el=document.getElementById('updates');el.innerHTML='';
u.forEach(x=>{const d=document.createElement('div');d.style.cssText='padding:8px 0;border-bottom:1px solid #e5e5e5';const m=document.createElement('div');m.style.cssText='font-size:.8rem;color:#666';m.textContent=x.created_at+' \u00b7 '+x.author;const t=document.createElement('div');t.style.whiteSpace='pre-wrap';t.textContent=x.note;d.append(m,t);el.appendChild(d)});
if(!u.length)el.innerHTML='<div style="color:#666">No updates yet</div>'}
async function saveUpdate(){const n=document.getElementById('update-note');if(!n.value.trim())return;
const r=await fetch('/api/jobs/'+currentJob.id+'/updates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:n.value})});
if(r.ok){n.value='';loadUpdates()}else alert('Error')}
async function reassignJob(){const t=document.getElementById('r-tech').value;if(!t||!t.trim())return;
const r=await fetch('/api/jobs/'+currentJob.id+'/reassign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tech:t.trim()})});
if(r.ok){currentJob.tech=t.trim();alert('Reassigned to '+t.trim())}else alert('Error')}
async function deleteJob(){if(!confirm('Delete job '+currentJob.id+'? This cannot be undone.'))return;
const r=await fetch('/api/jobs/'+currentJob.id,{method:'DELETE'});if(r.ok){showList()}else alert('Error')}
const PHOTO_LABELS={before:'Before',during:'During',after:'After',doors:'Doors',power:'Power',network:'Network',proposed:'Proposed locations'};
function renderPhotos(){photoGroups('photos',['before','during','after']);const el=document.getElementById('survey-photos');el.innerHTML='';
(currentJob.photos||[]).forEach(p=>{const a=document.createElement('a');a.href='/uploads/'+encodeURIComponent(p.filename);a.target='_blank';const i=document.createElement('img');i.src=a.href;a.appendChild(i);el.appendChild(a)});
if(!(currentJob.photos||[]).length)el.innerHTML='<div class="muted">No photos yet</div>'}
function renderHelp(){const b=document.getElementById('help-banner');if(!b)return;b.classList.toggle('hidden',!currentJob.help_open);if(!currentJob.help_open)return;
document.getElementById('help-meta').textContent=(currentJob.help_by||'')+' \u00b7 '+(currentJob.help_at||'');document.getElementById('help-text').textContent=currentJob.help_note||'';
const im=document.getElementById('help-img');im.innerHTML='';if(currentJob.help_photo){const a=document.createElement('a');a.href='/uploads/'+encodeURIComponent(currentJob.help_photo);a.target='_blank';const i=document.createElement('img');i.src=a.href;i.style.cssText='width:120px;height:120px;object-fit:cover;border-radius:8px';a.appendChild(i);im.appendChild(a)}}
async function sendHelp(){const fd=new FormData();fd.append('note',document.getElementById('help-note').value);
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/help',{method:'POST',body:fd});if(r.ok){Object.assign(currentJob,await r.json());document.getElementById('help-note').value='';document.getElementById('help-form').classList.add('hidden');renderHelp();alert('Sent. Pull down to see the answer.')}else alert('Error')}
function renderBlueprints(){const el=document.getElementById('bp-list');el.innerHTML='';const b=currentJob.blueprints||[];
b.forEach(x=>{const a=document.createElement('a');a.href='/uploads/'+encodeURIComponent(x.filename);a.target='_blank';a.style.cssText='display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #e5e5e5;color:#111';
if(x.image){const i=document.createElement('img');i.src=a.href;i.style.cssText='width:56px;height:56px;object-fit:cover;border-radius:6px';a.appendChild(i)}else{const ic=document.createElement('span');ic.textContent='📄';ic.style.fontSize='1.6rem';a.appendChild(ic)}
const t=document.createElement('span');t.textContent=x.name;a.appendChild(t);el.appendChild(a)});if(!b.length)el.innerHTML='<div class="muted">No blueprints yet</div>'}
document.getElementById('bp-file').onchange=async e=>{const inp=e.target;if(!inp.files.length)return;const fd=new FormData();[...inp.files].forEach(f=>fd.append('files',f));
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/blueprints',{method:'POST',body:fd});inp.value='';const d=await r.json().catch(()=>({}));
if(r.ok){currentJob.blueprints=d.blueprints;renderBlueprints()}else alert(d.error||'Upload failed')};
function photoGroups(elId,types){const el=document.getElementById(elId);el.innerHTML='';const ph=currentJob.photos||[];
types.forEach(t=>{const g=ph.filter(p=>p.photo_type===t);const h=document.createElement('div');h.className='ptitle';h.textContent=PHOTO_LABELS[t]+' ('+g.length+')';el.appendChild(h);
const w=document.createElement('div');w.className='thumbs';g.forEach(p=>{const a=document.createElement('a');a.href='/uploads/'+encodeURIComponent(p.filename);a.target='_blank';const i=document.createElement('img');i.src=a.href;a.appendChild(i);w.appendChild(a)});el.appendChild(w)})}
async function uploadPhotos(t){const inp=document.getElementById(t);if(!inp.files.length)return;const fd=new FormData();fd.append('type',t);[...inp.files].forEach(f=>fd.append('photos',f));
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/photos',{method:'POST',body:fd});inp.value='';if(r.ok){currentJob.photos=(await r.json()).photos;renderPhotos()}else alert('Photo upload failed')}
['before','during','after','survey'].forEach(t=>document.getElementById(t).onchange=()=>uploadPhotos(t));
function renderMats(){const el=document.getElementById('matlist');el.innerHTML='';const m=currentJob.materials_list||[];
m.forEach((x,i)=>{const l=document.createElement('label');l.className='mat';const c=document.createElement('input');c.type='checkbox';c.checked=x.used;c.onchange=()=>{x.used=c.checked;saveMats()};
const s=document.createElement('span');s.textContent=x.text;const b=document.createElement('button');b.type='button';b.textContent='\u00d7';b.onclick=e=>{e.preventDefault();m.splice(i,1);saveMats();renderMats()};l.append(c,s,b);el.appendChild(l)});
if(!m.length)el.innerHTML='<div class="muted">No materials yet. Check items off as you use them.</div>'}
async function saveMats(){await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/materials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({materials:currentJob.materials_list})})}
function addMat(){const n=document.getElementById('mat-new');const t=n.value.trim();if(!t)return;(currentJob.materials_list=currentJob.materials_list||[]).push({text:t,used:false});n.value='';renderMats();saveMats()}
function renderTotals(){document.getElementById('totals').textContent='Total: '+(currentJob.hours||0)+' hrs \u00b7 '+(currentJob.total_miles||0)+' miles'}
async function saveTime(mode){const h=document.getElementById('hours'),m=document.getElementById('miles');if(!h.value&&!m.value)return;
const body={mode:mode,hours:h.value||(mode==='set'?currentJob.hours:0),miles:m.value||(mode==='set'?currentJob.total_miles:0)};
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/time',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(r.ok){const d=await r.json();currentJob.hours=d.hours;currentJob.total_miles=d.total_miles;h.value='';m.value='';renderTotals()}else alert('Error')}
async function setStatus(v){const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:v})});
if(r.ok){currentJob.status=v;renderKelly()}else{alert('Error');document.getElementById('status').value=currentJob.status}}
async function savePrivate(){const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/private-notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({private_notes:document.getElementById('private-notes').value})});alert(r.ok?'Saved':'Error')}
function renderTicks(elId,items,save,removable){const el=document.getElementById(elId);el.innerHTML='';
items.forEach((x,i)=>{const l=document.createElement('label');l.className='mat';const c=document.createElement('input');c.type='checkbox';c.checked=x.done;c.onchange=()=>{x.done=c.checked;save()};
const s=document.createElement('span');s.textContent=x.text;l.append(c,s);if(removable){const b=document.createElement('button');b.type='button';b.textContent='\u00d7';b.onclick=e=>{e.preventDefault();items.splice(i,1);save();renderTicks(elId,items,save,removable)};l.appendChild(b)}el.appendChild(l)});
if(!items.length)el.innerHTML='<div class="muted">None yet</div>'}
function post(path,body){return fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
const savePunch=()=>post('/punch',{punch_list:currentJob.punch_list});
const SV_TEXT=['want_desc','contact_name','contact_phone','current_system','power','network','conduit','obstacle_notes','timeline','budget','decision_maker'];
function svCall(){const ph=document.getElementById('sv-contact_phone').value.replace(/[^0-9+]/g,''),a=document.getElementById('sv-call');if(ph){a.href='tel:'+ph;a.textContent='Call Contact ('+document.getElementById('sv-contact_phone').value+')';a.style.display='block'}else{a.removeAttribute('href');a.style.display='none'}}
let svEditing=false;
function renderKelly(){const done=currentJob.status==='completed'&&currentJob.job_type==='Survey';const lock=done&&!svEditing;
document.querySelectorAll('#survey-card input,#survey-card textarea').forEach(e=>e.disabled=lock);
document.getElementById('sv-add').classList.toggle('hidden',lock);document.getElementById('sv-save').classList.toggle('hidden',!(done&&svEditing));
const ed=document.getElementById('sv-edit');if(ed)ed.classList.toggle('hidden',!lock);
const n=document.getElementById('kelly-note');if(n)n.textContent=currentJob.kelly_at?'Sent to Kelly \u00b7 '+currentJob.kelly_at:'';
if(currentJob.job_type==='Survey')document.getElementById('finish').classList.toggle('hidden',done)}
function fillSurvey(){svEditing=false;renderKelly();const s=currentJob.survey||{};SV_TEXT.forEach(k=>document.getElementById('sv-'+k).value=s[k]||'');
document.querySelectorAll('.sv-ob').forEach(c=>c.checked=(s.obstacles||[]).includes(c.value));const o=document.getElementById('sv-office');if(o)o.value=currentJob.survey_office||'';svCall()}
async function saveSurvey(quiet){const s={obstacles:[...document.querySelectorAll('.sv-ob')].filter(c=>c.checked).map(c=>c.value)};SV_TEXT.forEach(k=>s[k]=document.getElementById('sv-'+k).value);
let ok=(await post('/survey',{survey:s})).ok;const o=document.getElementById('sv-office');if(ok&&o)ok=(await post('/survey-office',{survey_office:o.value})).ok;
if(ok){currentJob.survey=s;if(o)currentJob.survey_office=o.value;svCall();if(!quiet){alert('Survey saved');svEditing=false;renderKelly()}}else alert('Error');return ok}
function renderType(){const t=currentJob.job_type||'';const s=document.getElementById('jtype');if(s)s.value=t;const l=document.getElementById('jtype-label');if(l)l.textContent=t||'(not set)';
const sv=t==='Survey';document.getElementById('survey-card').classList.toggle('hidden',!sv);document.querySelectorAll('#detail .ns').forEach(e=>e.classList.toggle('hidden',sv));
document.getElementById('bp-card').classList.toggle('hidden',t==='Office Task');const f=document.getElementById('finish');f.textContent=sv?'Complete Survey':'Finish Job';f.classList.toggle('hidden',sv&&currentJob.status==='completed');if(!sv)resize()}
async function setType(t){if(!t)return;const r=await post('/type',{job_type:t});if(r.ok){const d=await r.json();currentJob.job_type=d.job_type;renderType()}else alert('Error')}
function addPunch(){const n=document.getElementById('punch-new');const t=n.value.trim();if(!t)return;(currentJob.punch_list=currentJob.punch_list||[]).push({text:t,done:false});n.value='';renderTicks('punch',currentJob.punch_list,savePunch,true);savePunch()}
async function saveQuick(){const r=await post('/quick-notes',{quick_notes:document.getElementById('quick-notes').value});alert(r.ok?'Saved':'Error')}
async function assignMe(){const r=await post('/reassign',{tech:{{ name|tojson }}});if(r.ok){currentJob.tech={{ name|tojson }};const rt=document.getElementById('r-tech');if(rt)rt.value=currentJob.tech;alert('Assigned to you')}else alert('Error')}
async function copyJob(){const id=prompt('New Job ID (leave blank to auto-number)','');if(id===null)return;const sd=prompt('Scheduled date (YYYY-MM-DD, blank = today)','');if(sd===null)return;
const r=await post('/copy',{id:id.trim(),scheduled_date:sd.trim()});const d=await r.json().catch(()=>({}));if(r.ok){alert('Copied as '+d.id);openJob(d.id)}else alert(d.error||'Error')}
async function openJob(id){const r=await fetch('/api/jobs/'+encodeURIComponent(id));if(!r.ok){alert('Job not found');return}currentJob=await r.json();history.replaceState(null,'','/?job='+encodeURIComponent(currentJob.id));
renderPhotos();renderBlueprints();renderHelp();document.getElementById('support-note').value='';loadSupport();document.getElementById('help-form').classList.add('hidden');document.getElementById('help-note').value='';renderMats();renderTotals();fillSurvey();renderTicks('punch',currentJob.punch_list||[],savePunch,true);document.getElementById('quick-notes').value=currentJob.quick_notes||'';document.getElementById('notes').value=currentJob.notes||'';document.getElementById('punch-new').value='';document.getElementById('status').value=currentJob.status||'assigned';document.getElementById('hours').value='';document.getElementById('miles').value='';
const pn=document.getElementById('private-notes');if(pn)pn.value=currentJob.private_notes||'';document.getElementById('print').href='/print/job/'+encodeURIComponent(currentJob.id);
document.getElementById('d-id').textContent=currentJob.id;document.getElementById('d-title').textContent=currentJob.title;
document.getElementById('d-customer').textContent=currentJob.customer;document.getElementById('d-address').textContent=currentJob.address;
document.getElementById('d-scope').textContent=currentJob.scope||'';document.getElementById('d-created').textContent=currentJob.created_by?'Created by '+currentJob.created_by:'';renderChecklist();loadUpdates();const rt=document.getElementById('r-tech');if(rt)rt.value=currentJob.tech||'';document.getElementById('update-note').value='';
const c=document.getElementById('call');const ph=(currentJob.phone||'').replace(/[^0-9+]/g,'');if(ph){c.href='tel:'+ph;c.textContent='Call Customer ('+currentJob.phone+')';c.style.display='block'}else{c.removeAttribute('href');c.style.display='none'}
document.getElementById('history').href='/clients?name='+encodeURIComponent(currentJob.customer||'');
document.getElementById('directions').href='https://maps.google.com/?q='+encodeURIComponent(currentJob.address);
document.getElementById('list').classList.add('hidden');document.getElementById('search-box').classList.add('hidden');document.getElementById('office').classList.add('hidden');document.getElementById('detail').classList.remove('hidden');renderType();clearSig()}
function showList(){history.replaceState(null,'','/');document.getElementById('detail').classList.add('hidden');document.getElementById('list').classList.remove('hidden');document.getElementById('search-box').classList.remove('hidden');document.getElementById('office').classList.remove('hidden');load()}
document.getElementById('finish').onclick=async()=>{const fd=new FormData();
fd.append('notes',document.getElementById('notes').value);
if(currentJob.job_type==='Survey'){if(!(await saveSurvey(true)))return;if(!(await post('/quick-notes',{quick_notes:document.getElementById('quick-notes').value})).ok){alert('Error');return}const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/complete',{method:'POST',body:fd});if(r.ok){alert('Survey complete. Kelly has been told it is ready for an estimate.');openJob(currentJob.id)}else alert('Error');return}
fd.append('materials',document.getElementById('materials').value);
fd.append('hours',document.getElementById('hours').value||0);fd.append('miles',document.getElementById('miles').value||0);
fd.append('checklist',JSON.stringify(currentJob.checklist||[]));fd.append('signature',canvas.toDataURL());
const r=await fetch('/api/jobs/'+currentJob.id+'/complete',{method:'POST',body:fd});
if(r.ok){alert('Job finished');showList()}else alert('Error')};
async function addJob(){const v=k=>document.getElementById('n-'+k).value.trim();
const body={id:v('id'),title:v('title'),customer:v('customer'),address:v('address'),phone:v('phone'),tech:v('tech'),scope:v('scope'),scheduled_date:v('date'),checklist:v('checklist'),job_type:v('type')};
if(!body.id||!body.title){alert('Job ID and title are required');return}
const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(r.ok){['id','title','customer','address','phone','scope','date','checklist'].forEach(k=>document.getElementById('n-'+k).value='');load()}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}}
load();
const qj=new URLSearchParams(location.search).get('job');if(qj)openJob(qj);
</script></body></html>'''

@app.route('/')
def index():
    techs=[(k.title(),k.title()) for k in sorted(USERS) if k not in OFFICE_NAMES]+[(k.title(),k.title()+' (office)') for k in sorted(USERS) if k in OFFICE_NAMES]
    return render_template_string(HTML, name=me(), office=is_office(), techs=techs, statuses=STATUSES, job_types=JOB_TYPES, obstacles=SURVEY_OBSTACLES)

@app.route('/api/jobs')
def list_jobs():
    status=request.args.get('status','assigned'); q=(request.args.get('q') or '').strip().lower()
    conn=get_db()
    if q:
        rows=conn.execute("SELECT * FROM jobs WHERE INSTR(LOWER(IFNULL(customer,'')),?)>0 OR INSTR(LOWER(IFNULL(address,'')),?)>0 OR INSTR(LOWER(id),?)>0 ORDER BY (status='completed'), COALESCE(scheduled_date, created_at) DESC",(q,q,q)).fetchall()
    elif status=='completed': rows=conn.execute("SELECT * FROM jobs WHERE status='completed' ORDER BY completed_at DESC").fetchall()
    else: rows=conn.execute("SELECT * FROM jobs WHERE status!='completed' ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([job_dict(r) for r in rows if can_see(r)])

@app.route('/api/jobs', methods=['POST'])
def create_job():
    d=request.get_json(silent=True) or request.form
    jid=(d.get('id') or '').strip(); title=(d.get('title') or '').strip()
    if not jid or not title: return jsonify({'error':'Job ID and title are required'}),400
    conn=get_db()
    if not is_office(): conn.close(); return jsonify({'error':'office only'}),403
    if conn.execute('SELECT 1 FROM jobs WHERE id=?',(jid,)).fetchone():
        conn.close(); return jsonify({'error':'That Job ID already exists'}),409
    lines = [x.strip() for x in (d.get('checklist') or '').splitlines() if x.strip()]
    chk = json.dumps([{'text': x, 'done': False} for x in (lines or DEFAULT_CHECKLIST)])
    sd = (d.get('scheduled_date') or '').strip() or today_local().isoformat()
    jt = (d.get('job_type') or '').strip(); jt = jt if jt in JOB_TYPES else 'Install'
    conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,phone,checklist,scheduled_date,created_by,job_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (jid,title,(d.get('customer') or '').strip(),(d.get('address') or '').strip(),(d.get('scope') or '').strip(),'assigned',(d.get('tech') or 'Mike').strip(),datetime.utcnow().isoformat(),(d.get('phone') or '').strip(),chk,sd,me(),jt))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'id':jid}),201

@app.route('/api/jobs/<job_id>/type', methods=['POST'])
def set_job_type(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    jt=((request.get_json(silent=True) or {}).get('job_type') or '').strip()
    if jt not in JOB_TYPES: return jsonify({'error':'bad type'}),400
    conn=get_db(); n=conn.execute('UPDATE jobs SET job_type=? WHERE id=?',(jt,job_id)).rowcount
    conn.commit(); conn.close()
    return (jsonify({'ok':True,'job_type':jt}) if n else ('',404))

def save_ticks(job_id, col, key):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    items=parse_ticks((request.get_json(silent=True) or {}).get(key) or [])
    conn.execute(f'UPDATE jobs SET {col}=? WHERE id=?',(json.dumps(items),job_id)); conn.commit(); conn.close()
    return jsonify({'ok':True,key:items})

@app.route('/api/jobs/<job_id>/survey', methods=['POST'])
def save_survey(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    if row['status']=='completed' and not is_office(): conn.close(); return jsonify({'error':'survey is complete; office can edit'}),403
    form=clean_survey((request.get_json(silent=True) or {}).get('survey'))
    conn.execute('UPDATE jobs SET survey_form=? WHERE id=?',(json.dumps(form),job_id)); conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/jobs/<job_id>/survey-office', methods=['POST'])
def save_survey_office(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    note=((request.get_json(silent=True) or {}).get('survey_office') or '')[:5000]
    conn=get_db(); n=conn.execute('UPDATE jobs SET survey_office=? WHERE id=?',(note,job_id)).rowcount; conn.commit()
    row=conn.execute('SELECT job_type,status FROM jobs WHERE id=?',(job_id,)).fetchone()
    if row and row['job_type']=='Survey' and row['status']=='completed':
        conn.execute('UPDATE jobs SET kelly_at=?,kelly_by=? WHERE id=?',(now_local_str(),me(),job_id)); conn.commit()
        notify_kelly(conn, job_id, 'survey_updated')
    conn.close()
    return (jsonify({'ok':True}) if n else ('',404))

@app.route('/api/jobs/<job_id>/punch', methods=['POST'])
def save_punch(job_id): return save_ticks(job_id, 'punch_list', 'punch_list')

@app.route('/api/jobs/<job_id>/quick-notes', methods=['POST'])
def save_quick_notes(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    note=((request.get_json(silent=True) or {}).get('quick_notes') or '')[:5000]
    conn.execute('UPDATE jobs SET quick_notes=? WHERE id=?',(note,job_id)); conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/jobs/<job_id>/copy', methods=['POST'])
def copy_job(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    d=request.get_json(silent=True) or {}
    conn=get_db(); src=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not src: conn.close(); return ('',404)
    nid=(d.get('id') or '').strip()
    if nid:
        if conn.execute('SELECT 1 FROM jobs WHERE id=?',(nid,)).fetchone(): conn.close(); return jsonify({'error':'That Job ID already exists'}),409
    else:
        n=2
        while conn.execute('SELECT 1 FROM jobs WHERE id=?',(f'{job_id}-{n}',)).fetchone(): n+=1
        nid=f'{job_id}-{n}'
    sd=(d.get('scheduled_date') or '').strip()
    try: sd=datetime.strptime(sd,'%Y-%m-%d').date().isoformat()
    except ValueError: sd=today_local().isoformat()
    untick=lambda raw,k: json.dumps([{'text':i['text'],k:False} for i in raw])
    src_d=job_dict(src)
    conn.execute('INSERT INTO jobs (id,title,customer,address,phone,scope,tech,job_type,status,checklist,materials_list,created_at,scheduled_date,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (nid,src['title'],src['customer'],src['address'],src['phone'],src['scope'],src['tech'],src['job_type'],'assigned',
                  untick(src_d['checklist'],'done'),untick(src_d['materials_list'],'used'),
                  datetime.utcnow().isoformat(),sd,me()))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'id':nid}),201

@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone(); conn.close()
    if not row: return ('',404)
    if not can_see(row): return jsonify({'error':'not your job'}),403
    conn=get_db(); photos=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help') ORDER BY id",(job_id,)).fetchall()]; bps=blueprints(conn, job_id); conn.close()
    return jsonify(dict(job_dict(row),photos=photos,blueprints=bps))

@app.route('/api/jobs/<job_id>/updates', methods=['GET','POST'])
def job_updates(job_id):
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return ('',404)
    if not can_see(row): conn.close(); return jsonify({'error':'not your job'}),403
    if request.method=='POST':
        note=((request.get_json(silent=True) or {}).get('note') or '').strip()[:2000]
        if not note: conn.close(); return jsonify({'error':'note required'}),400
        conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,me(),note,now_local_str())); conn.commit()
    rows=conn.execute('SELECT author,note,created_at FROM job_updates WHERE job_id=? ORDER BY id DESC',(job_id,)).fetchall(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/jobs/<job_id>/checklist', methods=['POST'])
def save_checklist(job_id):
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return ('',404)
    if not can_see(row): conn.close(); return jsonify({'error':'not your job'}),403
    items=(request.get_json(silent=True) or {}).get('checklist') or []
    clean=[{'text':str(i.get('text',''))[:200],'done':bool(i.get('done'))} for i in items if isinstance(i,dict) and i.get('text')]
    conn.execute('UPDATE jobs SET checklist=? WHERE id=?',(json.dumps(clean),job_id)); conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/jobs/<job_id>/complete', methods=['POST'])
def complete(job_id):
    notes=request.form.get('notes',''); materials=request.form.get('materials','')
    hours=float(request.form.get('hours') or 0); miles=float(request.form.get('miles') or 0)
    sig=request.form.get('signature',''); minutes=int(hours*60)
    chk_raw=request.form.get('checklist')
    conn=get_db(); now=datetime.utcnow().isoformat()
    row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return ('',404)
    if not can_see(row): conn.close(); return jsonify({'error':'not your job'}),403
    if chk_raw:
        try:
            items=json.loads(chk_raw)
            conn.execute('UPDATE jobs SET checklist=? WHERE id=?',(json.dumps([{'text':str(i.get('text',''))[:200],'done':bool(i.get('done'))} for i in items if isinstance(i,dict) and i.get('text')]),job_id))
        except Exception: pass
    if row['job_type']=='Survey':
        conn.execute('UPDATE jobs SET status=?,notes=?,completed_at=? WHERE id=?',('completed',notes,now,job_id))
        conn.execute('UPDATE jobs SET kelly_at=?,kelly_by=? WHERE id=?',(now_local_str(),me(),job_id))
        kelly_event='survey_completed'
    else:
        conn.execute('UPDATE jobs SET status=?,notes=?,materials=?,signature=?,total_minutes=total_minutes+?,total_miles=total_miles+?,completed_at=? WHERE id=?',
                     ('completed',notes,materials,sig,minutes,miles,now,job_id))
    for ptype in PHOTO_TYPES: save_photos(conn, job_id, request.files.getlist(ptype), ptype, now)
    conn.commit()
    if row['job_type']=='Survey': notify_kelly(conn, job_id, kelly_event)
    pkg=build_invoice_draft(conn, job_id, now); conn.close()
    return jsonify({'ok':True,'invoice_draft':pkg})

def build_invoice_draft(conn, job_id, now):
    j=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    photos=[{'filename':p['filename'],'type':p['photo_type']} for p in conn.execute('SELECT filename,photo_type FROM photos WHERE job_id=?',(job_id,)).fetchall()]
    pkg={'job_id':job_id,'customer':j['customer'],'phone':j['phone'],'address':j['address'],'title':j['title'],'scope':j['scope'],
         'tech':j['tech'],'materials':j['materials'] or '','materials_list':parse_materials(j['materials_list']),'hours':round((j['total_minutes'] or 0)/60,2),'miles':j['total_miles'] or 0,
         'notes':j['notes'] or '','photos':photos,'completed_at':j['completed_at'],'status':'ready_for_review (not sent)'}
    conn.execute('INSERT OR REPLACE INTO invoice_drafts (job_id,package,status,created_at) VALUES (?,?,?,?)',(job_id,json.dumps(pkg),'ready_for_review',now))
    conn.commit()
    app.logger.warning('INVOICE_DRAFT_READY %s', json.dumps(pkg))
    return pkg

def own_job(conn, job_id):
    row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: return None, ('',404)
    if not can_see(row): return None, (jsonify({'error':'not your job'}),403)
    return row, None

def save_photos(conn, job_id, files, ptype, now):
    for f in files:
        if f and f.filename:
            fn=f"{job_id}_{ptype}_{uuid.uuid4().hex[:8]}.jpg"
            f.save(os.path.join(app.config['UPLOAD_FOLDER'],secure_filename(fn)))
            conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at) VALUES (?,?,?,?)',(job_id,secure_filename(fn),ptype,now))

@app.route('/api/jobs/<job_id>/materials', methods=['POST'])
def save_materials(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    items=parse_materials(json.dumps((request.get_json(silent=True) or {}).get('materials') or []))
    conn.execute('UPDATE jobs SET materials_list=? WHERE id=?',(json.dumps(items),job_id)); conn.commit(); conn.close()
    return jsonify({'ok':True,'materials_list':items})

@app.route('/api/jobs/<job_id>/time', methods=['POST'])
def save_time(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    d=request.get_json(silent=True) or {}
    try: hours=float(d.get('hours') or 0); miles=float(d.get('miles') or 0)
    except (TypeError, ValueError): conn.close(); return jsonify({'error':'numbers only'}),400
    if d.get('mode')=='set':
        conn.execute('UPDATE jobs SET total_minutes=?,total_miles=? WHERE id=?',(max(0,int(round(hours*60))),max(0.0,miles),job_id))
    else:
        conn.execute('UPDATE jobs SET total_minutes=MAX(0,IFNULL(total_minutes,0)+?),total_miles=MAX(0,IFNULL(total_miles,0)+?) WHERE id=?',(int(round(hours*60)),miles,job_id))
    conn.commit(); j=conn.execute('SELECT total_minutes,total_miles FROM jobs WHERE id=?',(job_id,)).fetchone(); conn.close()
    return jsonify({'ok':True,'hours':round((j['total_minutes'] or 0)/60,2),'total_miles':j['total_miles'] or 0})

@app.route('/api/jobs/<job_id>/photos', methods=['POST'])
def upload_photos(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    ptype=request.form.get('type','')
    if ptype not in PHOTO_TYPES and not (ptype in SURVEY_PHOTO_TYPES and row['job_type']=='Survey'): conn.close(); return jsonify({'error':'bad photo label'}),400
    if row['job_type']=='Survey' and row['status']=='completed' and not is_office(): conn.close(); return jsonify({'error':'survey is complete; office can edit'}),403
    save_photos(conn, job_id, request.files.getlist('photos'), ptype, datetime.utcnow().isoformat()); conn.commit()
    photos=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help') ORDER BY id",(job_id,)).fetchall()]; conn.close()
    return jsonify({'ok':True,'photos':photos})

def blueprints(conn, job_id):
    out=[]
    for b in conn.execute("SELECT filename,orig_name FROM photos WHERE job_id=? AND photo_type='blueprint' ORDER BY id",(job_id,)).fetchall():
        ext=os.path.splitext(b['filename'])[1].lower()
        out.append({'filename':b['filename'],'name':b['orig_name'] or b['filename'],'image':ext in ('.png','.jpg','.jpeg','.webp')})
    return out

@app.route('/api/jobs/<job_id>/help', methods=['POST'])
def need_help(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    note=(request.form.get('note') or '').strip()[:2000]
    f=request.files.get('photo')
    if f and f.filename:
        fn=secure_filename(f"{job_id}_help_{uuid.uuid4().hex[:8]}.jpg")
        f.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
        conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at) VALUES (?,?,?,?)',(job_id,fn,'help',datetime.utcnow().isoformat()))
    else:
        last=conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type!='blueprint' ORDER BY id DESC LIMIT 1",(job_id,)).fetchone()
        fn=last['filename'] if last else ''
    conn.execute("UPDATE jobs SET help_flag='open',help_note=?,help_at=?,help_by=?,help_photo=? WHERE id=?",(note,now_local_str(),me(),fn,job_id))
    conn.commit(); j=job_dict(conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()); conn.close()
    conn=get_db(); conn.execute("INSERT INTO support_replies (job_id,author,note,created_at,kind) VALUES (?,?,?,?,'help')",(job_id,me(),note or 'Need Help',now_local_str())); conn.commit(); conn.close()
    notify_tech_support(j, fn)
    return jsonify({k:j[k] for k in ('help_open','help_note','help_at','help_by','help_photo')})

@app.route('/api/tech-support/reply', methods=['POST'])
def tech_support_reply():
    # Auth: Authorization: Bearer <HELP_WEBHOOK_KEY>. Body JSON: {"job_id": "...", "reply": "..."}
    key=os.environ.get('HELP_WEBHOOK_KEY','')
    got=(request.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
    if not key or not hmac.compare_digest(got, key): return jsonify({'error':'unauthorized'}),401
    d=request.get_json(silent=True) or {}
    job_id=str(d.get('job_id') or '').strip(); reply=str(d.get('reply') or '').strip()[:4000]
    if not job_id or not reply: return jsonify({'error':'job_id and reply required'}),400
    conn=get_db()
    if not conn.execute('SELECT 1 FROM jobs WHERE id=?',(job_id,)).fetchone(): conn.close(); return jsonify({'error':'job not found'}),404
    conn.execute('INSERT INTO support_replies (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,'Tech Support',reply,now_local_str())); conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/jobs/<job_id>/support', methods=['GET','POST'])
def support_list(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    if request.method=='POST':
        note=str((request.get_json(silent=True) or {}).get('note') or '').strip()[:2000]
        if not note: conn.close(); return jsonify({'error':'note required'}),400
        lastrow=conn.execute('SELECT author FROM support_replies WHERE job_id=? ORDER BY id DESC LIMIT 1',(job_id,)).fetchone()
        if not lastrow or lastrow['author']=='Resolved': conn.close(); return jsonify({'error':'No open thread. Use Need Help to start a new one.'}),409
        conn.execute('INSERT INTO support_replies (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,me(),note,now_local_str())); conn.commit()
        last=conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type!='blueprint' ORDER BY id DESC LIMIT 1",(job_id,)).fetchone()
        j=dict(job_dict(row)); j['help_by']=me(); j['help_note']=note
        notify_tech_support(j, last['filename'] if last else '')
    rows=[dict(x) for x in conn.execute("SELECT IFNULL(author,'Tech Support') AS author,note,created_at,IFNULL(kind,'') AS kind FROM support_replies WHERE job_id=? ORDER BY id DESC",(job_id,)).fetchall()]; conn.close()
    return jsonify(rows)

def help_photo_token(fn): return hmac.new(app.secret_key.encode(), ('help:'+fn).encode(), hashlib.sha256).hexdigest()[:32]

@app.route('/help-photo/<path:fn>')
def help_photo(fn):
    fn=secure_filename(fn)
    if not hmac.compare_digest(request.args.get('t',''), help_photo_token(fn)): return ('',403)
    return send_from_directory(app.config['UPLOAD_FOLDER'], fn)

def notify_kelly(conn, job_id, event):
    # Render env vars KELLY_WEBHOOK_URL and KELLY_WEBHOOK_KEY (Kelly's Grok Bot webhook routine). Skipped if unset.
    url=os.environ.get('KELLY_WEBHOOK_URL',''); key=os.environ.get('KELLY_WEBHOOK_KEY','')
    if not url or not key: return
    j=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not j: return
    sv=clean_survey(j['survey_form']); base=(os.environ.get('RENDER_EXTERNAL_URL') or request.host_url).rstrip('/')
    fns=[r['filename'] for r in conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help') ORDER BY id",(job_id,)).fetchall()]
    mats=[m['text'] for m in parse_materials(j['materials_list'])]
    body={'event':event,'job_id':j['id'],'title':j['title'] or '','customer':j['customer'] or '','address':j['address'] or '',
          'phone':j['phone'] or '','scope':j['scope'] or '','sent_by':me(),
          'what_they_want':sv['want_desc'],
          'materials_notes':j['survey_office'] or '','materials_list':mats,
          'existing_conditions':{'current_system':sv['current_system'],'power':sv['power'],'network':sv['network'],'conduit':sv['conduit']},
          'obstacles':sv['obstacles'],'obstacle_notes':sv['obstacle_notes'],
          'site_contact':{'name':sv['contact_name'],'phone':sv['contact_phone']},
          'client_expectations':{'timeline':sv['timeline'],'budget':sv['budget'],'decision_maker':sv['decision_maker']},
          'notes':j['notes'] or '','quick_notes':j['quick_notes'] or '',
          'photo_urls':[f"{base}/help-photo/{fn}?t={help_photo_token(fn)}" for fn in fns],
          'print_url':f"{base}/print/job/{j['id']}"}
    def send():
        try:
            req=urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                headers={'Content-Type':'application/json','Authorization':'Bearer '+key,'X-Automation-Key':key})
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            app.logger.warning('Kelly notify failed for %s: %s', job_id, e)
    threading.Thread(target=send, daemon=True).start()

def notify_tech_support(j, fn):
    # Render env vars HELP_WEBHOOK_URL and HELP_WEBHOOK_KEY (from the Grok Bot webhook routine). Skipped if unset.
    url=os.environ.get('HELP_WEBHOOK_URL',''); key=os.environ.get('HELP_WEBHOOK_KEY','')
    if not url or not key: return
    base=(os.environ.get('RENDER_EXTERNAL_URL') or request.host_url).rstrip('/')
    body={'job_id':j['id'],'title':j.get('title') or '','job_type':j.get('job_type') or '','customer':j.get('customer') or '',
          'address':j.get('address') or '','tech':j.get('help_by') or '','note':j.get('help_note') or '',
          'photo_url':(f"{base}/help-photo/{fn}?t={help_photo_token(fn)}" if fn else '')}
    def send():
        try:
            req=urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                headers={'Content-Type':'application/json','Authorization':'Bearer '+key,'X-Automation-Key':key})
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            app.logger.warning('Tech Support notify failed for %s: %s', j['id'], e)
    threading.Thread(target=send, daemon=True).start()

@app.route('/api/jobs/<job_id>/support/resolve', methods=['POST'])
def support_resolve(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    conn.execute("UPDATE jobs SET help_flag='' WHERE id=?",(job_id,))
    conn.execute('INSERT INTO support_replies (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,'Resolved','Issue resolved by '+me(),now_local_str())); conn.commit(); conn.close()
    return jsonify({'ok':True})


@app.route('/api/jobs/<job_id>/blueprints', methods=['POST'])
def upload_blueprints(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    if row['job_type']=='Office Task': conn.close(); return jsonify({'error':'not for Office Task jobs'}),400
    files=[f for f in request.files.getlist('files') if f and f.filename]
    if not files: conn.close(); return jsonify({'error':'no file'}),400
    exts=[os.path.splitext(f.filename)[1].lower() for f in files]
    if any(e not in BLUEPRINT_EXTS for e in exts): conn.close(); return jsonify({'error':'PDF or image files only (pdf, png, jpg, jpeg, webp, heic)'}),400
    now=datetime.utcnow().isoformat()
    for f,e in zip(files,exts):
        fn=secure_filename(f"{job_id}_blueprint_{uuid.uuid4().hex[:8]}{e}")
        f.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
        conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at,orig_name) VALUES (?,?,?,?,?)',(job_id,fn,'blueprint',now,os.path.basename(f.filename)[:200]))
    conn.commit(); bps=blueprints(conn, job_id); conn.close()
    return jsonify({'ok':True,'blueprints':bps})

@app.route('/api/jobs/<job_id>/status', methods=['POST'])
def set_status(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    st=((request.get_json(silent=True) or {}).get('status') or '').strip()
    if st not in STATUS_LABEL: conn.close(); return jsonify({'error':'bad status'}),400
    now=datetime.utcnow().isoformat()
    if st=='completed':
        conn.execute('UPDATE jobs SET status=?,completed_at=COALESCE(completed_at,?) WHERE id=?',(st,now,job_id)); conn.commit()
        if row['status']!='completed': build_invoice_draft(conn, job_id, now)
    else:
        conn.execute('UPDATE jobs SET status=?,completed_at=NULL WHERE id=?',(st,job_id)); conn.commit()
    conn.close(); return jsonify({'ok':True,'status':st,'status_label':STATUS_LABEL[st]})

@app.route('/api/jobs/<job_id>/private-notes', methods=['POST'])
def save_private_notes(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    note=((request.get_json(silent=True) or {}).get('private_notes') or '')[:5000]
    conn=get_db(); n=conn.execute('UPDATE jobs SET private_notes=? WHERE id=?',(note,job_id)).rowcount; conn.commit(); conn.close()
    return (jsonify({'ok':True}) if n else ('',404))

CLIENTS_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Clients</title>
<style>body{font-family:-apple-system,sans-serif;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:12px}
.job{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:16px;margin-bottom:12px}
.job-id{color:#a49f2a;font-weight:700;font-size:.8rem}a{color:#111}.muted{color:#666}
.btn{display:block;background:#f0f0f0;color:#111;padding:14px;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;margin:8px 0}
.client{display:block;text-decoration:none}h2{color:#a49f2a;margin:8px 0 12px}img{width:80px;height:80px;object-fit:cover;border-radius:8px;margin:4px 4px 0 0}
input{width:100%;padding:12px;border:1px solid #e5e5e5;border-radius:10px;margin:6px 0;font-size:1rem;box-sizing:border-box}
.sec{font-weight:600;margin-top:10px;font-size:.9rem}.badge{font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;background:#f0f0f0;color:#333}.folder{font-weight:700;color:#a49f2a;margin:16px 0 6px}summary{cursor:pointer}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;width:auto;object-fit:contain;margin:0 auto 4px;display:block;border-radius:0">
{% macro bplist(j) %}{% if j.job_type != 'Office Task' and j.blueprints %}<div class="sec">Blueprints ({{ j.blueprints|length }})</div>{% for b in j.blueprints %}<div style="margin:4px 0"><a href="/uploads/{{ b.filename|urlencode }}" target="_blank">{% if b.image %}<img src="/uploads/{{ b.filename|urlencode }}" alt="blueprint" style="width:56px;height:56px;vertical-align:middle">{% else %}&#128196;{% endif %} {{ b.name }}</a></div>{% endfor %}{% endif %}{% endmacro %}
{% macro photogroups(j) %}{% if j.job_type == 'Survey' %}{% for p in j.photos %}<a href="/uploads/{{ p.filename|urlencode }}" target="_blank"><img src="/uploads/{{ p.filename|urlencode }}" alt="photo"></a>{% else %}<div class="muted">None</div>{% endfor %}{% else %}{% for t,lbl in [('before','Before'),('during','During'),('after','After'),('doors','Doors'),('power','Power'),('network','Network'),('proposed','Proposed locations')] %}{% set g = j.photos|selectattr('photo_type','equalto',t)|list %}{% if loop.index <= 3 or g %}<div class="muted" style="font-size:.85rem;margin-top:4px">{{ lbl }} ({{ g|length }})</div>{% endif %}{% for p in g %}<a href="/uploads/{{ p.filename|urlencode }}" target="_blank"><img src="/uploads/{{ p.filename|urlencode }}" alt="{{ t }}"></a>{% endfor %}{% endfor %}{% endif %}{% endmacro %}
{% macro jobcard(j, open) %}<details class="job"{% if open %} open{% endif %}><summary><span class="job-id">{{ j.id }}</span> {% if office and j.help_open %}<span class="badge" style="background:#b00020;color:#fff;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px">Needs Help</span> {% endif %}<span class="badge">{{ j.status_label }}</span>{% if j.job_type %} <span class="badge">{{ j.job_type }}</span>{% endif %} <b>{{ j.title }}</b><div class="muted" style="font-size:.85rem">Tech: {{ j.tech or '-' }}{% if j.job_type != 'Survey' %} &middot; {{ j.hours }} hrs &middot; {{ j.total_miles }} mi{% endif %}{% if j.scheduled_date %} &middot; {{ j.scheduled_date }}{% endif %}{% if j.completed_at %} &middot; Done {{ j.completed_at[:10] }}{% endif %}{% if j.created_by %} &middot; Created by {{ j.created_by }}{% endif %}</div></summary>
{% if office and j.help_open %}<div style="border:2px solid #b00020;background:#fff5f5;border-radius:10px;padding:8px;margin-top:8px"><div style="color:#b00020;font-weight:700">&#9888; Needs Help</div><div class="muted" style="font-size:.8rem">{{ j.help_by }} &middot; {{ j.help_at }}</div><div style="white-space:pre-wrap">{{ j.help_note }}</div>{% if j.help_photo %}<a href="/uploads/{{ j.help_photo|urlencode }}" target="_blank"><img src="/uploads/{{ j.help_photo|urlencode }}" alt="help photo"></a>{% endif %}</div>{% endif %}
{% if j.job_type == 'Survey' %}<a class="btn" style="background:#6a1b9a;color:#fff" href="/?job={{ j.id|urlencode }}">Open Survey</a>{% endif %}
{% if j.scope %}<div class="sec">Scope</div><div style="white-space:pre-wrap">{{ j.scope }}</div>{% endif %}
{% if j.job_type != 'Survey' %}<div class="sec">Daily Updates ({{ j.updates|length }})</div>{% for u in j.updates %}<div style="padding:4px 0;border-bottom:1px solid #eee"><div class="muted" style="font-size:.8rem">{{ u.created_at }} &middot; {{ u.author }}</div><div style="white-space:pre-wrap">{{ u.note }}</div></div>{% else %}<div class="muted">None</div>{% endfor %}{% endif %}
<div class="sec">Notes</div><div style="white-space:pre-wrap">{{ j.notes or '' }}</div>{% if not j.notes %}<div class="muted">None</div>{% endif %}
{% if j.job_type == 'Survey' %}{% set s = j.survey %}<div class="sec">Estimating Site Survey</div><div style="border-left:3px solid #a49f2a;padding-left:8px">
<div><b>Site contact:</b> {{ s.contact_name or '-' }}{% if s.contact_phone %} &middot; {% if s.contact_tel %}<a href="tel:{{ s.contact_tel }}">{{ s.contact_phone }}</a>{% else %}{{ s.contact_phone }}{% endif %}{% endif %}</div>
{% if s.want_desc %}<div><b>What they want:</b> <span style="white-space:pre-wrap">{{ s.want_desc }}</span></div>{% endif %}
<div><b>Current system:</b> {{ s.current_system or '-' }}</div><div><b>Power:</b> {{ s.power or '-' }}</div><div><b>Network:</b> {{ s.network or '-' }}</div><div><b>Conduit / pathways:</b> {{ s.conduit or '-' }}</div>
<div style="margin-top:4px"><b>Obstacles:</b> {{ s.obstacles|join(', ') if s.obstacles else '-' }}</div>{% if s.obstacle_notes %}<div style="white-space:pre-wrap">{{ s.obstacle_notes }}</div>{% endif %}
<div style="margin-top:4px"><b>Timeline:</b> {{ s.timeline or '-' }} &middot; <b>Budget:</b> {{ s.budget or '-' }} &middot; <b>Decision maker:</b> {{ s.decision_maker or '-' }}</div>
{% if office and j.survey_office %}<div style="margin-top:4px"><b>Rough materials &amp; labor (office only):</b></div><div style="white-space:pre-wrap">{{ j.survey_office }}</div>{% endif %}</div>{% endif %}
{% if office and j.quick_notes %}<div class="sec">Quick Notes</div><div style="white-space:pre-wrap">{{ j.quick_notes }}</div>{% endif %}
{% if office and j.punch_list and j.job_type != 'Survey' %}<div class="sec">Internal Punch List ({{ j.punch_list|selectattr('done')|list|length }}/{{ j.punch_list|length }})</div>{% for c in j.punch_list %}<div>{{ '&#9745;'|safe if c.done else '&#9744;'|safe }} {{ c.text }}</div>{% endfor %}{% endif %}
{% if office and j.private_notes %}<div class="sec">Private Office Notes</div><div style="white-space:pre-wrap">{{ j.private_notes }}</div>{% endif %}
{% if j.job_type != 'Survey' %}{% if j.materials or j.materials_list %}<div class="sec">Materials</div>{% for m in j.materials_list %}<div>{{ '&#9745;'|safe if m.used else '&#9744;'|safe }} {{ m.text }}</div>{% endfor %}{% if j.materials %}<div>{{ j.materials }}</div>{% endif %}{% endif %}
<div class="sec">Checklist ({{ j.checklist|selectattr('done')|list|length }}/{{ j.checklist|length }})</div>{% for c in j.checklist %}<div>{{ '&#9745;'|safe if c.done else '&#9744;'|safe }} {{ c.text }}</div>{% endfor %}{% endif %}
<div class="sec">Photos ({{ j.photos|length }})</div>{{ photogroups(j) }}{{ bplist(j) }}
</details>{% endmacro %}
{% if client is none %}<h2>Clients</h2><a class="btn" href="/">Back to Jobs</a>
<form class="job" method="post" action="/clients/add"><div style="font-weight:600">Add Client</div>
<input name="name" placeholder="Client name" required><input name="address" placeholder="Address"><input name="phone" type="tel" placeholder="Phone">
<button class="btn" style="width:100%;border:none;font-size:1rem" type="submit">Add Client</button></form>
{% for c in clients %}<a class="job client" href="/clients?name={{ c.customer|urlencode }}"><div style="font-weight:600">&#128193; {{ c.customer }}</div>
<div class="muted">{{ c.address or '' }}</div><div class="muted">{{ c.jobs }} job{{ '' if c.jobs == 1 else 's' }}, {{ c.active }} active</div></a>
{% else %}<div class="job">No clients yet</div>{% endfor %}
{% else %}<h2>&#128193; {{ client }}</h2>{% if office %}<a class="btn" href="/clients">Back to Clients</a>{% else %}<a class="btn" href="/">Back to Jobs</a><div class="muted" style="margin-bottom:8px">Read-only client history</div>{% endif %}
{% set active = jobs|rejectattr('status','equalto','completed')|list %}{% set done = jobs|selectattr('status','equalto','completed')|rejectattr('job_type','equalto','Survey')|list %}{% set surveys = jobs|selectattr('status','equalto','completed')|selectattr('job_type','equalto','Survey')|list %}
<div class="job">{% if info.address %}<div>{{ info.address }}</div>{% endif %}{% if info.phone %}<div><a href="tel:{{ info.phone }}">{{ info.phone }}</a></div>{% endif %}
<div class="muted" style="margin-top:4px">{{ active|length }} active &middot; {{ done|length + surveys|length }} completed</div>
<div style="margin-top:4px;font-weight:600">Total: {{ (jobs|sum(attribute='hours'))|round(2) }} hrs &middot; {{ (jobs|sum(attribute='total_miles'))|round(1) }} miles</div></div>
<a class="btn" href="/print/client?name={{ client|urlencode }}" target="_blank">Print Summary</a>
{% if office %}<form method="post" action="/clients/delete" onsubmit="return confirm('Delete this client and ALL of its jobs, photos and notes? This cannot be undone.')"><input type="hidden" name="name" value="{{ client }}"><button class="btn" type="submit" style="width:100%;border:0;background:#b00020;color:#fff;font-size:1rem">Delete Client</button></form>{% endif %}
<div class="folder">Active Jobs</div>{% for j in active %}{{ jobcard(j, true) }}{% else %}<div class="muted" style="margin-bottom:12px">None</div>{% endfor %}
<div class="folder">Completed Surveys</div>{% for j in surveys %}{{ jobcard(j, false) }}{% else %}<div class="muted" style="margin-bottom:12px">None</div>{% endfor %}
<div class="folder">Completed Jobs</div>{% for j in done %}{{ jobcard(j, false) }}{% else %}<div class="muted">None</div>{% endfor %}
{% endif %}
</body></html>'''

def ckey(name): return (name or '').strip().lower()

def tech_has_client(conn, customer):
    return any(can_see(r) for r in conn.execute('SELECT tech FROM jobs WHERE LOWER(TRIM(customer))=?',(ckey(customer),)).fetchall())

@app.route('/clients')
def clients_page():
    conn=get_db(); name=request.args.get('name')
    if not is_office() and (name is None or not tech_has_client(conn, name)): conn.close(); return redirect('/')
    if name is None:
        groups={}
        for c in conn.execute('SELECT name,address FROM clients').fetchall():
            if ckey(c['name']): groups.setdefault(ckey(c['name']),{'customer':c['name'].strip(),'address':c['address'] or '','jobs':0,'active':0})
        for r in conn.execute("SELECT customer,address,status FROM jobs WHERE IFNULL(TRIM(customer),'')!=''").fetchall():
            g=groups.setdefault(ckey(r['customer']),{'customer':r['customer'].strip(),'address':'','jobs':0,'active':0})
            g['jobs']+=1; g['active']+=r['status']!='completed'; g['address']=g['address'] or r['address'] or ''
        conn.close(); return render_template_string(CLIENTS_HTML, client=None, clients=sorted(groups.values(),key=lambda g:g['customer'].lower()), office=True)
    jobs=client_jobs(conn, name)
    c=conn.execute('SELECT * FROM clients WHERE LOWER(TRIM(name))=?',(ckey(name),)).fetchone()
    conn.close()
    info={'address':(c['address'] if c and c['address'] else next((j['address'] for j in jobs if j['address']),'')),
          'phone':(c['phone'] if c and c['phone'] else next((j['phone'] for j in jobs if j['phone']),''))}
    return render_template_string(CLIENTS_HTML, client=(c['name'] if c else name).strip(), jobs=jobs, info=info, office=is_office())

def client_jobs(conn, name):
    rows=conn.execute("SELECT * FROM jobs WHERE LOWER(TRIM(customer))=? ORDER BY (status='completed'), COALESCE(scheduled_date, created_at) DESC",(ckey(name),)).fetchall()
    return [job_full(conn, r) for r in rows]

def job_full(conn, r):
    d=job_dict(r)
    d['updates']=[dict(u) for u in conn.execute('SELECT author,note,created_at FROM job_updates WHERE job_id=? ORDER BY id DESC',(r['id'],)).fetchall()]
    d['photos']=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help') ORDER BY id",(r['id'],)).fetchall()]
    d['blueprints']=blueprints(conn, r['id'])
    return d

CAL_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Calendar</title>
<style>body{font-family:-apple-system,sans-serif;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:12px}
.job{display:block;background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin-bottom:8px;text-decoration:none;color:#111}
.job-id{color:#a49f2a;font-weight:700;font-size:.8rem}.muted{color:#666}.day{font-weight:700;color:#a49f2a;margin:16px 0 6px}.today{background:#a49f2a;color:#fff;padding:2px 8px;border-radius:8px}
.btn{display:block;flex:1;background:#f0f0f0;color:#111;padding:14px;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;margin:8px 0}
.badge{font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;background:#f0f0f0;color:#333}h2{color:#a49f2a;margin:8px 0}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<h2>Calendar</h2><a class="btn" href="/">Back to Jobs</a>
<div style="display:flex;gap:8px"><a class="btn" href="/calendar?start={{ prev }}">&larr; Prev</a><a class="btn" href="/calendar">This Week</a><a class="btn" href="/calendar?start={{ next }}">Next &rarr;</a></div>
{% for d in days %}<div class="day">{% if d.today %}<span class="today">{{ d.label }}</span>{% else %}{{ d.label }}{% endif %}</div>
{% for j in d.jobs %}<a class="job" href="/?job={{ j.id|urlencode }}"><span class="job-id">{{ j.id }}</span> <span class="badge">{{ j.status_label }}</span>{% if j.help_open %} <span class="badge" style="background:#b00020;color:#fff;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px">Needs Help</span>{% endif %}<div style="font-weight:600">{{ j.title }}</div><div class="muted">{{ j.customer }} &middot; {{ j.tech or '-' }}</div></a>
{% else %}<div class="muted">No jobs</div>{% endfor %}{% endfor %}
</body></html>'''

@app.route('/calendar')
def calendar_page():
    if not is_office(): return redirect('/')
    t=today_local()
    try: start=datetime.strptime(request.args.get('start',''),'%Y-%m-%d').date()
    except ValueError: start=t
    start=start-timedelta(days=start.weekday())
    end=start+timedelta(days=6)
    conn=get_db(); rows=conn.execute('SELECT * FROM jobs WHERE scheduled_date BETWEEN ? AND ? ORDER BY scheduled_date, created_at',(start.isoformat(),end.isoformat())).fetchall(); conn.close()
    days=[]
    for i in range(7):
        d=start+timedelta(days=i)
        days.append({'label':d.strftime('%a, %b ')+str(d.day),'today':d==t,'jobs':[job_dict(r) for r in rows if r['scheduled_date']==d.isoformat()]})
    return render_template_string(CAL_HTML, days=days, prev=(start-timedelta(days=7)).isoformat(), next=(start+timedelta(days=7)).isoformat())

PRINT_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ heading }}</title>
<style>body{font-family:-apple-system,sans-serif;color:#111;max-width:760px;margin:auto;padding:16px}h1{font-size:1.3rem;margin:8px 0}h2{font-size:1.1rem;color:#a49f2a;margin:20px 0 4px;border-bottom:2px solid #a49f2a}
.sec{font-weight:700;margin-top:10px}.muted{color:#666}img{width:120px;height:120px;object-fit:cover;margin:4px 4px 0 0;border-radius:6px}
.btn{display:block;width:100%;background:#a49f2a;color:#fff;border:none;padding:16px;border-radius:10px;font-size:1.05rem;font-weight:600;margin:8px 0}
.job{page-break-inside:avoid;margin-bottom:16px}@media print{.noprint{display:none}}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;display:block">
<button class="btn noprint" onclick="window.print()">Print</button>
<h1>{{ heading }}</h1>{% if info %}<div>{{ info.address }}</div><div>{{ info.phone }}</div>{% endif %}
<div class="muted">{{ jobs|length }} job{{ '' if jobs|length == 1 else 's' }} &middot; Total {{ (jobs|sum(attribute='hours'))|round(2) }} hrs &middot; {{ (jobs|sum(attribute='total_miles'))|round(1) }} miles &middot; Printed {{ printed }}</div>
{% for j in jobs %}<div class="job"><h2>{{ j.id }} &middot; {{ j.title }}</h2>
<div>Status: <b>{{ j.status_label }}</b>{% if j.job_type %} &middot; Type: <b>{{ j.job_type }}</b>{% endif %} &middot; Tech: {{ j.tech or '-' }}{% if j.scheduled_date %} &middot; Scheduled {{ j.scheduled_date }}{% endif %}{% if j.completed_at %} &middot; Done {{ j.completed_at[:10] }}{% endif %}</div>
<div>{{ j.customer }} &middot; {{ j.address }}{% if j.phone %} &middot; {{ j.phone }}{% endif %}</div>
{% if j.job_type != 'Survey' %}<div>Hours: {{ j.hours }} &middot; Miles: {{ j.total_miles }}</div>{% endif %}
{% if j.scope %}<div class="sec">Scope</div><div style="white-space:pre-wrap">{{ j.scope }}</div>{% endif %}
{% if j.notes %}<div class="sec">Notes</div><div style="white-space:pre-wrap">{{ j.notes }}</div>{% endif %}
{% if j.job_type == 'Survey' %}{% set s = j.survey %}<div class="sec">Estimating Site Survey</div><div style="border-left:3px solid #a49f2a;padding-left:8px">
<div><b>Site contact:</b> {{ s.contact_name or '-' }}{% if s.contact_phone %} &middot; {% if s.contact_tel %}<a href="tel:{{ s.contact_tel }}">{{ s.contact_phone }}</a>{% else %}{{ s.contact_phone }}{% endif %}{% endif %}</div>
{% if s.want_desc %}<div><b>What they want:</b> <span style="white-space:pre-wrap">{{ s.want_desc }}</span></div>{% endif %}
<div><b>Current system:</b> {{ s.current_system or '-' }}</div><div><b>Power:</b> {{ s.power or '-' }}</div><div><b>Network:</b> {{ s.network or '-' }}</div><div><b>Conduit / pathways:</b> {{ s.conduit or '-' }}</div>
<div style="margin-top:4px"><b>Obstacles:</b> {{ s.obstacles|join(', ') if s.obstacles else '-' }}</div>{% if s.obstacle_notes %}<div style="white-space:pre-wrap">{{ s.obstacle_notes }}</div>{% endif %}
<div style="margin-top:4px"><b>Timeline:</b> {{ s.timeline or '-' }} &middot; <b>Budget:</b> {{ s.budget or '-' }} &middot; <b>Decision maker:</b> {{ s.decision_maker or '-' }}</div>
{% if office and j.survey_office %}<div style="margin-top:4px"><b>Rough materials &amp; labor (office only):</b></div><div style="white-space:pre-wrap">{{ j.survey_office }}</div>{% endif %}</div>{% endif %}
{% if office and j.quick_notes %}<div class="sec">Quick Notes</div><div style="white-space:pre-wrap">{{ j.quick_notes }}</div>{% endif %}
{% if office and j.punch_list and j.job_type != 'Survey' %}<div class="sec">Internal Punch List ({{ j.punch_list|selectattr('done')|list|length }}/{{ j.punch_list|length }})</div>{% for c in j.punch_list %}<div>{{ '&#9745;'|safe if c.done else '&#9744;'|safe }} {{ c.text }}</div>{% endfor %}{% endif %}
{% if office and j.private_notes %}<div class="sec">Private Office Notes</div><div style="white-space:pre-wrap">{{ j.private_notes }}</div>{% endif %}
{% if j.job_type != 'Survey' %}{% if j.materials_list or j.materials %}<div class="sec">Materials</div>{% for m in j.materials_list %}<div>{{ '&#9745;'|safe if m.used else '&#9744;'|safe }} {{ m.text }}</div>{% endfor %}{% if j.materials %}<div>{{ j.materials }}</div>{% endif %}{% endif %}
<div class="sec">Checklist</div>{% for c in j.checklist %}<div>{{ '&#9745;'|safe if c.done else '&#9744;'|safe }} {{ c.text }}</div>{% endfor %}
{% if j.updates %}<div class="sec">Daily Updates</div>{% for u in j.updates %}<div><span class="muted">{{ u.created_at }} &middot; {{ u.author }}:</span> {{ u.note }}</div>{% endfor %}{% endif %}{% endif %}
{% if j.job_type == 'Survey' and j.photos %}<div class="sec">Photos</div>{% for p in j.photos %}<img src="/uploads/{{ p.filename|urlencode }}" alt="photo">{% endfor %}{% endif %}
{% if j.job_type != 'Survey' %}{% for t,lbl in [('before','Before'),('during','During'),('after','After'),('doors','Doors'),('power','Power'),('network','Network'),('proposed','Proposed locations')] %}{% set g = j.photos|selectattr('photo_type','equalto',t)|list %}{% if g %}<div class="sec">{{ lbl }} Photos</div>{% for p in g %}<img src="/uploads/{{ p.filename|urlencode }}" alt="{{ t }}">{% endfor %}{% endif %}{% endfor %}
{% endif %}
{% if j.job_type != 'Office Task' and j.blueprints %}<div class="sec">Blueprints</div>{% for b in j.blueprints %}<div>{% if b.image %}<img src="/uploads/{{ b.filename|urlencode }}" alt="blueprint"><br>{% endif %}<a href="/uploads/{{ b.filename|urlencode }}">{{ b.name }}</a></div>{% endfor %}{% endif %}
{% if j.job_type != 'Survey' and j.signature and j.signature.startswith('data:image/') %}<div class="sec">Customer Signature</div><img src="{{ j.signature }}" alt="signature" style="width:240px;height:auto;object-fit:contain;border:1px solid #ddd">{% endif %}
</div>{% else %}<div class="muted">No jobs</div>{% endfor %}
</body></html>'''

@app.route('/print/job/<job_id>')
def print_job(job_id):
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return ('Not found',404)
    if not can_see(row): conn.close(); return ('Not your job',403)
    j=job_full(conn,row); conn.close()
    return render_template_string(PRINT_HTML, heading='Job Summary: '+job_id, jobs=[j], info=None, office=is_office(), printed=now_local_str())

@app.route('/print/client')
def print_client():
    name=request.args.get('name') or ''
    conn=get_db()
    if not ckey(name) or not (is_office() or tech_has_client(conn, name)): conn.close(); return ('Not allowed',403)
    jobs=client_jobs(conn, name)
    c=conn.execute('SELECT * FROM clients WHERE LOWER(TRIM(name))=?',(ckey(name),)).fetchone(); conn.close()
    info={'address':(c['address'] if c and c['address'] else next((j['address'] for j in jobs if j['address']),'')),
          'phone':(c['phone'] if c and c['phone'] else next((j['phone'] for j in jobs if j['phone']),''))}
    return render_template_string(PRINT_HTML, heading='Client Summary: '+((c['name'] if c else name).strip()), jobs=jobs, info=info, office=is_office(), printed=now_local_str())

ORDER_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>To Order</title>
<style>body{font-family:-apple-system,sans-serif;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:12px}
.job{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin-bottom:10px}.job-id{color:#a49f2a;font-weight:700;font-size:.8rem}.muted{color:#666}
.btn{display:block;background:#f0f0f0;color:#111;padding:14px;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;margin:8px 0}h2{color:#a49f2a;margin:8px 0}a{color:#111}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<h2>To Order</h2><a class="btn" href="/">Back to Jobs</a><div class="muted" style="margin-bottom:8px">Unchecked materials on open jobs ({{ total }} item{{ '' if total == 1 else 's' }})</div>
{% for j in jobs %}<div class="job"><a href="/?job={{ j.id|urlencode }}"><span class="job-id">{{ j.id }}</span> <b>{{ j.title }}</b></a>{% if j.help_open %} <span class="badge" style="background:#b00020;color:#fff;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px">Needs Help</span>{% endif %}<div class="muted">{{ j.customer }} &middot; {{ j.tech or '-' }} &middot; {{ j.status_label }}</div>
<ul style="margin:6px 0 0;padding-left:20px">{% for m in j.need %}<li>{{ m.text }}</li>{% endfor %}</ul></div>{% else %}<div class="job">Nothing to order</div>{% endfor %}
</body></html>'''

@app.route('/to-order')
def to_order():
    if not is_office(): return redirect('/')
    conn=get_db(); rows=conn.execute("SELECT * FROM jobs WHERE status!='completed' ORDER BY COALESCE(scheduled_date, created_at)").fetchall(); conn.close()
    jobs=[]
    for r in rows:
        d=job_dict(r); d['need']=[m for m in d['materials_list'] if not m['used']]
        if d['need']: jobs.append(d)
    return render_template_string(ORDER_HTML, jobs=jobs, total=sum(len(j['need']) for j in jobs))

@app.route('/clients/delete', methods=['POST'])
def delete_client():
    if not is_office(): return ('',403)
    k=ckey(request.form.get('name'))
    if not k: return redirect('/clients')
    conn=get_db()
    for row in conn.execute('SELECT id FROM jobs WHERE LOWER(TRIM(customer))=?',(k,)).fetchall():
        for ph in conn.execute('SELECT filename FROM photos WHERE job_id=?',(row['id'],)).fetchall():
            try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'],secure_filename(ph['filename'])))
            except OSError: pass
        for t in ('photos','job_updates','invoice_drafts'): conn.execute(f'DELETE FROM {t} WHERE job_id=?',(row['id'],))
    conn.execute('DELETE FROM jobs WHERE LOWER(TRIM(customer))=?',(k,))
    conn.execute('DELETE FROM clients WHERE LOWER(TRIM(name))=?',(k,)); conn.commit(); conn.close()
    return redirect('/clients')

@app.route('/clients/add', methods=['POST'])
def add_client():
    if not is_office(): return redirect('/')
    n=(request.form.get('name') or '').strip()
    if n:
        conn=get_db()
        if not conn.execute('SELECT 1 FROM clients WHERE LOWER(TRIM(name))=?',(ckey(n),)).fetchone():
            conn.execute('INSERT INTO clients (name,address,phone,created_at) VALUES (?,?,?,?)',(n,(request.form.get('address') or '').strip(),(request.form.get('phone') or '').strip(),datetime.utcnow().isoformat())); conn.commit()
        conn.close()
    return redirect('/clients')

@app.route('/api/jobs/<job_id>/reassign', methods=['POST'])
def reassign_job(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    tech=((request.get_json(silent=True) or {}).get('tech') or '').strip()
    if not tech: return jsonify({'error':'tech required'}),400
    conn=get_db(); n=conn.execute('UPDATE jobs SET tech=? WHERE id=?',(tech,job_id)).rowcount; conn.commit(); conn.close()
    return (jsonify({'ok':True,'tech':tech}) if n else ('',404))

@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    conn=get_db()
    for p in conn.execute('SELECT filename FROM photos WHERE job_id=?',(job_id,)).fetchall():
        try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'],secure_filename(p['filename'])))
        except OSError: pass
    for t in ('photos','job_updates','invoice_drafts'): conn.execute(f'DELETE FROM {t} WHERE job_id=?',(job_id,))
    n=conn.execute('DELETE FROM jobs WHERE id=?',(job_id,)).rowcount; conn.commit(); conn.close()
    return (jsonify({'ok':True}) if n else ('',404))

@app.route('/uploads/<path:fn>')
def uploads(fn):
    if not is_office():
        conn=get_db(); r=conn.execute('SELECT j.customer FROM photos p JOIN jobs j ON j.id=p.job_id WHERE p.filename=?',(fn,)).fetchone()
        ok=bool(r) and tech_has_client(conn, r['customer']); conn.close()
        if not ok: return ('',403)
    return send_from_directory(app.config['UPLOAD_FOLDER'], secure_filename(fn))

@app.route('/api/invoice-drafts')
def list_invoice_drafts():
    if not is_office(): return jsonify({'error':'office only'}),403
    conn=get_db(); rows=conn.execute('SELECT * FROM invoice_drafts ORDER BY created_at DESC').fetchall(); conn.close()
    return jsonify([dict(json.loads(r['package']),draft_status=r['status'],draft_created_at=r['created_at']) for r in rows])

@app.route('/api/jobs/<job_id>/invoice-draft')
def get_invoice_draft(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    conn=get_db(); r=conn.execute('SELECT * FROM invoice_drafts WHERE job_id=?',(job_id,)).fetchone(); conn.close()
    if not r: return ('',404)
    return jsonify(dict(json.loads(r['package']),draft_status=r['status'],draft_created_at=r['created_at']))

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',5000)))
