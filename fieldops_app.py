from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect
import sqlite3, os, shutil, uuid, hmac, hashlib, json, threading, urllib.request, urllib.parse, math, time
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
SURVEY_TEXT = ['site_conditions', 'want_desc', 'contact_name', 'contact_phone', 'current_system', 'power', 'network', 'conduit', 'obstacle_notes', 'timeline', 'budget', 'decision_maker']
SURVEY_OFFICE_ONLY = ['timeline', 'budget', 'decision_maker']
SURVEY_OBSTACLES = ['Lift needed', 'Concrete', 'Long runs', 'Access issues']

def clean_survey(raw):
    try: d = json.loads(raw) if isinstance(raw, str) and raw else raw
    except Exception: d = None
    d = d if isinstance(d, dict) else {}
    out = {k: str(d.get(k) or '').strip()[:4000] for k in SURVEY_TEXT}
    ob = d.get('obstacles') if isinstance(d.get('obstacles'), list) else []
    out['obstacles'] = [o for o in SURVEY_OBSTACLES if o in ob]
    return out

def legacy_conditions(sv):
    out = [f'{l}: {sv[k]}' for k, l in (('current_system', 'Current system'), ('power', 'Power'), ('network', 'Network'), ('conduit', 'Conduit / pathways')) if sv.get(k)]
    if sv.get('obstacles'): out.append('Obstacles: ' + ', '.join(sv['obstacles']))
    if sv.get('obstacle_notes'): out.append(sv['obstacle_notes'])
    return '\n'.join(out)

def site_conditions(sv): return sv.get('site_conditions') or legacy_conditions(sv)

def notes_log(conn, j):
    lines = [j['notes']] if j['notes'] else []
    lines += [f"{u['created_at']} {u['author']}: {u['note']}" for u in conn.execute('SELECT author,note,created_at FROM job_updates WHERE job_id=? ORDER BY id', (j['id'],)).fetchall()]
    return '\n'.join(lines)

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

STATUSES = [('assigned', 'Open'), ('in_progress', 'In Progress'), ('waiting_parts', 'Waiting on Parts'), ('review', 'Needs Review'), ('completed', 'Complete')]
STATUS_LABEL = dict(STATUSES)
PHOTO_TYPES = ['before', 'during', 'after']
SURVEY_PHOTO_TYPES = ['survey']
DEVICE_TYPES = [('Dome Cam','C','#1565c0'),('Bullet Cam','C','#0d47a1'),('PTZ Cam','C','#5e35b1'),('Card Reader','R','#2e7d32'),('Door Contact','DC','#00897b'),('Motion','M','#f9a825'),('Keypad','K','#6d4c41'),('Panel','P','#424242'),('NVR','N','#37474f'),('Intercom','I','#ad1457'),('WAP','W','#0277bd'),('Data Drop','D','#ef6c00'),('Speaker','S','#7b1fa2'),('TV','TV','#c62828')]
DEV_PREFIX = {n: p for n, p, c in DEVICE_TYPES}
PLAN_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}

def parse_devices(raw):
    try: items = json.loads(raw) if isinstance(raw, str) and raw else raw
    except Exception: items = []
    out = []
    for i in (items if isinstance(items, list) else []):
        if not isinstance(i, dict) or i.get('type') not in DEV_PREFIX: continue
        try: x = min(100.0, max(0.0, float(i.get('x')))); y = min(100.0, max(0.0, float(i.get('y'))))
        except (TypeError, ValueError): continue
        out.append({'id': secure_filename(str(i.get('id') or ''))[:20], 'plan': secure_filename(str(i.get('plan') or '')), 'type': i['type'],
                    'label': str(i.get('label') or '')[:10], 'x': round(x, 2), 'y': round(y, 2), 'note': str(i.get('note') or '').strip()[:500],
                    'height': str(i.get('height') or '').strip()[:50], 'cable': str(i.get('cable') or '').strip()[:100],
                    'installed': bool(i.get('installed')), 'mac': str(i.get('mac') or '').strip()[:60], 'ip': str(i.get('ip') or '').strip()[:45],
                    'port': str(i.get('port') or '').strip()[:40], 'clabel': str(i.get('clabel') or '').strip()[:60], 'photo': secure_filename(str(i.get('photo') or ''))[:100]})
    return out

def number_devices(devs):
    used = {}; todo = []
    for d in devs:
        if not d['id']: d['id'] = uuid.uuid4().hex[:10]
        pre = DEV_PREFIX[d['type']]; lab = d['label']; num = lab[len(pre):]
        if lab.startswith(pre) and num.isdigit() and int(num) not in used.setdefault(pre, set()): used[pre].add(int(num))
        else: todo.append(d)
    for d in todo:
        pre = DEV_PREFIX[d['type']]; n = max(used.setdefault(pre, set()) or {0}) + 1; used[pre].add(n); d['label'] = f'{pre}{n}'
    return devs

def dev_counts(devs):
    c = {}
    for n, p, col in DEVICE_TYPES:
        k = sum(1 for d in devs if d['type'] == n)
        if k: c[n] = k
    return c

def _num(v, lo, hi, dflt):
    try: f = float(v)
    except (TypeError, ValueError): return dflt
    return dflt if f != f else min(hi, max(lo, f))

def feet_between(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(h))) * 3.28084

def parse_map(raw):
    try: d = json.loads(raw) if isinstance(raw, str) and raw else raw
    except Exception: d = None
    d = d if isinstance(d, dict) else {}
    cams = []; used = set()
    for c in (d.get('cameras') if isinstance(d.get('cameras'), list) else [])[:200]:
        if not isinstance(c, dict) or _num(c.get('lat'), -90, 90, None) is None or _num(c.get('lng'), -180, 180, None) is None: continue
        cams.append({'id': secure_filename(str(c.get('id') or ''))[:20] or uuid.uuid4().hex[:10], 'label': str(c.get('label') or '')[:10],
                     'lat': round(_num(c['lat'], -90, 90, 0), 7), 'lng': round(_num(c['lng'], -180, 180, 0), 7), 'dir': round(_num(c.get('dir'), -10000, 10000, 0) % 360),
                     'fov': round(_num(c.get('fov'), 10, 360, 90)), 'range': round(_num(c.get('range'), 5, 2000, 60))})
    for c in cams:
        n = c['label'][1:]
        if c['label'].startswith('C') and n.isdigit() and int(n) not in used: used.add(int(n))
        else: c['label'] = ''
    for c in cams:
        if not c['label']: n = max(used or {0}) + 1; used.add(n); c['label'] = f'C{n}'
    runs = []
    for r in (d.get('runs') if isinstance(d.get('runs'), list) else [])[:100]:
        if not isinstance(r, dict) or not isinstance(r.get('pts'), list): continue
        pts = [[round(_num(q[0], -90, 90, 0), 7), round(_num(q[1], -180, 180, 0), 7)] for q in r['pts'][:200] if isinstance(q, list) and len(q) == 2 and _num(q[0], -90, 90, None) is not None and _num(q[1], -180, 180, None) is not None]
        if len(pts) < 2: continue
        segs = [round(feet_between(pts[i], pts[i + 1]), 1) for i in range(len(pts) - 1)]; slack = round(_num(r.get('slack'), 0, 100, 0), 1); tot = round(sum(segs), 1)
        runs.append({'id': secure_filename(str(r.get('id') or ''))[:20] or uuid.uuid4().hex[:10], 'label': str(r.get('label') or '').strip()[:80], 'pts': pts,
                     'slack': slack, 'segments_ft': segs, 'total_ft': tot, 'adjusted_ft': round(tot * (1 + slack / 100), 1)})
    v = d.get('view') if isinstance(d.get('view'), dict) else {}
    view = {'lat': _num(v.get('lat'), -90, 90, None), 'lng': _num(v.get('lng'), -180, 180, None), 'zoom': _num(v.get('zoom'), 1, 22, 18)}
    g = d.get('geo') if isinstance(d.get('geo'), dict) else None
    return {'cameras': cams, 'runs': runs, 'view': view if view['lat'] is not None and view['lng'] is not None else None, 'geo': g}

def compass(deg): return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][int(round(deg / 45.0)) % 8]

app.jinja_env.globals.update(compass=compass, DEV_COLORS={n: c for n, p, c in DEVICE_TYPES}, dev_counts=dev_counts)
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
    d['survey']['conditions'] = site_conditions(d['survey'])
    d['survey']['contact_tel'] = ''.join(c for c in d['survey']['contact_phone'] if c.isdigit() or c == '+')
    if not is_office():
        d.pop('survey_office', None)
        for k in SURVEY_OFFICE_ONLY: d['survey'].pop(k, None)
    else: d['survey_office'] = d.get('survey_office') or ''
    d['punch_list'] = parse_ticks(d.get('punch_list')); d['quick_notes'] = d.get('quick_notes') or ''
    d['help_open'] = d.get('help_flag') == 'open'
    d['devices'] = parse_devices(d.pop('survey_devices', None))
    d['map'] = parse_map(d.pop('map_data', None))
    if not is_office():
        for k in ('private_notes','hub_nonce','hub_created_at','hub_approved_at','hub_approved_name','labor_rates','review_by','review_at'): d.pop(k, None)
    return d

def me():
    n = (session.get('name') or '').strip()
    return n if n.lower() in USERS else ''
def is_office(): return me().lower() in OFFICE_NAMES
def can_see(job): return is_office() or (job['tech'] or '').strip().lower() == me().lower()

LOGIN_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Field Ops"><title>True Sense Field</title>
<style>body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:420px;margin:auto;padding:24px}
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
    if request.path in ('/login', '/healthz') or request.path.startswith('/help-photo/') or request.path.startswith('/hub/') or request.path=='/api/tech-support/reply': return
    if not USERS:
        return (render_template_string(LOGIN_HTML, setup=True), 503) if not request.path.startswith('/api') else (jsonify({'error':'Add user passwords in Render'}), 503)
    if not me():
        return (jsonify({'error':'login required'}), 401) if request.path.startswith(('/api','/uploads')) else redirect('/login')
    if not is_office() and request.path not in ('/agreement', '/agreement/sign', '/logout') and not request.path.startswith('/agreement/copy/'):
        conn=get_db(); blocked=agreement_required(conn) and not signed_current(conn, me()); conn.close()
        if blocked: return (jsonify({'error':'Sign the Subcontractor Agreement first'}), 403) if request.path.startswith(('/api','/uploads')) else redirect('/agreement')

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
AGREEMENT_V12 = '''TRUE SENSE SECURITY
SUBCONTRACTOR AGREEMENT
Low-Voltage and Security Systems Work

Version 1.2 - Draft dated November 15, 2026

====================================================================
DRAFT FOR ATTORNEY REVIEW - NOT LEGAL ADVICE

This draft is not legal advice and is not ready for use until a
licensed Texas attorney has reviewed and approved it.
====================================================================


1. PARTIES

This Agreement is between True Sense Security,
[legal entity name: __________], Texas DPS Private Security
license B31163401, Houston, Texas, 713-364-8409,
info@truesensesecurity.com ("Company"),

and the independent contractor below ("Subcontractor"):

Legal name: [____________________]    DBA: [____________________]
Entity type: [ ] LLC  [ ] Corporation  [ ] Sole proprietor
             [ ] Other: [__________]
Address: [____________________]
Phone: [__________]    Email: [____________________]
Tax ID (as on Form W-9): [____________________]

For insurance, confidentiality, non-solicitation, and indemnity,
"Company" also includes its owners, officers, and employees.
The Effective Date is acceptance of this version in the Field
Ops app.


2. INDEPENDENT CONTRACTOR

Subcontractor is an independent contractor, not an employee,
partner, or agent of Company. Subcontractor controls how
the work is done, supplies its own tools and vehicle unless a
work order says otherwise, may accept or decline work orders, and
may work for others. Subcontractor pays its own taxes, including
self-employment tax, and is not eligible for Company benefits.
Company will issue Form 1099-NEC as required. Subcontractor
cannot bind Company.


3. WORK ORDERS AND PAY

Company may offer installs, service calls, and site surveys
(cameras, access control, intercom, data cabling, fiber, and AV)
through the Field Ops app. Each work order states the scope,
schedule, and rate (fixed price or hourly). Subcontractor may
accept or decline any work order. Accepting it accepts that rate
and the duty to finish the job. Extra work needs a change
approved in the app before it is done.

Payment is due net [15] days after Company approves a completed
work order with the required documentation. Hourly pay covers
only authorized hours actually worked. Company may withhold
amounts allowed by law for incomplete or defective work, missing
documents, unreturned materials, or damage caused by
Subcontractor.


4. INSURANCE AND W-9

Before the first job, Subcontractor will carry:

(a) Commercial general liability insurance of at least $1,000,000
per occurrence and $2,000,000 general aggregate, including
completed operations;

(b) Automobile liability insurance covering owned, hired, and
non-owned vehicles used in the work, with a combined single limit
of at least $[1,000,000] per accident; and

(c) Workers' compensation at statutory limits if required by law
or if Subcontractor has employees; or, if not required, an
occupational accident policy with limits of at least
$[__________], or a signed workers' compensation waiver.

A Certificate of Insurance and a completed Form W-9 must be on
file before the first job and before any payment, and coverage
must stay in force through any warranty period.


5. LICENSING

Subcontractor and anyone it sends to a job will hold all licenses
and registrations required for the work. For work regulated under
Texas Occupations Code Chapter 1702, this includes the required
Texas DPS Private Security Bureau license and individual
registration (such as alarm installer). Required TDLR or other
licenses, and any city permit assigned to Subcontractor, must be
in place before that work begins.


6. DOCUMENTATION, WARRANTY, AND CALLBACKS

A work order is not complete, and payment is not due, until
Subcontractor submits the required documentation in the Field Ops
app, including photos of the completed work, as-builts or device
locations, and any serial numbers or test results the work order
requires.

Subcontractor will do the work in a good and workmanlike manner
and warrants its workmanship for [1 year] after approval. During
that period, Subcontractor will correct its defective work,
including labor, at no charge. This does not cover manufacturer
defects, abuse, work by others, or later changes.


7. CONFIDENTIALITY

Subcontractor may learn client and Company confidential
information, including site details, system designs, camera
locations, access codes, passwords, and network information.
Subcontractor will use it only to do the work, share it only with
workers who need it, and return or destroy it when the work ends
or Company asks. Subcontractor will not post photos or details of
client sites or systems on social media or elsewhere without
Company's written consent. These duties survive this Agreement
and, for codes, credentials, network information, and camera
locations, last as long as the information could affect a
client's security.


8. NON-SOLICITATION OF CLIENTS

During this Agreement and for 12 months after it ends,
Subcontractor will not solicit any client that Subcontractor was
introduced to through Company, or served under this Agreement,
for the same or substantially similar services. This does not
prohibit general advertising, or work for a client who approaches
Subcontractor without that solicitation. If a court finds this
limit too broad, the parties ask that it be narrowed to the
maximum enforceable scope under Texas law.


9. INDEMNITY

TO THE EXTENT PERMITTED BY TEXAS LAW, INCLUDING INSURANCE CODE
CHAPTER 151 WHERE IT APPLIES, SUBCONTRACTOR SHALL DEFEND,
INDEMNIFY, AND HOLD HARMLESS COMPANY AND ITS OWNERS,
OFFICERS, AND EMPLOYEES FROM THIRD-PARTY CLAIMS, DAMAGES, LOSSES,
AND REASONABLE ATTORNEYS' FEES TO THE EXTENT CAUSED BY THE
NEGLIGENCE, WILLFUL MISCONDUCT, OR BREACH OF THIS AGREEMENT BY
SUBCONTRACTOR OR ITS WORKERS. THIS DOES NOT REQUIRE SUBCONTRACTOR
TO INDEMNIFY ANYONE FOR THAT PERSON'S OWN NEGLIGENCE OR WILLFUL
MISCONDUCT. WHERE MORE THAN ONE PERSON IS AT FAULT, SUBCONTRACTOR
IS RESPONSIBLE ONLY FOR ITS PROPORTIONATE SHARE OF FAULT.


10. TERMINATION

Either party may end this Agreement for convenience with [14]
days' written notice. Company may also end a work order for
convenience by notice in the app. Subcontractor will then be paid
for work properly completed through the end date, less amounts
withheld as allowed above. Either party may end this Agreement or
a work order if the other materially breaches and does not cure
within [7] days, or immediately for a safety, security, or
licensing risk. When work ends, Subcontractor will stop as
directed, leave the site safe, and return Company and customer
property, unused materials, keys, badges, and documentation. Sections 2, 4 (as to amounts owed), and 6 through
11 survive.


11. TEXAS LAW AND E-SIGN

This Agreement is governed by Texas law. Exclusive venue is
Harris County, Texas, or the federal court in Houston. The
parties consent to electronic signatures and records under the
Texas Uniform Electronic Transactions Act (Business and Commerce
Code Chapter 322) and the federal E-SIGN Act. Signing in the
Field Ops app means typing a legal name, checking "I agree," and
applying a finger or stylus signature, recorded with the version
and the date and time. That record is as valid as a signed paper
original. This is the entire agreement, together with accepted
work orders. A new version binds Subcontractor only after
acceptance in the app. Already-accepted work orders stay under
the version then in effect. If any part is unenforceable, the
rest remains in effect.


12. SIGNATURE AND ACCEPTANCE

By signing, each party agrees to this Agreement, Version 1.2 -
Draft dated November 15, 2026.

SUBCONTRACTOR

    Typed legal name: [________________________________]

    [ ] I AGREE

    Finger or stylus signature: [sign on device]

    Date and time (Field Ops app): [__________]

    Version accepted: 1.2

    Title (if signing for an entity): [__________]


COMPANY SIGNER

    Company: True Sense Security

    Typed name: [________________________________]

    Title: [__________]

    [ ] I AGREE

    Signature (finger, stylus, or other electronic signature):
    [sign on device]

    Date and time (Field Ops app): [__________]

    Version accepted: 1.2
'''

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
    for col in ['signature TEXT', 'materials TEXT', 'phone TEXT', 'checklist TEXT', 'scheduled_date TEXT', 'created_by TEXT', 'materials_list TEXT', 'private_notes TEXT', 'job_type TEXT', 'survey_list TEXT', 'quick_notes TEXT', 'punch_list TEXT', 'survey_form TEXT', 'survey_office TEXT', 'help_flag TEXT', 'help_note TEXT', 'help_at TEXT', 'help_by TEXT', 'help_photo TEXT', 'kelly_at TEXT', 'kelly_by TEXT', 'survey_devices TEXT', 'map_data TEXT', 'hub_approved_at TEXT', 'hub_approved_name TEXT', 'hub_created_at TEXT', 'hub_nonce TEXT', 'review_by TEXT', 'review_at TEXT', 'labor_rates TEXT']:
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
    conn.execute('''CREATE TABLE IF NOT EXISTS followups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, due TEXT, note TEXT, done INTEGER DEFAULT 0, created_by TEXT, created_at TEXT, done_by TEXT, done_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS msg_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, kind TEXT, phone TEXT, body TEXT, status TEXT DEFAULT 'pending', ref_date TEXT, created_by TEXT, created_at TEXT, approved_by TEXT, approved_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS subs (
        key TEXT PRIMARY KEY, company TEXT, updated_at TEXT)''')
    for col in ['rate_hourly REAL', 'rate_flat REAL']:
        try: conn.execute(f'ALTER TABLE subs ADD COLUMN {col}')
        except: pass
    conn.execute('''CREATE TABLE IF NOT EXISTS agreements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT, body TEXT, created_at TEXT, created_by TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS agreement_signs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sub TEXT, agreement_id INTEGER, version TEXT, typed_name TEXT, signature TEXT, signed_at TEXT, ip TEXT, user_agent TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    if not conn.execute('SELECT 1 FROM agreements').fetchone():
        conn.execute('INSERT INTO agreements (version,body,created_at,created_by) VALUES (?,?,?,?)',('1.2',AGREEMENT_V12,now_local_str(),'Seed'))
    conn.execute('''CREATE TABLE IF NOT EXISTS sub_docs (
        sub TEXT, kind TEXT, filename TEXT, orig_name TEXT, expires TEXT, uploaded_at TEXT, PRIMARY KEY (sub, kind))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS job_hours (
        job_id TEXT, sub TEXT, minutes INTEGER DEFAULT 0, PRIMARY KEY (job_id, sub))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS checklist_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, job_type TEXT, items TEXT, created_at TEXT)''')
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
*,*::before,*::after{box-sizing:border-box}
.job{overflow-wrap:anywhere}
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
body.office{max-width:1100px}body.office #detail{max-width:480px;margin:auto}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
.tile{background:#fff;border:2px solid var(--border);border-radius:12px;padding:12px 8px;text-align:center;font-size:.95rem;font-weight:600;color:var(--text)}
.tile b{display:block;font-size:2rem;color:var(--accent)}.tile.on{border-color:var(--accent);background:#fbfaf0}.tile.red{border-color:#b00020}.tile.red b{color:#b00020}
.menu{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}.menu .big-btn{margin:0;padding:18px 8px;box-sizing:border-box;font-family:inherit}
.tech-h{font-weight:700;font-size:1.1rem;margin:18px 0 6px;padding-bottom:4px;border-bottom:2px solid var(--accent)}
@media(min-width:900px){.dash{display:flex;gap:20px;align-items:flex-start}.menu{display:flex;flex-direction:column;flex:0 0 210px;position:sticky;top:12px}.dash main{flex:1;min-width:0}.tiles{grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}}
.pal{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin:8px 0}.pal button{padding:12px 4px;border:2px solid var(--border);border-radius:10px;background:#fff;font-size:.95rem;font-weight:600;color:var(--text)}.pal button.on{border-color:var(--text);background:#fbfaf0}
.pal i,.pin{display:inline-block;color:#fff;font-style:normal;font-weight:700;border-radius:16px;text-align:center}.pal i{min-width:22px;padding:0 3px;line-height:22px;font-size:.75rem;margin-right:4px}
.plan{position:relative;margin:8px 0;border:1px solid var(--border);border-radius:8px;overflow:hidden}.plan img{width:100%;height:auto;display:block}
.pin{position:absolute;transform:translate(-50%,-50%);min-width:32px;padding:0 4px;height:32px;line-height:28px;font-size:.85rem;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.5);box-sizing:border-box}.pin.sel{outline:3px solid #111}
#mp{position:fixed;inset:0;background:#fff;z-index:40;overflow:auto;padding:10px}#mp.hidden{display:none}#mp-map{height:55vh;border-radius:10px}
.mp-tools{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}.mp-tools button,.mp-b button{padding:14px 4px;border:2px solid var(--border);border-radius:10px;background:#fff;font-weight:700;font-size:1rem;color:var(--text)}.mp-tools button.on{border-color:var(--text);background:#fbfaf0}
.mp-b{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:6px 0}.mp-addr{display:inline-block;background:#111;color:#fff;font-weight:700;font-size:12px;padding:3px 6px;border-radius:6px;white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis;transform:translate(-50%,-100%);position:relative;top:-6px}.mp-addr:after{content:'';position:absolute;left:50%;bottom:-6px;margin-left:-6px;border:6px solid transparent;border-bottom:0;border-top-color:#111}
.mp-seg{background:rgba(0,0,0,.7);color:#fff;font-weight:700;font-size:12px;padding:2px 5px;border-radius:6px;white-space:nowrap}
#sk{position:fixed;inset:0;background:#fff;z-index:50;overflow:auto;padding:10px}#sk.hidden{display:none}
.sk-tools{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-bottom:6px}.sk-tools button{padding:14px 2px;border:2px solid var(--border);border-radius:10px;background:#fff;font-weight:700;font-size:.95rem;color:var(--text)}.sk-tools button.on{border-color:var(--text);background:#fbfaf0}

canvas{touch-action:none;-webkit-user-select:none;user-select:none;border:2px dashed var(--border);border-radius:10px;background:#fafafa;width:100%;height:160px}
</style></head><body{% if office %} class="office"{% endif %}>
<div class="header">
  <img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px; margin:0 auto 4px; display:block;">
  <div class="sub">Field Ops</div>
  <div style="font-size:.85rem;color:#666;margin-top:4px">{{ name }} &middot; <a href="/logout" style="color:#666" onclick="return confirm('Log out?')">Log out</a></div>
</div>
<div id="home"{% if office %} class="dash"{% endif %}>
{% if office %}<nav class="menu"><button class="big-btn" onclick="toggleNew()">+ New Job</button><a href="/clients" class="big-btn">Clients &amp; Surveys</a><a href="/schedule" class="big-btn gray">Schedule</a><a href="/calendar" class="big-btn gray">Calendar</a><a href="/to-order" class="big-btn gray">To Order</a><a href="/techs" class="big-btn gray">Subs</a><a href="/checklists" class="big-btn gray">Checklists</a><a href="/agreement" class="big-btn gray">Agreement</a></nav><main>
<div class="tiles"><button class="tile" data-f="today" onclick="setF(this.dataset.f)"><b id="t-today">0</b>Today</button><button class="tile" data-f="progress" onclick="setF(this.dataset.f)"><b id="t-progress">0</b>In Progress</button><button class="tile" data-f="review" onclick="setF(this.dataset.f)"><b id="t-review">0</b>Needs Review</button><button class="tile" data-f="help" onclick="setF(this.dataset.f)"><b id="t-help">0</b>Needs Help</button><button class="tile" data-f="invoice" onclick="setF(this.dataset.f)"><b id="t-invoice">0</b>Ready to Invoice</button><button class="tile" data-f="follow" onclick="setF(this.dataset.f)"><b id="t-follow">0</b>Follow-ups Due</button><button class="tile" data-f="texts" onclick="setF(this.dataset.f)"><b id="t-texts">0</b>Texts to Review</button><button class="tile" id="tile-docs" onclick="location.href='/techs'"><b id="t-docs">0</b>Sub Docs Due</button></div>{% endif %}
{% if not office and agr %}{% if agr.signed %}<a href="/agreement" style="display:block;text-align:center;color:#666;margin:6px 0">My signed Subcontractor Agreement (v{{ agr.version }})</a>{% else %}<a href="/agreement" class="big-btn" style="display:block;text-align:center;text-decoration:none;box-sizing:border-box;margin:8px 0">Please sign the Subcontractor Agreement (v{{ agr.version }})</a>{% endif %}{% endif %}
<div id="search-box" class="row" style="align-items:center"><input type="search" id="q" placeholder="Search client, address, or job ID" onkeydown="if(event.key==='Enter')load()"><button class="big-btn gray" style="flex:0 0 90px" onclick="load()">Search</button></div>
{% if office %}<div id="office" class="job hidden"><div style="font-weight:600;margin-bottom:4px">Add New Job</div>
<input type="text" id="n-id" placeholder="Job ID (e.g. TS-2026-0144)">
<input type="text" id="n-title" placeholder="Title">
<input type="text" id="n-customer" placeholder="Customer">
<input type="text" id="n-address" placeholder="Address">
<input type="tel" id="n-phone" placeholder="Customer phone">
<select id="n-tech" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff">{% for v,l in techs %}<option value="{{ v }}">{{ l }}</option>{% else %}<option value="">No subs set in Render</option>{% endfor %}</select>
<select id="n-type" onchange="tplOpts()" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff">{% for t in job_types %}<option value="{{ t }}">{{ t }}</option>{% endfor %}</select>
<select id="n-tpl" onchange="tplPick()" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff"></select>
<textarea id="n-scope" placeholder="Scope" rows="3"></textarea>
<div style="font-size:.85rem;color:#666">Scheduled date</div><input type="date" id="n-date">
<textarea id="n-checklist" placeholder="Checklist, one item per line (optional)" rows="3"></textarea>
<button class="big-btn" onclick="addJob()">Add Job</button><button class="big-btn gray" onclick="toggleNew()">Cancel</button></div>{% else %}<div id="office" class="hidden"></div>{% endif %}
<div id="list"></div>
{% if office %}</main>{% endif %}</div>
<div id="detail" class="hidden">
<button class="big-btn gray" style="font-size:1.3rem;margin-bottom:8px" onclick="showList()">&larr; Back</button>
<div class="job"><div class="job-id" id="d-id"></div><div id="d-title" style="font-weight:600;font-size:1.15rem"></div><div id="d-customer" style="color:#666"></div><div id="d-address" style="color:#666"></div><div id="d-scope" style="white-space:pre-wrap;margin-top:8px"></div><div id="d-created" style="color:#666;font-size:.85rem;margin-top:6px"></div></div>
<div id="sec-help">{% if office %}<div id="help-banner" class="job hidden" style="border:2px solid #b00020;background:#fff5f5"><div style="color:#b00020;font-weight:700;font-size:1.1rem">&#9888; Needs Help</div>
<div id="help-meta" class="muted" style="font-size:.85rem"></div><div id="help-text" style="white-space:pre-wrap;margin:6px 0"></div><div id="help-img"></div>
</div>{% endif %}
<button class="big-btn" style="background:#b00020;font-size:1.5rem;padding:22px" onclick="document.getElementById('help-form').classList.toggle('hidden');document.getElementById('help-note').focus()">Need Help</button>
<div style="text-align:center;font-size:1.1rem;font-weight:600;margin:4px 0 10px">Pull down to see the answer.</div>
<div id="help-form" class="job hidden"><textarea id="help-note" rows="4" style="font-size:1.25rem" autocapitalize="sentences" autocorrect="on" spellcheck="true" enterkeyhint="send" placeholder="Say or type what you need help with"></textarea>
<button class="big-btn" style="background:#b00020;font-size:1.3rem" onclick="sendHelp()">Send</button></div>
<div class="job" style="border:2px solid #1565c0;overflow-wrap:anywhere;word-break:break-word;min-width:0;max-width:100%;box-sizing:border-box"><div style="font-weight:800;font-size:1.5rem;color:#1565c0">Tech Support</div>
<div id="support-open"><textarea id="support-note" rows="3" style="font-size:1.25rem" autocapitalize="sentences" autocorrect="on" spellcheck="true" enterkeyhint="send" placeholder="Say or type a follow-up"></textarea>
<button class="big-btn" style="background:#1565c0;font-size:1.3rem" onclick="sendSupport()">Send Follow-up</button>
<button class="big-btn" style="background:#2e7d32;font-size:1.3rem" onclick="resolveSupport()">Issue Resolved</button></div><div id="support"></div></div></div>
<div id="sec-links"><a id="directions" class="big-btn" target="_blank">Directions</a>
<a id="call" class="big-btn gray">Call Customer</a>
<a id="history" class="big-btn gray">Client History</a>
<a id="print" class="big-btn gray" target="_blank">Print Summary</a></div>
<div id="sec-type"><div class="job"><div style="font-weight:600">Job Type</div>{% if office %}<select id="jtype" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff" onchange="setType(this.value)"><option value="">(not set)</option>{% for t in job_types %}<option value="{{ t }}">{{ t }}</option>{% endfor %}</select>{% else %}<div id="jtype-label" style="margin:6px 0"></div>{% endif %}</div></div>
<div id="sec-survey"><div class="job" id="survey-card"><div style="font-weight:800;font-size:1.4rem">Site Survey for Estimate</div>
<div class="ptitle" style="font-size:1.2rem">1. Photos</div><div class="big-btn" id="sv-add" onclick="document.getElementById('survey').click()">Add Photos</div>
<input type="file" id="survey" accept="image/*" multiple capture="environment" style="display:none"><div id="survey-photos" class="thumbs"></div>
<div class="ptitle" style="font-size:1.2rem">2. What They Want</div><textarea id="sv-want_desc" rows="3" style="font-size:1.1rem" placeholder="Short description of what the client wants (tap the mic to talk)"></textarea>
<div class="ptitle" style="font-size:1.2rem">3. Site Conditions</div><textarea id="sv-site_conditions" rows="5" style="font-size:1.1rem" placeholder="Current system, power, network, conduit, and obstacles like lift, concrete, long runs, access (tap the mic to talk)"></textarea>
<div id="sv-slot"><div id="markup"><div class="ptitle" style="font-size:1.2rem" id="mk-title">5. Floor Plan Devices</div><div id="ab-copy"></div><div class="row" id="plan-add"><div class="big-btn" onclick="document.getElementById('plan-file').click()">Add Floor Plan or Photo</div><div class="big-btn gray" onclick="skOpen()">Draw Plan</div></div>
<input type="file" id="plan-file" accept="image/*" multiple style="display:none" onchange="uploadPlans(this)">
<div id="dev-palette" class="pal"></div><div id="dev-hint" style="font-weight:700;font-size:1.1rem;text-align:center;margin:6px 0"></div>
<div id="plans"></div>
<div id="dev-edit" class="job hidden" style="border:2px solid var(--accent)"><div id="dv-title" style="font-weight:800;font-size:1.3rem"></div>
<label class="chk ab-only" style="font-size:1.3rem;font-weight:700"><input type="checkbox" id="dv-installed"><span>Installed</span></label>
<input type="text" id="dv-note" placeholder="Note (optional)"><input type="text" id="dv-height" placeholder="Mount height (optional)"><input type="text" id="dv-cable" placeholder="Cable run (optional)">
<div class="ab-only"><input type="text" id="dv-mac" placeholder="MAC / serial (optional)"><input type="text" id="dv-ip" placeholder="IP address (optional)"><input type="text" id="dv-port" placeholder="Switch port (optional)"><input type="text" id="dv-clabel" placeholder="Cable label (optional)">
<div id="dv-photo"></div><div class="big-btn gray" id="dv-photo-btn" onclick="document.getElementById('dv-photo-file').click()">Add Device Photo</div><input type="file" id="dv-photo-file" accept="image/*" capture="environment" style="display:none" onchange="devPhoto(this)"></div>
<div id="dv-btns"><button class="big-btn" onclick="devSave()">Save</button><div class="row"><button class="big-btn gray" onclick="devMoveStart()">Move</button><button class="big-btn" style="background:#b00020" onclick="devDelete()">Delete</button></div></div>
<button class="big-btn gray" onclick="devSel=null;renderDevices()">Close</button></div>
<div id="dev-counts"></div></div></div><div id="sv-mapslot"></div>
<div class="ptitle" style="font-size:1.2rem">5. Site Contact</div><input type="text" id="sv-contact_name" placeholder="Contact name"><input type="tel" id="sv-contact_phone" placeholder="Contact phone"><a id="sv-call" class="big-btn gray" style="display:none">Call Contact</a>
{% if office %}<div id="sv-expect" class="muted" style="margin:6px 0"></div><div class="ptitle">Rough Materials &amp; Labor (office only)</div><textarea id="sv-office" rows="3" placeholder="Rough materials and labor notes"></textarea>{% endif %}
<button class="big-btn hidden" id="sv-save" onclick="saveSurvey()">Save Changes</button>
{% if office %}<button id="sv-edit" class="big-btn hidden" style="background:#6a1b9a;font-size:1.3rem" onclick="svEditing=true;renderKelly()">Edit Survey</button><button id="sv-tojob" class="big-btn green hidden" style="font-size:1.3rem" onclick="surveyToJob()">Create Job from Survey</button>{% endif %}
{% if office %}<div id="kelly-box"><div id="kelly-note" style="font-weight:700;font-size:1.1rem;color:#6a1b9a;text-align:center"></div></div>{% endif %}</div></div>
<div id="sec-review">{% if office %}<div id="review-box" class="job hidden" style="border:2px solid #e69500;background:#fffaf0"><div style="font-weight:700;font-size:1.2rem;color:#b26a00">Needs Review</div>
<div id="review-info" style="margin:4px 0"></div><div class="muted">Check the photos, as-built and notes on this job. Approve makes the invoice draft. Nothing is sent.</div>
<button class="big-btn green" style="font-size:1.2rem;padding:18px" onclick="approveJob()">Approve</button></div>{% endif %}</div>
<div id="sec-omw"><button class="big-btn green" style="font-size:1.3rem;padding:20px" onclick="onMyWay()">&#128663; On My Way</button><div class="muted" style="font-size:.9rem;text-align:center">Makes a text for the office to check. Nothing goes to the client until the office sends it.</div></div>
<div id="sec-status"><div class="job"><div style="font-weight:600">Status</div><select id="status" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff" onchange="setStatus(this.value)">{% for v,l in statuses %}<option value="{{ v }}">{{ l }}</option>{% endfor %}</select></div></div>
<div id="sec-log" class="job"><div style="font-weight:700;font-size:1.15rem;margin-bottom:6px" id="log-title">Notes &amp; Updates</div>
<textarea id="update-note" rows="3" placeholder="Say or type a note (tap the mic to talk)"></textarea><button class="big-btn" id="log-add" onclick="saveUpdate()">Add Note</button>
<div id="updates"></div><div id="log-legacy"></div></div>
<div id="sec-photos"><div class="job"><div style="font-weight:600;margin-bottom:6px">Photos</div>
<div class="row"><div class="big-btn" onclick="document.getElementById('before').click()">Before</div><div class="big-btn" onclick="document.getElementById('during').click()">During</div><div class="big-btn" onclick="document.getElementById('after').click()">After</div></div>
<input type="file" id="before" accept="image/*" multiple capture="environment" style="display:none">
<input type="file" id="during" accept="image/*" multiple capture="environment" style="display:none">
<input type="file" id="after" accept="image/*" multiple capture="environment" style="display:none">
<div id="photos"></div></div></div>
<div id="sec-mat"><div class="job"><div style="font-weight:600;margin-bottom:6px" id="mat-title">Materials</div><div id="mat-legacy" class="muted" style="white-space:pre-wrap"></div><div id="matlist"></div>
<div class="row" style="align-items:center"><input type="text" id="mat-new" placeholder="Add material (e.g. 2x Maglock)" onkeydown="if(event.key==='Enter')addMat()"><button class="big-btn" style="flex:0 0 70px" onclick="addMat()">Add</button></div></div></div>
<div id="sec-time"><div class="job"><div style="font-weight:600">Time</div><div id="totals" style="font-size:1.1rem;margin:6px 0"></div>
<input type="number" id="hours" placeholder="Hours" step="0.25" inputmode="decimal">
<div class="row"><button class="big-btn" onclick="saveTime('add')">Add</button><button class="big-btn gray" onclick="saveTime('set')">Set Totals</button></div>{% if office %}<div style="margin-top:12px;border-top:1px solid var(--border);padding-top:8px"><div style="font-weight:600">Labor cost (office only)</div><div id="labor"></div></div>{% endif %}</div></div>
<div id="sec-check"><div class="job"><div style="font-weight:600;margin-bottom:6px">Checklist</div><div id="checklist"></div>
<div id="punch-wrap"><div style="font-weight:600;margin:12px 0 4px">Punch items</div><div class="muted" style="font-size:.85rem">Office and assigned sub only. Not on client printouts.</div><div id="punch"></div>
<div class="row" style="align-items:center"><input type="text" id="punch-new" placeholder="Add punch item" onkeydown="if(event.key==='Enter')addPunch()"><button class="big-btn" style="flex:0 0 70px" onclick="addPunch()">Add</button></div></div></div></div>
<div id="sec-ab"><div class="job" id="ab-card"><div style="font-weight:800;font-size:1.4rem">As-Built</div><div id="ab-slot"></div>
<div class="ptitle" style="font-size:1.1rem;margin-top:14px">Blueprints</div><div id="bp-list"></div>
<div class="big-btn gray" onclick="document.getElementById('bp-file').click()">Upload Blueprint (PDF or image)</div>
<input type="file" id="bp-file" accept=".pdf,.png,.jpg,.jpeg,.webp,.heic,application/pdf,image/*" multiple style="display:none"></div></div>
<div id="sec-map"><button class="big-btn gray" onclick="mapOpen()">Site Map <span id="map-sum" style="font-weight:400;font-size:.9rem"></span></button></div>
<div id="sec-follow">{% if office %}<div class="job"><div style="font-weight:600">Follow-ups (office only)</div><div id="fu-list"></div>
<div style="font-size:.85rem;color:#666;margin-top:8px">Remind me on</div><input type="date" id="fu-date"><input type="text" id="fu-note" placeholder="What to follow up on (e.g. call about estimate)"><button class="big-btn" onclick="addFollow()">Add Follow-up</button></div>{% endif %}</div>
<div id="sec-hub">{% if office %}<div class="job"><div style="font-weight:600">Client Link (office only)</div><div class="muted" style="font-size:.9rem">Anyone with this link can see this job's summary, plan, photos and signature. Copy it and send it yourself.</div>
<input type="text" id="hub-url" readonly onclick="this.select()"><div id="hub-exp" style="font-weight:600"></div><div class="row"><button class="big-btn gray" onclick="copyHub()">Copy Link</button><button class="big-btn gray" onclick="newHub()">New Link</button></div><div id="hub-appr" style="font-weight:700;font-size:1.1rem;color:#2e7d32"></div></div>{% endif %}</div>
<div id="sec-private">{% if office %}<div class="job"><div style="font-weight:600">Private Office Notes</div><div class="muted" style="font-size:.85rem">Only office can see these</div>
<textarea id="private-notes" rows="3" placeholder="Private notes"></textarea><button class="big-btn gray" onclick="savePrivate()">Save Private Notes</button></div>{% endif %}</div>
<div id="sec-sig"><div class="job"><div style="font-weight:600">Customer Signature</div><canvas id="sig"></canvas>
<button class="big-btn gray" onclick="clearSig()" style="padding:12px">Clear Signature</button></div></div>
<div id="sec-finish"><button class="big-btn green" id="finish">Finish Job</button></div>
<div id="sec-more"><button class="big-btn gray" id="more-btn" onclick="toggleMore()">Show more</button><div id="more-box" class="hidden"></div></div>
<div id="sec-admin">{% if office %}<select id="r-tech" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff">{% for v,l in techs %}<option value="{{ v }}">{{ l }}</option>{% endfor %}</select>
<div class="row"><button class="big-btn gray" onclick="reassignJob()">Reassign Sub</button><button class="big-btn gray" onclick="assignMe()">Assign to Me</button></div>
<button class="big-btn gray" onclick="copyJob()">Copy Job</button>
<button class="big-btn" style="background:#b00020" onclick="deleteJob()">Delete Job</button>{% endif %}
<button class="big-btn gray" onclick="showList()">Back</button></div>
<div id="mp" class="hidden"><div class="mp-tools" id="mp-tools"><button data-m="cam" onclick="mapMode(this.dataset.m)">+ Camera</button><button data-m="measure" onclick="mapMode(this.dataset.m)">Measure</button><button onclick="mapClose()">Done</button></div>
<div class="row" id="mp-find" style="align-items:center"><input type="search" id="mp-q" placeholder="Job address" onkeydown="if(event.key==='Enter')mapFind()"><button class="big-btn gray" style="flex:0 0 80px" onclick="mapFind()">Find</button></div>
<div class="mp-b"><button onclick="meCenter()" style="background:#1565c0;color:#fff;border-color:#1565c0">My Location</button><button id="mp-usesite" class="hidden" onclick="useMeSite()">Use My Location as Site</button></div>
<div id="mp-hint" style="font-weight:700;text-align:center;margin:4px 0"></div><div id="mp-loc" class="muted" style="text-align:center"></div><div id="mp-map"></div><div id="mp-panel"></div><div id="mp-list"></div>
<button class="big-btn gray" onclick="mapClose()">Close Map</button></div>
<div id="sk" class="hidden"><div style="font-weight:800;font-size:1.3rem;text-align:center;margin-bottom:6px">Draw Plan</div>
<div class="sk-tools"><button data-t="pen" onclick="skTool(this.dataset.t)">Draw</button><button data-t="line" onclick="skTool(this.dataset.t)">Line</button><button data-t="erase" onclick="skTool(this.dataset.t)">Eraser</button><button data-t="text" onclick="skTool(this.dataset.t)">Room Label</button><button onclick="skUndo()">Undo</button></div>
<canvas id="sk-c" width="1200" height="900" style="height:auto;max-height:70vh;object-fit:contain;border:2px solid #ccc;border-style:solid"></canvas>
<div id="sk-hint" class="muted" style="text-align:center;font-size:1.05rem"></div>
<div class="row"><button class="big-btn gray" onclick="skClose()">Cancel</button><button class="big-btn green" onclick="skSave()">Save Plan</button></div></div>
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
const OFFICE={{ office|tojson }};let F='';
const card=j=>`<div class="job"><div class="job-id">${esc(j.id)}${j.help_open?'<span class="badge" style="background:#b00020;color:#fff;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px">Needs Help</span>':''}<span class="badge">${esc(j.status_label)}</span>${j.job_type?'<span class="badge">'+esc(j.job_type)+'</span>':''}</div><div style="font-weight:600">${esc(j.title)}</div><div style="color:#666">${esc(j.customer)}</div><div style="color:#666">${esc(j.address)}</div>${j.company?'<div style="color:#666">Sub: '+esc(j.tech)+' ('+esc(j.company)+')</div>':''}<button class="big-btn" data-id="${esc(j.id)}" onclick="openJob(this.dataset.id)">Open Job</button></div>`;
const dcard=j=>`<div class="job" style="cursor:pointer" data-id="${esc(j.id)}" onclick="openJob(this.dataset.id)"><div class="job-id" style="font-size:1rem">${esc(j.id)}${j.help_open?'<span class="badge" style="background:#b00020;color:#fff">Needs Help</span>':''}${j.job_type?'<span class="badge">'+esc(j.job_type)+'</span>':''}<span class="badge">${esc(j.status_label)}</span></div><div style="font-weight:600">${esc(j.customer||j.title)}</div><div class="muted">${esc(j.title)}</div>${j.company?'<div class="muted">Sub: '+esc(j.tech)+' ('+esc(j.company)+')</div>':''}<div class="muted">${j.scheduled_date?esc(dayLabel(j.scheduled_date)):'No date'}</div></div>`;
function setF(f){F=F===f?'':f;load()}
const TPLS={{ tpls|tojson }};
function tplOpts(){const t=document.getElementById('n-type').value,s=document.getElementById('n-tpl');if(!s)return;s.innerHTML='';const o=new Option('Checklist: none (type your own below)','');s.add(o);
TPLS.filter(x=>!x.job_type||x.job_type===t).forEach(x=>s.add(new Option('Checklist: '+x.name,String(x.id))))}
function tplPick(){const id=document.getElementById('n-tpl').value;const x=TPLS.find(k=>String(k.id)===id);if(x)document.getElementById('n-checklist').value=x.items.join('\\n')}
function toggleNew(){tplOpts();const o=document.getElementById('office');o.classList.toggle('hidden');if(!o.classList.contains('hidden')){o.scrollIntoView({behavior:'smooth'});document.getElementById('n-id').focus()}}
async function load(){const q=(document.getElementById('q').value||'').trim();
if(OFFICE&&!q)return loadDash();
const r=await fetch(q?'/api/jobs?q='+encodeURIComponent(q):'/api/jobs?status=assigned');const jobs=await r.json();
const groups={};jobs.forEach(j=>{const k=j.scheduled_date||'';(groups[k]=groups[k]||[]).push(j)});
const keys=Object.keys(groups).sort((a,b)=>a===''?1:b===''?-1:a.localeCompare(b));
document.getElementById('list').innerHTML=(keys.map(k=>`<div class="day">${dayLabel(k)}</div>`+groups[k].map(card).join('')).join('')||'<div class="job">'+(q?'No matching jobs':'No jobs')+'</div>')}
async function loadDash(){const [a,c,fu,tx,dx]=await Promise.all([fetch('/api/jobs?status=assigned').then(r=>r.json()),fetch('/api/jobs?status=completed').then(r=>r.json()),fetch('/api/followups').then(r=>r.json()),fetch('/api/drafts').then(r=>r.json()),fetch('/api/sub-docs/alerts').then(r=>r.json())]);
document.getElementById('t-docs').textContent=dx.length;document.getElementById('tile-docs').classList.toggle('red',dx.length>0);document.getElementById('tile-docs').title=dx.map(x=>x.sub+' '+x.kind+(x.missing?' missing':(x.expired?' expired ':' expires ')+x.expires)).join(', ');
const today=ymd(new Date());const lists={today:a.filter(j=>j.scheduled_date===today),progress:a.filter(j=>j.status==='in_progress'),review:a.filter(j=>j.status==='review'),help:a.filter(j=>j.help_open),invoice:c.filter(j=>j.job_type!=='Survey'),follow:fu.filter(x=>x.due<=today),texts:tx.filter(x=>x.status==='pending')};
document.querySelector('.tile[data-f="follow"]').classList.toggle('red',fu.some(x=>x.due<today));document.querySelector('.tile[data-f="review"]').classList.toggle('red',lists.review.length>0);document.querySelector('.tile[data-f="texts"]').classList.toggle('red',lists.texts.length>0);
if(F==='follow'||F==='texts'){Object.keys(lists).forEach(k=>{document.getElementById('t-'+k).textContent=lists[k].length;document.querySelector('.tile[data-f="'+k+'"]').classList.toggle('on',F===k)});document.querySelector('.tile[data-f="help"]').classList.toggle('red',lists.help.length>0);return F==='follow'?renderFollowList(fu,today):renderTextList(tx)}
Object.keys(lists).forEach(k=>{document.getElementById('t-'+k).textContent=lists[k].length;document.querySelector('.tile[data-f="'+k+'"]').classList.toggle('on',F===k)});
document.querySelector('.tile[data-f="help"]').classList.toggle('red',lists.help.length>0);
const jobs=F?lists[F]:a;const g={};jobs.forEach(j=>{const k=(j.tech||'').trim()||'Unassigned';(g[k]=g[k]||[]).push(j)});
const keys=Object.keys(g).sort((x,y)=>x==='Unassigned'?-1:y==='Unassigned'?1:x.localeCompare(y));
keys.forEach(k=>g[k].sort((x,y)=>(x.scheduled_date||'9999').localeCompare(y.scheduled_date||'9999')));
const title=F?'<div class="day">'+esc(document.querySelector('.tile[data-f="'+F+'"]').textContent.replace(/^\d+/,''))+' <a href="#" onclick="setF(F);return false" style="color:#666;font-weight:400">(show all)</a></div>':'';
document.getElementById('list').innerHTML=title+(keys.map(k=>`<div class="tech-h">${esc(k)}${g[k][0].company?' ('+esc(g[k][0].company)+')':''} &middot; ${g[k].length} job${g[k].length===1?'':'s'}</div>`+g[k].map(dcard).join('')).join('')||'<div class="job">No jobs</div>')}
function fuLabel(d,today){return d<today?'Overdue, '+dayLabel(d).replace(/^Overdue, /,''):dayLabel(d)}
function renderFollowList(fu,today){const el=document.getElementById('list');el.innerHTML='<div class="day">Follow-ups <a href="#" onclick="setF(F);return false" style="color:#666;font-weight:400">(show all jobs)</a></div>';
if(!fu.length){el.innerHTML+='<div class="job">No follow-ups</div>';return}
fu.forEach(x=>{const d=document.createElement('div');d.className='job';d.style.borderColor=x.due<today?'#b00020':x.due===today?'var(--accent)':'';const h=document.createElement('div');h.style.cssText='font-weight:700;color:'+(x.due<today?'#b00020':'var(--accent)');h.textContent=fuLabel(x.due,today);
const j=document.createElement('div');j.style.fontWeight='600';j.textContent=x.job_id+' \u00b7 '+(x.customer||x.title||'');const n=document.createElement('div');n.style.cssText='white-space:pre-wrap;font-size:1.1rem';n.textContent=x.note;
const r=document.createElement('div');r.className='row';const b1=document.createElement('button');b1.className='big-btn green';b1.textContent='Done';b1.onclick=async()=>{if((await fetch('/api/followups/'+x.id+'/done',{method:'POST'})).ok)load();else alert('Error')};
const b2=document.createElement('button');b2.className='big-btn gray';b2.textContent='Open Job';b2.onclick=()=>openJob(x.job_id);r.append(b1,b2);d.append(h,j,n,r);el.appendChild(d)})}
function renderTextList(tx){const el=document.getElementById('list');el.innerHTML='<div class="day">Texts to Review <a href="#" onclick="setF(F);return false" style="color:#666;font-weight:400">(show all jobs)</a></div><div class="muted" style="margin-bottom:8px">The app never texts clients. Approve, then tap Text Client to send it from your phone.</div>';
if(!tx.length){el.innerHTML+='<div class="job">No texts to review</div>';return}
tx.forEach(x=>{const d=document.createElement('div');d.className='job';const h=document.createElement('div');h.style.cssText='font-weight:700;color:var(--accent)';h.textContent=(x.kind==='reminder'?'Visit reminder':'On my way')+' \u00b7 '+(x.status==='pending'?'Needs review':'Approved');
const j=document.createElement('div');j.style.fontWeight='600';j.textContent=x.job_id+' \u00b7 '+(x.customer||x.title||'');const w=document.createElement('div');w.className='muted';w.textContent='From '+(x.created_by||'')+' \u00b7 '+(x.created_at||'');
const ph=document.createElement('input');ph.type='tel';ph.placeholder='Client phone';ph.value=x.phone||'';const bd=document.createElement('textarea');bd.rows=4;bd.value=x.body||'';
const r=document.createElement('div');r.className='row';const act=async(a)=>{const res=await fetch('/api/drafts/'+x.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a,phone:ph.value,body:bd.value})});if(res.ok)load();else{const e=await res.json().catch(()=>({}));alert(e.error||'Error')}};
const mk=(t,c,f)=>{const b=document.createElement('button');b.className='big-btn '+c;b.textContent=t;b.onclick=f;return b};
if(x.status==='pending'){r.append(mk('Approve','green',()=>act('approve')),mk('Dismiss','gray',()=>{if(confirm('Dismiss this text?'))act('dismiss')}))}
else{ph.readOnly=true;bd.readOnly=true;const s=document.createElement('a');s.className='big-btn green';s.style.cssText='display:block;text-align:center;text-decoration:none;box-sizing:border-box';s.textContent='Text Client';s.href='sms:'+encodeURIComponent((x.phone||'').replace(/[^0-9+]/g,''))+'?&body='+encodeURIComponent(x.body||'');
r.append(s,mk('Copy','gray',async()=>{try{await navigator.clipboard.writeText(x.body)}catch(e){bd.select();document.execCommand('copy')}alert('Text copied')}),mk('Done','gray',()=>act('done')))}
const r2=document.createElement('div');r2.className='row';r2.appendChild(mk('Open Job','gray',()=>openJob(x.job_id)));d.append(h,j,w,ph,bd,r,r2);el.appendChild(d)})}
async function onMyWay(){if(!confirm('Tell the office you are on your way?'))return;const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/onmyway',{method:'POST'});
if(r.ok)alert('Done. The office will check the text and send it.');else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}}
function renderReview(){const b=document.getElementById('review-box');if(!b)return;b.classList.toggle('hidden',currentJob.status!=='review');
const n=(currentJob.photos||[]).length,dv=(currentJob.devices||[]).length;document.getElementById('review-info').textContent=(currentJob.tech||'Sub')+' finished this job. '+n+' photo'+(n===1?'':'s')+', '+dv+' device'+(dv===1?'':'s')+' on the as-built.'}
async function approveJob(){if(!confirm('Approve this job and make the invoice draft?'))return;const r=await post('/approve',{});
if(r.ok){alert('Approved. Invoice draft is ready to review.');openJob(currentJob.id)}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}}
function renderHub(){const u=document.getElementById('hub-url');if(!u)return;u.value=currentJob.hub_url?location.origin+currentJob.hub_url:'';document.getElementById('hub-exp').textContent=currentJob.hub_expires?'Link works until '+currentJob.hub_expires:'';
document.getElementById('hub-appr').textContent=currentJob.hub_approved_at?'Estimate approved by '+(currentJob.hub_approved_name||'client')+' on '+currentJob.hub_approved_at:(currentJob.job_type==='Survey'?'Estimate not approved yet':'')}
async function newHub(){if(!confirm('Make a new link? The old link will stop working.'))return;const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/hub-new',{method:'POST'});
if(!r.ok)return alert('Error');Object.assign(currentJob,await r.json());renderHub();alert('New link made. Copy it and send it yourself.')}
async function copyHub(){const u=document.getElementById('hub-url');try{await navigator.clipboard.writeText(u.value);alert('Link copied')}catch(e){u.select();document.execCommand('copy');alert('Link copied')}}
async function loadFollow(){const el=document.getElementById('fu-list');if(!el)return;const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/followups');const u=r.ok?await r.json():[];el.innerHTML='';const today=ymd(new Date());
u.forEach(x=>{const d=document.createElement('div');d.style.cssText='padding:8px 0;border-bottom:1px solid #e5e5e5;'+(x.done?'color:#999':'');const h=document.createElement('div');h.style.cssText='font-weight:700;'+(!x.done&&x.due<today?'color:#b00020':'');h.textContent=x.done?'Done \u00b7 was due '+dayLabel(x.due).replace(/^Overdue, /,''):fuLabel(x.due,today);
const t=document.createElement('div');t.style.whiteSpace='pre-wrap';t.textContent=x.note;d.append(h,t);if(!x.done){const b=document.createElement('button');b.className='big-btn green';b.style.padding='10px';b.textContent='Mark Done';b.onclick=async()=>{if((await fetch('/api/followups/'+x.id+'/done',{method:'POST'})).ok)loadFollow();else alert('Error')};d.appendChild(b)}el.appendChild(d)});
if(!u.length)el.innerHTML='<div class="muted">No follow-ups</div>'}
async function addFollow(){const dt=document.getElementById('fu-date').value,n=document.getElementById('fu-note').value.trim();if(!dt||!n){alert('Pick a date and type a note');return}
const r=await post('/followups',{due:dt,note:n});if(r.ok){document.getElementById('fu-note').value='';document.getElementById('fu-date').value='';loadFollow()}else alert('Error')}
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
if(!u.length)el.innerHTML='<div style="color:#666">No notes yet</div>';
const lg=document.getElementById('log-legacy');lg.innerHTML='';[['Notes',currentJob.notes],['Quick notes',currentJob.quick_notes]].forEach(([k,v])=>{if(!v)return;const d=document.createElement('div');d.style.cssText='padding:8px 0;border-bottom:1px solid #e5e5e5';const m=document.createElement('div');m.style.cssText='font-size:.8rem;color:#666';m.textContent='Earlier '+k.toLowerCase();const t=document.createElement('div');t.style.whiteSpace='pre-wrap';t.textContent=v;d.append(m,t);lg.appendChild(d)})}
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
function renderTotals(){document.getElementById('totals').textContent='Total: '+(currentJob.hours||0)+' hrs';renderLabor()}
const money=n=>'$'+Number(n||0).toFixed(2);
function renderLabor(){const el=document.getElementById('labor');if(!el||!currentJob.labor)return;const L=currentJob.labor;
el.innerHTML=L.lines.map(l=>'<div>'+esc(l.sub)+': '+l.hours+' hrs'+(l.no_rate?' &middot; <b style="color:#b00020">No rate set</b>':(l.rate_hourly!=null?' x '+money(l.rate_hourly)+'/hr':'')+(l.rate_flat!=null?' + '+money(l.rate_flat)+' flat':'')+' = <b>'+money(l.cost)+'</b>')+'</div>').join('')+'<div style="font-size:1.2rem;font-weight:700;margin-top:4px">Total labor: '+money(L.total)+'</div>'+(L.locked?'<div class="muted">Rates locked when the job was completed.</div>':'<div class="muted">Uses current rates until the job is completed.</div>')}
async function saveTime(mode){const h=document.getElementById('hours');if(!h.value)return;
const body={mode:mode,hours:h.value,miles:mode==='set'?(currentJob.total_miles||0):0};
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/time',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(r.ok){const d=await r.json();currentJob.hours=d.hours;currentJob.total_miles=d.total_miles;if(d.labor)currentJob.labor=d.labor;h.value='';renderTotals()}else alert('Error')}
async function setStatus(v){const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:v})});
if(r.ok){const d=await r.json();currentJob.status=d.status;document.getElementById('status').value=d.status;if(d.status==='review'&&v!=='review')alert('Sent to the office for review.');renderKelly();renderReview()}else{alert('Error');document.getElementById('status').value=currentJob.status}}
async function savePrivate(){const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/private-notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({private_notes:document.getElementById('private-notes').value})});alert(r.ok?'Saved':'Error')}
function renderTicks(elId,items,save,removable){const el=document.getElementById(elId);el.innerHTML='';
items.forEach((x,i)=>{const l=document.createElement('label');l.className='mat';const c=document.createElement('input');c.type='checkbox';c.checked=x.done;c.onchange=()=>{x.done=c.checked;save()};
const s=document.createElement('span');s.textContent=x.text;l.append(c,s);if(removable){const b=document.createElement('button');b.type='button';b.textContent='\u00d7';b.onclick=e=>{e.preventDefault();items.splice(i,1);save();renderTicks(elId,items,save,removable)};l.appendChild(b)}el.appendChild(l)});
if(!items.length)el.innerHTML='<div class="muted">None yet</div>'}
function post(path,body){return fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
const savePunch=()=>post('/punch',{punch_list:currentJob.punch_list});
const SV_TEXT=['want_desc','contact_name','contact_phone','site_conditions'];
function legacyCond(s){const out=[];[['current_system','Current system'],['power','Power'],['network','Network'],['conduit','Conduit / pathways']].forEach(([k,l])=>{if(s[k])out.push(l+': '+s[k])});
if((s.obstacles||[]).length)out.push('Obstacles: '+s.obstacles.join(', '));if(s.obstacle_notes)out.push(s.obstacle_notes);return out.join('\\n')}
function svCall(){const ph=document.getElementById('sv-contact_phone').value.replace(/[^0-9+]/g,''),a=document.getElementById('sv-call');if(ph){a.href='tel:'+ph;a.textContent='Call Contact ('+document.getElementById('sv-contact_phone').value+')';a.style.display='block'}else{a.removeAttribute('href');a.style.display='none'}}
let svEditing=false;
const DEVS={{ dev_types|tojson }};let devType='',devSel=null,devMove=null;
function devLock(){return currentJob.job_type==='Survey'&&currentJob.status==='completed'&&!svEditing}
function isAB(){return currentJob.job_type!=='Survey'}
function devColor(t){const d=DEVS.find(x=>x[0]===t);return d?d[2]:'#333'}
function devPrefix(t){const d=DEVS.find(x=>x[0]===t);return d?d[1]:'?'}
function renderDevices(){if(!currentJob||currentJob.job_type==='Office Task')return;const ab=isAB();const lock=devLock();const plans=currentJob.plans||[];const devs=currentJob.devices=currentJob.devices||[];
if(lock){devType='';devMove=null}
document.getElementById('plan-add').classList.toggle('hidden',lock);
const pal=document.getElementById('dev-palette');pal.classList.toggle('hidden',lock||!plans.length);pal.innerHTML='';
DEVS.forEach(([n,p,c])=>{const b=document.createElement('button');b.className=devType===n?'on':'';const i=document.createElement('i');i.style.background=c;i.textContent=p;b.append(i,n);b.onclick=()=>{devType=devType===n?'':n;devMove=null;renderDevices()};pal.appendChild(b)});
document.getElementById('dev-hint').textContent=lock||!plans.length?'':devMove?'Tap the new spot on the plan':devType?'Tap the plan to drop a '+devType:'Pick a device, then tap the plan';
const w=document.getElementById('plans');w.innerHTML='';
plans.forEach((fn,pi)=>{const box=document.createElement('div');box.className='plan';const img=document.createElement('img');img.src='/uploads/'+encodeURIComponent(fn);img.alt='floor plan';box.appendChild(img);
devs.filter(d=>d.plan===fn).forEach(d=>{const pin=document.createElement('div');pin.className='pin'+(devSel===d.id?' sel':'');pin.style.left=d.x+'%';pin.style.top=d.y+'%';pin.style.background=devColor(d.type);if(ab&&!d.installed){pin.style.background='#fff';pin.style.color=devColor(d.type);pin.style.borderColor=devColor(d.type)}pin.textContent=d.label||devPrefix(d.type);
pin.onclick=e=>{e.stopPropagation();if(devMove)return planTap(e,fn,box);devSel=d.id;devType='';renderDevices();document.getElementById('dev-edit').scrollIntoView({behavior:'smooth',block:'center'})};box.appendChild(pin)});
box.onclick=e=>planTap(e,fn,box);w.appendChild(box);
if(!lock){const rb=document.createElement('button');rb.className='big-btn gray';rb.textContent='Remove Plan '+(pi+1);rb.onclick=()=>removePlan(fn);w.appendChild(rb)}});
const d=devs.find(x=>x.id===devSel);const ed=document.getElementById('dev-edit');ed.classList.toggle('hidden',!d);
if(d){document.getElementById('dv-title').textContent=d.label+' \u00b7 '+d.type;['note','height','cable'].forEach(k=>{const el=document.getElementById('dv-'+k);el.value=d[k]||'';el.disabled=lock});document.getElementById('dv-btns').classList.toggle('hidden',lock);
document.querySelectorAll('#dev-edit .ab-only').forEach(e=>e.classList.toggle('hidden',!ab));
if(ab){['mac','ip','port','clabel'].forEach(k=>document.getElementById('dv-'+k).value=d[k]||'');document.getElementById('dv-installed').checked=!!d.installed;
const ph=document.getElementById('dv-photo');ph.innerHTML='';if(d.photo){const a=document.createElement('a');a.href='/uploads/'+encodeURIComponent(d.photo);a.target='_blank';const i=document.createElement('img');i.src=a.href;i.style.cssText='width:120px;height:120px;object-fit:cover;border-radius:8px';a.appendChild(i);ph.appendChild(a)}
document.getElementById('dv-photo-btn').textContent=d.photo?'Replace Device Photo':'Add Device Photo'}}
const cp=document.getElementById('ab-copy');cp.innerHTML='';if(ab&&!lock&&!plans.length)(currentJob.survey_plan_sources||[]).forEach(x=>{const b=document.createElement('button');b.className='big-btn';b.style.background='#6a1b9a';b.textContent='Copy Survey Plan ('+x.id+')';b.onclick=()=>copyPlan(x.id);cp.appendChild(b)});
const cnt={};devs.forEach(x=>cnt[x.type]=(cnt[x.type]||0)+1);const c=document.getElementById('dev-counts');c.innerHTML='';
if(devs.length){const h=document.createElement('div');h.className='ptitle';h.textContent='Device Count ('+devs.length+' total'+(ab?', '+devs.filter(x=>x.installed).length+' installed':'')+')';c.appendChild(h);
DEVS.forEach(([n,p,col])=>{if(!cnt[n])return;const r=document.createElement('div');r.style.cssText='font-size:1.15rem;padding:6px 0;border-bottom:1px solid #e5e5e5';const i=document.createElement('span');i.className='pin';i.style.cssText='position:static;transform:none;margin-right:8px;background:'+col;i.textContent=p;const inst=devs.filter(x=>x.type===n&&x.installed).length;r.append(i,n+' x'+cnt[n]+(ab?' ('+inst+' installed, '+(cnt[n]-inst)+' planned)':''));c.appendChild(r)})}
else if(plans.length)c.innerHTML='<div class="muted">No devices yet</div>'}
function planTap(e,fn,box){if(devLock())return;const r=box.querySelector('img').getBoundingClientRect();const x=(e.clientX-r.left)/r.width*100,y=(e.clientY-r.top)/r.height*100;if(x<0||x>100||y<0||y>100)return;
if(devMove){const d=currentJob.devices.find(k=>k.id===devMove);devMove=null;if(d){d.x=x;d.y=y;d.plan=fn}saveDevices();return}
if(!devType){alert('Pick a device first');return}
currentJob.devices.push({type:devType,plan:fn,x:x,y:y});saveDevices()}
async function saveDevices(){const r=await post('/devices',{devices:currentJob.devices});if(r.ok){currentJob.devices=(await r.json()).devices}else{alert('Error, not saved');const g=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id));if(g.ok)currentJob.devices=(await g.json()).devices}renderDevices()}
function devSave(){const d=currentJob.devices.find(k=>k.id===devSel);if(!d)return;['note','height','cable'].concat(isAB()?['mac','ip','port','clabel']:[]).forEach(k=>d[k]=document.getElementById('dv-'+k).value.trim());if(isAB())d.installed=document.getElementById('dv-installed').checked;devSel=null;saveDevices()}
function devMoveStart(){devMove=devSel;devSel=null;devType='';renderDevices();document.getElementById('dev-hint').scrollIntoView({behavior:'smooth',block:'center'})}
function devDelete(){const d=currentJob.devices.find(k=>k.id===devSel);if(!d||!confirm('Delete '+d.label+'?'))return;currentJob.devices=currentJob.devices.filter(k=>k.id!==devSel);devSel=null;saveDevices()}
async function devPhoto(inp){if(!inp.files.length||!devSel)return;const fd=new FormData();fd.append('photo',inp.files[0]);inp.value='';
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/devices/'+encodeURIComponent(devSel)+'/photo',{method:'POST',body:fd});if(r.ok){currentJob.devices=(await r.json()).devices;renderDevices()}else alert('Error')}
async function copyPlan(src){if(!confirm('Copy the plan and devices from survey '+src+'?'))return;const r=await post('/plans/copy',{from:src});const d=await r.json().catch(()=>({}));if(r.ok){currentJob.plans=d.plans;currentJob.devices=d.devices;renderDevices()}else alert(d.error||'Error')}
let skOps=[],skT='pen',skCur=null;const skC=document.getElementById('sk-c'),skX=skC.getContext('2d');
function skOpen(){skOps=[];skCur=null;document.getElementById('sk').classList.remove('hidden');skTool('pen');skDraw()}
function skClose(){if(skOps.length&&!confirm('Throw away this drawing?'))return;document.getElementById('sk').classList.add('hidden')}
function skTool(t){skT=t;document.querySelectorAll('.sk-tools button[data-t]').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
document.getElementById('sk-hint').textContent={pen:'Draw with your finger',line:'Drag to draw a straight line',erase:'Rub out lines',text:'Tap where the room name goes'}[t]}
function skUndo(){skOps.pop();skDraw()}
function skP(e){const r=skC.getBoundingClientRect();return{x:(e.clientX-r.left)*skC.width/r.width,y:(e.clientY-r.top)*skC.height/r.height}}
function skDraw(){const L=document.createElement('canvas');L.width=skC.width;L.height=skC.height;const g=L.getContext('2d');g.lineCap='round';g.lineJoin='round';
skOps.concat(skCur?[skCur]:[]).forEach(o=>{g.globalCompositeOperation=o.t==='erase'?'destination-out':'source-over';g.strokeStyle='#111';g.fillStyle='#111';
if(o.t==='text'){g.font='bold 40px sans-serif';g.textAlign='center';g.fillText(o.s,o.x,o.y);return}
g.lineWidth=o.t==='erase'?40:6;g.beginPath();g.moveTo(o.p[0].x,o.p[0].y);(o.t==='line'?[o.p[o.p.length-1]]:o.p).forEach(q=>g.lineTo(q.x,q.y));g.stroke()});
skX.fillStyle='#fff';skX.fillRect(0,0,skC.width,skC.height);skX.strokeStyle='#dde6f0';skX.lineWidth=1;skX.beginPath();
for(let x=0;x<=skC.width;x+=40){skX.moveTo(x,0);skX.lineTo(x,skC.height)}for(let y=0;y<=skC.height;y+=40){skX.moveTo(0,y);skX.lineTo(skC.width,y)}skX.stroke();skX.drawImage(L,0,0)}
skC.addEventListener('pointerdown',e=>{e.preventDefault();const q=skP(e);if(skT==='text'){const s=prompt('Room name','');if(s&&s.trim()){skOps.push({t:'text',x:q.x,y:q.y,s:s.trim().slice(0,40)});skDraw()}return}
try{skC.setPointerCapture(e.pointerId)}catch(_){}skCur={t:skT,p:[q,q]};skDraw()});
skC.addEventListener('pointermove',e=>{if(!skCur)return;e.preventDefault();const q=skP(e);if(skCur.t==='line')skCur.p[1]=q;else skCur.p.push(q);skDraw()});
['pointerup','pointercancel'].forEach(t=>skC.addEventListener(t,e=>{if(!skCur)return;skOps.push(skCur);skCur=null;skDraw()}));
async function skSave(){if(!skOps.length){alert('Draw something first');return}skDraw();const blob=await new Promise(r=>skC.toBlob(r,'image/png'));const fd=new FormData();fd.append('files',blob,'sketch.png');
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/plans',{method:'POST',body:fd});const d=await r.json().catch(()=>({}));if(r.ok){currentJob.plans=d.plans;skOps=[];document.getElementById('sk').classList.add('hidden');renderDevices()}else alert(d.error||'Error')}
const MCOL=['#ff1744','#2979ff','#00e676','#ffea00','#d500f9','#ff9100'];let mpL=null,mpLay=null,mpM='',mpSel=null,mpRun=null;
function mjob(){currentJob.map=currentJob.map||{};const m=currentJob.map;m.cameras=m.cameras||[];m.runs=m.runs||[];return m}
function mft(a,b){return L.latLng(a).distanceTo(L.latLng(b))*3.28084}
function loadLeaflet(){if(window.L)return Promise.resolve(true);return new Promise(res=>{const c=document.createElement('link');c.rel='stylesheet';c.href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';document.head.appendChild(c);
const s=document.createElement('script');s.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';s.onload=()=>res(!!window.L);s.onerror=()=>res(false);document.head.appendChild(s)})}
function mapSummary(){const m=mjob();document.getElementById('map-sum').textContent=m.cameras.length||m.runs.length?m.cameras.length+' camera'+(m.cameras.length===1?'':'s')+', '+m.runs.length+' run'+(m.runs.length===1?'':'s'):'No cameras or runs yet'}
let meW=null,me=null,meLay=null;
async function mapOpen(){document.getElementById('mp').classList.remove('hidden');window.scrollTo(0,0);if(!(await loadLeaflet())){document.getElementById('mp-hint').textContent='Map could not load. Check your signal.';return}
if(!mpL){mpL=L.map('mp-map');L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:21,maxNativeZoom:19,attribution:'Tiles &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community'}).addTo(mpL);mpLay=L.layerGroup().addTo(mpL);meLay=L.layerGroup().addTo(mpL);mpL.on('click',mapTap)}
mpM='';mpSel=null;mpRun=null;const m=mjob();setTimeout(()=>mpL.invalidateSize(),60);document.getElementById('mp-q').value=(m.geo&&m.geo.q&&m.geo.q!=='My location')?m.geo.q:(currentJob.address||'');document.getElementById('mp-loc').textContent='';meStart();
if(m.view)mpL.setView([m.view.lat,m.view.lng],m.view.zoom);else if(m.geo&&m.geo.lat!=null)mpL.setView([m.geo.lat,m.geo.lng],19);else mpL.setView([29.76,-95.37],11);
mapRender();if(m.geo&&m.geo.addr===(currentJob.address||'').trim())return;
document.getElementById('mp-hint').textContent='Finding the address...';const r=await post('/geocode',{});const d=await r.json().catch(()=>({}));
if(d.geo)m.geo=d.geo;if(r.ok&&d.lat!=null&&!m.view)mpL.setView([d.lat,d.lng],19);mapRender()}
function mapClose(){if(mpRun&&mpRun.pts.length&&!confirm('Throw away the run you are drawing?'))return;mpRun=null;mpM='';meStop();document.getElementById('mp').classList.add('hidden');if(mpL&&!devLock())mapSave();mapSummary()}
async function mapFind(){if(devLock())return;const q=document.getElementById('mp-q').value.trim();if(!q){alert('Type the address');return}document.getElementById('mp-hint').textContent='Finding...';
const r=await post('/geocode',{q:q});const d=await r.json().catch(()=>({}));const m=mjob();if(r.ok&&d.geo){m.geo=d.geo;mpL.setView([d.lat,d.lng],19);mapRender();mapSave()}else{mapRender();document.getElementById('mp-hint').textContent=r.status===404?'Address not found. Try a shorter address, or pan to the site.':'Lookup failed. Try again.'}}
function meStart(){meStop();me=null;if(!navigator.geolocation){document.getElementById('mp-loc').textContent='Location not available on this device.';return}
meW=navigator.geolocation.watchPosition(p=>{me={lat:p.coords.latitude,lng:p.coords.longitude,acc:p.coords.accuracy};document.getElementById('mp-loc').textContent='';meDraw()},
e=>{me=null;meDraw();document.getElementById('mp-loc').textContent=e.code===1?'Location blocked. Allow location for this site in your browser settings.':'Location not available right now.'},{enableHighAccuracy:true,maximumAge:10000,timeout:20000})}
function meStop(){if(meW!==null&&navigator.geolocation)navigator.geolocation.clearWatch(meW);meW=null}
function meDraw(){if(!meLay)return;meLay.clearLayers();if(!me)return;L.circle([me.lat,me.lng],{radius:me.acc||0,color:'#1e88e5',weight:1,fillOpacity:.15,interactive:false}).addTo(meLay);L.circleMarker([me.lat,me.lng],{radius:8,color:'#fff',weight:3,fillColor:'#1e88e5',fillOpacity:1,interactive:false}).addTo(meLay)}
function meCenter(){if(me){mpL.setView([me.lat,me.lng],Math.max(mpL.getZoom(),18));return}const t=document.getElementById('mp-loc');if(!t.textContent)t.textContent='Finding your location...';if(meW===null)meStart()}
async function useMeSite(){if(devLock())return;if(!me){meCenter();alert('Waiting for your location. Try again in a moment.');return}if(!confirm('Set the job site to where you are now?'))return;
const r=await post('/site',{lat:me.lat,lng:me.lng});const d=await r.json().catch(()=>({}));if(r.ok){mjob().geo=d.geo;mpL.setView([me.lat,me.lng],19);mapRender();mapSave()}else alert(d.error||'Error')}
function mapMode(md){if(devLock())return;mpM=mpM===md?'':md;mpSel=null;mpRun=mpM==='measure'?{pts:[]}:null;mapRender()}
function mapTap(e){if(devLock())return;const m=mjob();const p=[e.latlng.lat,e.latlng.lng];
if(mpM==='cam'){m.cameras.push({lat:p[0],lng:p[1],dir:0,fov:90,range:60});mpM='';mapSave('cam')}
else if(mpM==='measure'&&mpRun){mpRun.pts.push(p);mapRender()}}
function cone(c){const R=6378137,d=c.range*0.3048,n=Math.max(8,Math.round(c.fov/6)),pts=[[c.lat,c.lng]];
for(let i=0;i<=n;i++){const b=(c.dir-c.fov/2+c.fov*i/n)*Math.PI/180;pts.push([c.lat+d*Math.cos(b)/R*180/Math.PI,c.lng+d*Math.sin(b)/(R*Math.cos(c.lat*Math.PI/180))*180/Math.PI])}return pts}
function cmp(d){return['N','NE','E','SE','S','SW','W','NW'][Math.round(d/45)%8]}
function segLabels(pts,col){for(let i=0;i<pts.length-1;i++){const a=pts[i],b=pts[i+1];L.marker([(a[0]+b[0])/2,(a[1]+b[1])/2],{interactive:false,icon:L.divIcon({className:'',html:'<span class="mp-seg">'+Math.round(mft(a,b))+' ft</span>',iconSize:null})}).addTo(mpLay)}}
function mapRender(){if(!mpL)return;const m=mjob(),lock=devLock();mpLay.clearLayers();
document.querySelectorAll('#mp-tools button[data-m]').forEach(b=>{b.classList.toggle('on',b.dataset.m===mpM);b.style.display=lock?'none':''});
const gh=m.geo&&m.geo.lat!=null;document.getElementById('mp-find').classList.toggle('hidden',lock);document.getElementById('mp-usesite').classList.toggle('hidden',lock||gh);
if(gh){const at=(m.geo.q==='My location'?'Site (set by location): ':'')+(currentJob.address||m.geo.q||'Job site');L.marker([m.geo.lat,m.geo.lng],{zIndexOffset:-500,icon:L.divIcon({className:'',html:'<span class="mp-addr"></span>',iconSize:[0,0],iconAnchor:[0,0]})}).addTo(mpLay).bindPopup(esc(at)+(m.geo.q&&m.geo.q!==currentJob.address&&m.geo.q!=='My location'?'<br><span style="color:#666">Found by: '+esc(m.geo.q)+'</span>':'')).getElement().querySelector('.mp-addr').textContent=at}
m.cameras.forEach((c,i)=>{const col=MCOL[i%MCOL.length];L.polygon(cone(c),{color:col,weight:2,fillOpacity:.3,interactive:false}).addTo(mpLay);
const mk=L.marker([c.lat,c.lng],{draggable:!lock,icon:L.divIcon({className:'',html:'<span class="pin" style="position:static;transform:none;display:inline-block;background:'+col+';color:#111">'+esc(c.label||'C')+'</span>',iconSize:[36,36],iconAnchor:[18,18]})}).addTo(mpLay);
mk.on('click',()=>{if(mpM)return;mpSel={k:'cam',id:c.id};mapRender()});mk.on('dragend',ev=>{const q=ev.target.getLatLng();c.lat=q.lat;c.lng=q.lng;mapSave()})});
m.runs.forEach(r=>{const ln=L.polyline(r.pts,{color:'#ffea00',weight:(mpSel&&mpSel.id===r.id)?7:4}).addTo(mpLay);ln.on('click',ev=>{L.DomEvent.stopPropagation(ev);if(mpM)return;mpSel={k:'run',id:r.id};mapRender()});segLabels(r.pts)});
if(mpRun&&mpRun.pts.length){L.polyline(mpRun.pts,{color:'#00e5ff',weight:4,dashArray:'8 6'}).addTo(mpLay);mpRun.pts.forEach(q=>L.circleMarker(q,{radius:6,color:'#00e5ff',fillOpacity:1,interactive:false}).addTo(mpLay));segLabels(mpRun.pts)}
document.getElementById('mp-hint').textContent=!gh&&!lock&&!mpM&&!mpSel?'Address not found. Type it above and tap Find, use My Location, or pan to the site.':lock?'View only':mpM==='cam'?'Tap the map where the camera goes':mpM==='measure'?'Tap points along the run':'Tap a camera or run to change it';
const P=document.getElementById('mp-panel');P.innerHTML='';const btn=(t,f,cls)=>{const b=document.createElement('button');b.textContent=t;b.onclick=f;if(cls)b.style.cssText=cls;return b};const grid=(...bs)=>{const g=document.createElement('div');g.className='mp-b';bs.forEach(b=>g.appendChild(b));return g};
const box=document.createElement('div');box.className='job';box.style.border='2px solid var(--accent)';const ttl=document.createElement('div');ttl.style.cssText='font-weight:800;font-size:1.2rem';box.appendChild(ttl);const inf=document.createElement('div');inf.style.fontSize='1.1rem';box.appendChild(inf);
if(mpRun){const tot=mpRun.pts.slice(1).reduce((a,q,i)=>a+mft(mpRun.pts[i],q),0);ttl.textContent='New Run';inf.textContent=mpRun.pts.length+' points, '+Math.round(tot)+' ft';
box.appendChild(grid(btn('Undo Point',()=>{mpRun.pts.pop();mapRender()}),btn('Finish Run',finishRun,'background:#2d6a4f;color:#fff')));P.appendChild(box)}
else if(mpSel){const it=(mpSel.k==='cam'?m.cameras:m.runs).find(x=>x.id===mpSel.id);if(!it){mpSel=null}else if(mpSel.k==='cam'){const c=it;ttl.textContent=c.label+' Camera';inf.textContent='Facing '+c.dir+'\u00b0 '+cmp(c.dir)+', view '+c.fov+'\u00b0, range '+c.range+' ft';
const ch=(k,dv,lo,hi,wrap)=>()=>{let v=c[k]+dv;v=wrap?((v%360)+360)%360:Math.min(hi,Math.max(lo,v));c[k]=v;mapSave()};
if(!lock){box.appendChild(grid(btn('Turn Left',ch('dir',-15,0,0,1)),btn('Turn Right',ch('dir',15,0,0,1)),btn('Narrower',ch('fov',-10,10,360)),btn('Wider',ch('fov',10,10,360)),btn('Range -10 ft',ch('range',-10,5,2000)),btn('Range +10 ft',ch('range',10,5,2000))));
const h=document.createElement('div');h.className='muted';h.textContent='Drag the pin to move it';box.appendChild(h);box.appendChild(grid(btn('Delete',()=>{if(!confirm('Delete '+c.label+'?'))return;m.cameras=m.cameras.filter(x=>x!==c);mpSel=null;mapSave()},'background:#b00020;color:#fff'),btn('Close',()=>{mpSel=null;mapRender()})))}
else box.appendChild(grid(btn('Close',()=>{mpSel=null;mapRender()})));P.appendChild(box)}
else{const r=it;ttl.textContent=r.label||'Run';inf.textContent=(r.segments_ft||[]).map(Math.round).join(' + ')+' = '+Math.round(r.total_ft||0)+' ft'+(r.slack?', with '+r.slack+'% slack = '+Math.round(r.adjusted_ft)+' ft':'');
const li=document.createElement('input');li.type='text';li.placeholder='Run name (example: Bldg A to B)';li.value=r.label||'';li.disabled=lock;const si=document.createElement('input');si.type='number';si.inputMode='decimal';si.placeholder='Slack percent (optional)';si.value=r.slack||'';si.disabled=lock;box.append(li,si);
if(!lock)box.appendChild(grid(btn('Save',()=>{r.label=li.value.trim();r.slack=parseFloat(si.value)||0;mapSave()},'background:var(--accent);color:#fff'),btn('Delete',()=>{if(!confirm('Delete this run?'))return;m.runs=m.runs.filter(x=>x!==r);mpSel=null;mapSave()},'background:#b00020;color:#fff')));
box.appendChild(grid(btn('Close',()=>{mpSel=null;mapRender()})));P.appendChild(box)}}
const Ls=document.getElementById('mp-list');Ls.innerHTML='';const line=t=>{const d=document.createElement('div');d.style.cssText='font-size:1.1rem;padding:6px 0;border-bottom:1px solid #e5e5e5';d.textContent=t;Ls.appendChild(d)};
m.cameras.forEach(c=>line(c.label+': facing '+cmp(c.dir)+', '+c.fov+'\u00b0, '+c.range+' ft'));m.runs.forEach(r=>line((r.label||'Run')+': '+Math.round(r.total_ft||0)+' ft'+(r.slack?' ('+Math.round(r.adjusted_ft)+' ft with slack)':'')));mapSummary()}
function finishRun(){if(!mpRun||mpRun.pts.length<2){alert('Tap at least 2 points');return}const lb=prompt('Name this run (example: Bldg A to B)','');if(lb===null)return;mjob().runs.push({label:lb.trim(),pts:mpRun.pts,slack:0});mpRun=null;mpM='';mapSave('run')}
async function mapSave(sel){const m=mjob();const c=mpL?mpL.getCenter():null;const r=await post('/map',{cameras:m.cameras,runs:m.runs,view:c?{lat:c.lat,lng:c.lng,zoom:mpL.getZoom()}:m.view});
if(r.ok){const d=(await r.json()).map;currentJob.map=Object.assign(d,{geo:m.geo});if(sel==='cam'){const x=d.cameras[d.cameras.length-1];if(x)mpSel={k:'cam',id:x.id}}if(sel==='run'){const x=d.runs[d.runs.length-1];if(x)mpSel={k:'run',id:x.id}}}else alert('Error, not saved');mapRender()}
async function surveyToJob(){const id=prompt('New Install Job ID (leave blank to auto-number)','');if(id===null)return;const r=await post('/to-job',{id:id.trim()});const d=await r.json().catch(()=>({}));
if(r.ok){alert('Install job '+d.id+' created. Set its date and sub.');openJob(d.id)}else alert(d.error||'Error')}
async function uploadPlans(inp){if(!inp.files.length)return;const fd=new FormData();[...inp.files].forEach(f=>fd.append('files',f));inp.value='';
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/plans',{method:'POST',body:fd});const d=await r.json().catch(()=>({}));if(r.ok){currentJob.plans=d.plans;renderDevices()}else alert(d.error||'Error')}
async function removePlan(fn){if(!confirm('Remove this plan and all its devices?'))return;const r=await post('/plans/delete',{filename:fn});if(r.ok){const d=await r.json();currentJob.plans=d.plans;currentJob.devices=d.devices;devSel=null;devMove=null;renderDevices()}else alert('Error')}
function renderKelly(){const done=currentJob.status==='completed'&&currentJob.job_type==='Survey';const lock=done&&!svEditing;
document.querySelectorAll('#survey-card input,#survey-card textarea').forEach(e=>e.disabled=lock);
document.getElementById('sv-add').classList.toggle('hidden',lock);document.getElementById('sv-save').classList.toggle('hidden',!(done&&svEditing));
const ed=document.getElementById('sv-edit');if(ed)ed.classList.toggle('hidden',!lock);const tj=document.getElementById('sv-tojob');if(tj)tj.classList.toggle('hidden',!lock);
const n=document.getElementById('kelly-note');if(n)n.textContent=currentJob.kelly_at?'Sent to Kelly \u00b7 '+currentJob.kelly_at:'';
if(currentJob.job_type==='Survey')document.getElementById('finish').classList.toggle('hidden',done);const la=lock&&!{{ office|tojson }};document.getElementById('update-note').classList.toggle('hidden',la);document.getElementById('log-add').classList.toggle('hidden',la);renderDevices()}
function fillSurvey(){svEditing=false;devType='';devSel=null;devMove=null;renderKelly();const s=currentJob.survey||{};SV_TEXT.forEach(k=>document.getElementById('sv-'+k).value=s[k]||'');
if(!s.site_conditions)document.getElementById('sv-site_conditions').value=legacyCond(s);const o=document.getElementById('sv-office');if(o)o.value=currentJob.survey_office||'';const ex=document.getElementById('sv-expect');if(ex)ex.textContent=(s.timeline||s.budget||s.decision_maker)?'Earlier entry (office only): Timeline '+(s.timeline||'-')+' \u00b7 Budget '+(s.budget||'-')+' \u00b7 Decision maker '+(s.decision_maker||'-'):'';svCall()}
async function saveSurvey(quiet){const s=Object.assign({},currentJob.survey||{});SV_TEXT.forEach(k=>s[k]=document.getElementById('sv-'+k).value);
let ok=(await post('/survey',{survey:s})).ok;const o=document.getElementById('sv-office');if(ok&&o)ok=(await post('/survey-office',{survey_office:o.value})).ok;
if(ok){currentJob.survey=s;if(o)currentJob.survey_office=o.value;svCall();if(!quiet){alert('Survey saved');svEditing=false;renderKelly()}}else alert('Error');return ok}
const LAYOUT={Survey:{show:['help','links','type','omw','survey','log','follow','hub','finish'],more:[]},
Install:{show:['help','review','links','type','omw','status','photos','mat','time','check','log','ab','map','follow','hub','private','sig','finish'],more:[]},
Service:{show:['help','review','links','type','omw','status','log','photos','mat','time','follow','private','sig','finish'],more:['ab','map','check','hub']},
'Office Task':{show:['type','status','log','check','follow','finish'],more:[]}};
function toggleMore(){const b=document.getElementById('more-box');b.classList.toggle('hidden');document.getElementById('more-btn').textContent=b.classList.contains('hidden')?'Show more':'Show less'}
function renderType(){const t=currentJob.job_type||'';const s=document.getElementById('jtype');if(s)s.value=t;const l=document.getElementById('jtype-label');if(l)l.textContent=t||'(not set)';
const sv=t==='Survey',L=LAYOUT[t]||LAYOUT.Install,det=document.getElementById('detail'),mb=document.getElementById('more-box'),before=document.getElementById('sec-admin');
['help','links','type','survey','status','log','photos','mat','time','omw','review','check','ab','map','follow','hub','private','sig','finish'].forEach(k=>{const e=document.getElementById('sec-'+k);e.classList.toggle('hidden',!L.show.includes(k)&&!L.more.includes(k));det.appendChild(e)});
L.show.forEach(k=>det.insertBefore(document.getElementById('sec-'+k),before));L.more.forEach(k=>mb.appendChild(document.getElementById('sec-'+k)));
if(sv){const m=document.getElementById('sec-map');m.classList.remove('hidden');document.getElementById('sv-mapslot').appendChild(m)}
det.insertBefore(document.getElementById('sec-more'),before);document.getElementById('sec-more').classList.toggle('hidden',!L.more.length);mb.classList.add('hidden');document.getElementById('more-btn').textContent='Show more';
document.getElementById(sv?'sv-slot':'ab-slot').appendChild(document.getElementById('markup'));document.getElementById('mk-title').textContent=sv?'4. Devices and Map':'Plan and Devices';
document.getElementById('log-title').textContent={Survey:'Notes',Service:'Problem & What Was Done','Office Task':'Notes'}[t]||'Notes & Updates';
document.getElementById('mat-title').textContent=t==='Service'?'Parts Used':'Materials';document.getElementById('mat-new').placeholder=t==='Service'?'Add part (e.g. 1x 4TB hard drive)':'Add material (e.g. 2x Maglock)';
document.getElementById('mat-legacy').textContent=currentJob.materials?'Earlier note: '+currentJob.materials:'';
document.getElementById('punch-wrap').classList.toggle('hidden',t==='Office Task');
const f=document.getElementById('finish');f.textContent=sv?'Complete Survey':t==='Office Task'?'Done':'Finish Job';f.classList.toggle('hidden',sv&&currentJob.status==='completed');mapSummary();renderDevices();if(!sv&&t!=='Office Task')resize()}
async function setType(t){if(!t)return;const r=await post('/type',{job_type:t});if(r.ok){const d=await r.json();currentJob.job_type=d.job_type;renderType()}else alert('Error')}
function addPunch(){const n=document.getElementById('punch-new');const t=n.value.trim();if(!t)return;(currentJob.punch_list=currentJob.punch_list||[]).push({text:t,done:false});n.value='';renderTicks('punch',currentJob.punch_list,savePunch,true);savePunch()}
async function assignMe(){const r=await post('/reassign',{tech:{{ name|tojson }}});if(r.ok){currentJob.tech={{ name|tojson }};const rt=document.getElementById('r-tech');if(rt)rt.value=currentJob.tech;alert('Assigned to you')}else alert('Error')}
async function copyJob(){const id=prompt('New Job ID (leave blank to auto-number)','');if(id===null)return;const sd=prompt('Scheduled date (YYYY-MM-DD, blank = today)','');if(sd===null)return;
const r=await post('/copy',{id:id.trim(),scheduled_date:sd.trim()});const d=await r.json().catch(()=>({}));if(r.ok){alert('Copied as '+d.id);openJob(d.id)}else alert(d.error||'Error')}
async function openJob(id){const r=await fetch('/api/jobs/'+encodeURIComponent(id));if(!r.ok){alert('Job not found');return}currentJob=await r.json();history.replaceState(null,'','/?job='+encodeURIComponent(currentJob.id));
renderPhotos();renderBlueprints();renderHelp();document.getElementById('support-note').value='';loadSupport();document.getElementById('help-form').classList.add('hidden');document.getElementById('help-note').value='';renderMats();renderTotals();fillSurvey();renderTicks('punch',currentJob.punch_list||[],savePunch,true);document.getElementById('punch-new').value='';document.getElementById('status').value=currentJob.status||'assigned';document.getElementById('hours').value='';
const pn=document.getElementById('private-notes');if(pn)pn.value=currentJob.private_notes||'';document.getElementById('print').href='/print/job/'+encodeURIComponent(currentJob.id);
document.getElementById('d-id').textContent=currentJob.id;document.getElementById('d-title').textContent=currentJob.title;
document.getElementById('d-customer').textContent=currentJob.customer;document.getElementById('d-address').textContent=currentJob.address;
document.getElementById('d-scope').textContent=currentJob.scope||'';document.getElementById('d-created').textContent=currentJob.created_by?'Created by '+currentJob.created_by:'';renderChecklist();loadUpdates();loadFollow();renderHub();renderReview();document.getElementById('sec-omw').style.display=['completed','review'].includes(currentJob.status)?'none':'';const rt=document.getElementById('r-tech');if(rt)rt.value=currentJob.tech||'';document.getElementById('update-note').value='';
const c=document.getElementById('call');const ph=(currentJob.phone||'').replace(/[^0-9+]/g,'');if(ph){c.href='tel:'+ph;c.textContent='Call Customer ('+currentJob.phone+')';c.style.display='block'}else{c.removeAttribute('href');c.style.display='none'}
document.getElementById('history').href='/clients?name='+encodeURIComponent(currentJob.customer||'');
document.getElementById('directions').href='https://maps.google.com/?q='+encodeURIComponent(currentJob.address);
document.getElementById('home').classList.add('hidden');document.getElementById('detail').classList.remove('hidden');window.scrollTo(0,0);renderType();clearSig()}
function showList(){history.replaceState(null,'','/');document.getElementById('detail').classList.add('hidden');document.getElementById('home').classList.remove('hidden');load()}
async function flushNote(){const n=document.getElementById('update-note');if(!n.value.trim())return true;
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/updates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:n.value})});if(r.ok){n.value='';loadUpdates();return true}alert('Error saving note');return false}
document.getElementById('finish').onclick=async()=>{const fd=new FormData();
fd.append('notes',currentJob.notes||'');
if(currentJob.job_type==='Survey'){if(!(await saveSurvey(true)))return;if(!(await flushNote()))return;const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/complete',{method:'POST',body:fd});if(r.ok){alert('Survey complete. Kelly has been told it is ready for an estimate.');openJob(currentJob.id)}else alert('Error');return}
if(!(await flushNote()))return;
fd.append('materials',currentJob.materials||'');
fd.append('hours',document.getElementById('hours').value||0);fd.append('miles',0);
fd.append('checklist',JSON.stringify(currentJob.checklist||[]));fd.append('signature',canvas.toDataURL());
const r=await fetch('/api/jobs/'+currentJob.id+'/complete',{method:'POST',body:fd});
if(r.ok){const d=await r.json().catch(()=>({}));alert(currentJob.job_type==='Office Task'?'Task done':d.status==='review'?'Job finished. The office will review it.':'Job finished');showList()}else alert('Error')};
async function addJob(){const v=k=>document.getElementById('n-'+k).value.trim();
const body={id:v('id'),title:v('title'),customer:v('customer'),address:v('address'),phone:v('phone'),tech:v('tech'),scope:v('scope'),scheduled_date:v('date'),checklist:v('checklist'),job_type:v('type')};
if(!body.id||!body.title){alert('Job ID and title are required');return}
const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(r.ok){['id','title','customer','address','phone','scope','date','checklist'].forEach(k=>document.getElementById('n-'+k).value='');document.getElementById('office').classList.add('hidden');load()}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}}
load();
const qj=new URLSearchParams(location.search).get('job');if(qj)openJob(qj);
</script></body></html>'''

@app.route('/')
def index():
    techs=[(k.title(),k.title()) for k in sorted(USERS) if k not in OFFICE_NAMES]+[(k.title(),k.title()+' (office)') for k in sorted(USERS) if k in OFFICE_NAMES]
    tpls=[]
    if is_office():
        conn=get_db(); tpls=[{'id':r['id'],'name':r['name'],'job_type':r['job_type'] or '','items':json.loads(r['items'] or '[]')} for r in conn.execute('SELECT * FROM checklist_templates ORDER BY name').fetchall()]; conn.close()
    agr=None
    if not is_office():
        conn=get_db(); a=current_agreement(conn); agr={'version':a['version'],'signed':signed_current(conn, me())}; conn.close()
    return render_template_string(HTML, agr=agr, name=me(), office=is_office(), techs=techs, statuses=STATUSES, job_types=JOB_TYPES, obstacles=SURVEY_OBSTACLES, dev_types=DEVICE_TYPES, tpls=tpls)

@app.route('/api/jobs')
def list_jobs():
    status=request.args.get('status','assigned'); q=(request.args.get('q') or '').strip().lower()
    conn=get_db()
    if q:
        rows=conn.execute("SELECT * FROM jobs WHERE INSTR(LOWER(IFNULL(customer,'')),?)>0 OR INSTR(LOWER(IFNULL(address,'')),?)>0 OR INSTR(LOWER(id),?)>0 ORDER BY (status='completed'), COALESCE(scheduled_date, created_at) DESC",(q,q,q)).fetchall()
    elif status=='completed': rows=conn.execute("SELECT * FROM jobs WHERE status='completed' ORDER BY completed_at DESC").fetchall()
    else: rows=conn.execute("SELECT * FROM jobs WHERE status!='completed' ORDER BY created_at DESC").fetchall()
    co=sub_companies(conn) if is_office() else {}; conn.close()
    out=[job_dict(r) for r in rows if can_see(r)]
    for j in out:
        if co: j['company']=co.get(ckey(j['tech']),'')
    return jsonify(out)

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
    if not is_office():
        old=clean_survey(row['survey_form'])
        for k in SURVEY_OFFICE_ONLY: form[k]=old[k]
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
    conn=get_db(); photos=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto') ORDER BY id",(job_id,)).fetchall()]; bps=blueprints(conn, job_id); plans=plan_files(conn, job_id); srcs=survey_plan_sources(conn, row)
    extra={}
    if is_office():
        n,c=(row['hub_nonce'],row['hub_created_at']) if row['hub_nonce'] and row['hub_created_at'] else hub_new(conn, job_id)
        extra=hub_info(job_id,n,c); extra['labor']=job_labor(conn,row)
    conn.close()
    return jsonify(dict(job_dict(row),photos=photos,blueprints=bps,plans=plans,survey_plan_sources=srcs,**extra))

@app.route('/api/jobs/<job_id>/updates', methods=['GET','POST'])
def job_updates(job_id):
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return ('',404)
    if not can_see(row): conn.close(); return jsonify({'error':'not your job'}),403
    if request.method=='POST':
        if row['job_type']=='Survey' and row['status']=='completed' and not is_office(): conn.close(); return jsonify({'error':'survey is complete; office can edit'}),403
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
                     (finish_status(row),notes,materials,sig,minutes,miles,now,job_id))
    for ptype in PHOTO_TYPES: save_photos(conn, job_id, request.files.getlist(ptype), ptype, now)
    if row['job_type']!='Survey' and minutes: add_sub_minutes(conn, row, minutes)
    if row['job_type']=='Survey' or finish_status(row)=='completed': lock_rates(conn, job_id)
    conn.commit()
    if row['job_type']=='Survey': notify_kelly(conn, job_id, kelly_event)
    st=conn.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()['status']; conn.close()
    return jsonify({'ok':True,'status':st})

def needs_review(row): return (row['job_type'] or '') not in ('Survey','Office Task')
def finish_status(row): return 'review' if needs_review(row) else 'completed'

@app.route('/api/jobs/<job_id>/approve', methods=['POST'])
def approve_job(job_id):
    if not is_office(): return jsonify({'error':'Office only'}),403
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return ('',404)
    if row['status']!='review': conn.close(); return jsonify({'error':'This job is not waiting for review'}),400
    now=datetime.utcnow().isoformat()
    conn.execute("UPDATE jobs SET status='completed',completed_at=COALESCE(completed_at,?),review_by=?,review_at=? WHERE id=?",(now,me(),now_local_str(),job_id)); lock_rates(conn, job_id); conn.commit()
    pkg=build_invoice_draft(conn, job_id, now) if needs_review(row) else None; conn.close()
    return jsonify({'ok':True,'status':'completed','invoice_draft':pkg})

def build_invoice_draft(conn, job_id, now):
    j=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    photos=[{'filename':p['filename'],'type':p['photo_type']} for p in conn.execute('SELECT filename,photo_type FROM photos WHERE job_id=?',(job_id,)).fetchall()]
    pkg={'job_id':job_id,'customer':j['customer'],'phone':j['phone'],'address':j['address'],'title':j['title'],'scope':j['scope'],
         'tech':j['tech'],'materials':j['materials'] or '','materials_list':parse_materials(j['materials_list']),'hours':round((j['total_minutes'] or 0)/60,2),'miles':j['total_miles'] or 0,
         'notes':notes_log(conn, j),'photos':photos,'completed_at':j['completed_at'],'status':'ready_for_review (not sent)'}
    conn.execute('INSERT OR REPLACE INTO invoice_drafts (job_id,package,status,created_at) VALUES (?,?,?,?)',(job_id,json.dumps(pkg),'ready_for_review',now))
    conn.commit()
    app.logger.warning('INVOICE_DRAFT_READY %s', json.dumps(pkg))
    return pkg

def add_sub_minutes(conn, row, delta):
    if not delta: return
    if not conn.execute('SELECT 1 FROM job_hours WHERE job_id=?',(row['id'],)).fetchone() and (row['total_minutes'] or 0) and ckey(row['tech']):
        conn.execute('INSERT INTO job_hours (job_id,sub,minutes) VALUES (?,?,?)',(row['id'],ckey(row['tech']),row['total_minutes']))
    who=ckey(row['tech'] or me()) if is_office() else ckey(me())
    conn.execute('INSERT INTO job_hours (job_id,sub,minutes) VALUES (?,?,0) ON CONFLICT(job_id,sub) DO NOTHING',(row['id'],who))
    conn.execute('UPDATE job_hours SET minutes=MAX(0,minutes+?) WHERE job_id=? AND sub=?',(int(delta),row['id'],who))

def job_labor(conn, row):
    hrs={r['sub']:r['minutes'] for r in conn.execute('SELECT sub,minutes FROM job_hours WHERE job_id=?',(row['id'],)).fetchall()}
    if not hrs and (row['total_minutes'] or 0) and ckey(row['tech']): hrs={ckey(row['tech']):row['total_minutes']}
    if ckey(row['tech']): hrs.setdefault(ckey(row['tech']),0)
    rates={r['key']:{'h':r['rate_hourly'],'f':r['rate_flat']} for r in conn.execute('SELECT * FROM subs').fetchall()}
    try: locked=json.loads(row['labor_rates'] or '{}') if row['status']=='completed' else {}
    except Exception: locked={}
    lines=[]
    for k,m in hrs.items():
        r=locked.get(k) or rates.get(k); rh=(r['h'] if r else None); rf=(r['f'] if r else None); h=round(m/60,2)
        lines.append({'sub':k.title(),'hours':h,'rate_hourly':rh,'rate_flat':rf,'no_rate':rh is None and rf is None,'cost':round(h*(rh or 0)+(rf or 0),2)})
    return {'lines':lines,'total':round(sum(l['cost'] for l in lines),2),'locked':bool(locked)}

def lock_rates(conn, job_id):
    row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    keys={r['sub'] for r in conn.execute('SELECT sub FROM job_hours WHERE job_id=?',(job_id,)).fetchall()}
    if ckey(row['tech']): keys.add(ckey(row['tech']))
    rates={r['key']:{'h':r['rate_hourly'],'f':r['rate_flat']} for r in conn.execute('SELECT * FROM subs').fetchall()}
    conn.execute('UPDATE jobs SET labor_rates=? WHERE id=?',(json.dumps({k:rates.get(k,{'h':None,'f':None}) for k in keys}),job_id))

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
    old=row['total_minutes'] or 0
    if d.get('mode')=='set':
        conn.execute('UPDATE jobs SET total_minutes=?,total_miles=? WHERE id=?',(max(0,int(round(hours*60))),max(0.0,miles),job_id))
    else:
        conn.execute('UPDATE jobs SET total_minutes=MAX(0,IFNULL(total_minutes,0)+?),total_miles=MAX(0,IFNULL(total_miles,0)+?) WHERE id=?',(int(round(hours*60)),miles,job_id))
    j=conn.execute('SELECT total_minutes,total_miles FROM jobs WHERE id=?',(job_id,)).fetchone()
    add_sub_minutes(conn, row, (j['total_minutes'] or 0)-old); conn.commit()
    out={'ok':True,'hours':round((j['total_minutes'] or 0)/60,2),'total_miles':j['total_miles'] or 0}
    if is_office(): out['labor']=job_labor(conn, conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
    conn.close(); return jsonify(out)

@app.route('/api/jobs/<job_id>/photos', methods=['POST'])
def upload_photos(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    ptype=request.form.get('type','')
    if ptype not in PHOTO_TYPES and not (ptype in SURVEY_PHOTO_TYPES and row['job_type']=='Survey'): conn.close(); return jsonify({'error':'bad photo label'}),400
    if row['job_type']=='Survey' and row['status']=='completed' and not is_office(): conn.close(); return jsonify({'error':'survey is complete; office can edit'}),403
    save_photos(conn, job_id, request.files.getlist('photos'), ptype, datetime.utcnow().isoformat()); conn.commit()
    photos=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto') ORDER BY id",(job_id,)).fetchall()]; conn.close()
    return jsonify({'ok':True,'photos':photos})

def blueprints(conn, job_id):
    out=[]
    for b in conn.execute("SELECT filename,orig_name FROM photos WHERE job_id=? AND photo_type='blueprint' ORDER BY id",(job_id,)).fetchall():
        ext=os.path.splitext(b['filename'])[1].lower()
        out.append({'filename':b['filename'],'name':b['orig_name'] or b['filename'],'image':ext in ('.png','.jpg','.jpeg','.webp')})
    return out

def plan_files(conn, job_id):
    return [r['filename'] for r in conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type='plan' ORDER BY id",(job_id,)).fetchall()]

def survey_edit_row(conn, job_id):
    row,err=own_job(conn, job_id)
    if err: return None, err
    if row['job_type']=='Office Task': return None, (jsonify({'error':'not for Office Task jobs'}),400)
    if row['job_type']=='Survey' and row['status']=='completed' and not is_office(): return None, (jsonify({'error':'survey is complete; office can edit'}),403)
    return row, None

@app.route('/api/jobs/<job_id>/plans', methods=['POST'])
def upload_plans(job_id):
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    files=[f for f in request.files.getlist('files') if f and f.filename]
    if not files: conn.close(); return jsonify({'error':'no file'}),400
    exts=[os.path.splitext(f.filename)[1].lower() or '.jpg' for f in files]
    if any(e not in PLAN_EXTS for e in exts): conn.close(); return jsonify({'error':'Image files only (jpg, png, webp)'}),400
    now=datetime.utcnow().isoformat()
    for f,e in zip(files,exts):
        fn=secure_filename(f"{job_id}_plan_{uuid.uuid4().hex[:8]}{e}")
        f.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
        conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at,orig_name) VALUES (?,?,?,?,?)',(job_id,fn,'plan',now,os.path.basename(f.filename)[:200]))
    conn.commit(); plans=plan_files(conn, job_id); conn.close()
    return jsonify({'ok':True,'plans':plans})

@app.route('/api/jobs/<job_id>/plans/delete', methods=['POST'])
def delete_plan(job_id):
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    fn=secure_filename(str((request.get_json(silent=True) or {}).get('filename') or ''))
    if not fn or fn not in plan_files(conn, job_id): conn.close(); return ('',404)
    conn.execute("DELETE FROM photos WHERE job_id=? AND filename=? AND photo_type='plan'",(job_id,fn))
    devs=[d for d in parse_devices(row['survey_devices']) if d['plan']!=fn]
    conn.execute('UPDATE jobs SET survey_devices=? WHERE id=?',(json.dumps(devs),job_id)); prune_devphotos(conn, job_id, devs); conn.commit()
    try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'],fn))
    except OSError: pass
    plans=plan_files(conn, job_id); conn.close()
    return jsonify({'ok':True,'plans':plans,'devices':devs})

@app.route('/api/jobs/<job_id>/devices', methods=['POST'])
def save_devices(job_id):
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    plans=set(plan_files(conn, job_id))
    devs=number_devices([d for d in parse_devices((request.get_json(silent=True) or {}).get('devices') or []) if d['plan'] in plans])
    dp=devphoto_files(conn, job_id)
    for d in devs:
        if d['photo'] not in dp: d['photo']=''
    conn.execute('UPDATE jobs SET survey_devices=? WHERE id=?',(json.dumps(devs),job_id)); prune_devphotos(conn, job_id, devs); conn.commit(); conn.close()
    return jsonify({'ok':True,'devices':devs})

@app.route('/api/jobs/<job_id>/map', methods=['POST'])
def save_map(job_id):
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    d=request.get_json(silent=True) or {}
    m=parse_map({'cameras':d.get('cameras'),'runs':d.get('runs'),'view':d.get('view'),'geo':parse_map(row['map_data'])['geo']})
    store={'cameras':m['cameras'],'runs':[{k:r[k] for k in ('id','label','pts','slack')} for r in m['runs']],'view':m['view'],'geo':m['geo']}
    conn.execute('UPDATE jobs SET map_data=? WHERE id=?',(json.dumps(store),job_id)); conn.commit(); conn.close()
    return jsonify({'ok':True,'map':m})

GEO_LOCK=threading.Lock(); GEO_LAST=[0.0]
def nominatim(q):
    with GEO_LOCK:
        wait=1.1-(time.time()-GEO_LAST[0])
        if wait>0: time.sleep(wait)
        try:
            req=urllib.request.Request('https://nominatim.openstreetmap.org/search?'+urllib.parse.urlencode({'format':'json','limit':1,'q':q}),
                headers={'User-Agent':'TrueSense-FieldOps/1.0 (truesense-fieldops.onrender.com)'})
            res=json.loads(urllib.request.urlopen(req, timeout=8).read().decode())
        finally: GEO_LAST[0]=time.time()
    return (float(res[0]['lat']),float(res[0]['lon'])) if res else None

def set_geo(conn, row, g):
    raw=json.loads(row['map_data']) if row['map_data'] else {}
    raw=raw if isinstance(raw,dict) else {}
    raw['geo']=g; conn.execute('UPDATE jobs SET map_data=? WHERE id=?',(json.dumps(raw),row['id'])); conn.commit()

@app.route('/api/jobs/<job_id>/geocode', methods=['POST'])
def geocode_job(job_id):
    q=str((request.get_json(silent=True) or {}).get('q') or '').strip()[:200]
    conn=get_db(); row,err=(survey_edit_row if q else own_job)(conn, job_id)
    if err: conn.close(); return err
    addr=(row['address'] or '').strip(); g=parse_map(row['map_data'])['geo']
    if not q and not addr: conn.close(); return jsonify({'error':'no address'}),404
    if q or not (g and g.get('addr')==addr):
        try: hit=nominatim(q or addr)
        except Exception as e:
            app.logger.warning('Geocode failed for %s: %s', job_id, e); conn.close(); return jsonify({'error':'lookup failed'}),502
        if hit: g={'addr':addr,'q':q or addr,'lat':hit[0],'lng':hit[1]}
        elif q: conn.close(); return jsonify({'error':'address not found'}),404
        else: g={'addr':addr,'q':addr,'lat':None,'lng':None}
        set_geo(conn, row, g)
    conn.close()
    return (jsonify({'ok':True,'geo':g,'lat':g['lat'],'lng':g['lng']}) if g.get('lat') is not None else (jsonify({'error':'address not found','geo':g}),404))

@app.route('/api/jobs/<job_id>/site', methods=['POST'])
def set_site(job_id):
    d=request.get_json(silent=True) or {}
    lat=_num(d.get('lat'),-90,90,None); lng=_num(d.get('lng'),-180,180,None)
    if lat is None or lng is None: return jsonify({'error':'bad location'}),400
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    g={'addr':(row['address'] or '').strip(),'q':'My location','lat':round(lat,7),'lng':round(lng,7)}
    set_geo(conn, row, g); conn.close()
    return jsonify({'ok':True,'geo':g})

def devphoto_files(conn, job_id):
    return {r['filename'] for r in conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type='devphoto'",(job_id,)).fetchall()}

def prune_devphotos(conn, job_id, devs):
    keep={d['photo'] for d in devs if d['photo']}
    for fn in devphoto_files(conn, job_id)-keep:
        conn.execute("DELETE FROM photos WHERE job_id=? AND filename=? AND photo_type='devphoto'",(job_id,fn))
        try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'],fn))
        except OSError: pass

@app.route('/api/jobs/<job_id>/devices/<dev_id>/photo', methods=['POST'])
def device_photo(job_id, dev_id):
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    f=request.files.get('photo')
    if not f or not f.filename: conn.close(); return jsonify({'error':'no file'}),400
    devs=parse_devices(row['survey_devices']); d=next((x for x in devs if x['id']==dev_id),None)
    if not d: conn.close(); return ('',404)
    fn=secure_filename(f"{job_id}_devphoto_{uuid.uuid4().hex[:8]}.jpg"); f.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
    conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at) VALUES (?,?,?,?)',(job_id,fn,'devphoto',datetime.utcnow().isoformat()))
    d['photo']=fn; conn.execute('UPDATE jobs SET survey_devices=? WHERE id=?',(json.dumps(devs),job_id)); prune_devphotos(conn, job_id, devs); conn.commit(); conn.close()
    return jsonify({'ok':True,'devices':devs})

def survey_plan_sources(conn, row):
    if row['job_type'] in ('Survey','Office Task'): return []
    cust=ckey(row['customer']); addr=(row['address'] or '').strip().lower()
    if not cust and not addr: return []
    rows=conn.execute("SELECT id,title FROM jobs WHERE job_type='Survey' AND id!=? AND ((?!='' AND LOWER(TRIM(IFNULL(customer,'')))=?) OR (?!='' AND LOWER(TRIM(IFNULL(address,'')))=?)) AND EXISTS (SELECT 1 FROM photos p WHERE p.job_id=jobs.id AND p.photo_type='plan') ORDER BY created_at DESC",
                      (row['id'],cust,cust,addr,addr)).fetchall()
    return [{'id':r['id'],'title':r['title'] or ''} for r in rows]

def copy_plans(conn, src_id, job_id, devs):
    src=conn.execute('SELECT survey_devices FROM jobs WHERE id=?',(src_id,)).fetchone(); now=datetime.utcnow().isoformat(); mp={}
    for fn in plan_files(conn, src_id):
        nf=secure_filename(f"{job_id}_plan_{uuid.uuid4().hex[:8]}{os.path.splitext(fn)[1]}")
        try: shutil.copyfile(os.path.join(app.config['UPLOAD_FOLDER'],fn),os.path.join(app.config['UPLOAD_FOLDER'],nf))
        except OSError: continue
        conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at,orig_name) VALUES (?,?,?,?,?)',(job_id,nf,'plan',now,'From survey '+src_id)); mp[fn]=nf
    if not mp: return None
    for d in parse_devices(src['survey_devices']):
        if d['plan'] in mp: devs.append(dict(d,id='',plan=mp[d['plan']],installed=False,mac='',ip='',port='',clabel='',photo=''))
    return number_devices(devs)

def fu_dict(r): return {'id':r['id'],'job_id':r['job_id'],'due':r['due'],'note':r['note'],'done':bool(r['done']),'done_by':r['done_by'],'done_at':r['done_at']}

@app.route('/api/followups')
def list_followups():
    if not is_office(): return jsonify({'error':'office only'}),403
    conn=get_db(); rows=conn.execute("SELECT f.*,j.customer,j.title FROM followups f JOIN jobs j ON j.id=f.job_id WHERE f.done=0 ORDER BY f.due,f.id").fetchall(); conn.close()
    return jsonify([dict(fu_dict(r),customer=r['customer'] or '',title=r['title'] or '') for r in rows])

@app.route('/api/jobs/<job_id>/followups', methods=['GET','POST'])
def job_followups(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    conn=get_db()
    if not conn.execute('SELECT 1 FROM jobs WHERE id=?',(job_id,)).fetchone(): conn.close(); return ('',404)
    if request.method=='POST':
        d=request.get_json(silent=True) or {}; note=str(d.get('note') or '').strip()[:1000]
        try: due=datetime.strptime(str(d.get('due') or ''),'%Y-%m-%d').date().isoformat()
        except ValueError: conn.close(); return jsonify({'error':'pick a date'}),400
        if not note: conn.close(); return jsonify({'error':'note required'}),400
        conn.execute('INSERT INTO followups (job_id,due,note,created_by,created_at) VALUES (?,?,?,?,?)',(job_id,due,note,me(),now_local_str())); conn.commit()
    rows=conn.execute('SELECT * FROM followups WHERE job_id=? ORDER BY done,due,id',(job_id,)).fetchall(); conn.close()
    return jsonify([fu_dict(r) for r in rows])

@app.route('/api/followups/<int:fid>/done', methods=['POST'])
def followup_done(fid):
    if not is_office(): return jsonify({'error':'office only'}),403
    conn=get_db(); n=conn.execute('UPDATE followups SET done=1,done_by=?,done_at=? WHERE id=?',(me(),now_local_str(),fid)).rowcount; conn.commit(); conn.close()
    return (jsonify({'ok':True}) if n else ('',404))

@app.route('/api/jobs/<job_id>/to-job', methods=['POST'])
def survey_to_job(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    conn=get_db(); src=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not src: conn.close(); return ('',404)
    if src['job_type']!='Survey' or src['status']!='completed': conn.close(); return jsonify({'error':'only a completed survey'}),400
    nid=str((request.get_json(silent=True) or {}).get('id') or '').strip()[:40]
    if nid:
        if conn.execute('SELECT 1 FROM jobs WHERE id=?',(nid,)).fetchone(): conn.close(); return jsonify({'error':'That Job ID already exists'}),409
    else:
        n=1
        while conn.execute('SELECT 1 FROM jobs WHERE id=?',(f'{job_id}-I{n}' if n>1 else f'{job_id}-I',)).fetchone(): n+=1
        nid=f'{job_id}-I{n}' if n>1 else f'{job_id}-I'
    sv=clean_survey(src['survey_form']); devs=parse_devices(src['survey_devices'])
    scope='\n\n'.join(x for x in [('What they want:\n'+sv['want_desc']) if sv['want_desc'] else '',('Site conditions:\n'+site_conditions(sv)) if site_conditions(sv) else '','From survey '+job_id] if x)
    mats=[{'text':f'{n}x {t}','used':False} for t,n in dev_counts(devs).items()]
    chk=[{'text':f"Mount and test {d['label']} ({d['type']})",'done':False} for d in devs]+[{'text':t,'done':False} for t in DEFAULT_CHECKLIST]
    title=('Install: '+(src['title'] or src['customer'] or job_id))[:200]
    conn.execute('INSERT INTO jobs (id,title,customer,address,phone,scope,tech,job_type,status,checklist,materials_list,created_at,scheduled_date,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (nid,title,src['customer'],src['address'],src['phone'] or sv['contact_phone'],scope,src['tech'],'Install','assigned',json.dumps(chk),json.dumps(mats),datetime.utcnow().isoformat(),None,me()))
    nd=copy_plans(conn, job_id, nid, []) if plan_files(conn, job_id) else []
    conn.execute('UPDATE jobs SET survey_devices=? WHERE id=?',(json.dumps(nd or []),nid)); conn.commit(); conn.close()
    return jsonify({'ok':True,'id':nid}),201

@app.route('/api/jobs/<job_id>/plans/copy', methods=['POST'])
def copy_survey_plan(job_id):
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    src_id=str((request.get_json(silent=True) or {}).get('from') or '')
    if src_id not in [x['id'] for x in survey_plan_sources(conn, row)]: conn.close(); return jsonify({'error':'no matching survey plan'}),400
    devs=copy_plans(conn, src_id, job_id, parse_devices(row['survey_devices']))
    if devs is None: conn.close(); return jsonify({'error':'survey plan files are missing'}),400
    conn.execute('UPDATE jobs SET survey_devices=? WHERE id=?',(json.dumps(devs),job_id)); conn.commit(); plans=plan_files(conn, job_id); conn.close()
    return jsonify({'ok':True,'plans':plans,'devices':devs})

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

HUB_DAYS = 30
def hub_token(job_id, nonce, created): return hmac.new(app.secret_key.encode(), ('hub:'+job_id+':'+nonce+':'+created).encode(), hashlib.sha256).hexdigest()[:32]
def hub_now(): return datetime.utcnow()
def hub_new(conn, job_id):
    n=uuid.uuid4().hex[:12]; c=hub_now().replace(microsecond=0).isoformat()
    conn.execute('UPDATE jobs SET hub_nonce=?,hub_created_at=? WHERE id=?',(n,c,job_id)); conn.commit()
    return n, c
def hub_info(job_id, n, c):
    exp=datetime.fromisoformat(c)+timedelta(days=HUB_DAYS)
    try:
        from zoneinfo import ZoneInfo
        el=exp.replace(tzinfo=__import__('datetime').timezone.utc).astimezone(ZoneInfo('America/Chicago'))
    except Exception: el=exp
    return {'hub_url':'/hub/'+urllib.parse.quote(job_id)+'?t='+hub_token(job_id,n,c),'hub_expires':el.strftime('%b %d, %Y').replace(' 0',' ')}

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
    fns=[r['filename'] for r in conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto') ORDER BY id",(job_id,)).fetchall()]
    mats=[m['text'] for m in parse_materials(j['materials_list'])]
    body={'event':event,'job_id':j['id'],'title':j['title'] or '','customer':j['customer'] or '','address':j['address'] or '',
          'phone':j['phone'] or '','scope':j['scope'] or '','sent_by':me(),
          'what_they_want':sv['want_desc'],
          'materials_notes':j['survey_office'] or '','materials_list':mats,
          'existing_conditions':{'current_system':sv['current_system'],'power':sv['power'],'network':sv['network'],'conduit':sv['conduit']},
          'obstacles':sv['obstacles'],'obstacle_notes':sv['obstacle_notes'],
          'site_contact':{'name':sv['contact_name'],'phone':sv['contact_phone']},
          'client_expectations':{'timeline':sv['timeline'],'budget':sv['budget'],'decision_maker':sv['decision_maker']},
          'site_conditions':site_conditions(sv),
          'notes':notes_log(conn, j),'quick_notes':j['quick_notes'] or '',
          'photo_urls':[f"{base}/help-photo/{fn}?t={help_photo_token(fn)}" for fn in fns],
          'print_url':f"{base}/print/job/{j['id']}",
          'plan_urls':[f"{base}/help-photo/{fn}?t={help_photo_token(fn)}" for fn in plan_files(conn, j['id'])],
          'devices':[{'type':d['type'],'label':d['label'],'note':d['note'],'height':d['height'],'cable':d['cable'],
                      'plan_url':f"{base}/help-photo/{d['plan']}?t={help_photo_token(d['plan'])}"} for d in parse_devices(j['survey_devices'])],
          'device_counts':dev_counts(parse_devices(j['survey_devices'])),
          'map_cameras':[{'label':c['label'],'lat':c['lat'],'lng':c['lng'],'direction_deg':c['dir'],'direction':compass(c['dir']),'fov_deg':c['fov'],'range_ft':c['range']} for c in parse_map(j['map_data'])['cameras']],
          'map_runs':[{'label':r['label'],'segments_ft':r['segments_ft'],'total_ft':r['total_ft'],'slack_pct':r['slack'],'adjusted_ft':r['adjusted_ft']} for r in parse_map(j['map_data'])['runs']]}
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
    if st=='completed' and needs_review(row) and not is_office(): st='review'
    if st in ('completed','review'):
        conn.execute('UPDATE jobs SET status=?,completed_at=COALESCE(completed_at,?) WHERE id=?',(st,now,job_id))
        if st=='completed' and row['status']!='completed' and needs_review(row): conn.execute('UPDATE jobs SET review_by=?,review_at=? WHERE id=?',(me(),now_local_str(),job_id))
        if st=='completed' and row['status']!='completed': lock_rates(conn, job_id)
        if st=='review': conn.execute('UPDATE jobs SET labor_rates=NULL WHERE id=?',(job_id,))
        conn.commit()
        if st=='completed' and row['status']!='completed' and needs_review(row): build_invoice_draft(conn, job_id, now)
    else:
        conn.execute('UPDATE jobs SET status=?,completed_at=NULL,labor_rates=NULL WHERE id=?',(st,job_id)); conn.commit()
    conn.close(); return jsonify({'ok':True,'status':st,'status_label':STATUS_LABEL[st]})

@app.route('/api/jobs/<job_id>/private-notes', methods=['POST'])
def save_private_notes(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    note=((request.get_json(silent=True) or {}).get('private_notes') or '')[:5000]
    conn=get_db(); n=conn.execute('UPDATE jobs SET private_notes=? WHERE id=?',(note,job_id)).rowcount; conn.commit(); conn.close()
    return (jsonify({'ok':True}) if n else ('',404))

CLIENTS_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Clients</title>
<style>body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:12px}
.job{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:16px;margin-bottom:12px}
.job-id{color:#a49f2a;font-weight:700;font-size:.8rem}a{color:#111}.muted{color:#666}
.btn{display:block;background:#f0f0f0;color:#111;padding:14px;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;margin:8px 0}
.client{display:block;text-decoration:none}h2{color:#a49f2a;margin:8px 0 12px}img{width:80px;height:80px;object-fit:cover;border-radius:8px;margin:4px 4px 0 0}
input{width:100%;padding:12px;border:1px solid #e5e5e5;border-radius:10px;margin:6px 0;font-size:1rem;box-sizing:border-box}
.sec{font-weight:600;margin-top:10px;font-size:.9rem}.badge{font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;background:#f0f0f0;color:#333}.folder{font-weight:700;color:#a49f2a;margin:16px 0 6px}summary{cursor:pointer}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;width:auto;object-fit:contain;margin:0 auto 4px;display:block;border-radius:0">
{% macro bplist(j) %}{% if j.job_type != 'Office Task' and j.blueprints %}<div class="sec">Blueprints ({{ j.blueprints|length }})</div>{% for b in j.blueprints %}<div style="margin:4px 0"><a href="/uploads/{{ b.filename|urlencode }}" target="_blank">{% if b.image %}<img src="/uploads/{{ b.filename|urlencode }}" alt="blueprint" style="width:56px;height:56px;vertical-align:middle">{% else %}&#128196;{% endif %} {{ b.name }}</a></div>{% endfor %}{% endif %}{% endmacro %}
{% macro photogroups(j) %}{% if j.job_type == 'Survey' %}{% for p in j.photos %}<a href="/uploads/{{ p.filename|urlencode }}" target="_blank"><img src="/uploads/{{ p.filename|urlencode }}" alt="photo"></a>{% else %}<div class="muted">None</div>{% endfor %}{% else %}{% for t,lbl in [('before','Before'),('during','During'),('after','After'),('doors','Doors'),('power','Power'),('network','Network'),('proposed','Proposed locations')] %}{% set g = j.photos|selectattr('photo_type','equalto',t)|list %}{% if loop.index <= 3 or g %}<div class="muted" style="font-size:.85rem;margin-top:4px">{{ lbl }} ({{ g|length }})</div>{% endif %}{% for p in g %}<a href="/uploads/{{ p.filename|urlencode }}" target="_blank"><img src="/uploads/{{ p.filename|urlencode }}" alt="{{ t }}"></a>{% endfor %}{% endfor %}{% endif %}{% endmacro %}
{% macro jobcard(j, open) %}<details class="job"{% if open %} open{% endif %}><summary><span class="job-id">{{ j.id }}</span> {% if office and j.help_open %}<span class="badge" style="background:#b00020;color:#fff;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px">Needs Help</span> {% endif %}<span class="badge">{{ j.status_label }}</span>{% if j.job_type %} <span class="badge">{{ j.job_type }}</span>{% endif %} <b>{{ j.title }}</b><div class="muted" style="font-size:.85rem">Sub: {{ j.tech or '-' }}{% if j.job_type not in ('Survey','Office Task') %} &middot; {{ j.hours }} hrs{% endif %}{% if j.scheduled_date %} &middot; {{ j.scheduled_date }}{% endif %}{% if j.completed_at %} &middot; Done {{ j.completed_at[:10] }}{% endif %}{% if j.created_by %} &middot; Created by {{ j.created_by }}{% endif %}</div></summary>
{% if office and j.help_open %}<div style="border:2px solid #b00020;background:#fff5f5;border-radius:10px;padding:8px;margin-top:8px"><div style="color:#b00020;font-weight:700">&#9888; Needs Help</div><div class="muted" style="font-size:.8rem">{{ j.help_by }} &middot; {{ j.help_at }}</div><div style="white-space:pre-wrap">{{ j.help_note }}</div>{% if j.help_photo %}<a href="/uploads/{{ j.help_photo|urlencode }}" target="_blank"><img src="/uploads/{{ j.help_photo|urlencode }}" alt="help photo"></a>{% endif %}</div>{% endif %}
{% if j.job_type == 'Survey' %}<a class="btn" style="background:#6a1b9a;color:#fff" href="/?job={{ j.id|urlencode }}">Open Survey</a>{% endif %}
{% if j.scope %}<div class="sec">Scope</div><div style="white-space:pre-wrap">{{ j.scope }}</div>{% endif %}
<div class="sec">{{ {'Service':'Problem &amp; What Was Done','Survey':'Notes','Office Task':'Notes'}.get(j.job_type, 'Notes &amp; Updates')|safe }}</div>{% for u in j.updates %}<div style="padding:4px 0;border-bottom:1px solid #eee"><div class="muted" style="font-size:.8rem">{{ u.created_at }} &middot; {{ u.author }}</div><div style="white-space:pre-wrap">{{ u.note }}</div></div>{% endfor %}{% if j.notes %}<div style="white-space:pre-wrap">{{ j.notes }}</div>{% endif %}{% if not j.notes and not j.updates %}<div class="muted">None</div>{% endif %}
{% if j.job_type == 'Survey' %}{% set s = j.survey %}{% if j.hub_approved_at %}<div style="font-weight:700;color:#2e7d32">Estimate approved by {{ j.hub_approved_name or 'client' }} on {{ j.hub_approved_at }}</div>{% endif %}<div class="sec">Estimating Site Survey</div><div style="border-left:3px solid #a49f2a;padding-left:8px">
<div><b>Site contact:</b> {{ s.contact_name or '-' }}{% if s.contact_phone %} &middot; {% if s.contact_tel %}<a href="tel:{{ s.contact_tel }}">{{ s.contact_phone }}</a>{% else %}{{ s.contact_phone }}{% endif %}{% endif %}</div>
{% if s.want_desc %}<div><b>What they want:</b> <span style="white-space:pre-wrap">{{ s.want_desc }}</span></div>{% endif %}
<div style="margin-top:4px"><b>Site conditions:</b></div><div style="white-space:pre-wrap">{{ s.conditions or '-' }}</div>
{% if office and (s.timeline or s.budget or s.decision_maker) %}<div style="margin-top:4px" class="muted">Earlier entry (office only): Timeline {{ s.timeline or '-' }} &middot; Budget {{ s.budget or '-' }} &middot; Decision maker {{ s.decision_maker or '-' }}</div>{% endif %}
{% if office and j.survey_office %}<div style="margin-top:4px"><b>Rough materials &amp; labor (office only):</b></div><div style="white-space:pre-wrap">{{ j.survey_office }}</div>{% endif %}</div>{% endif %}
{% if office and j.quick_notes %}<div class="sec">Quick Notes</div><div style="white-space:pre-wrap">{{ j.quick_notes }}</div>{% endif %}
{% if office and j.punch_list and j.job_type != 'Survey' %}<div class="sec">Punch Items ({{ j.punch_list|selectattr('done')|list|length }}/{{ j.punch_list|length }})</div>{% for c in j.punch_list %}<div>{{ '&#9745;'|safe if c.done else '&#9744;'|safe }} {{ c.text }}</div>{% endfor %}{% endif %}
{% if office and j.private_notes %}<div class="sec">Private Office Notes</div><div style="white-space:pre-wrap">{{ j.private_notes }}</div>{% endif %}
{% if j.job_type not in ('Survey','Office Task') %}{% if j.materials or j.materials_list %}<div class="sec">{{ 'Parts Used' if j.job_type == 'Service' else 'Materials' }}</div>{% for m in j.materials_list %}<div>{{ '&#9745;'|safe if m.used else '&#9744;'|safe }} {{ m.text }}</div>{% endfor %}{% if j.materials %}<div>{{ j.materials }}</div>{% endif %}{% endif %}{% endif %}
{% if j.job_type in ('Install','Office Task','') or (j.job_type == 'Service' and j.checklist|selectattr('done')|list) %}<div class="sec">Checklist ({{ j.checklist|selectattr('done')|list|length }}/{{ j.checklist|length }})</div>{% for c in j.checklist %}<div>{{ '&#9745;'|safe if c.done else '&#9744;'|safe }} {{ c.text }}</div>{% endfor %}{% endif %}
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
<div style="margin-top:4px;font-weight:600">Total: {{ (jobs|sum(attribute='hours'))|round(2) }} hrs</div></div>
<a class="btn" href="/print/client?name={{ client|urlencode }}" target="_blank">Print Summary</a>
{% if office %}<form method="post" action="/clients/delete" onsubmit="return confirm('Delete this client and ALL of its jobs, photos and notes? This cannot be undone.')"><input type="hidden" name="name" value="{{ client }}"><button class="btn" type="submit" style="width:100%;border:0;background:#b00020;color:#fff;font-size:1rem">Delete Client</button></form>{% endif %}
<div class="folder">Active Jobs</div>{% for j in active %}{{ jobcard(j, true) }}{% else %}<div class="muted" style="margin-bottom:12px">None</div>{% endfor %}
<div class="folder">Completed Surveys</div>{% for j in surveys %}{{ jobcard(j, false) }}{% else %}<div class="muted" style="margin-bottom:12px">None</div>{% endfor %}
<div class="folder">Completed Jobs</div>{% for j in done %}{{ jobcard(j, false) }}{% else %}<div class="muted">None</div>{% endfor %}
{% endif %}
</body></html>'''

def ckey(name): return (name or '').strip().lower()

SUB_DOCS = [('insurance', 'Insurance'), ('license', 'License')]
def sub_names(conn):
    n=[k.title() for k in sorted(USERS) if k not in OFFICE_NAMES]
    for r in conn.execute("SELECT DISTINCT TRIM(tech) t FROM jobs WHERE status!='completed' AND IFNULL(TRIM(tech),'')!=''").fetchall():
        if r['t'].lower() not in [x.lower() for x in n] and r['t'].lower() not in OFFICE_NAMES: n.append(r['t'].title())
    return n
def doc_alerts(conn):
    soon=(today_local()+timedelta(days=30)).isoformat(); t=today_local().isoformat(); out=[]
    names={x.lower() for x in sub_names(conn)}
    for r in conn.execute("SELECT * FROM sub_docs WHERE IFNULL(expires,'')!='' AND expires<=? ORDER BY expires",(soon,)).fetchall():
        if r['sub'] in names: out.append({'sub':r['sub'].title(),'kind':dict(SUB_DOCS).get(r['kind'],r['kind']),'expires':r['expires'],'expired':r['expires']<t,'missing':False})
    have={(r['sub'],r['kind']) for r in conn.execute("SELECT sub,kind FROM sub_docs WHERE IFNULL(filename,'')!=''").fetchall()}
    for n in sub_names(conn):
        for k,l in SUB_DOCS:
            if (n.lower(),k) not in have: out.append({'sub':n,'kind':l,'expires':'','expired':False,'missing':True})
    return out

def sub_companies(conn): return {r['key']:r['company'] for r in conn.execute("SELECT key,company FROM subs WHERE IFNULL(company,'')!=''").fetchall()}

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
    return [job_full(conn, r) for r in rows if can_see(r)]

def job_full(conn, r):
    d=job_dict(r)
    d['updates']=[dict(u) for u in conn.execute('SELECT author,note,created_at FROM job_updates WHERE job_id=? ORDER BY id DESC',(r['id'],)).fetchall()]
    d['photos']=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto') ORDER BY id",(r['id'],)).fetchall()]
    d['blueprints']=blueprints(conn, r['id']); d['plans']=plan_files(conn, r['id'])
    return d

CAL_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Calendar</title>
<style>body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:12px}
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
<style>body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;color:#111;max-width:760px;margin:auto;padding:16px}h1{font-size:1.3rem;margin:8px 0}h2{font-size:1.1rem;color:#a49f2a;margin:20px 0 4px;border-bottom:2px solid #a49f2a}
.sec{font-weight:700;margin-top:10px}.muted{color:#666}img{width:120px;height:120px;object-fit:cover;margin:4px 4px 0 0;border-radius:6px}
.btn{display:block;width:100%;background:#a49f2a;color:#fff;border:none;padding:16px;border-radius:10px;font-size:1.05rem;font-weight:600;margin:8px 0}
.job{page-break-inside:avoid;margin-bottom:16px}@media print{.noprint{display:none}}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;display:block">
<button class="btn noprint" onclick="window.print()">Print</button>
<h1>{{ heading }}</h1>{% if info %}<div>{{ info.address }}</div><div>{{ info.phone }}</div>{% endif %}
<div class="muted">{{ jobs|length }} job{{ '' if jobs|length == 1 else 's' }} &middot; Total {{ (jobs|sum(attribute='hours'))|round(2) }} hrs &middot; Printed {{ printed }}</div>
{% for j in jobs %}<div class="job"><h2>{{ j.id }} &middot; {{ j.title }}</h2>
<div>Status: <b>{{ j.status_label }}</b>{% if j.job_type %} &middot; Type: <b>{{ j.job_type }}</b>{% endif %} &middot; Tech: {{ j.tech or '-' }}{% if j.scheduled_date %} &middot; Scheduled {{ j.scheduled_date }}{% endif %}{% if j.completed_at %} &middot; Done {{ j.completed_at[:10] }}{% endif %}</div>
<div>{{ j.customer }} &middot; {{ j.address }}{% if j.phone %} &middot; {{ j.phone }}{% endif %}</div>
{% if j.job_type not in ('Survey','Office Task') %}<div>Hours: {{ j.hours }}</div>{% endif %}
{% if j.scope %}<div class="sec">Scope</div><div style="white-space:pre-wrap">{{ j.scope }}</div>{% endif %}
{% set lt = {'Service':'Problem &amp; What Was Done','Survey':'Notes','Office Task':'Notes'}.get(j.job_type, 'Notes &amp; Updates') %}{% if j.notes or j.updates %}<div class="sec">{{ lt|safe }}</div>{% for u in j.updates|reverse %}<div><span class="muted">{{ u.created_at }} &middot; {{ u.author }}:</span> <span style="white-space:pre-wrap">{{ u.note }}</span></div>{% endfor %}{% if j.notes %}<div style="white-space:pre-wrap">{{ j.notes }}</div>{% endif %}{% endif %}
{% if j.job_type == 'Survey' %}{% set s = j.survey %}{% if j.hub_approved_at %}<div style="font-weight:700;color:#2e7d32">Estimate approved by {{ j.hub_approved_name or 'client' }} on {{ j.hub_approved_at }}</div>{% endif %}<div class="sec">Estimating Site Survey</div><div style="border-left:3px solid #a49f2a;padding-left:8px">
<div><b>Site contact:</b> {{ s.contact_name or '-' }}{% if s.contact_phone %} &middot; {% if s.contact_tel %}<a href="tel:{{ s.contact_tel }}">{{ s.contact_phone }}</a>{% else %}{{ s.contact_phone }}{% endif %}{% endif %}</div>
{% if s.want_desc %}<div><b>What they want:</b> <span style="white-space:pre-wrap">{{ s.want_desc }}</span></div>{% endif %}
<div style="margin-top:4px"><b>Site conditions:</b></div><div style="white-space:pre-wrap">{{ s.conditions or '-' }}</div>
{% if office and (s.timeline or s.budget or s.decision_maker) %}<div style="margin-top:4px" class="muted">Earlier entry (office only): Timeline {{ s.timeline or '-' }} &middot; Budget {{ s.budget or '-' }} &middot; Decision maker {{ s.decision_maker or '-' }}</div>{% endif %}
{% if office and j.survey_office %}<div style="margin-top:4px"><b>Rough materials &amp; labor (office only):</b></div><div style="white-space:pre-wrap">{{ j.survey_office }}</div>{% endif %}</div>{% endif %}
{% if office and j.quick_notes %}<div class="sec">Quick Notes</div><div style="white-space:pre-wrap">{{ j.quick_notes }}</div>{% endif %}
{% if office and j.punch_list and j.job_type != 'Survey' %}<div class="sec">Punch Items ({{ j.punch_list|selectattr('done')|list|length }}/{{ j.punch_list|length }})</div>{% for c in j.punch_list %}<div>{{ '&#9745;'|safe if c.done else '&#9744;'|safe }} {{ c.text }}</div>{% endfor %}{% endif %}
{% if office and j.private_notes %}<div class="sec">Private Office Notes</div><div style="white-space:pre-wrap">{{ j.private_notes }}</div>{% endif %}
{% if j.job_type not in ('Survey','Office Task') %}{% if j.materials_list or j.materials %}<div class="sec">{{ 'Parts Used' if j.job_type == 'Service' else 'Materials' }}</div>{% for m in j.materials_list %}<div>{{ '&#9745;'|safe if m.used else '&#9744;'|safe }} {{ m.text }}</div>{% endfor %}{% if j.materials %}<div>{{ j.materials }}</div>{% endif %}{% endif %}{% endif %}
{% if j.job_type in ('Install','Office Task','') or (j.job_type == 'Service' and j.checklist|selectattr('done')|list) %}<div class="sec">Checklist</div>{% for c in j.checklist %}<div>{{ '&#9745;'|safe if c.done else '&#9744;'|safe }} {{ c.text }}</div>{% endfor %}{% endif %}
{% if j.job_type == 'Survey' and j.photos %}<div class="sec">Photos</div>{% for p in j.photos %}<img src="/uploads/{{ p.filename|urlencode }}" alt="photo">{% endfor %}{% endif %}
{% set ab = j.job_type != 'Survey' %}{% if j.job_type != 'Office Task' and j.plans %}<div class="sec">{{ 'As-Built' if ab else 'Floor Plan Devices' }}</div>{% if ab %}<div class="muted">Solid pin = installed, white pin = planned</div>{% endif %}{% for fn in j.plans %}<div style="position:relative;margin:6px 0;page-break-inside:avoid"><img src="/uploads/{{ fn|urlencode }}" alt="floor plan" style="width:100%;height:auto;object-fit:contain;border-radius:0;margin:0;display:block;border:1px solid #ddd">{% for d in j.devices if d.plan == fn %}<span style="position:absolute;left:{{ d.x }}%;top:{{ d.y }}%;transform:translate(-50%,-50%);{% if ab and not d.installed %}background:#fff;color:{{ DEV_COLORS[d.type] }};border:2px solid {{ DEV_COLORS[d.type] }};{% else %}background:{{ DEV_COLORS[d.type] }};color:#fff;border:2px solid #fff;{% endif %}border-radius:14px;min-width:24px;padding:0 3px;height:24px;line-height:24px;text-align:center;font-size:11px;font-weight:700;-webkit-print-color-adjust:exact;print-color-adjust:exact">{{ d.label }}</span>{% endfor %}</div>{% endfor %}
{% if j.devices %}<div style="margin-top:6px"><b>Device count:</b> {% for t,n in dev_counts(j.devices).items() %}{{ t }} x{{ n }}{% if ab %} ({{ j.devices|selectattr('type','equalto',t)|selectattr('installed')|list|length }} installed){% endif %}{% if not loop.last %}, {% endif %}{% endfor %} ({{ j.devices|length }} total{% if ab %}, {{ j.devices|selectattr('installed')|list|length }} installed{% endif %})</div>
<table style="width:100%;border-collapse:collapse;margin-top:6px;font-size:.9rem"><tr style="text-align:left;border-bottom:1px solid #999"><th>Label</th><th>Device</th>{% if ab %}<th>Installed</th><th>MAC / Serial</th><th>IP</th><th>Switch port</th><th>Cable label</th>{% endif %}<th>Height</th><th>Cable run</th><th>Note</th>{% if ab %}<th>Photo</th>{% endif %}</tr>{% for d in j.devices %}<tr style="border-bottom:1px solid #ddd"><td><b>{{ d.label }}</b></td><td>{{ d.type }}</td>{% if ab %}<td>{{ 'Yes' if d.installed else 'Planned' }}</td><td>{{ d.mac }}</td><td>{{ d.ip }}</td><td>{{ d.port }}</td><td>{{ d.clabel }}</td>{% endif %}<td>{{ d.height }}</td><td>{{ d.cable }}</td><td>{{ d.note }}</td>{% if ab %}<td>{% if d.photo %}<img src="/uploads/{{ d.photo|urlencode }}" alt="{{ d.label }}" style="width:60px;height:60px">{% endif %}</td>{% endif %}</tr>{% endfor %}</table>{% endif %}{% endif %}
{% if j.job_type != 'Office Task' and (j.map.cameras or j.map.runs) %}<div class="sec">Site Map</div>
{% if j.map.cameras %}<div><b>Exterior cameras:</b></div>{% for c in j.map.cameras %}<div>{{ c.label }}: facing {{ c.dir }}&deg; {{ compass(c.dir) }}, view {{ c.fov }}&deg;, range {{ c.range }} ft <span class="muted">({{ c.lat }}, {{ c.lng }})</span></div>{% endfor %}{% endif %}
{% if j.map.runs %}<div style="margin-top:4px"><b>Measured runs:</b></div>{% for r in j.map.runs %}<div>{{ r.label or 'Run' }}: {{ r.segments_ft|join(' + ') }} = <b>{{ r.total_ft }} ft</b>{% if r.slack %}, plus {{ r.slack }}% slack = <b>{{ r.adjusted_ft }} ft</b>{% endif %}</div>{% endfor %}{% endif %}{% endif %}
{% if j.job_type != 'Survey' %}{% for t,lbl in [('before','Before'),('during','During'),('after','After'),('doors','Doors'),('power','Power'),('network','Network'),('proposed','Proposed locations')] %}{% set g = j.photos|selectattr('photo_type','equalto',t)|list %}{% if g %}<div class="sec">{{ lbl }} Photos</div>{% for p in g %}<img src="/uploads/{{ p.filename|urlencode }}" alt="{{ t }}">{% endfor %}{% endif %}{% endfor %}
{% endif %}
{% if j.job_type != 'Office Task' and j.blueprints %}<div class="sec">Blueprints</div>{% for b in j.blueprints %}<div>{% if b.image %}<img src="/uploads/{{ b.filename|urlencode }}" alt="blueprint"><br>{% endif %}<a href="/uploads/{{ b.filename|urlencode }}">{{ b.name }}</a></div>{% endfor %}{% endif %}
{% if j.job_type not in ('Survey','Office Task') and j.signature and j.signature.startswith('data:image/') %}<div class="sec">Customer Signature</div><img src="{{ j.signature }}" alt="signature" style="width:240px;height:auto;object-fit:contain;border:1px solid #ddd">{% endif %}
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
<style>body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:12px}
.job{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin-bottom:10px}.job-id{color:#a49f2a;font-weight:700;font-size:.8rem}.muted{color:#666}
.btn{display:block;background:#f0f0f0;color:#111;padding:14px;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;margin:8px 0}h2{color:#a49f2a;margin:8px 0}a{color:#111}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<h2>To Order</h2><a class="btn" href="/">Back to Jobs</a><div class="muted" style="margin-bottom:8px">Unchecked materials on open jobs ({{ total }} item{{ '' if total == 1 else 's' }})</div>
{% for j in jobs %}<div class="job"><a href="/?job={{ j.id|urlencode }}"><span class="job-id">{{ j.id }}</span> <b>{{ j.title }}</b></a>{% if j.help_open %} <span class="badge" style="background:#b00020;color:#fff;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px">Needs Help</span>{% endif %}<div class="muted">{{ j.customer }} &middot; {{ j.tech or '-' }} &middot; {{ j.status_label }}</div>
<ul style="margin:6px 0 0;padding-left:20px">{% for m in j.need %}<li>{{ m.text }}</li>{% endfor %}</ul></div>{% else %}<div class="job">Nothing to order</div>{% endfor %}
</body></html>'''

TECHS_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Subs</title>
<style>body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:12px}
.job{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin-bottom:10px}.job-id{color:#a49f2a;font-weight:700;font-size:.8rem}.muted{color:#666}
.btn{display:block;background:#f0f0f0;color:#111;padding:14px;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;margin:8px 0}h2{color:#a49f2a;margin:8px 0}h3{margin:18px 0 6px;border-bottom:2px solid #a49f2a;padding-bottom:4px}a{color:#111}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<h2>Subs</h2><a class="btn" href="/">Back to Jobs</a>
{% for t, jobs in techs %}<h3>{{ t }}{% if co.get(t.lower()) %} ({{ co.get(t.lower()) }}){% endif %} &middot; {{ jobs|length }} open job{{ '' if jobs|length == 1 else 's' }}</h3>
<form method="post" action="/subs/save" class="job"><input type="hidden" name="name" value="{{ t }}"><label style="font-weight:600">Company name<input name="company" value="{{ co.get(t.lower(), '') }}" maxlength="100" placeholder="Company name" style="width:100%;box-sizing:border-box;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:6px 0"></label>
<div style="display:flex;gap:8px"><label style="flex:1;font-weight:600">Hourly rate $<input name="rate_hourly" type="number" step="0.01" min="0" inputmode="decimal" value="{{ rates.get(t.lower(), {}).get('h', '') }}" placeholder="Per hour" style="width:100%;box-sizing:border-box;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:6px 0"></label>
<label style="flex:1;font-weight:600">Flat per job $<input name="rate_flat" type="number" step="0.01" min="0" inputmode="decimal" value="{{ rates.get(t.lower(), {}).get('f', '') }}" placeholder="Per job" style="width:100%;box-sizing:border-box;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:6px 0"></label></div>
<div style="color:#666;font-size:.85rem">Rates are office only. Subs never see them. Leave blank if not used.</div>
<button type="submit" style="width:100%;background:#a49f2a;color:#fff;border:0;padding:14px;border-radius:10px;font-size:1rem;font-weight:600;margin-top:6px">Save</button></form>
{% for k, label in doc_kinds %}{% set d = docs.get((t.lower(), k)) %}<form method="post" action="/subs/doc" enctype="multipart/form-data" class="job" style="border-color:{{ '#b00020' if d and d.expires and d.expires < today else ('#e69500' if d and d.expires and d.expires <= soon else '#e5e5e5') }}"><input type="hidden" name="name" value="{{ t }}"><input type="hidden" name="kind" value="{{ k }}">
<div style="font-weight:700">{{ label }}: {% if not d %}<span style="color:#b00020">Missing</span>{% elif d.expires < today %}<span style="color:#b00020">EXPIRED {{ d.expires }}</span>{% elif d.expires <= soon %}<span style="color:#e69500">Expires soon {{ d.expires }}</span>{% else %}<span style="color:#2e7d32">Good until {{ d.expires }}</span>{% endif %}</div>
{% if d %}<a href="/uploads/{{ d.filename|urlencode }}" target="_blank" style="display:block;margin:4px 0">Open {{ d.orig_name or 'file' }}</a>{% endif %}
<label style="font-weight:600;display:block">{{ 'Replace file (optional)' if d else 'Upload file' }}<input type="file" name="file" accept="image/*,application/pdf" {{ '' if d else 'required' }} style="display:block;margin:6px 0"></label>
<label style="font-weight:600;display:block">Expiration date<input type="date" name="expires" value="{{ d.expires if d else '' }}" required style="width:100%;box-sizing:border-box;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:6px 0"></label>
<button type="submit" style="width:100%;background:#f0f0f0;color:#111;border:0;padding:14px;border-radius:10px;font-size:1rem;font-weight:600">Save {{ label }}</button></form>{% endfor %}
{% for j in jobs %}<div class="job"><a href="/?job={{ j.id|urlencode }}"><span class="job-id">{{ j.id }}</span> <b>{{ j.customer or j.title }}</b></a>{% if j.help_open %} <span style="background:#b00020;color:#fff;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px">Needs Help</span>{% endif %}<div class="muted">{{ j.title }} &middot; {{ j.scheduled_date or 'No date' }} &middot; {{ j.status_label }}</div></div>{% else %}<div class="job muted">No open jobs</div>{% endfor %}{% endfor %}
</body></html>'''

CHK_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Checklists</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:12px}
.job{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin-bottom:10px}.muted{color:#666}h2{color:#a49f2a;margin:8px 0}
.btn{display:block;width:100%;background:#f0f0f0;color:#111;padding:14px;border:0;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;font-size:1rem;margin:8px 0}
.go{background:#a49f2a;color:#fff}.red{background:#b00020;color:#fff}input,select,textarea{width:100%;padding:12px;border:1px solid #e5e5e5;border-radius:10px;margin:6px 0;font-size:1rem;background:#fff}summary{font-weight:700;font-size:1.1rem;padding:4px 0}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<h2>Checklists</h2><a class="btn" href="/">Back to Jobs</a><div class="muted">Pick one of these when you add a job to fill its checklist.</div>
{% macro tform(t) %}<form method="post" action="/checklists/save"><input type="hidden" name="id" value="{{ t.id if t else '' }}"><input name="name" placeholder="Name (e.g. Camera install)" value="{{ t.name if t else '' }}" required>
<select name="job_type"><option value="">Any job type</option>{% for jt in job_types %}<option value="{{ jt }}"{% if t and t.job_type == jt %} selected{% endif %}>{{ jt }}</option>{% endfor %}</select>
<textarea name="items" rows="6" placeholder="One item per line" required>{{ t['items']|join('\n') if t else '' }}</textarea><button class="btn go" type="submit">Save Checklist</button></form>{% endmacro %}
<details class="job"{% if not tpls %} open{% endif %}><summary>+ New Checklist</summary>{{ tform(none) }}</details>
{% for t in tpls %}<details class="job"><summary>{{ t.name }} <span class="muted" style="font-weight:400">({{ t.job_type or 'Any type' }}, {{ t['items']|length }} items)</span></summary>{{ tform(t) }}
<form method="post" action="/checklists/delete" onsubmit="return confirm('Delete this checklist? Jobs already made keep their items.')"><input type="hidden" name="id" value="{{ t.id }}"><button class="btn red" type="submit">Delete</button></form></details>
{% endfor %}</body></html>'''

@app.route('/checklists')
def checklists_page():
    if not is_office(): return redirect('/')
    conn=get_db(); tpls=[{'id':r['id'],'name':r['name'],'job_type':r['job_type'] or '','items':json.loads(r['items'] or '[]')} for r in conn.execute('SELECT * FROM checklist_templates ORDER BY name').fetchall()]; conn.close()
    return render_template_string(CHK_HTML, tpls=tpls, job_types=JOB_TYPES)

@app.route('/checklists/save', methods=['POST'])
def checklists_save():
    if not is_office(): return ('',403)
    name=(request.form.get('name') or '').strip()[:100]; jt=request.form.get('job_type') or ''; jt=jt if jt in JOB_TYPES else ''
    items=[x.strip()[:200] for x in (request.form.get('items') or '').splitlines() if x.strip()][:100]
    if not name or not items: return redirect('/checklists')
    conn=get_db(); tid=request.form.get('id') or ''
    if tid.isdigit(): conn.execute('UPDATE checklist_templates SET name=?,job_type=?,items=? WHERE id=?',(name,jt,json.dumps(items),int(tid)))
    else: conn.execute('INSERT INTO checklist_templates (name,job_type,items,created_at) VALUES (?,?,?,?)',(name,jt,json.dumps(items),datetime.utcnow().isoformat()))
    conn.commit(); conn.close(); return redirect('/checklists')

@app.route('/checklists/delete', methods=['POST'])
def checklists_delete():
    if not is_office(): return ('',403)
    tid=request.form.get('id') or ''
    if tid.isdigit(): conn=get_db(); conn.execute('DELETE FROM checklist_templates WHERE id=?',(int(tid),)); conn.commit(); conn.close()
    return redirect('/checklists')

SCHED_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Schedule</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:1200px;margin:auto;padding:12px}
h2{color:#a49f2a;margin:8px 0}.btn{display:block;flex:1;background:#f0f0f0;color:#111;padding:14px;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;margin:8px 0}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid #e5e5e5;border-radius:10px;background:#fff}
table{border-collapse:collapse;min-width:100%}th,td{border:1px solid #eee;vertical-align:top;padding:4px}th{background:#fafafa;font-size:.85rem;white-space:nowrap;padding:8px 4px}
th.today{background:#a49f2a;color:#fff}td{min-width:130px;height:70px}td.tech{min-width:80px;font-weight:700;background:#fafafa;position:sticky;left:0;z-index:1}
td.drop{cursor:pointer}td.drop.hot{background:#fbfaf0;outline:2px dashed #a49f2a}
.card{background:#fff;border:1px solid #ddd;border-left:5px solid #a49f2a;border-radius:8px;padding:8px;margin:3px 0;font-size:.85rem;cursor:grab;user-select:none}
.card.sel{outline:3px solid #111}.card .id{color:#a49f2a;font-weight:700}.card a{color:#666;font-size:.8rem}
#bar{position:sticky;top:0;z-index:5;background:#111;color:#fff;padding:12px;border-radius:10px;font-weight:700;text-align:center;margin:8px 0}#bar.hidden{display:none}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<h2>Schedule</h2><a class="btn" href="/">Back to Jobs</a>
<div style="display:flex;gap:8px"><a class="btn" href="/schedule?start={{ prev }}">&larr; Prev</a><a class="btn" href="/schedule">This Week</a><a class="btn" href="/schedule?start={{ next }}">Next &rarr;</a></div>
<div style="color:#666">Drag a job to a day and sub. On a phone, tap the job, then tap the box where it goes.</div>
<div id="bar" class="hidden"></div>
<div class="wrap"><table><tr><th>Sub</th><th>No date</th>{% for d in days %}<th class="{{ 'today' if d.today else '' }}">{{ d.label }}</th>{% endfor %}</tr>
{% for t in techs %}<tr><td class="tech">{{ t }}{% if co.get(t.lower()) %}<div style="font-weight:400;font-size:.8rem;color:#666">{{ co.get(t.lower()) }}</div>{% endif %}</td>{% for d in [{'iso':''}] + days %}<td class="drop" data-date="{{ d.iso }}" data-tech="{{ t }}">{% for j in jobs if (j.tech or '').strip().lower() == t.lower() and (j.scheduled_date or '') == d.iso %}<div class="card" draggable="true" data-id="{{ j.id }}"><div class="id">{{ j.id }}</div><div><b>{{ j.customer or j.title }}</b></div><div>{{ j.job_type or 'Job' }} &middot; {{ j.status_label }}</div><a href="/?job={{ j.id|urlencode }}" onclick="event.stopPropagation()">Open</a></div>{% endfor %}</td>{% endfor %}</tr>{% endfor %}
</table></div>
<script>
let sel=null;const bar=document.getElementById('bar');
function pick(c){document.querySelectorAll('.card.sel').forEach(x=>x.classList.remove('sel'));if(sel===c.dataset.id){sel=null;bar.classList.add('hidden');return}sel=c.dataset.id;c.classList.add('sel');bar.textContent='Tap the box where '+sel+' goes. Tap the job again to cancel.';bar.classList.remove('hidden')}
async function move(id,date,tech){bar.textContent='Moving '+id+'...';bar.classList.remove('hidden');const r=await fetch('/api/jobs/'+encodeURIComponent(id)+'/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scheduled_date:date,tech:tech})});
if(r.ok)location.reload();else{const d=await r.json().catch(()=>({}));alert(d.error||'Error');bar.classList.add('hidden')}}
document.querySelectorAll('.card').forEach(c=>{c.addEventListener('click',e=>{e.stopPropagation();pick(c)});c.addEventListener('dragstart',e=>{e.dataTransfer.setData('text/plain',c.dataset.id)})});
document.querySelectorAll('td.drop').forEach(td=>{td.addEventListener('click',()=>{if(sel)move(sel,td.dataset.date,td.dataset.tech)});
td.addEventListener('dragover',e=>{e.preventDefault();td.classList.add('hot')});td.addEventListener('dragleave',()=>td.classList.remove('hot'));
td.addEventListener('drop',e=>{e.preventDefault();td.classList.remove('hot');const id=e.dataTransfer.getData('text/plain');if(id)move(id,td.dataset.date,td.dataset.tech)})});
</script></body></html>'''

@app.route('/schedule')
def schedule_page():
    if not is_office(): return redirect('/')
    t=today_local()
    try: start=datetime.strptime(request.args.get('start',''),'%Y-%m-%d').date()
    except ValueError: start=t
    start=start-timedelta(days=start.weekday()); end=start+timedelta(days=6)
    conn=get_db(); rows=conn.execute("SELECT * FROM jobs WHERE status!='completed' AND (scheduled_date IS NULL OR scheduled_date='' OR scheduled_date BETWEEN ? AND ?) ORDER BY created_at",(start.isoformat(),end.isoformat())).fetchall(); co=sub_companies(conn); conn.close()
    jobs=[job_dict(r) for r in rows]
    techs=[k.title() for k in sorted(USERS) if k not in OFFICE_NAMES]+[k.title() for k in sorted(USERS) if k in OFFICE_NAMES]
    techs+=sorted({(j['tech'] or '').strip().title() for j in jobs if (j['tech'] or '').strip() and (j['tech'] or '').strip().lower() not in [x.lower() for x in techs]})
    days=[{'iso':(start+timedelta(days=i)).isoformat(),'label':(start+timedelta(days=i)).strftime('%a %b ')+str((start+timedelta(days=i)).day),'today':start+timedelta(days=i)==t} for i in range(7)]
    return render_template_string(SCHED_HTML, co=co, days=days, techs=techs, jobs=jobs, prev=(start-timedelta(days=7)).isoformat(), next=(start+timedelta(days=7)).isoformat())

@app.route('/api/jobs/<job_id>/schedule', methods=['POST'])
def schedule_job(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    d=request.get_json(silent=True) or {}; sd=str(d.get('scheduled_date') or '').strip(); tech=str(d.get('tech') or '').strip()[:60]
    if sd:
        try: sd=datetime.strptime(sd,'%Y-%m-%d').date().isoformat()
        except ValueError: return jsonify({'error':'bad date'}),400
    if not tech: return jsonify({'error':'tech required'}),400
    conn=get_db(); n=conn.execute('UPDATE jobs SET scheduled_date=?,tech=? WHERE id=?',(sd or None,tech,job_id)).rowcount; conn.commit(); conn.close()
    return (jsonify({'ok':True}) if n else ('',404))

HUB_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>True Sense Security: {{ j.title }}</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:640px;margin:auto;padding:14px}
h1{font-size:1.3rem;margin:10px 0 4px}h2{font-size:1.1rem;color:#a49f2a;margin:20px 0 6px;border-bottom:2px solid #a49f2a}.muted{color:#666}.card{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin:10px 0}
.ph img{width:31%;aspect-ratio:1;object-fit:cover;border-radius:8px;margin:1%}.plan{position:relative;margin:8px 0;border:1px solid #ddd}.plan img{width:100%;display:block}
.pin{position:absolute;transform:translate(-50%,-50%);color:#fff;border:2px solid #fff;border-radius:14px;min-width:26px;padding:0 3px;height:26px;line-height:22px;text-align:center;font-size:11px;font-weight:700}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #eee;padding:6px;text-align:left}input{width:100%;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:8px 0}
.btn{width:100%;background:#2e7d32;color:#fff;border:0;padding:18px;border-radius:10px;font-size:1.2rem;font-weight:700}.ok{background:#e8f5e9;border:2px solid #2e7d32;border-radius:12px;padding:14px;font-weight:700;font-size:1.1rem;color:#1b5e20}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;display:block;margin:0 auto">
<h1>{{ j.title }}</h1><div>{{ j.customer }}</div><div class="muted">{{ j.address }}</div>
<div class="muted">{{ j.job_type or 'Job' }} &middot; {{ j.status_label }}{% if j.scheduled_date %} &middot; Scheduled {{ j.scheduled_date }}{% endif %}</div>
{% if j.job_type == 'Survey' %}<h2>Site Survey</h2><div class="card">{% if j.survey.want_desc %}<div><b>What you want:</b></div><div style="white-space:pre-wrap">{{ j.survey.want_desc }}</div>{% endif %}
{% if j.survey.conditions %}<div style="margin-top:8px"><b>Site conditions:</b></div><div style="white-space:pre-wrap">{{ j.survey.conditions }}</div>{% endif %}</div>{% endif %}
{% if j.plans %}<h2>{{ 'Plan and Devices' if j.job_type == 'Survey' else 'As-Built Plan' }}</h2>{% for fn in j.plans %}<div class="plan"><img src="{{ signed(fn) }}" alt="plan">{% for d in j.devices if d.plan == fn %}<span class="pin" style="left:{{ d.x }}%;top:{{ d.y }}%;background:{{ DEV_COLORS[d.type] }}">{{ d.label }}</span>{% endfor %}</div>{% endfor %}
{% if j.devices %}<div><b>Device count:</b> {% for t,n in dev_counts(j.devices).items() %}{{ t }} x{{ n }}{% if not loop.last %}, {% endif %}{% endfor %}</div>
<table><tr><th>Label</th><th>Device</th><th>Note</th>{% if j.job_type != 'Survey' %}<th>Status</th>{% endif %}</tr>{% for d in j.devices %}<tr><td><b>{{ d.label }}</b></td><td>{{ d.type }}</td><td>{{ d.note }}</td>{% if j.job_type != 'Survey' %}<td>{{ 'Installed' if d.installed else 'Planned' }}</td>{% endif %}</tr>{% endfor %}</table>{% endif %}{% endif %}
{% if j.photos %}<h2>Photos</h2><div class="ph">{% for p in j.photos %}<a href="{{ signed(p.filename) }}" target="_blank"><img src="{{ signed(p.filename) }}" alt="photo"></a>{% endfor %}</div>{% endif %}
{% if j.job_type not in ('Survey','Office Task') and j.status == 'completed' and j.signature and j.signature.startswith('data:image/') %}<h2>Completion Signature</h2><img src="{{ j.signature }}" alt="signature" style="width:260px;max-width:100%;border:1px solid #ddd">{% if j.completed_at %}<div class="muted">Completed {{ j.completed_at[:10] }}</div>{% endif %}{% endif %}
{% if j.job_type == 'Survey' %}<h2>Estimate</h2>{% if j.hub_approved_at %}<div class="ok">&#10003; Estimate approved by {{ j.hub_approved_name or 'client' }} on {{ j.hub_approved_at }}</div>
{% else %}<form method="post" action="/hub/{{ j.id|urlencode }}/approve" onsubmit="return confirm('Approve the estimate?')"><input type="hidden" name="t" value="{{ t }}"><input name="name" placeholder="Your name" required maxlength="100"><button class="btn" type="submit">Approve Estimate</button></form>
<div class="muted" style="margin-top:6px">Tapping Approve tells True Sense Security you approve the estimate you were sent.</div>{% endif %}{% endif %}
<div class="muted" style="margin-top:24px;text-align:center">True Sense Security</div></body></html>'''

HUB_EXPIRED_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>Link expired</title></head>
<body style="font-family:-apple-system,sans-serif;max-width:640px;margin:auto;padding:24px;text-align:center;color:#111">
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px">
<h1 style="font-size:1.4rem;margin-top:30px">This link has expired.</h1><p style="font-size:1.2rem">Contact True Sense for a new one.</p></body></html>'''

def hub_job(job_id):
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    ok = row and row['hub_nonce'] and row['hub_created_at'] and hmac.compare_digest(request.values.get('t',''), hub_token(job_id,row['hub_nonce'],row['hub_created_at'])) \
         and hub_now() < datetime.fromisoformat(row['hub_created_at'])+timedelta(days=HUB_DAYS)
    if not ok: conn.close(); return None, None
    return conn, row

@app.route('/api/jobs/<job_id>/hub-new', methods=['POST'])
def hub_new_link(job_id):
    if not is_office(): return jsonify({'error':'Office only'}),403
    conn=get_db(); row=conn.execute('SELECT id FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return ('',404)
    n,c=hub_new(conn, job_id); conn.close()
    return jsonify(hub_info(job_id,n,c))

@app.route('/hub/<job_id>')
def hub_page(job_id):
    conn,row=hub_job(job_id)
    if not row: return (HUB_EXPIRED_HTML,410)
    j=job_full(conn,row); conn.close()
    return render_template_string(HUB_HTML, j=j, t=request.values.get('t',''), signed=lambda fn: '/help-photo/'+urllib.parse.quote(fn)+'?t='+help_photo_token(fn))

@app.route('/hub/<job_id>/approve', methods=['POST'])
def hub_approve(job_id):
    conn,row=hub_job(job_id)
    if not row: return (HUB_EXPIRED_HTML,410)
    if row['job_type']=='Survey' and not row['hub_approved_at']:
        conn.execute('UPDATE jobs SET hub_approved_at=?,hub_approved_name=? WHERE id=?',(now_local_str(),(request.form.get('name') or '').strip()[:100],job_id)); conn.commit()
    conn.close()
    return redirect('/hub/'+urllib.parse.quote(job_id)+'?t='+urllib.parse.quote(request.values.get('t','')))

def draft_phone(r):
    p=(r['phone'] or '').strip()
    if not p:
        try: p=(json.loads(r['survey_form'] or '{}').get('contact_phone') or '').strip()
        except Exception: p=''
    return p

def first_name(n): return ((n or '').strip().split() or [''])[0]

@app.route('/api/jobs/<job_id>/onmyway', methods=['POST'])
def on_my_way(job_id):
    conn=get_db(); r,err=own_job(conn, job_id)
    if err: conn.close(); return err
    if r['job_type']=='Office Task': conn.close(); return jsonify({'error':'Not for office tasks'}),400
    who=first_name(me()) or 'your tech'
    body=f"Hi{(' '+r['customer'].strip()) if (r['customer'] or '').strip() else ''}, this is {who} with True Sense Security. I am on my way{(' to '+r['address']) if r['address'] else ''}. See you soon."
    today=today_local().isoformat()
    ex=conn.execute("SELECT id FROM msg_drafts WHERE job_id=? AND kind='onmyway' AND ref_date=? AND status='pending'",(job_id,today)).fetchone()
    if ex: conn.execute('UPDATE msg_drafts SET body=?,created_by=?,created_at=? WHERE id=?',(body,me(),now_local_str(),ex['id']))
    else: conn.execute("INSERT INTO msg_drafts (job_id,kind,phone,body,status,ref_date,created_by,created_at) VALUES (?,?,?,?,'pending',?,?,?)",(job_id,'onmyway',draft_phone(r),body,today,me(),now_local_str()))
    conn.commit(); conn.close(); return jsonify({'ok':True})

def make_reminders(conn):
    tm=(today_local()+timedelta(days=1)).isoformat()
    for r in conn.execute("SELECT * FROM jobs WHERE scheduled_date=? AND status!='completed' AND COALESCE(job_type,'')!='Office Task'",(tm,)).fetchall():
        ph=draft_phone(r)
        if not ph or conn.execute("SELECT 1 FROM msg_drafts WHERE job_id=? AND kind='reminder' AND ref_date=?",(r['id'],tm)).fetchone(): continue
        when=datetime.strptime(tm,'%Y-%m-%d').strftime('%A, %b %d').replace(' 0',' ')
        body=f"Hi{(' '+r['customer'].strip()) if (r['customer'] or '').strip() else ''}, this is True Sense Security. Reminder: we will see you tomorrow, {when}{(' at '+r['address']) if r['address'] else ''}. Reply to this text if you need to change it."
        conn.execute("INSERT INTO msg_drafts (job_id,kind,phone,body,status,ref_date,created_by,created_at) VALUES (?,?,?,?,'pending',?,?,?)",(r['id'],'reminder',ph,body,tm,'Auto',now_local_str()))
    conn.commit()

@app.route('/api/drafts')
def list_drafts():
    if not is_office(): return jsonify({'error':'Office only'}),403
    conn=get_db(); make_reminders(conn)
    rows=[dict(x) for x in conn.execute("SELECT d.*,j.customer,j.title FROM msg_drafts d LEFT JOIN jobs j ON j.id=d.job_id WHERE d.status IN ('pending','approved') ORDER BY d.status DESC, d.id DESC").fetchall()]
    conn.close(); return jsonify(rows)

@app.route('/api/drafts/<int:did>', methods=['POST'])
def update_draft(did):
    if not is_office(): return jsonify({'error':'Office only'}),403
    d=request.get_json(silent=True) or {}; a=d.get('action')
    conn=get_db(); row=conn.execute('SELECT * FROM msg_drafts WHERE id=?',(did,)).fetchone()
    if not row: conn.close(); return jsonify({'error':'Not found'}),404
    if a=='approve':
        body=(d.get('body') or '').strip()
        if not body: conn.close(); return jsonify({'error':'Text is empty'}),400
        conn.execute("UPDATE msg_drafts SET status='approved',body=?,phone=?,approved_by=?,approved_at=? WHERE id=?",(body[:1000],(d.get('phone') or '').strip()[:40],me(),now_local_str(),did))
    elif a in ('dismiss','done'): conn.execute('UPDATE msg_drafts SET status=? WHERE id=?',('dismissed' if a=='dismiss' else 'done',did))
    else: conn.close(); return jsonify({'error':'Bad action'}),400
    conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/techs')
def techs_page():
    if not is_office(): return redirect('/')
    conn=get_db(); rows=conn.execute("SELECT * FROM jobs WHERE status!='completed' ORDER BY COALESCE(scheduled_date, '9999')").fetchall(); co=sub_companies(conn)
    rates={r['key']:{'h':('' if r['rate_hourly'] is None else r['rate_hourly']),'f':('' if r['rate_flat'] is None else r['rate_flat'])} for r in conn.execute('SELECT * FROM subs').fetchall()}; conn.close()
    conn=get_db(); docs={(r['sub'],r['kind']):r for r in conn.execute('SELECT * FROM sub_docs').fetchall()}; names=sub_names(conn); conn.close()
    jobs=[job_dict(r) for r in rows]
    return render_template_string(TECHS_HTML, co=co, rates=rates, docs=docs, doc_kinds=SUB_DOCS, today=today_local().isoformat(), soon=(today_local()+timedelta(days=30)).isoformat(), techs=[(n,[j for j in jobs if (j['tech'] or '').strip().lower()==n.lower()]) for n in names])

@app.route('/subs/save', methods=['POST'])
def subs_save():
    if not is_office(): return ('',403)
    k=ckey(request.form.get('name'))
    if not k: return redirect('/techs')
    def rate(f):
        try: v=float(request.form.get(f) or ''); return round(v,2) if 0<=v<1e6 else None
        except ValueError: return None
    conn=get_db(); conn.execute('INSERT INTO subs (key,company,rate_hourly,rate_flat,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET company=excluded.company,rate_hourly=excluded.rate_hourly,rate_flat=excluded.rate_flat,updated_at=excluded.updated_at',
                 (k,(request.form.get('company') or '').strip()[:100],rate('rate_hourly'),rate('rate_flat'),now_local_str())); conn.commit(); conn.close()
    return redirect('/techs')

@app.route('/subs/doc', methods=['POST'])
def subs_doc():
    if not is_office(): return ('',403)
    k=ckey(request.form.get('name')); kind=request.form.get('kind')
    if not k or kind not in dict(SUB_DOCS): return ('Bad request',400)
    try: exp=datetime.strptime(request.form.get('expires',''),'%Y-%m-%d').date().isoformat()
    except ValueError: return ('Pick an expiration date',400)
    f=request.files.get('file'); conn=get_db(); old=conn.execute('SELECT * FROM sub_docs WHERE sub=? AND kind=?',(k,kind)).fetchone()
    if f and f.filename:
        ext=os.path.splitext(f.filename)[1].lower()
        if ext not in ('.pdf','.jpg','.jpeg','.png','.heic','.webp'): conn.close(); return ('PDF or photo only',400)
        fn=secure_filename(f"subdoc_{k}_{kind}_{uuid.uuid4().hex[:8]}{ext}"); f.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
        if old and old['filename']:
            try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'],secure_filename(old['filename'])))
            except OSError: pass
        conn.execute('INSERT OR REPLACE INTO sub_docs (sub,kind,filename,orig_name,expires,uploaded_at) VALUES (?,?,?,?,?,?)',(k,kind,fn,secure_filename(f.filename)[:100],exp,now_local_str()))
    elif old: conn.execute('UPDATE sub_docs SET expires=? WHERE sub=? AND kind=?',(exp,k,kind))
    else: conn.close(); return ('Pick a file',400)
    conn.commit(); conn.close(); return redirect('/techs')

@app.route('/api/sub-docs/alerts')
def sub_doc_alerts():
    if not is_office(): return jsonify({'error':'Office only'}),403
    conn=get_db(); a=doc_alerts(conn); conn.close(); return jsonify(a)

def current_agreement(conn): return conn.execute('SELECT * FROM agreements ORDER BY id DESC LIMIT 1').fetchone()
def agreement_required(conn):
    r=conn.execute("SELECT value FROM settings WHERE key='require_agreement'").fetchone(); return bool(r and r['value']=='1')
def last_sign(conn, name): return conn.execute('SELECT * FROM agreement_signs WHERE sub=? ORDER BY id DESC LIMIT 1',(ckey(name),)).fetchone()
def signed_current(conn, name):
    a=current_agreement(conn); return bool(conn.execute('SELECT 1 FROM agreement_signs WHERE sub=? AND agreement_id=?',(ckey(name),a['id'])).fetchone())
def bump_version(v):
    p=(v or '').split('.')
    try: p[-1]=str(int(p[-1])+1); return '.'.join(p)
    except ValueError: return (v or '1')+'.1'

AGR_HEAD = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Subcontractor Agreement</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:720px;margin:auto;padding:12px}
h2{color:#a49f2a;margin:8px 0;text-align:center}.job{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin:10px 0}.muted{color:#666}
.btn{display:block;width:100%;background:#f0f0f0;color:#111;padding:16px;border:0;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;font-size:1.05rem;margin:8px 0}.go{background:#2e7d32;color:#fff}.gold{background:#a49f2a;color:#fff}
.text{white-space:pre-wrap;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.85rem;background:#fff;border:1px solid #ddd;border-radius:10px;padding:12px}
input[type=text],textarea{width:100%;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:6px 0}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #eee;padding:8px 4px;text-align:left}
@media print{.noprint{display:none}body{max-width:none;padding:0}.text{border:0;padding:0;max-height:none!important;overflow:visible!important}}</style></head><body>
<img src="LOGO_URL" alt="True Sense Security" style="height:56px;margin:0 auto 4px;display:block">""".replace('LOGO_URL', "https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w")

AGR_SUB_HTML = AGR_HEAD + """<h2>Subcontractor Agreement</h2><div class="muted" style="text-align:center">Version {{ a.version }}</div>
{% if need %}<div class="job" style="border:2px solid #b00020"><b>Please read and sign before you can open jobs.</b></div>{% endif %}
{% if mine %}<div class="job" style="border:2px solid #2e7d32"><b style="color:#2e7d32">You signed version {{ mine.version }} on {{ mine.signed_at }}.</b>{% if not current %} <b style="color:#b00020">A new version needs your signature.</b>{% endif %}
<a class="btn" href="/agreement/copy/{{ mine.id }}">View my signed copy</a></div>{% endif %}
{% if not current %}<div class="text" style="max-height:60vh;overflow-y:auto">{{ a.body }}</div>
<form method="post" action="/agreement/sign" class="job" onsubmit="return go()"><input type="hidden" name="agreement_id" value="{{ a.id }}"><input type="hidden" name="signature" id="sigdata">
<label style="font-weight:600">Type your full legal name<input type="text" name="typed_name" required maxlength="100" autocomplete="name"></label>
<label style="display:flex;gap:10px;align-items:center;font-weight:700;font-size:1.1rem;margin:10px 0"><input type="checkbox" name="agree" value="1" required style="width:28px;height:28px"> I AGREE to version {{ a.version }}</label>
<div style="font-weight:600">Sign with your finger</div><canvas id="pad" style="width:100%;height:180px;border:2px solid #ccc;border-radius:10px;background:#fff;touch-action:none"></canvas>
<button type="button" class="btn" onclick="clr()">Clear Signature</button><button type="submit" class="btn go" style="font-size:1.2rem">Sign</button></form>
<script>
const c=document.getElementById('pad'),x=c.getContext('2d');let d=false,inked=false;
function fit(){c.width=c.clientWidth*2;c.height=c.clientHeight*2;x.scale(2,2);x.lineWidth=2.5;x.lineCap='round';x.strokeStyle='#111';inked=false}fit();
function pt(e){const r=c.getBoundingClientRect();return [e.clientX-r.left,e.clientY-r.top]}
c.addEventListener('pointerdown',e=>{d=true;c.setPointerCapture(e.pointerId);const [a,b]=pt(e);x.beginPath();x.moveTo(a,b)});
c.addEventListener('pointermove',e=>{if(!d)return;const [a,b]=pt(e);x.lineTo(a,b);x.stroke();inked=true});
c.addEventListener('pointerup',()=>d=false);c.addEventListener('pointercancel',()=>d=false);
function clr(){x.setTransform(1,0,0,1,0,0);x.clearRect(0,0,c.width,c.height);fit()}
function go(){if(!inked){alert('Please sign with your finger');return false}document.getElementById('sigdata').value=c.toDataURL('image/png');return true}
</script>{% endif %}
{% if not need %}<a class="btn noprint" href="/">Back to Jobs</a>{% endif %}<a class="btn noprint" href="/logout">Log out</a></body></html>"""

AGR_OFFICE_HTML = AGR_HEAD + """<h2>Subcontractor Agreement</h2><a class="btn" href="/">Back to Jobs</a>
<form method="post" action="/agreement/require" class="job" style="border:2px solid {{ '#2e7d32' if required else '#ddd' }}"><div style="font-weight:700;font-size:1.1rem">Require subs to sign: {{ 'ON' if required else 'OFF' }}</div>
<div class="muted">{{ 'Subs must sign the current version before they can open jobs.' if required else 'Subs are not blocked. They can still sign from a link on their home screen.' }}</div>
<input type="hidden" name="on" value="{{ '0' if required else '1' }}"><button class="btn {{ '' if required else 'gold' }}" type="submit">{{ 'Turn OFF' if required else 'Turn ON' }}</button></form>
<div class="job"><div style="font-weight:700">Sub status (current version {{ a.version }})</div><table>{% for n, st, sid in rows %}<tr><td><b>{{ n }}</b></td><td style="color:{{ '#2e7d32' if st.startswith('Signed') else '#b00020' }};font-weight:600">{{ st }}</td><td>{% if sid %}<a href="/agreement/copy/{{ sid }}">Signed copy</a>{% endif %}</td></tr>{% else %}<tr><td>No subs set in Render</td></tr>{% endfor %}</table></div>
<div class="job"><div style="font-weight:700">Current version {{ a.version }}</div><div class="muted">Added {{ a.created_at }} by {{ a.created_by }}</div>
<div class="text" style="max-height:50vh;overflow-y:auto;margin-top:8px">{{ a.body }}</div></div>
<form method="post" action="/agreement/new" enctype="multipart/form-data" class="job" onsubmit="return confirm('Make this the new version? Every sub will need to sign again.')"><div style="font-weight:700">Add a new version</div>
<div class="muted">Paste the new text or pick a .txt file. All subs will need to sign again.</div>
<label style="font-weight:600">Version number<input type="text" name="version" value="{{ next_v }}" maxlength="20"></label>
<textarea name="body" rows="8" placeholder="Paste the full agreement text here"></textarea><input type="file" name="file" accept=".txt,text/plain" style="display:block;margin:6px 0">
<button class="btn gold" type="submit">Save New Version</button></form></body></html>"""

AGR_COPY_HTML = AGR_HEAD + """<h2>Subcontractor Agreement: Signed Copy</h2>
<div class="noprint"><button class="btn gold" onclick="print()">Print</button><a class="btn" href="/agreement">Back</a></div>
<div class="job"><div><b>Version:</b> {{ s.version }}</div><div><b>Signed by:</b> {{ s.typed_name }} (login: {{ s.sub|title }})</div><div><b>Date and time:</b> {{ s.signed_at }}</div></div>
<div class="text">{{ body }}</div>
<div class="job" style="page-break-inside:avoid"><div style="font-weight:700">Subcontractor Signature</div><div><b>Typed name:</b> {{ s.typed_name }}</div><div>&#9745; I AGREE to version {{ s.version }}</div>
{% if s.signature and s.signature.startswith('data:image/') %}<img src="{{ s.signature }}" alt="signature" style="width:320px;max-width:100%;border-bottom:2px solid #111;display:block;margin:8px 0">{% endif %}
<div><b>Signed:</b> {{ s.signed_at }}</div><div class="muted" style="font-size:.8rem">Recorded by True Sense Field Ops. IP {{ s.ip or '-' }}. Device: {{ s.user_agent or '-' }}</div></div></body></html>"""

@app.route('/agreement')
def agreement_page():
    conn=get_db(); a=current_agreement(conn)
    if is_office():
        rows=[]
        for n in sub_names(conn):
            ls=last_sign(conn, n)
            if not ls: rows.append((n,'Not signed',None))
            elif ls['agreement_id']==a['id']: rows.append((n,'Signed v'+ls['version']+' on '+ls['signed_at'],ls['id']))
            else: rows.append((n,'Needs re-sign (signed v'+ls['version']+')',ls['id']))
        req=agreement_required(conn); conn.close()
        return render_template_string(AGR_OFFICE_HTML, a=a, rows=rows, required=req, next_v=bump_version(a['version']))
    mine=last_sign(conn, me()); cur=signed_current(conn, me()); need=agreement_required(conn) and not cur; conn.close()
    return render_template_string(AGR_SUB_HTML, a=a, mine=mine, current=cur, need=need)

@app.route('/agreement/sign', methods=['POST'])
def agreement_sign():
    if is_office(): return redirect('/agreement')
    f=request.form; name=(f.get('typed_name') or '').strip()[:100]; sig=f.get('signature') or ''
    conn=get_db(); a=current_agreement(conn)
    if str(a['id'])!=(f.get('agreement_id') or ''): conn.close(); return ('The agreement changed. Go back and read the new version.',409)
    if not name or f.get('agree')!='1' or not sig.startswith('data:image/png;base64,') or len(sig)>600000: conn.close(); return ('Type your name, check I agree, and sign.',400)
    ip=(request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()[:60]
    cur=conn.execute('INSERT INTO agreement_signs (sub,agreement_id,version,typed_name,signature,signed_at,ip,user_agent) VALUES (?,?,?,?,?,?,?,?)',
                     (ckey(me()),a['id'],a['version'],name,sig,now_local_str(),ip,(request.headers.get('User-Agent') or '')[:300]))
    conn.commit(); sid=cur.lastrowid; conn.close()
    return redirect(f'/agreement/copy/{sid}')

@app.route('/agreement/copy/<int:sid>')
def agreement_copy(sid):
    conn=get_db(); s=conn.execute('SELECT * FROM agreement_signs WHERE id=?',(sid,)).fetchone()
    if not s or not (is_office() or s['sub']==ckey(me())): conn.close(); return ('Not allowed',403)
    a=conn.execute('SELECT body FROM agreements WHERE id=?',(s['agreement_id'],)).fetchone(); conn.close()
    return render_template_string(AGR_COPY_HTML, s=s, body=a['body'] if a else '')

@app.route('/agreement/require', methods=['POST'])
def agreement_require():
    if not is_office(): return ('',403)
    conn=get_db(); conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('require_agreement',?)",('1' if request.form.get('on')=='1' else '0',)); conn.commit(); conn.close()
    return redirect('/agreement')

@app.route('/agreement/new', methods=['POST'])
def agreement_new():
    if not is_office(): return ('',403)
    body=(request.form.get('body') or '').strip(); f=request.files.get('file')
    if f and f.filename:
        try: body=f.read(500000).decode('utf-8-sig').strip()
        except UnicodeDecodeError: return ('Use a plain .txt file',400)
    if not body: return ('Paste the text or pick a .txt file',400)
    conn=get_db(); a=current_agreement(conn); v=(request.form.get('version') or '').strip()[:20] or bump_version(a['version'])
    conn.execute('INSERT INTO agreements (version,body,created_at,created_by) VALUES (?,?,?,?)',(v,body.replace('\r\n','\n')[:200000],now_local_str(),me())); conn.commit(); conn.close()
    return redirect('/agreement')

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
        for t in ('photos','job_updates','invoice_drafts','followups','msg_drafts','job_hours'): conn.execute(f'DELETE FROM {t} WHERE job_id=?',(row['id'],))
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
    for t in ('photos','job_updates','invoice_drafts','followups','msg_drafts','job_hours'): conn.execute(f'DELETE FROM {t} WHERE job_id=?',(job_id,))
    n=conn.execute('DELETE FROM jobs WHERE id=?',(job_id,)).rowcount; conn.commit(); conn.close()
    return (jsonify({'ok':True}) if n else ('',404))

@app.route('/uploads/<path:fn>')
def uploads(fn):
    if not is_office():
        conn=get_db(); r=conn.execute('SELECT j.tech FROM photos p JOIN jobs j ON j.id=p.job_id WHERE p.filename=?',(fn,)).fetchone()
        ok=bool(r) and can_see(r); conn.close()
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
