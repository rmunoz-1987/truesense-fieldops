from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect
import re, sqlite3, os, shutil, uuid, hmac, hashlib, json, threading, urllib.request, urllib.parse, math, time, sys
from datetime import timedelta
try:
    from zoneinfo import ZoneInfo
except Exception: ZoneInfo=None
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'))
DATA_DIR = os.environ.get('DATA_DIR') or ('/var/data' if os.path.isdir('/var/data') else '.')
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER') or os.path.join(DATA_DIR, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
# Each user gets a Render env var NAME_PASSWORD (e.g. ROLAND_PASSWORD, WOODY_RODRIGUEZ_PASSWORD); the login name is NAME (underscores = spaces).
USERS = {' '.join(k[:-len('_PASSWORD')].replace('_', ' ').split()).lower(): v for k, v in os.environ.items()
         if k.endswith('_PASSWORD') and k != 'APP_PASSWORD' and v and k[:-len('_PASSWORD')]}
ON_RENDER = bool(os.environ.get('RENDER'))
OFFICE_NAMES = [' '.join(n.split()).lower() for n in os.environ.get('OFFICE_NAMES', 'Roland').split(',') if n.strip()]
app.secret_key = os.environ.get('SECRET_KEY') or hashlib.sha256(('fieldops:' + json.dumps(sorted(USERS.items()))).encode()).hexdigest()
app.permanent_session_lifetime = timedelta(days=30)

DEFAULT_CHECKLIST = ['Cleaned up', 'Checked in with the client', 'Walked the finished work with the client']
NO_CLOSING_STEPS = ('Survey', 'Office Task')  # these job types do not get the 3 closing steps
JOB_TYPES = ['Install', 'Service', 'Survey', 'Office Task']
# Built-in checklist templates: (name, job type, steps). Seeded once by name; office edits and deletes are kept.
BUILTIN_TEMPLATES = [
    ('Site Survey', 'Survey', ['Meet the client contact and get their name and phone', 'Photo of the main entry and the building outside', 'Find the MDF and IDF rooms and take photos', 'Mark where power outlets are for new gear', 'Mark where network switches and open ports are', 'Note ceiling type and height in each area', 'Note wall and mounting surfaces (drywall, block, brick, metal)', 'Plan cable paths and note hard spots (firewalls, long runs, lifts)', 'Photo and list existing equipment (cameras, panels, NVR, intercom)', 'Ask what the client wants and any deadline', 'Put every device on the plan markup', 'Take photos of each device spot']),
    ('General Install', 'Install', ['Check the plan and scope with the client', 'Confirm power and network are ready', 'Mount devices per the plan', 'Pull and terminate cables', 'Label cables and devices', 'Test every device', 'Program and set up the system', 'Update the as-built with any changes', 'Take after photos', 'Show the client how it works']),
    ('Camera Install', 'Install', ['Check the camera plan and spots with the client', 'Mount cameras', 'Pull and terminate cable', 'Test each cable', 'Confirm PoE power at each camera', 'Add every camera to the NVR', 'Aim and focus each camera', 'Set recording and check retention days', 'Set up the remote view app on the client phone', 'Label cameras and cables', 'Update the as-built', 'Take photos of each camera view']),
    ('Access Control Install', 'Install', ['Check door hardware and frames on every door', 'Confirm lock type and lock power needed', 'Install locks or strikes', 'Install readers', 'Install REX and door position switches', 'Wire the panel and power supply', 'Tie in to the fire alarm if required', 'Enroll credentials for all users', 'Set door schedules', 'Test every door: badge, REX, door held, forced', 'Update the as-built', 'Train the client on the software']),
    ('Intercom Install', 'Install', ['Check the intercom plan with the client', 'Mount door stations', 'Mount inside stations or set up the app', 'Pull and terminate cable', 'Wire the door release to the lock', 'Set up call groups and names', 'Test audio and video at each station', 'Test door release from each station', 'Label cables and stations', 'Update the as-built', 'Show the client how to answer and open the door']),
    ('Structured Cabling Install', 'Install', ['Check the drop plan with the client', 'Pull cables', 'Terminate jacks and patch panel', 'Label both ends of every cable', 'Test and certify every cable', 'Save the test results', 'Dress and tie cables neatly', 'Dress the patch panel', 'Firestop every wall penetration', 'Update the as-built with drop numbers', 'Take photos of the rack and patch panel']),
    ('Fiber Install', 'Install', ['Check the fiber path and counts with the client', 'Pull fiber in innerduct where needed', 'Leave a service loop at each end', 'Mount fiber enclosures', 'Splice or terminate fiber', 'Clean and inspect every connector', 'Test each strand (light meter or OTDR)', 'Save the test results', 'Label both ends', 'Firestop every penetration', 'Update the as-built', 'Take photos of the enclosures']),
    ('AV Install', 'Install', ['Check the AV plan and TV or speaker spots with the client', 'Confirm power and backing in walls', 'Mount TVs, displays, and speakers', 'Pull and terminate HDMI, speaker, and data cable', 'Install and wire the rack or equipment', 'Set up sources and inputs', 'Set volume levels and test sound in each zone', 'Test picture on every display', 'Set up remotes or control app', 'Label cables', 'Update the as-built', 'Show the client how to use it']),
    ('Service Call', 'Service', ['Talk to the client about the problem', 'Take before photos', 'Make the problem happen again', 'Check power', 'Check network and cables', 'Fix the problem', 'Test that it works now', 'Take after photos', 'Write notes on what was done', 'List the parts used', 'Get the client to sign off']),
    ('Office Task', 'Office Task', ['Read the task notes', 'Do the task', 'Write what was done in the notes', 'Mark it done']),
]
DEFAULT_TEMPLATE = {'Install': 'General Install', 'Service': 'Service Call', 'Survey': 'Site Survey', 'Office Task': 'Office Task'}
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

def parse_checklist(raw, job_type=''):
    try:
        items = json.loads(raw) if raw else None
    except Exception:
        items = None
    closing = (job_type or '') not in NO_CLOSING_STEPS
    if not items:
        items = [{'text': t, 'done': False} for t in DEFAULT_CHECKLIST] if closing else []
    out = [{'text': str(i.get('text', '')), 'done': bool(i.get('done'))} for i in items if isinstance(i, dict) and i.get('text')]
    have = {i['text'] for i in out}
    if not closing: return [i for i in out if i['done'] or i['text'] not in DEFAULT_CHECKLIST]
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
                    'port': str(i.get('port') or '').strip()[:40], 'clabel': str(i.get('clabel') or '').strip()[:60], 'photo': secure_filename(str(i.get('photo') or ''))[:100],
                    'part': str(i.get('part') or '').strip()[:80]})
        if i['type'] in ('Dome Cam', 'Bullet Cam', 'PTZ Cam'):
            for k, lo, hi in (('dir', 0, 359), ('fov', 10, 360), ('range', 2, 100), ('rft', 1, 2000)):
                if i.get(k) not in (None, ''):
                    v = _num(i.get(k), lo, hi, None)
                    if v is not None: out[-1][k] = round(v) if k in ('dir', 'fov') else round(v, 1)
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
    d = dict(row); d['checklist'] = parse_checklist(d.get('checklist'), d.get('job_type'))
    d['materials_list'] = parse_materials(d.get('materials_list'))
    d['tech'] = nice_name(d.get('tech'))
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
        for k in ('private_notes','hub_nonce','hub_created_at','hub_approved_at','hub_approved_name','labor_rates','review_by','review_at','sale_amount','estimate_json','pay_link','paid_at'): d.pop(k, None)
    return d

def nice_name(n): return ' '.join((n or '').split()).title()
def me():
    n = nice_name(session.get('name'))
    if not n: return ''
    k = n.lower()
    if k in USERS: return n
    try:
        import fieldops_users as _users
        u = _users.active_user(k)
        if u: return u['name']
    except Exception:
        pass
    return ''
def office_name_set():
    names = set(OFFICE_NAMES)
    try:
        import fieldops_users as _users
        names.update(_users.office_keys())
    except Exception:
        pass
    return names
def is_office():
    k = me().lower()
    return bool(k) and k in office_name_set()
def can_see(job):
    if is_office(): return True
    mek=(me() or '').lower()
    if ' '.join((job['tech'] or '').split()).lower()==mek: return True
    fn=globals().get('ops_sub_on_job')
    if callable(fn):
        try: return bool(fn(job['id'], mek))
        except Exception: return False
    return False
def sub_logins():
    names = []
    seen = set()
    office = office_name_set()
    for k in sorted(USERS):
        if k not in office:
            n = nice_name(k)
            names.append(n); seen.add(k)
    try:
        import fieldops_users as _users
        for u in _users.active_field_users():
            k = u['name_key']
            if k not in seen and k not in office:
                names.append(u['name']); seen.add(k)
    except Exception:
        pass
    return names
def office_assignees():
    out = []
    seen = set()
    for k in sorted(office_name_set()):
        if k in seen: continue
        seen.add(k)
        label = nice_name(k)
        try:
            import fieldops_users as _users
            u = _users.active_user(k)
            if u: label = u['name']
        except Exception:
            pass
        if k in USERS or True:
            out.append((label, label+' (office)'))
    # Only people who can actually log in (env or active db)
    real = []
    for label, shown in out:
        k = label.lower()
        if k in USERS:
            real.append((label, shown)); continue
        try:
            import fieldops_users as _users
            if _users.active_user(k):
                real.append((label, shown))
        except Exception:
            pass
    return real
def default_sub(): return (sub_logins() or [''])[0]
def has_any_login():
    if USERS: return True
    try:
        import fieldops_users as _users
        return _users.has_any()
    except Exception:
        return False

LOGIN_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Field Ops"><title>True Sense Field</title>
<style>body{font-family:-apple-system,sans-serif;overflow-wrap:anywhere;background:#fcfcfc;color:#111;max-width:420px;margin:auto;padding:24px}
input{width:100%;padding:14px;border:1px solid #e5e5e5;border-radius:10px;margin:8px 0;font-size:1rem;box-sizing:border-box}
button{width:100%;background:#a49f2a;color:#fff;border:none;padding:16px;border-radius:10px;font-size:1.05rem;font-weight:600}
.err{color:#b00020}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<div style="text-align:center;color:#a49f2a;font-weight:600;margin-bottom:16px">Field Ops</div>
{% if setup %}<p>Add user passwords in Render, e.g. ROLAND_PASSWORD and WOODY_RODRIGUEZ_PASSWORD</p>{% else %}
<form method="post" action="/login"><input name="name" placeholder="Your name" autocomplete="username" required>
<input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
{% if error %}<div class="err">{{ error }}</div>{% endif %}<button type="submit">Log In</button></form>{% endif %}
</body></html>'''

app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=ON_RENDER)

# ---- login throttle: 5 misses per name (20 per IP) in 15 min locks for 15 min ----
LOGIN_FAILS = {}; LOGIN_LOCK = threading.Lock(); LOCK_SECS = 900; MAX_FAILS = 5
def client_ip(): return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
def login_keys(name): return ['n:'+name.lower(), 'ip:'+client_ip()]
def login_locked(name):
    now=time.time()
    with LOGIN_LOCK:
        for k in login_keys(name):
            v=[t for t in LOGIN_FAILS.get(k,[]) if now-t<LOCK_SECS]; LOGIN_FAILS[k]=v
            if len(v)>=(MAX_FAILS if k.startswith('n:') else MAX_FAILS*4): return True
    return False
def login_fail(name):
    with LOGIN_LOCK:
        for k in login_keys(name): LOGIN_FAILS.setdefault(k,[]).append(time.time())
        if len(LOGIN_FAILS)>5000: LOGIN_FAILS.clear()
def login_ok(name):
    with LOGIN_LOCK: LOGIN_FAILS.pop('n:'+name.lower(), None)

# ---- CSRF: browser writes must come from this site (Origin / Referer check) ----
CSRF_EXEMPT = ('/api/qbo/pending', '/api/qbo/result', '/api/qbo/import-estimates', '/api/qbo-monthly', '/api/tech-support/reply')
@app.before_request
def csrf_check():
    if request.method in ('GET','HEAD','OPTIONS') or request.path in CSRF_EXEMPT or request.path.startswith('/api/material-requests/'): return
    src=request.headers.get('Origin') or request.headers.get('Referer') or ''
    if not src: return  # non-browser clients (browsers always send Origin on cross-site posts)
    if urllib.parse.urlsplit(src).netloc!=request.host:
        app.logger.warning('CSRF block %s from %s', request.path, src[:80])
        return (jsonify({'error':'Blocked: request came from another site'}),403) if request.path.startswith('/api') else ('Blocked: request came from another site',403)

@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options','nosniff')
    resp.headers.setdefault('X-Frame-Options','SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy','strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy','geolocation=(self), camera=(self), microphone=()')
    if ON_RENDER: resp.headers.setdefault('Strict-Transport-Security','max-age=31536000')
    return resp

@app.before_request
def require_login():
    if request.path in ('/login', '/healthz', '/sw.js', '/outbox.js', '/manifest.webmanifest', '/offline', '/api/qbo/pending', '/api/qbo/result', '/api/qbo/import-estimates') or request.path.startswith('/static/') or request.path.startswith('/help-photo/') or request.path.startswith('/hub/') or request.path.startswith('/q/') or request.path.startswith('/p/') or request.path.startswith('/co/') or request.path.startswith('/s/') or request.path.startswith('/agreement/view/') or request.path.startswith('/api/material-requests/') or request.path in ('/api/tech-support/reply', '/api/qbo-monthly'): return
    if not has_any_login():
        return (render_template_string(LOGIN_HTML, setup=True), 503) if not request.path.startswith('/api') else (jsonify({'error':'Add user passwords in Render'}), 503)
    if not me():
        return (jsonify({'error':'login required'}), 401) if request.path.startswith(('/api','/uploads')) else redirect('/login')
    if not is_office() and request.path not in ('/agreement', '/agreement/sign', '/logout') and not request.path.startswith('/agreement/copy/'):
        conn=get_db(); blocked=agreement_required(conn) and not signed_current(conn, me()); conn.close()
        if blocked: return (jsonify({'error':'Sign the Subcontractor Agreement first'}), 403) if request.path.startswith(('/api','/uploads')) else redirect('/agreement')

TAP_CSS = """<style id="tap-css">
button,a[href],[onclick],summary,select,label[for],label:has(input[type=checkbox]),label:has(input[type=radio]),label:has(input[type=file]),input[type=checkbox],input[type=radio],input[type=file],input[type=submit],input[type=button],[role=button],.tile,.stp,.big-btn,.btn,.leaflet-control a{cursor:pointer}
button,.big-btn,.btn,.tile,.stp,[onclick]{transition:filter .12s ease,transform .06s ease}
@media (hover:hover){button:not(:disabled):hover,a.big-btn:hover,a.btn:hover,.tile:hover,.stp:hover,[onclick]:not(:disabled):hover,label:has(input[type=file]):hover{filter:brightness(.93)}}
button:not(:disabled):active,a.big-btn:active,a.btn:active,.tile:active,.stp:active,[onclick]:not(:disabled):active,label:has(input[type=file]):active{filter:brightness(.86);transform:translateY(1px)}
:focus-visible{outline:3px solid #1565c0 !important;outline-offset:2px}
button:disabled,input:disabled,select:disabled,textarea:disabled,[aria-disabled=true]{cursor:not-allowed !important;opacity:.55;filter:grayscale(.7)}
</style>"""

PWA_HEAD = """<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#a49f2a"><link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png"><link rel="icon" href="/static/icons/icon-192.png">
<script>if('serviceWorker' in navigator)window.addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}))</script><script src="/outbox.js" defer></script>"""

MANIFEST = {'name':'True Sense Field Ops','short_name':'Field Ops','start_url':'/','scope':'/','display':'standalone','background_color':'#ffffff','theme_color':'#a49f2a',
    'icons':[{'src':'/static/icons/icon-192.png','sizes':'192x192','type':'image/png'},{'src':'/static/icons/icon-512.png','sizes':'512x512','type':'image/png'},
             {'src':'/static/icons/maskable-512.png','sizes':'512x512','type':'image/png','purpose':'maskable'}]}

SW_JS = r"""const V='fo-v5';const SHELL=['/offline','/outbox.js','/static/logo.png','/static/icons/icon-192.png','/static/icons/icon-512.png'];
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(V).then(c=>c.addAll(SHELL)))});
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==V).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('message',e=>{if(e.data==='logout')e.waitUntil(caches.keys().then(ks=>Promise.all(ks.map(k=>caches.delete(k)))))});
self.addEventListener('fetch',e=>{const r=e.request,u=new URL(r.url);if(r.method!=='GET'||u.origin!==location.origin)return;
if(r.mode==='navigate'&&/^\/job\/[^/]+\/materials$/.test(u.pathname)){e.respondWith(fetch(r).then(res=>{if(res.ok&&!res.redirected){const c=res.clone();caches.open(V).then(x=>x.put(u.pathname,c))}return res}).catch(()=>caches.match(u.pathname).then(m=>m||caches.match('/offline'))));return}
if(r.mode==='navigate'){if(u.pathname!=='/')return e.respondWith(fetch(r).catch(()=>caches.match('/offline')));
e.respondWith(fetch(r).then(res=>{if(res.ok&&!res.redirected){const c=res.clone();caches.open(V).then(x=>x.put('/',c))}return res}).catch(()=>caches.match('/').then(m=>m||caches.match('/offline'))));return}
if(u.pathname==='/outbox.js'){e.respondWith(fetch(r).then(res=>{if(res.ok){const c=res.clone();caches.open(V).then(x=>x.put('/outbox.js',c))}return res}).catch(()=>caches.match('/outbox.js')));return}
if(u.pathname.startsWith('/static/')){e.respondWith(caches.match(r).then(m=>m||fetch(r).then(res=>{const c=res.clone();caches.open(V).then(x=>x.put(r,c));return res})));return}
if(u.pathname.startsWith('/api/jobs')||u.pathname.startsWith('/uploads/')){e.respondWith(fetch(r).then(res=>{if(res.ok){const c=res.clone();caches.open(V).then(x=>x.put(r,c))}return res}).catch(()=>caches.match(r).then(m=>m||new Response(JSON.stringify({error:'offline'}),{status:503,headers:{'Content-Type':'application/json'}}))))}});
self.addEventListener('push',e=>{let d={};try{d=e.data.json()}catch(x){d={title:'Field Ops',body:e.data?e.data.text():''}}
e.waitUntil(self.registration.showNotification(d.title||'Field Ops',{body:d.body||'',icon:'/static/icons/icon-192.png',badge:'/static/icons/icon-192.png',data:{url:d.url||'/'}}))});
self.addEventListener('notificationclick',e=>{e.notification.close();const url=(e.notification.data&&e.notification.data.url)||'/';
e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(ws=>{for(const w of ws){if('focus' in w){w.navigate(url);return w.focus()}}return clients.openWindow(url)}))});"""

OUTBOX_JS = r'''(()=>{if(window.foSend)return;const DB='fo-outbox',ST='q';let dbp=null,busy=false;
function db(){return dbp||(dbp=new Promise((ok,no)=>{const r=indexedDB.open(DB,1);r.onupgradeneeded=()=>r.result.createObjectStore(ST,{keyPath:'id',autoIncrement:true});r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)}))}
async function tx(mode,fn){const d=await db();return new Promise((ok,no)=>{const t=d.transaction(ST,mode),s=t.objectStore(ST);let out;const r=fn(s);if(r)r.onsuccess=()=>{out=r.result};t.oncomplete=()=>ok(out);t.onerror=()=>no(t.error)})}
const all=()=>tx('readonly',s=>s.getAll()),put=v=>tx('readwrite',s=>s.put(v)),del=id=>tx('readwrite',s=>s.delete(id));
async function pack(body){if(body instanceof FormData){const e=[];for(const [k,v] of body.entries()){if(v instanceof Blob){if(!v.size&&!v.name)continue;e.push([k,{f:1,name:v.name||'file',type:v.type,buf:await v.arrayBuffer()}])}else e.push([k,v])}return {form:e}}return {text:body==null?null:String(body)}}
function unpack(it){if(it.form){const fd=new FormData();it.form.forEach(([k,v])=>v&&v.f?fd.append(k,new Blob([v.buf],{type:v.type}),v.name):fd.append(k,v));return fd}return it.text}
function queuedResp(){return {ok:false,queued:true,status:0,json:async()=>({queued:true}),text:async()=>''}}
async function enqueue(url,opt,label,needOk){const p=await pack(opt.body);await put(Object.assign({url,method:opt.method||'POST',headers:opt.headers||{},label:label||'Item',page:location.pathname+location.search,at:Date.now(),tries:0,state:'wait',needOk:!!needOk},p));toast(label+' saved on this phone. It will upload when you have signal.');paint()}
window.foSend=async function(url,opt,label){opt=opt||{};if(navigator.onLine===false){await enqueue(url,opt,label);return queuedResp()}
try{return await fetch(url,opt)}catch(e){await enqueue(url,opt,label);return queuedResp()}};
window.foQueue=enqueue;
async function flush(){if(busy)return;busy=true;try{for(const it of await all()){if(it.state==='failed')continue;
let r;try{r=await fetch(it.url,{method:it.method,headers:it.headers,body:unpack(it),credentials:'same-origin'})}catch(e){break}
if(r.status===401||(r.redirected&&new URL(r.url).pathname==='/login')){it.state='login';await put(it);break}
if(r.ok&&(!it.needOk||r.headers.get('X-FO-OK'))){await del(it.id);continue}
if(r.status>=500){it.tries++;await put(it);break}
it.state='failed';it.err=r.status>=400?'Server said no ('+r.status+')':'The office page needs a fix';await put(it)}}catch(e){}busy=false;paint()}
window.foFlush=flush;
function toast(m){let t=document.getElementById('fo-toast');if(!t){t=document.createElement('div');t.id='fo-toast';document.body.appendChild(t)}t.textContent=m;t.className='show';clearTimeout(t._h);t._h=setTimeout(()=>t.className='',4500)}
window.foToast=toast;
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c])}
async function paint(){let L=[];try{L=await all()}catch(e){}let p=document.getElementById('fo-pill');
if(!L.length){if(p)p.remove();const pn=document.getElementById('fo-panel');if(pn)pn.remove();return}
if(!p){p=document.createElement('button');p.id='fo-pill';p.type='button';p.onclick=panel;document.body.appendChild(p)}
const f=L.filter(i=>i.state==='failed').length,lg=L.some(i=>i.state==='login');
p.className=f?'bad':'';p.textContent=(L.length-f? (L.length-f)+' waiting to upload':'')+(f?(L.length-f?', ':'')+f+' could not upload':'')+(lg?' (sign in)':'');
if(document.getElementById('fo-panel'))panel(true)}
async function panel(keep){const L=await all();let pn=document.getElementById('fo-panel');if(pn&&keep!==true){pn.remove();return}
if(!pn){pn=document.createElement('div');pn.id='fo-panel';document.body.appendChild(pn)}
const off=navigator.onLine===false;
pn.innerHTML='<div class="fo-h">Waiting to upload</div><div class="fo-s">'+(off?'No signal right now. These are saved on this phone and upload by themselves when signal comes back.':'Uploading when the server answers. Keep the app open.')+'</div>'+
L.map(i=>'<div class="fo-i"><div><b>'+esc(i.label)+'</b><br><span>'+new Date(i.at).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})+(i.state==='failed'?' &middot; '+esc(i.err||'failed'):i.state==='login'?' &middot; sign in to send':' &middot; waiting')+'</span></div>'+(i.state==='failed'?'<button type="button" data-d="'+i.id+'">Discard</button>':'')+'</div>').join('')+
'<button type="button" class="fo-b" id="fo-retry">Try now</button>'+(L.some(i=>i.state==='login')?'<a class="fo-b" href="/login">Sign in</a>':'')+'<button type="button" class="fo-b fo-g" id="fo-close">Close</button>';
pn.querySelectorAll('[data-d]').forEach(b=>b.onclick=async()=>{if(confirm('Discard this item? It will not upload.')){await del(+b.dataset.d);paint()}});
pn.querySelector('#fo-retry').onclick=async()=>{for(const i of await all())if(i.state!=='failed'){i.state='wait';await put(i)}flush()};
pn.querySelector('#fo-close').onclick=()=>pn.remove()}
const css=document.createElement('style');css.textContent='#fo-pill{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(14px + env(safe-area-inset-bottom));z-index:99999;background:#a49f2a;color:#fff;border:0;border-radius:999px;padding:12px 18px;min-height:44px;font:700 1rem system-ui,sans-serif;box-shadow:0 3px 12px rgba(0,0,0,.25);max-width:92vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#fo-pill.bad{background:#b00020}'+
'#fo-panel{position:fixed;left:8px;right:8px;bottom:calc(70px + env(safe-area-inset-bottom));z-index:99999;background:#fff;border:2px solid #a49f2a;border-radius:14px;padding:14px;box-shadow:0 6px 24px rgba(0,0,0,.25);max-width:520px;margin:0 auto;max-height:70vh;overflow:auto;font-family:system-ui,sans-serif}'+
'#fo-panel .fo-h{font-weight:800;font-size:1.2rem}#fo-panel .fo-s{color:#555;margin:4px 0 8px}#fo-panel .fo-i{display:flex;gap:8px;align-items:center;justify-content:space-between;border-top:1px solid #eee;padding:8px 0}#fo-panel .fo-i span{color:#666;font-size:.9rem}'+
'#fo-panel .fo-i button{min-height:44px;border:2px solid #b00020;color:#b00020;background:#fff;border-radius:10px;font-weight:700;padding:0 12px}#fo-panel .fo-b{display:block;width:100%;min-height:48px;margin-top:8px;border:0;border-radius:12px;background:#a49f2a;color:#fff;font-weight:800;font-size:1.05rem;text-align:center;line-height:48px;text-decoration:none;padding:0}#fo-panel .fo-g{background:#eee;color:#111}'+
'#fo-toast{position:fixed;left:12px;right:12px;bottom:calc(72px + env(safe-area-inset-bottom));z-index:99999;background:#222;color:#fff;border-radius:12px;padding:14px;font:600 1rem system-ui,sans-serif;text-align:center;opacity:0;pointer-events:none;transition:opacity .2s;max-width:520px;margin:0 auto}#fo-toast.show{opacity:1}';
document.head.appendChild(css);
document.addEventListener('submit',async e=>{const f=e.target;if(!f.hasAttribute('data-outbox'))return;if(f._go)return;e.preventDefault();
let up=navigator.onLine!==false;if(up){try{const c=new AbortController(),t=setTimeout(()=>c.abort(),5000);const r=await fetch('/healthz',{cache:'no-store',signal:c.signal});clearTimeout(t);up=r.ok}catch(x){up=false}}
if(up){f._go=1;f.submit();return}
await enqueue(f.action||location.href,{method:'POST',body:new FormData(f)},f.getAttribute('data-outbox'),true);
f.querySelectorAll('[type=submit]').forEach(b=>b.disabled=false);
const m=document.createElement('div');m.className='card';m.id='fo-saved';m.style.cssText='border:2px solid #a49f2a;text-align:center;font-weight:700;font-size:1.1rem;padding:14px;margin:10px 0';m.textContent='Saved on this phone. It will send to the office when you have signal. You can go back to the job.';f.replaceWith(m)},true);
const go=()=>{paint();flush()};if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',go);else go();
window.addEventListener('online',flush);document.addEventListener('visibilitychange',()=>{if(!document.hidden)flush()});setInterval(()=>{all().then(L=>{if(L.some(i=>i.state!=='failed'))flush()}).catch(()=>{})},30000)})();
'''

VAPID_PUBLIC_DEFAULT = 'BOE9zipIF8cw8atKM4E0IBGRa7_SP2347axDzashhpPwtE5qt39ynGBkbi51MIDWPnxcCdbTt2JSuyYs5DgQjPs'
def vapid_public(): return os.environ.get('VAPID_PUBLIC_KEY') or VAPID_PUBLIC_DEFAULT

def push_to_sub(sub, title, body, url='/'):
    # Free web push (VAPID). Private key lives only in the Render env var VAPID_PRIVATE_KEY.
    priv=os.environ.get('VAPID_PRIVATE_KEY'); key=ckey(sub)
    if not priv or not key: return
    def run():
        try: from pywebpush import webpush, WebPushException
        except Exception: return
        conn=get_db(); rows=conn.execute('SELECT * FROM push_subs WHERE sub=?',(key,)).fetchall(); conn.close()
        for r in rows:
            try:
                webpush({'endpoint':r['endpoint'],'keys':{'p256dh':r['p256dh'],'auth':r['auth']}}, json.dumps({'title':title,'body':body,'url':url}),
                        vapid_private_key=priv, vapid_claims={'sub':'mailto:rmunoz@truesensesecurity.com'}, timeout=10)
            except WebPushException as e:
                code=getattr(getattr(e,'response',None),'status_code',0)
                if code in (404,410):
                    c2=get_db(); c2.execute('DELETE FROM push_subs WHERE endpoint=?',(r['endpoint'],)); c2.commit(); c2.close()
            except Exception: pass
    threading.Thread(target=run, daemon=True).start()

def nice_day(sd):
    try: return datetime.strptime(sd,'%Y-%m-%d').strftime('%a %b %-d')
    except Exception: return 'no date set'

@app.route('/api/push/key')
def push_key(): return jsonify({'key':vapid_public(),'on':bool(os.environ.get('VAPID_PRIVATE_KEY'))})

@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    if not me(): return jsonify({'error':'login'}),401
    d=request.get_json(silent=True) or {}; ep=str(d.get('endpoint') or '')[:1000]; k=d.get('keys') or {}
    if not ep.startswith('https://') or not k.get('p256dh') or not k.get('auth'): return jsonify({'error':'bad subscription'}),400
    conn=get_db(); conn.execute('INSERT OR REPLACE INTO push_subs (endpoint,sub,p256dh,auth,created_at) VALUES (?,?,?,?,?)',(ep,ckey(me()),str(k['p256dh'])[:200],str(k['auth'])[:100],now_local_str()))
    conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/api/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    if not me(): return jsonify({'error':'login'}),401
    ep=str((request.get_json(silent=True) or {}).get('endpoint') or '')
    conn=get_db(); conn.execute('DELETE FROM push_subs WHERE endpoint=? AND sub=?',(ep,ckey(me()))); conn.commit(); conn.close(); return jsonify({'ok':True})

OFFLINE_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Offline</title><style>body{font-family:Arial,sans-serif;background:#fff;color:#111;text-align:center;padding:40px 16px;font-size:1.2rem}button{font-size:1.2rem;padding:18px;width:100%;border:0;border-radius:10px;background:#a49f2a;color:#fff;font-weight:700}</style></head>
<body><img src="/static/icons/icon-192.png" alt="True Sense" style="width:120px"><h2>No signal</h2><p>You are offline. Anything you saved on a job will send when you are back online.</p><button onclick="location.href='/'">Try Again</button></body></html>"""

@app.route('/manifest.webmanifest')
def manifest():
    r=jsonify(MANIFEST); r.headers['Content-Type']='application/manifest+json'; return r

@app.route('/sw.js')
def service_worker():
    r=app.response_class(SW_JS, mimetype='application/javascript'); r.headers['Cache-Control']='no-cache'; r.headers['Service-Worker-Allowed']='/'; return r

@app.route('/outbox.js')
def outbox_js():
    r=app.response_class(OUTBOX_JS, mimetype='application/javascript'); r.headers['Cache-Control']='no-cache'; return r

@app.route('/offline')
def offline_page(): return OFFLINE_HTML

@app.after_request
def add_tap_css(resp):
    try:
        # /healthz must stay a plain "ok" for Render. Public quote and share links keep their own markup.
        if request.path=='/healthz' or request.path.startswith(('/q/','/p/','/co/','/s/')):
            return resp
        if resp.mimetype=='text/html' and not resp.direct_passthrough and resp.status_code<400 or resp.mimetype=='text/html' and resp.status_code==503:
            h=resp.get_data(as_text=True)
            if 'id="tap-css"' not in h:
                h=h.replace('</head>', TAP_CSS+PWA_HEAD+'</head>', 1) if '</head>' in h else TAP_CSS+h
                if resp.status_code<400:
                    try: h=apply_shell(h, request.path)
                    except Exception as e: app.logger.warning('shell failed: %s', e)
                resp.set_data(h)
    except Exception: pass
    return resp

# ---- background timer: daily DB backup (keep 7) and reminder drafts. Drafts only, nothing is sent. ----
BACKUP_DIR = os.path.join(DATA_DIR, 'backups'); BACKUP_KEEP = 7; _BG = {'started': False}; _BG_LOCK = threading.Lock()
def backup_db():
    day=today_local().isoformat(); os.makedirs(BACKUP_DIR, exist_ok=True)
    dst=os.path.join(BACKUP_DIR, 'fieldops-%s.db' % day)
    if os.path.exists(dst): return None
    tmp=dst+'.tmp'; src=sqlite3.connect(DB_PATH); out=sqlite3.connect(tmp)
    try: src.backup(out)
    finally: out.close(); src.close()
    os.replace(tmp, dst)
    old=sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('fieldops-') and f.endswith('.db') and f!='fieldops-pre-platform-v2.db')
    for f in old[:-BACKUP_KEEP]: os.remove(os.path.join(BACKUP_DIR, f))
    print('BACKUP saved %s (%d kept)' % (os.path.basename(dst), min(len(old), BACKUP_KEEP)), flush=True)
    return dst
def bg_tick():
    try: backup_db()
    except Exception as e: print('BACKUP failed %s' % e, flush=True)
    try:
        conn=get_db(); REMIND_LAST[0]=0; make_reminders(conn); run_plans(conn); conn.close()
    except Exception as e: print('REMINDERS failed %s' % e, flush=True)
def bg_loop():
    while True:
        bg_tick(); time.sleep(int(os.environ.get('BG_EVERY', 900)))
@app.before_request
def bg_start():
    if _BG['started']: return
    with _BG_LOCK:
        if _BG['started']: return
        _BG['started']=True
    if os.environ.get('BG_TIMER','1')!='0': threading.Thread(target=bg_loop, daemon=True, name='fo-bg').start()

# ---------- App shell: top search, tabs / rail, quick-add, theme ----------
def _ico(d): return '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+d+'</svg>'
ICONS = {
 'today':_ico('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
 'jobs':_ico('<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1M9 10h6M9 14h6M9 18h3"/>'),
 'clients':_ico('<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5"/><circle cx="17" cy="9" r="2.5"/><path d="M17.5 14.5c2 .4 3.4 2 4 4.5"/>'),
 'money':_ico('<path d="M12 2v20M17 6H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>'),
 'more':_ico('<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>'),
 'cal':_ico('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'),
 'me':_ico('<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4.5 4.2-7 8-7s7 2.5 8 7"/>'),
 'search':_ico('<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>'),
 'plus':_ico('<path d="M12 5v14M5 12h14"/>'),
 'box':_ico('<path d="M21 8l-9-5-9 5 9 5 9-5zM3 8v8l9 5 9-5V8M12 13v8"/>'),
 'repeat':_ico('<path d="M17 2l4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3"/>'),
 'tool':_ico('<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/>'),
 'eye':_ico('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>'),
 'note':_ico('<path d="M4 4h12l4 4v12H4z"/><path d="M8 12h8M8 16h5"/>'),
 'chart':_ico('<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'),
 'clock':_ico('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
 'qbo':_ico('<circle cx="12" cy="12" r="9"/><path d="M9 8v8M9 8h3a2.5 2.5 0 0 1 0 5H9M15 16v-8"/>'),
 'users':_ico('<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5M16 4.5a3.5 3.5 0 0 1 0 7M18.5 14.5c1.6.6 2.6 2.4 3 5.5"/>'),
 'check':_ico('<path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>'),
 'shield':_ico('<path d="M12 2l8 3v6c0 5-3.5 9-8 11-4.5-2-8-6-8-11V5z"/><path d="M9 12l2 2 4-4"/>'),
 'pen':_ico('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
 'gear':_ico('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
 'cart':_ico('<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/><path d="M2 3h3l2.7 12.5h11.6L22 7H6"/>'),
 'moon':_ico('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'),
 'bell':_ico('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"/>'),
 'out':_ico('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>'),
 'doc':_ico('<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h8"/>'),
 'alert':_ico('<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/>'),
 'pin':_ico('<path d="M12 22s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12z"/><circle cx="12" cy="10" r="2.5"/>'),
}

SHELL_CSS = """<style id="shell-css">
:root{--brand:#7a7a12;--gold:#baa205;--gold2:#dab703;--accent:#a49f2a;--ink:#1d1f14;--muted:#6b6d60;--line:#ecece6;--soft:#f7f6ef;--card:#fff;
--shadow:0 1px 2px rgba(29,31,20,.06),0 8px 24px rgba(29,31,20,.06);--r:16px;
--display:"Palatino Linotype",Palatino,"Book Antiqua",Georgia,"Times New Roman",serif;
--text:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
html.fo-shell{--fo-head:172px;background:var(--soft);padding:calc(var(--fo-head) + env(safe-area-inset-top)) 0 32px;scroll-padding-top:calc(var(--fo-head) + 12px)}
html.fo-shell body{font-family:var(--text)!important;color:var(--ink);background:var(--soft)!important;-webkit-font-smoothing:antialiased;overflow-x:hidden;max-width:1280px!important;margin:0 auto!important;padding-left:16px!important;padding-right:16px!important}
html.fo-shell .header{display:none}
html.fo-shell body>img[alt="True Sense Security"],html.fo-shell body>a>img[alt="True Sense Security"]{display:none!important}
html.fo-shell .fo-back{display:inline-flex;align-items:center;width:auto;min-height:44px;padding:10px 18px;font-size:1rem;margin:0 0 4px}
html.fo-shell #search-box{display:none!important}
html.fo-shell h1,html.fo-shell h2,html.fo-shell .plat-title,html.fo-shell .fo-hello h1{font-family:var(--display)}
.fo-top{position:fixed;top:0;left:0;right:0;z-index:45;background:rgba(255,255,255,.94);backdrop-filter:saturate(1.4) blur(12px);-webkit-backdrop-filter:saturate(1.4) blur(12px);border-bottom:1px solid var(--line);
display:grid;grid-template-columns:auto 1fr auto;grid-template-areas:"logo switch tools" "links links links" "search search search";gap:8px 12px;align-items:center;padding:8px 12px calc(8px);padding-top:calc(8px + env(safe-area-inset-top))}
.fo-logo{grid-area:logo;display:flex;align-items:center;gap:8px;text-decoration:none;color:var(--ink);min-height:44px}
.fo-logo img{height:24px;width:auto;display:block}
.fo-word{display:none;font-family:var(--display);font-size:1.15rem;letter-spacing:.01em}
.fo-switch{grid-area:switch;display:inline-flex;background:var(--soft);border:1px solid var(--line);border-radius:999px;padding:3px;gap:2px;justify-self:start;flex-shrink:0;white-space:nowrap}
.fo-switch a{display:inline-flex;align-items:center;justify-content:center;min-height:32px;padding:0 9px;border-radius:999px;text-decoration:none;color:var(--muted);font-weight:700;font-size:.8rem;white-space:nowrap}
.fo-switch a.on{background:var(--brand);color:#fff}
.fo-links{grid-area:links;display:flex;gap:2px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;min-height:40px;align-items:center}
.fo-links::-webkit-scrollbar{display:none}
.fo-links a,.fo-links summary{display:inline-flex;align-items:center;min-height:40px;padding:0 12px;color:var(--ink);text-decoration:none;font-weight:650;font-size:.95rem;white-space:nowrap;border-bottom:3px solid transparent;background:none;border-radius:0;cursor:pointer;font-family:inherit}
.fo-links a.on{border-bottom-color:var(--accent);color:var(--brand)}
.fo-search{grid-area:search;display:flex;align-items:center;gap:8px;background:var(--soft);border:1px solid var(--line);border-radius:999px;padding:0 14px;height:42px;margin:0}
.fo-search svg{flex:0 0 18px;width:18px;height:18px;color:var(--muted)}
.fo-search input{border:0!important;background:transparent!important;outline:0;font-size:16px;width:100%;padding:0!important;margin:0!important;height:40px;color:var(--ink);box-shadow:none!important}
.fo-tools{grid-area:tools;display:flex;align-items:center;gap:8px}
.fo-icon-btn{width:42px;height:42px;border-radius:50%;border:0;background:var(--brand);color:#fff;display:inline-flex;align-items:center;justify-content:center;cursor:pointer;text-decoration:none}
.fo-icon-btn svg{width:22px;height:22px;stroke-width:2.4}
.fo-icon-btn.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.fo-avatar{width:42px;height:42px;border-radius:50%;border:1px solid var(--line);background:var(--soft);color:var(--brand);font-weight:800;display:inline-flex;align-items:center;justify-content:center;text-decoration:none;cursor:pointer;font-size:.85rem}
.fo-user{position:relative}
.fo-user summary{list-style:none}.fo-user summary::-webkit-details-marker{display:none}
.fo-user-menu{position:absolute;right:0;top:calc(100% + 6px);background:#fff;border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);min-width:200px;padding:8px;z-index:50}
.fo-user-menu a,.fo-user-menu button{display:block;width:100%;text-align:left;background:none;border:0;padding:12px 12px;border-radius:10px;font:inherit;font-weight:650;color:var(--ink);text-decoration:none;cursor:pointer}
.fo-user-menu a:hover,.fo-user-menu button:hover{background:var(--soft)}
.fo-more{position:relative;display:inline-flex}
.fo-more summary{list-style:none}.fo-more summary::-webkit-details-marker{display:none}
.fo-more-menu{position:absolute;left:0;top:calc(100% + 4px);background:#fff;border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);min-width:220px;padding:8px;z-index:50;display:flex;flex-direction:column}
.fo-more-menu a{border:0;min-height:44px;border-radius:10px;padding:0 12px}
.fo-more-menu a:hover{background:var(--soft)}
.fo-veil{position:fixed;inset:0;background:rgba(20,21,14,.38);z-index:70;opacity:0;pointer-events:none;transition:opacity .2s}
.fo-veil.on{opacity:1;pointer-events:auto}
.fo-sheet{position:fixed;left:0;right:0;bottom:0;z-index:71;background:#fff;border-radius:22px 22px 0 0;padding:10px 16px calc(18px + env(safe-area-inset-bottom));transform:translateY(105%);transition:transform .28s cubic-bezier(.2,.8,.2,1);box-shadow:0 -10px 30px rgba(0,0,0,.15);max-width:640px;margin:0 auto;visibility:hidden}
.fo-sheet.on{transform:none;visibility:visible}.fo-grab{width:44px;height:5px;border-radius:5px;background:#ddd;margin:0 auto 10px}
.fo-sheet h3{margin:4px 0 10px;font-size:1.15rem;font-family:var(--display)}.fo-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fo-tile{display:flex;align-items:center;gap:10px;padding:16px 14px;border-radius:14px;background:var(--soft);border:1px solid var(--line);color:var(--ink);text-decoration:none;font-weight:700;font-size:1rem;min-height:60px;text-align:left;width:100%;font-family:inherit}
.fo-tile svg{flex:0 0 24px;color:var(--brand)}
.fo-seg{display:flex;background:var(--soft);border:1px solid var(--line);border-radius:14px;padding:4px;gap:4px;margin:0 auto 12px;max-width:520px}
.fo-seg a{flex:1;text-align:center;padding:10px 6px;border-radius:10px;color:var(--muted);text-decoration:none;font-weight:700}.fo-seg a.on{background:#fff;color:var(--ink);box-shadow:var(--shadow)}
html.fo-shell .job,html.fo-shell .card{border:1px solid var(--line)!important;border-radius:var(--r)!important;box-shadow:var(--shadow);background:#fff}
html.fo-shell .big-btn,html.fo-shell .btn,html.fo-shell .plat-btn{border-radius:14px!important;font-weight:700;letter-spacing:.1px;min-height:48px}
html.fo-shell .big-btn:not(.gray):not(.green):not(.stop):not(.red){background:linear-gradient(145deg,var(--gold),var(--brand));color:#fff}
html.fo-shell input,html.fo-shell select,html.fo-shell textarea{border-radius:12px!important;font-family:inherit}
html.fo-shell .tile{border-radius:16px;box-shadow:var(--shadow);border-color:var(--line)}
.fo-page-h{display:flex;align-items:center;gap:10px;margin:4px 0 12px}.fo-page-h h1{margin:0}
.fo-cards{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.fo-card{display:block;background:#fff;border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);padding:16px;color:var(--ink);text-decoration:none;min-height:96px}
.fo-card svg{color:var(--brand)}.fo-card b{display:block;font-size:1.08rem;margin-top:8px}.fo-card small{display:block;color:var(--muted);margin-top:2px;font-size:.86rem}
.fo-skel{background:linear-gradient(90deg,#f0efe8 0,#faf9f4 40%,#f0efe8 80%);background-size:200% 100%;animation:fo-shim 1.2s linear infinite;border-radius:12px}
@keyframes fo-shim{to{background-position:-200% 0}}
.fo-hello h1{font-size:2rem;margin:2px 0 2px;letter-spacing:-.3px;font-weight:500}.fo-hello{margin-bottom:12px}
.fo-prog{margin:0 0 14px}.fo-bar{display:flex;height:12px;border-radius:12px;background:#eeede4;overflow:hidden}.fo-bar i{display:block;height:100%;transition:width .6s ease}
.fo-legend{display:flex;gap:16px;margin-top:6px;color:var(--muted);font-size:.9rem}.fo-legend b{color:var(--ink)}
.fo-kpis{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:6px}
.fo-kpi{display:block;background:#fff;border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);padding:14px;text-decoration:none;color:var(--ink);min-height:108px;position:relative;overflow:hidden}
.fo-kpi small{display:block;color:var(--muted);font-weight:650;font-size:.85rem}.fo-kpi b{display:block;font-size:1.9rem;letter-spacing:-.5px;margin-top:4px;line-height:1.1}
.fo-kpi.hot b{color:#b3261e}.fo-spark{display:block;width:100%;height:30px;margin-top:6px}
.fo-h{font-size:1.2rem!important;color:var(--ink)!important;border:0!important;margin:22px 0 10px!important;letter-spacing:-.2px;font-family:var(--display)!important;font-weight:500!important}
.fo-act{display:flex;align-items:center;gap:12px;background:#fff;border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);padding:12px;margin-bottom:10px;color:var(--ink);text-decoration:none;min-height:64px}
.fo-ai{flex:0 0 42px;height:42px;border-radius:12px;background:var(--soft);color:var(--brand);display:flex;align-items:center;justify-content:center;font-size:1.3rem}
.fo-act.red .fo-ai{background:#fdecea;color:#b3261e}.fo-act.ok .fo-ai{background:#e8f5e9;color:#2e7d32}
.fo-at{flex:1;min-width:0}.fo-at b{display:block}.fo-at em{font-style:normal;background:var(--ink);color:#fff;border-radius:10px;padding:0 8px;font-size:.8rem;margin-left:4px;vertical-align:2px}
.fo-act.red .fo-at em{background:#b3261e}.fo-at small{display:block;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fo-ab{flex:0 0 auto;background:linear-gradient(145deg,var(--gold),var(--brand));color:#fff;font-weight:700;border-radius:12px;padding:10px 14px;min-width:72px;text-align:center}
#fo-map{height:240px;border-radius:var(--r);overflow:hidden;margin-bottom:10px;border:1px solid var(--line)}
.fo-row{padding:12px 4px;border-bottom:1px solid var(--line);cursor:pointer}.fo-row:last-child{border-bottom:0}
.fo-pulse{display:inline-block;width:10px;height:10px;border-radius:50%;background:#2e7d32;margin-right:8px;box-shadow:0 0 0 0 rgba(46,125,50,.5);animation:fo-pl 1.8s infinite}
@keyframes fo-pl{70%{box-shadow:0 0 0 10px rgba(46,125,50,0)}100%{box-shadow:0 0 0 0 rgba(46,125,50,0)}}
.fo-kpi.hot b{color:#b3261e}.fo-cap{display:block;font-style:normal;color:var(--muted);font-size:.72rem;margin-top:2px}
.fo-empty{background:#fff;border:1px dashed var(--line);border-radius:var(--r);padding:16px;color:var(--muted);text-align:center}
html.fo-shell body:has(.plat-head) > a.fo-back,html.fo-shell body:has(.plat-head) > h1{display:none!important}
/* Platform record pages */
.plat-head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px 20px;margin:8px 0 18px}
.plat-kicker{font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);margin-bottom:4px}
.plat-title{font-family:var(--display);font-weight:500;font-size:clamp(2rem,5vw,2.8rem);line-height:1.05;margin:0;letter-spacing:-.01em}
.plat-dot{color:var(--accent)}
.plat-actions{display:flex;flex-wrap:wrap;gap:8px}
.plat-btn{display:inline-flex;align-items:center;justify-content:center;min-height:48px;padding:0 18px;border-radius:999px;font-weight:700;text-decoration:none;border:0;cursor:pointer;font-size:1rem;font-family:inherit}
.plat-btn-gold{background:linear-gradient(145deg,var(--gold),var(--brand));color:#fff}
.plat-btn-ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.plat-msg{background:#fff;border:1px solid var(--line);border-left:5px solid var(--accent);border-radius:12px;padding:12px 14px;margin:0 0 14px}
.plat-cards{display:grid;grid-template-columns:1fr;gap:12px}
.plat-card{display:block;background:#fff;border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);padding:16px;color:var(--ink);text-decoration:none;min-height:88px}
.plat-card b{display:block;font-size:1.08rem;color:var(--ink)}
.plat-panel{background:#fff;border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);padding:16px;margin:0 0 14px}
.plat-panel h2{margin:0 0 10px;font-size:1.25rem;font-weight:500}
.plat-panel-h{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}
.plat-panel-h h2{margin:0}
.plat-list{list-style:none;margin:0;padding:0}
.plat-list li{padding:10px 0;border-bottom:1px solid var(--line)}
.plat-list li:last-child{border-bottom:0}
.plat-list a{color:var(--ink);text-decoration:none}
.plat-pill{display:inline-block;background:var(--soft);border:1px solid var(--line);border-radius:999px;padding:2px 8px;font-size:.78rem;font-weight:700;margin-left:6px}
.plat-form label{display:block;font-weight:700;margin:10px 0 4px}
.plat-form input,.plat-form select,.plat-form textarea{width:100%;padding:12px 12px;border:1px solid var(--line);border-radius:12px;font-size:1rem;background:#fff;box-sizing:border-box}
.plat-row{display:grid;grid-template-columns:1fr;gap:10px}
.plat-filter{display:flex;gap:8px;margin:0 0 14px}
.plat-filter input{flex:1;padding:12px 14px;border:1px solid var(--line);border-radius:999px;font-size:16px;background:#fff}
.plat-empty{background:#fff;border:1px dashed var(--line);border-radius:var(--r);padding:20px;color:var(--muted);text-align:center}
.plat-stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:0 0 16px}
.plat-stat{background:#fff;border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);padding:14px;text-decoration:none;color:var(--ink);display:block}
.plat-stat small{display:block;color:var(--muted);font-weight:700;font-size:.8rem;letter-spacing:.04em;text-transform:uppercase}
.plat-stat b{display:block;font-family:var(--display);font-weight:500;font-size:1.7rem;margin-top:4px}
.plat-pills{display:flex;gap:6px;overflow-x:auto;padding:0 0 12px;-webkit-overflow-scrolling:touch}
.plat-pill-link{display:inline-flex;align-items:center;gap:6px;min-height:40px;padding:0 12px;border-radius:999px;border:1px solid var(--line);background:#fff;color:var(--ink);text-decoration:none;font-weight:700;white-space:nowrap;font-size:.9rem}
.plat-pill-link em{font-style:normal;color:var(--muted)}
.plat-pill-link.on{background:var(--brand);color:#fff;border-color:var(--brand)}
.plat-pill-link.on em{color:#fff}
.plat-board{display:flex;flex-direction:column;gap:16px}
.plat-col-h{display:flex;justify-content:space-between;font-weight:700;margin:0 0 8px}
.plat-col-h em{font-style:normal;color:var(--muted);font-weight:650}
.plat-mini-actions{margin-top:4px;font-size:.9rem}
.plat-mini-actions a{color:var(--brand);font-weight:700;text-decoration:none}
.plat-grid-2{display:grid;grid-template-columns:1fr;gap:12px}
@media(min-width:700px){
  .plat-cards{grid-template-columns:repeat(2,1fr)}
  .plat-stats{grid-template-columns:repeat(3,1fr)}
  .plat-row{grid-template-columns:1fr 1fr}
  .plat-grid-2{grid-template-columns:1fr 1fr}
}
.fo-more-desk{display:none}
@media(min-width:980px){
  .fo-more-phone{display:none!important}
  .fo-more-desk{display:inline-flex}
}
@media(min-width:980px){
  html.fo-shell{--fo-head:72px;padding-left:0;padding-right:0}
  html.fo-shell body{padding-left:28px!important;padding-right:28px!important}
  .fo-top{grid-template-columns:auto auto 1fr minmax(200px,420px) auto;grid-template-areas:"logo switch links search tools";padding:10px 22px;align-items:center}
  .fo-word{display:inline}
  .fo-logo img{height:32px}
  .fo-links{overflow:visible}
  .fo-search{max-width:420px;justify-self:end;width:100%}
  .fo-switch a{min-height:34px;padding:0 14px}
  .fo-kpis{grid-template-columns:repeat(4,1fr)}
  #today{display:grid;grid-template-columns:1fr 1fr;column-gap:24px}#today>.fo-hello,#today>.fo-prog,#today>.fo-kpis{grid-column:1/-1}
  .fo-cards{grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
  .plat-cards{grid-template-columns:repeat(3,1fr)}
  .plat-board{display:flex;flex-direction:row;gap:12px;align-items:flex-start;overflow-x:auto;padding-bottom:8px}
  .plat-col{background:rgba(255,255,255,.5);border:1px solid var(--line);border-radius:14px;padding:10px;flex:1 0 186px;min-width:186px}
  .plat-col-h{white-space:nowrap}
  .plat-col .plat-card{margin-bottom:8px;min-height:72px}
.plat-col.over{outline:2px solid var(--gold,#a49f2a);background:#fff}
.plat-opp{position:relative;cursor:grab;touch-action:none;user-select:none}
.plat-opp:active{cursor:grabbing}
.plat-opp.dragging{opacity:.45}
.plat-opp .plat-opp-move{display:flex;flex-direction:column;align-items:stretch;gap:4px;margin-top:8px}
.plat-opp .plat-opp-move select{width:100%;font-size:.85rem;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:#fff}
.plat-ghost{position:fixed;z-index:80;pointer-events:none;width:200px;opacity:.92;box-shadow:0 8px 24px rgba(0,0,0,.18)}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}
html[data-theme=dark]{filter:invert(.93) hue-rotate(180deg);background:#fff}
html[data-theme=dark] img,html[data-theme=dark] video,html[data-theme=dark] canvas,html[data-theme=dark] .leaflet-container,html[data-theme=dark] iframe,html[data-theme=dark] [style*="background-image"]{filter:invert(1) hue-rotate(180deg)}
html[data-theme=dark] .leaflet-container img{filter:none}
.plat-link-btn{background:none;border:0;color:var(--gold,#6b6a16);padding:0;font:inherit;font-weight:650;cursor:pointer;text-decoration:underline}
.plat-mini-actions{display:flex;flex-wrap:wrap;gap:10px 16px;margin-top:6px}
.cat-row{display:grid;grid-template-columns:minmax(8em,2fr) 6em 5em 7em 6em 6em auto;gap:6px 8px;align-items:end;margin:0 0 8px}
.cat-row label{display:flex;flex-direction:column;gap:2px;font-size:.75rem;font-weight:650;color:var(--muted);margin:0}
.cat-row input{width:100%;margin:0;padding:8px 10px}
.cat-btns{display:flex;gap:6px;flex-wrap:wrap}
@media (max-width:800px){.cat-row{grid-template-columns:1fr 1fr}}
</style>"""


def plat_area(path=''):
    path = path or (request.path if request else '/')
    try:
        if hasattr(sys.modules[__name__], 'plat_area_of'):
            return sys.modules[__name__].plat_area_of(path)
    except Exception:
        pass
    import fieldops_platform as _plat
    saved = (session.get('fo_area') if session else None) or ''
    area_arg = (request.args.get('area') if request else None) or ''
    return _plat.area_from_request(path, saved, area_arg)[0]

def initials_of(name):
    parts=[x for x in (name or '').split() if x]
    if not parts: return 'TS'
    if len(parts)==1: return parts[0][:2].upper()
    return (parts[0][0]+parts[-1][0]).upper()

def shell_tabs(path, office):
    # Kept for compatibility. Office chrome now uses shell_links().
    if office:
        tabs=[('/','Today','today'),('/jobs','Jobs','jobs'),('/customers','Customers','clients'),('/money','Money','money'),('/more','More','more')]
        groups={'today':('/',),'jobs':('/jobs','/schedule','/calendar','/new-job','/history'),'clients':('/customers','/clients','/contacts','/sites','/print/client'),'money':('/money','/revenue','/reports','/qbo','/timelog'),
                'more':('/more','/techs','/stock','/checklists','/safety','/agreement','/settings','/equipment','/plans','/to-order','/orders')}
    else:
        tabs=[('/','My Jobs','jobs'),('/my-schedule','Schedule','cal'),('/me','Me','me')]
        groups={'jobs':('/','/job/'),'cal':('/my-schedule',),'me':('/me','/agreement')}
    def on(k):
        for p_ in groups.get(k,()):
            if (p_=='/' and path=='/') or (p_!='/' and (path==p_ or path.startswith(p_.rstrip('/')+'/'))): return True
        return False
    return ''.join('<a href="%s" class="%s"%s>%s<span>%s</span><span class="fo-dot"></span></a>'%(u,'on' if on(k) else '',' aria-current="page"' if on(k) else '',ICONS[k],lab) for u,lab,k in tabs)

def shell_links(path, office):
    area=plat_area(path)
    more=[('/stock','Stock'),('/orders','Orders'),('/plans','Service plans'),('/tickets','Service tickets'),('/compliance','Sub compliance'),('/equipment','Equipment'),('/techs','Subs'),('/users','Users'),('/checklists','Checklists'),('/safety','Safety'),('/agreement','Agreements'),('/to-order','To order'),('/money','Money'),('/costing','Job costing'),('/timesheets','Timesheets'),('/change-orders','Change orders'),('/reports','Reports'),('/timelog','Time log'),('/settings','Settings')]
    if area=='sales':
        main=[('/sales','Home'),('/opportunities','Opportunities'),('/tasks','Tasks'),('/customers','Customers')]
    elif area=='quote':
        main=[('/quote','Estimates'),('/catalog','Catalog'),('/customers','Customers')]
    else:
        main=[('/','Today'),('/jobs','Jobs'),('/schedule','Schedule'),('/customers','Customers')]
    def on_url(u):
        if u=='/' and path=='/': return True
        if u!='/' and (path==u or path.startswith(u.rstrip('/')+'/')): return True
        if u=='/customers' and path.startswith(('/clients','/contacts','/sites','/systems')): return True
        if u=='/jobs' and path.startswith(('/history','/calendar','/new-job')): return True
        if u=='/opportunities' and path.startswith('/opportunity'): return True
        if u=='/quote' and (path.startswith('/quote') or path.startswith('/job/') and path.rstrip('/').endswith('/estimate')): return True
        if u=='/sales' and path=='/sales': return True
        return False
    html=''.join('<a href="%s" class="%s"%s>%s</a>'%(h(u),'on' if on_url(u) else '',' aria-current="page"' if on_url(u) else '',h(lab)) for u,lab in main)
    more_on=any((path==u or path.startswith(u.rstrip('/')+'/')) for u,_ in more)
    html+='<a class="fo-more-phone %s" href="/more">More</a>'%('on' if path.startswith('/more') or more_on else '')
    html+='<details class="fo-more fo-more-desk"><summary class="%s">More</summary><div class="fo-more-menu">'%('on' if more_on else '')
    html+=''.join('<a href="%s">%s</a>'%(h(u),h(lab)) for u,lab in more)
    html+='</div></details>'
    return html

def shell_html(path, office, q=''):
    area=plat_area(path)
    switch=''.join('<a href="%s" class="%s">%s</a>'%(u,'on' if area==k else '',lab) for k,u,lab in (('ops','/?area=ops','Ops'),('sales','/sales','Sales'),('quote','/quote','Quote')))
    links=shell_links(path, office)
    who=me() or ''
    top=('<header class="fo-top fo-sh"><a class="fo-logo" href="/" aria-label="Home"><img src="/static/logo.png" alt="True Sense Security"><span class="fo-word">Field Ops</span></a>'
         '<nav class="fo-switch" aria-label="Area">%s</nav>'
         '<nav class="fo-links" aria-label="Main">%s</nav>'
         '<form class="fo-search" action="/search" role="search">%s<input type="search" name="q" value="%s" placeholder="Search customers, jobs, notes" aria-label="Search" autocomplete="off"></form>'
         '<div class="fo-tools"><button class="fo-icon-btn" type="button" aria-label="Quick add" onclick="foSheet(true)">%s</button>'
         '<details class="fo-user"><summary class="fo-avatar" title="%s">%s</summary><div class="fo-user-menu">'
         '<a href="/me">My settings</a><a href="/settings">Office settings</a>'
         '<button type="button" id="fo-theme" onclick="foTheme(document.documentElement.getAttribute(&quot;data-theme&quot;)===&quot;dark&quot;?&quot;light&quot;:&quot;dark&quot;)">Dark mode</button>'
         '<button type="button" onclick="foLogout()">Log out</button>'
         '</div></details></div></header>')%(switch, links, ICONS['search'], h(q), ICONS['plus'], h(who), h(initials_of(who)))
    seg=''
    if office and path in ('/jobs','/schedule','/calendar'):
        seg='<div class="fo-seg fo-sh">'+''.join('<a href="%s" class="%s">%s</a>'%(u,'on' if path==u else '',l) for u,l in (('/jobs','List'),('/schedule','Schedule'),('/calendar','Calendar')))+'</div>'
    items=[('/new-job?t=Install','Install','tool'),('/new-job?t=Service','Service','repeat'),('/new-job?t=Survey','Survey','eye'),('/new-job?t=Office Task','Office Task','note'),
           ('/customers/new','Company','clients'),('/opportunities/new','Opportunity','pen'),('/stock','Stock Item','box'),('/plans','Service Plan','cal'),('/equipment','Equipment','shield')]
    sheet=('<div class="fo-veil fo-sh" id="fo-veil" onclick="foSheet(false)"></div><div class="fo-sheet fo-sh" id="fo-sheet" role="dialog" aria-label="Quick add"><div class="fo-grab"></div><h3>Quick add</h3><div class="fo-grid">'+
           ''.join('<a class="fo-tile" href="%s" data-new="%s" onclick="return foNew(this)">%s%s</a>'%(h(u),h(u.split('t=')[1]) if '?t=' in u else '',ICONS[i],h(l)) for u,l,i in items)+'</div></div>')
    return top+seg, sheet

SHELL_JS = """<script id="shell-js">
function foSheet(on){var s=document.getElementById('fo-sheet'),v=document.getElementById('fo-veil');if(!s)return;s.classList.toggle('on',on);v.classList.toggle('on',on)}
function foNew(a){var t=a.dataset.new;if(t&&typeof quickType==='function'&&document.getElementById('n-id')){foSheet(false);quickType(t);return false}return true}
document.addEventListener('keydown',function(e){if(e.key==='Escape')foSheet(false)});
function foTheme(t){document.documentElement.setAttribute('data-theme',t);fetch('/api/me/theme',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({theme:t})});var b=document.getElementById('fo-theme');if(b)b.textContent=t==='dark'?'Dark mode is on':'Dark mode is off'}
function foLogout(){if(!confirm('Log out?'))return;try{navigator.serviceWorker.controller&&navigator.serviceWorker.controller.postMessage('logout')}catch(e){}location.href='/logout'}
function foB64(x){var p='='.repeat((4-x.length%4)%4);var r=atob((x+p).replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from(r,function(c){return c.charCodeAt(0)})}
async function foPush(btn){if(!('serviceWorker' in navigator)||!('PushManager' in window)){alert('On iPhone: tap Share, then Add to Home Screen, then open Field Ops from your home screen.');return}
btn.disabled=true;try{var perm=await Notification.requestPermission();if(perm!=='granted'){alert('Alerts are off. Turn them on in your phone settings.');btn.disabled=false;return}
var k=await (await fetch('/api/push/key')).json();var reg=await navigator.serviceWorker.ready;var sub=(await reg.pushManager.getSubscription())||(await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:foB64(k.key)}));
var r=await fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sub)});btn.textContent=r.ok?'Job alerts are on':'Could not turn on alerts';}catch(e){btn.textContent='Could not turn on alerts';btn.disabled=false}}
</script>"""

def theme_of(user):
    try:
        conn=get_db(); r=conn.execute('SELECT value FROM settings WHERE key=?',('theme:'+ckey(user),)).fetchone(); conn.close()
        return 'dark' if r and r['value']=='dark' else 'light'
    except Exception: return 'light'

SHELL_SKIP = ('/design/','/hub/','/s/','/p/','/help-photo/','/print/','/agreement/view/','/agreement/copy/','/login','/offline','/uploads/','/logout')
def apply_shell(html, path):
    if 'id="shell-css"' in html or not me() or not is_office() or path.startswith(SHELL_SKIP) or '<body' not in html: return html
    office=True; top,bottom=shell_html(path, office, request.args.get('q','') if path=='/search' else '')
    html=html.replace('<html','<html class="fo-shell" data-theme="%s"'%theme_of(me()),1)
    html=html.replace('</head>',SHELL_CSS+'</head>',1)
    i=html.find('>',html.find('<body'))+1
    html=html[:i]+top+html[i:]
    j=html.rfind('</body>')
    return (html[:j]+bottom+SHELL_JS+html[j:]) if j>0 else html+bottom+SHELL_JS

@app.route('/api/me/theme', methods=['POST'])
def set_theme():
    if not me(): return jsonify({'error':'login'}),401
    t='dark' if (request.get_json(silent=True) or {}).get('theme')=='dark' else 'light'
    conn=get_db(); conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)',('theme:'+ckey(me()),t)); conn.commit(); conn.close()
    return jsonify({'ok':True,'theme':t})

def card_grid(items):
    return '<div class="fo-cards">'+''.join('<a class="fo-card" href="%s">%s<b>%s</b><small>%s</small></a>'%(h(u),ICONS.get(i,''),h(t),h(s)) for u,t,s,i in items)+'</div>'

@app.route('/jobs')
def jobs_view():
    if not is_office(): return redirect('/')
    return index(view='jobs')

@app.route('/money')
def money_page():
    if not is_office(): return redirect('/')
    conn=get_db(); t=today_local(); mo=t.strftime('%Y-%m')
    sales=sum((r['sale_amount'] or 0) for r in conn.execute("SELECT sale_amount,completed_at FROM jobs WHERE status='completed'") if utc_day(r['completed_at'])[:7]==mo)
    qp=conn.execute('SELECT paid FROM qbo_monthly WHERE month=?',(mo,)).fetchone()
    queued=conn.execute("SELECT COUNT(*) FROM qbo_queue WHERE status='queued'").fetchone()[0]; conn.close()
    body='<div class="fo-cards" style="margin-bottom:14px"><div class="fo-card">%s<small>Field Ops sales, %s</small><b style="font-size:1.6rem">%s</b></div><div class="fo-card">%s<small>QuickBooks paid, %s</small><b style="font-size:1.6rem">%s</b></div></div>'%(
        ICONS['chart'],t.strftime('%B'),money(sales),ICONS['qbo'],t.strftime('%B'),money(qp['paid']) if qp and qp['paid'] is not None else 'Not synced')
    body+=card_grid([('/revenue','Revenue','Sales vs QuickBooks, 12 months','chart'),('/reports','Reports','Profit by job, sub and type','money'),
                     ('/qbo','QuickBooks','%d waiting to export'%queued if queued else 'Export approved invoices','qbo'),('/timelog','Time Log','Clock-ins, GPS and hours','clock')])
    return ops_page('Money', body, back=False)

@app.route('/more')
def more_page():
    if not is_office(): return redirect('/')
    return ops_page('More', card_grid([('/techs','Subs','Rates, docs and jobs','users'),('/stock','Stock','Shop and truck parts','box'),('/orders','Orders','Material requests to ADI','cart'),('/to-order','To Order','Parts to buy','cart'),
        ('/equipment','Equipment','Devices by site','shield'),('/plans','Service Plans','Recurring visits','repeat'),('/tickets','Service tickets','Call and portal intake queue','alert'),('/compliance','Sub compliance','COI, W-9, agreements and 1099 totals','doc'),('/checklists','Checklists','Job templates','check'),
        ('/safety','Safety','Daily checks and talks','alert'),('/agreement','Agreement','Sub agreement and signatures','pen'),('/users','Users','Office and field logins','users'),('/history','Job History','Completed jobs','clock'),('/schedule','Schedule','Crew board, day week fortnight and month','cal'),('/calendar','Calendar','Month view of the same board','cal'),('/costing','Job costing','Sale, labor, materials and profit','money'),('/timesheets','Timesheets','Weekly approval and sub-pay CSV','clock'),('/change-orders','Change orders','Priced extras the client can accept','note'),('/revenue','Revenue','Sales vs QuickBooks','chart'),('/settings','Settings','Dark mode and account','gear')]), back=False)

def me_settings_body():
    dark=theme_of(me())=='dark'
    return ('<div class="fo-card" style="margin-bottom:12px"><b style="margin:0">%s</b><small>%s</small></div>'%(h(me()),'Office' if is_office() else 'Sub')+
            '<button id="fo-theme" class="big-btn gray" onclick="foTheme(document.documentElement.getAttribute(\'data-theme\')===\'dark\'?\'light\':\'dark\')">%s</button>'%('Dark mode is on' if dark else 'Dark mode is off'))

@app.route('/settings', methods=['GET','POST'])
def settings_page():
    if not is_office(): return redirect('/me')
    msg=''
    post=globals().get('quote_settings_post')
    if request.method=='POST' and callable(post):
        try:
            msg=post() or ''
        except Exception:
            app.logger.exception('quote settings failed')
            msg='Could not save terms.'
    extra=''
    html_fn=globals().get('quote_settings_html')
    if callable(html_fn):
        try:
            extra=html_fn() or ''
        except Exception:
            app.logger.exception('quote settings html failed')
    return ops_page('Settings', me_settings_body()+extra+'<button class="big-btn gray" onclick="foLogout()">Log out</button>', back='/more', msg=msg)

@app.route('/me')
def me_page():
    if not me(): return redirect('/login')
    if is_office(): return redirect('/settings')
    conn=get_db(); a=current_agreement(conn); signed=signed_current(conn, me())
    docs={r['kind']:r for r in conn.execute('SELECT * FROM sub_docs WHERE sub=?',(ckey(me()),)).fetchall()}; conn.close()
    body=me_settings_body()
    body+='<h2>My Agreement</h2><a class="fo-card" href="/agreement">%s<b>Subcontractor Agreement v%s</b><small>%s</small></a>'%(ICONS['pen'],h(a['version']),'Signed' if signed else 'Not signed yet. Tap to sign.')
    body+='<h2>My Documents</h2>'+''.join('<div class="fo-card" style="margin-bottom:8px">%s<b>%s</b><small>%s</small></div>'%(ICONS['doc'],h(lab),
        ('Expires '+h(docs[k]['expires'])+(' (expired)' if (docs[k]['expires'] or '')<today_local().isoformat() else '')) if k in docs else 'Missing. Send it to the office.') for k,lab in SUB_DOCS)
    body+='<h2>Job Alerts</h2><button class="big-btn" onclick="foPush(this)">Turn on job alerts</button><div class="muted">Pings you for new jobs and schedule changes.</div>'
    body+='<button class="big-btn gray" style="margin-top:18px" onclick="foLogout()">Log out</button>'
    return ops_page('Me', body, back=False)

@app.route('/my-schedule')
def my_schedule():
    if not me(): return redirect('/login')
    if is_office(): return redirect('/schedule')
    conn=get_db(); rows=[r for r in conn.execute("SELECT * FROM jobs WHERE status!='completed' ORDER BY COALESCE(scheduled_date,'9999')").fetchall() if can_see(r)]
    t=today_local(); days=[(t+timedelta(days=i)) for i in range(14)]; body=''
    by_day={}
    try:
        for s in conn.execute("SELECT job_id, day FROM job_slots WHERE day>=? AND day<=?",(t.isoformat(),(t+timedelta(days=13)).isoformat())):
            by_day.setdefault(s['day'], set()).add(s['job_id'])
    except Exception:
        pass
    conn.close()
    late=[r for r in rows if (r['scheduled_date'] and r['scheduled_date']<t.isoformat())]
    if late: body+='<h2>Past due</h2>'+''.join(sched_card(r) for r in late)
    seen_ids=set()
    jobs_by_id={r['id']:r for r in rows}
    for d in days:
        js=[r for r in rows if r['scheduled_date']==d.isoformat()]
        for jid in by_day.get(d.isoformat(), ()):
            if jid in jobs_by_id and all(x['id']!=jid for x in js):
                js.append(jobs_by_id[jid])
        lab='Today' if d==t else ('Tomorrow' if d==t+timedelta(days=1) else d.strftime('%a %b ')+str(d.day))
        body+='<h2>%s</h2>'%lab+(''.join(sched_card(r) for r in js) or '<div class="muted" style="margin:0 0 6px">Nothing scheduled</div>')
    slotted=set().union(*by_day.values()) if by_day else set()
    nd=[r for r in rows if not r['scheduled_date'] and r['id'] not in slotted]
    if nd: body+='<h2>No date yet</h2>'+''.join(sched_card(r) for r in nd)
    return ops_page('My Schedule', body, back=False)
def sched_card(r):
    return '<a class="card" href="/?job=%s"><b>%s</b> <span class="muted">%s</span><div>%s</div><div class="muted">%s</div></a>'%(urllib.parse.quote(r['id']),h(r['customer'] or r['title']),h(r['job_type'] or ''),h(r['title']),h(r['address'] or ''))
# ---------- end shell ----------

def utc_day(iso):
    if not iso: return ''
    try:
        d=datetime.fromisoformat(str(iso).replace('Z','')[:26])
        return d.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo('America/Chicago')).date().isoformat()
    except Exception: return str(iso)[:10]

# ---------- Today dashboard ----------
def week_start(d): return d-timedelta(days=d.weekday())
@app.route('/api/today')
def api_today():
    if not is_office(): return jsonify({'error':'Office only'}),403
    conn=get_db(); t=today_local(); ti=t.isoformat(); mo=ti[:7]
    try: make_reminders(conn)
    except Exception: pass
    open_rows=conn.execute("SELECT * FROM jobs WHERE status!='completed'").fetchall()
    done_rows=conn.execute("SELECT id,job_type,sale_amount,completed_at,scheduled_date FROM jobs WHERE status='completed'").fetchall()
    today=[job_dict(r) for r in open_rows if r['scheduled_date']==ti]
    today.sort(key=lambda j:(j['status']!='in_progress', (j.get('tech') or '').lower()))
    done_today=sum(1 for r in done_rows if r['scheduled_date']==ti or utc_day(r['completed_at'])==ti)
    prog={'togo':sum(1 for j in today if j['status']!='in_progress'),'active':sum(1 for j in today if j['status']=='in_progress'),'done':done_today}
    in_prog=[r for r in open_rows if r['status']=='in_progress']
    review=[r for r in open_rows if r['status']=='review']
    help_=[r for r in open_rows if r['help_flag']=='open']
    sign=pending_countersign(conn)
    queued={r['job_id']:r['status'] for r in conn.execute('SELECT job_id,status FROM qbo_queue')}
    ready=[r for r in conn.execute("SELECT d.job_id,j.customer FROM invoice_drafts d JOIN jobs j ON j.id=d.job_id WHERE d.status='ready_for_review'") if r['job_id'] not in queued]
    follow=conn.execute("SELECT COUNT(*) FROM followups WHERE done=0 AND due<=?",(ti,)).fetchone()[0]
    texts=conn.execute("SELECT COUNT(*) FROM msg_drafts WHERE status='pending'").fetchone()[0]
    alerts_fn=globals().get('compliance_alerts')
    docs=alerts_fn(conn) if callable(alerts_fn) else doc_alerts(conn); low=low_stock(conn)
    qq=sum(1 for v in queued.values() if v=='queued'); qerr=sum(1 for v in queued.values() if v=='error')
    def one(rows, label):
        return {'href':'/?job='+urllib.parse.quote(rows[0]['id']),'btn':'Open'} if len(rows)==1 else {'href':'/jobs?f='+label,'btn':'Show'}
    acts=[]
    def add(n,title,detail,icon,href,btn,tone=''):
        if n: acts.append({'n':n,'title':title,'detail':detail,'icon':ICONS[icon],'href':href,'btn':btn,'tone':tone})
    if help_: add(len(help_),'Sub needs help',', '.join((r['customer'] or r['id']) for r in help_[:3]),'alert',one(help_,'help')['href'],'Help','red')
    add(len(sign),'Needs your signature',', '.join(nice_name(r['sub']) for r in sign[:3])+' signed the agreement','pen','/agreement#pending','Sign','red')
    if review: o=one(review,'review'); add(len(review),'Needs review',', '.join((r['customer'] or r['id']) for r in review[:3]),'check',o['href'],'Review')
    add(len(ready),'Ready to invoice',', '.join((r['customer'] or r['job_id']) for r in ready[:3]),'money','/qbo','Export')
    add(follow,'Follow-ups due','Calls to make today','bell','/jobs?f=follow','Call list')
    add(texts,'Texts to review','Drafts waiting for your OK. Nothing sends on its own.','note','/jobs?f=texts','Review')
    add(len(docs),'Sub compliance',', '.join('%s %s %s'%(d['sub'],d.get('kind') or 'doc','missing' if d.get('missing') else ('expired' if d.get('expired') else ('expires '+(d.get('expires') or '')))) for d in docs[:3]),'doc','/compliance' if callable(alerts_fn) else '/techs','Check','red' if any(d.get('expired') or d.get('missing') for d in docs) else '')
    mrn={r['status']:r['n'] for r in conn.execute("SELECT status,COUNT(*) n FROM material_requests WHERE status IN ('submitted','waiting_roland') GROUP BY status")}
    add(sum(mrn.values()),'Materials to order',', '.join(x for x in ('%d new'%mrn['submitted'] if mrn.get('submitted') else '','%d waiting on you'%mrn['waiting_roland'] if mrn.get('waiting_roland') else '') if x),'cart','/orders','Order')
    add(len(low),'Low stock',', '.join(r['name'] for r in low[:3]),'box','/stock','Restock')
    add(qq+qerr,'QuickBooks queue','%d waiting%s'%(qq,', %d failed'%qerr if qerr else ''),'qbo','/qbo','Open','red' if qerr else '')
    # sparklines: last 8 weeks, completed jobs and sales
    ws=week_start(t); weeks=[(ws-timedelta(weeks=7-i)).isoformat() for i in range(8)]
    sj=[0]*8; sm=[0.0]*8; month=0.0
    for r in done_rows:
        d=utc_day(r['completed_at'])
        if not d: continue
        if d[:7]==mo: month+=fnum(r['sale_amount'])
        try: w=week_start(datetime.strptime(d,'%Y-%m-%d').date()).isoformat()
        except ValueError: continue
        if w in weeks: i=weeks.index(w); sj[i]+=1; sm[i]+=fnum(r['sale_amount'])
    live=[]
    for p in conn.execute("SELECT p.*,j.customer,j.title,j.address FROM time_punches p LEFT JOIN jobs j ON j.id=p.job_id WHERE p.out_at IS NULL ORDER BY p.in_at DESC").fetchall():
        live.append({'sub':nice_name(p['sub']),'job':p['job_id'],'customer':p['customer'] or '','title':p['title'] or '','address':p['address'] or '','lat':p['in_lat'],'lng':p['in_lng'],'since':p['in_at'],'flag':p['in_flag'] or ''})
    feed=[{'job':r['job_id'],'author':r['author'] or '','note':(r['note'] or '')[:140],'at':r['created_at'] or '','customer':r['customer'] or ''} for r in
          conn.execute('SELECT u.*,j.customer FROM job_updates u LEFT JOIN jobs j ON j.id=u.job_id ORDER BY u.id DESC LIMIT 8')]
    qm=conn.execute('SELECT invoiced FROM qbo_monthly WHERE month=?',(mo,)).fetchone()
    msrc='Field Ops sales'
    if qm and qm['invoiced'] is not None and fnum(qm['invoiced'])>=month: month=fnum(qm['invoiced']); msrc='QuickBooks invoiced'
    extra_fn=globals().get('crm_today_actions')
    if callable(extra_fn):
        try:
            for a in (extra_fn(conn, ti) or []):
                acts.append(a)
        except Exception:
            app.logger.exception('crm today actions failed')
    tix_fn=globals().get('tickets_today_actions')
    if callable(tix_fn):
        try:
            for a in (tix_fn(conn, ti) or []):
                acts.append(a)
        except Exception:
            app.logger.exception('ticket today actions failed')
    rmr_fn=globals().get('plans_rmr')
    rmr_val=rmr_fn(conn) if callable(rmr_fn) else 0
    conn.close()
    hr=datetime.now(ZoneInfo('America/Chicago')).hour if ZoneInfo else 9
    return jsonify({'name':nice_name(me()),'hello':'Good morning' if hr<12 else ('Good afternoon' if hr<17 else 'Good evening'),
        'today':today,'prog':prog,'kpi':{'today':len(today),'progress':len(in_prog),'waiting':sum(a['n'] for a in acts),'money':money(month),'month':t.strftime('%B'),'spark_jobs':sj,'spark_money':sm,'money_src':msrc,'rmr':money(rmr_val)},
        'actions':acts,'live':live,'feed':feed})
# ---------- end Today ----------

@app.route('/healthz')
def healthz(): return 'ok'

@app.route('/login', methods=['GET','POST'])
def login():
    if not has_any_login(): return render_template_string(LOGIN_HTML, setup=True), 503
    error = None
    if request.method == 'POST':
        name = nice_name(request.form.get('name'))
        pw = request.form.get('password') or ''
        expected = USERS.get(name.lower())
        db_ok = False
        canon = name
        if not expected:
            try:
                import fieldops_users as _users
                u, db_ok = _users.check_login(name, pw)
                if db_ok and u:
                    canon = u['name']
            except Exception:
                db_ok = False
        if login_locked(name): error = 'Too many wrong tries. Wait 15 minutes and try again.'
        elif expected and hmac.compare_digest(pw.encode(), expected.encode()):
            login_ok(name); session.clear(); session.permanent = True; session['name'] = name
            return redirect('/')
        elif db_ok:
            login_ok(canon); session.clear(); session.permanent = True; session['name'] = canon
            return redirect('/')
        else:
            login_fail(name); error = 'Wrong name or password'
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
[OFFICE: legal entity name], Texas DPS Private Security
license B31163401, Houston, Texas, 713-364-8409,
info@truesensesecurity.com ("Company"),

and the independent contractor below ("Subcontractor"):

Legal name: [SUB: Legal name]    DBA: [SUB?: DBA (if any)]
Entity type: [SUB-BOX: LLC] LLC  [SUB-BOX: Corporation] Corporation  [SUB-BOX: Sole proprietor] Sole proprietor
             [SUB-BOX: Other] Other: [SUB?: Other entity type]
Address: [SUB: Address]
Phone: [SUB: Phone]    Email: [SUB: Email]
Tax ID (as on Form W-9): [SUB: Tax ID]

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

Payment is due net [OFFICE: payment days = 15] days after Company approves a completed
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
of at least $[OFFICE: auto liability limit = 1,000,000] per accident; and

(c) Workers' compensation at statutory limits if required by law
or if Subcontractor has employees; or, if not required, an
occupational accident policy with limits of at least
$[OFFICE: occupational accident limit], or a signed workers' compensation waiver.

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
and warrants its workmanship for [OFFICE: warranty period = 1 year] after approval. During
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

Either party may end this Agreement for convenience with [OFFICE: notice days = 14]
days' written notice. Company may also end a work order for
convenience by notice in the app. Subcontractor will then be paid
for work properly completed through the end date, less amounts
withheld as allowed above. Either party may end this Agreement or
a work order if the other materially breaches and does not cure
within [OFFICE: cure days = 7] days, or immediately for a safety, security, or
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

    Typed legal name: [AUTO: name]

    [AUTO: agree] I AGREE

    Finger or stylus signature: [AUTO: signature]

    Date and time (Field Ops app): [AUTO: date]

    Version accepted: 1.2

    Title (if signing for an entity): [SUB?: Title]


COMPANY SIGNER

    Company: True Sense Security

    Typed name: [OFFICE: company signer name]

    Title: [OFFICE: company signer title]

    [ ] I AGREE

    Signature (finger, stylus, or other electronic signature):
    [sign on device]

    Date and time (Field Ops app): [__________]

    Version accepted: 1.2
'''

DB_PATH = os.environ.get('DB_PATH') or os.path.join(DATA_DIR, 'fieldops.db')
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

BUILTIN_SAFETY_TOPICS = [
    ('Ladder Safety', 'Check rungs, feet and locks before you climb.\nSet it on flat ground, 1 foot out for every 4 feet up.\nKeep 3 points of contact. Do not stand on the top 2 steps.\nMove the ladder. Do not lean out past the rails.'),
    ('Lift Use', 'Only trained people run the lift.\nDo the walk around check: tires, controls, guard rails, alarms.\nWear the harness and clip in on boom lifts.\nWatch for overhead lines, ceilings and people below.'),
    ('Working Near Live Electrical', 'Treat every wire as live until tested.\nFind the panel and breaker before you start.\nKeep low voltage cable away from high voltage. Use separate boxes and paths.\nIf in doubt, stop and call the office. Never open a panel you are not trained for.'),
    ('Fall Protection', 'Over 6 feet with no rail means fall protection.\nCheck harness and lanyard before use.\nTie off to a rated anchor, not pipe or conduit.\nCover or mark floor holes and open edges.'),
    ('Ceiling Work and Asbestos Awareness', 'Older buildings may have asbestos in ceiling tiles, pipe wrap and flooring.\nIf it looks old, crumbly or the customer is not sure, do not cut, drill or break it. Stop and call the office.\nMove tiles gently. Wear a dust mask and glasses.\nWatch for wires, sprinklers and sharp edges above the ceiling.'),
    ('Eye Protection', 'Wear safety glasses when drilling, cutting, or working overhead.\nDust and debris fall when you open ceilings.\nUse a face shield for grinding.\nClean glasses so you can see. Replace scratched ones.'),
]
SAFETY_ITEMS = [('ppe','PPE on (hard hat, glasses, gloves, boots)'),('ladder','Ladders checked and set right'),('lift','Lift checked, trained operator'),
    ('electrical','Live power found and kept clear'),('housekeeping','Work area clean, no trip hazards'),('crewtalk','Site hazards talked over with crew')]

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, title TEXT, customer TEXT, address TEXT, scope TEXT,
        status TEXT DEFAULT 'assigned', tech TEXT, notes TEXT, completed_at TEXT,
        created_at TEXT, total_minutes INTEGER DEFAULT 0, total_miles REAL DEFAULT 0,
        signature TEXT, materials TEXT, phone TEXT)''')
    for col in ['signature TEXT', 'materials TEXT', 'phone TEXT', 'checklist TEXT', 'scheduled_date TEXT', 'created_by TEXT', 'materials_list TEXT', 'private_notes TEXT', 'job_type TEXT', 'survey_list TEXT', 'quick_notes TEXT', 'punch_list TEXT', 'survey_form TEXT', 'survey_office TEXT', 'help_flag TEXT', 'help_note TEXT', 'help_at TEXT', 'help_by TEXT', 'help_photo TEXT', 'kelly_at TEXT', 'kelly_by TEXT', 'survey_devices TEXT', 'map_data TEXT', 'hub_approved_at TEXT', 'hub_approved_name TEXT', 'hub_created_at TEXT', 'hub_nonce TEXT', 'review_by TEXT', 'review_at TEXT', 'labor_rates TEXT', 'sale_amount REAL', 'plan_id INTEGER', 'estimate_json TEXT', 'pay_link TEXT', 'paid_at TEXT', 'bom_json TEXT', 'asbuilt_json TEXT']:
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
    for col in ['fields TEXT', 'co_name TEXT', 'co_title TEXT', 'co_signature TEXT', 'co_signed_at TEXT', 'co_by TEXT', 'co_ip TEXT', 'co_user_agent TEXT', 'email_status TEXT', 'email_at TEXT']:
        try: conn.execute(f'ALTER TABLE agreement_signs ADD COLUMN {col}')
        except: pass
    conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS qbo_monthly (month TEXT PRIMARY KEY, invoiced REAL, paid REAL, synced_at TEXT, source TEXT)''')
    if not conn.execute('SELECT 1 FROM agreements').fetchone():
        conn.execute('INSERT INTO agreements (version,body,created_at,created_by) VALUES (?,?,?,?)',('1.2',AGREEMENT_V12,now_local_str(),'Seed'))
    conn.execute("UPDATE agreements SET body=? WHERE created_by='Seed' AND version='1.2' AND body NOT LIKE '%[SUB:%'",(AGREEMENT_V12,))
    conn.execute('''CREATE TABLE IF NOT EXISTS equipment (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT, site TEXT, job_id TEXT, device TEXT, model TEXT, serial TEXT, mac TEXT, ip TEXT,
        install_date TEXT, warranty_end TEXT, asbuilt_label TEXT, password TEXT, notes TEXT, created_at TEXT, updated_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS stock_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, sku TEXT, unit TEXT, cost REAL DEFAULT 0, low_at REAL DEFAULT 0, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS stock_levels (item_id INTEGER, location TEXT, qty REAL DEFAULT 0, PRIMARY KEY (item_id, location))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS stock_moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, location TEXT, qty REAL, job_id TEXT, who TEXT, note TEXT, unit_cost REAL, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS service_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT, site TEXT, phone TEXT, title TEXT, every_months INTEGER DEFAULT 12, next_date TEXT, tech TEXT,
        price REAL DEFAULT 0, checklist TEXT, active INTEGER DEFAULT 1, last_job_id TEXT, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS qbo_queue (
        job_id TEXT PRIMARY KEY, status TEXT, payload TEXT, queued_at TEXT, queued_by TEXT, qbo_invoice_id TEXT, doc_number TEXT, total REAL, balance REAL, note TEXT, updated_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS push_subs (
        endpoint TEXT PRIMARY KEY, sub TEXT, p256dh TEXT, auth TEXT, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS sub_docs (
        sub TEXT, kind TEXT, filename TEXT, orig_name TEXT, expires TEXT, uploaded_at TEXT, PRIMARY KEY (sub, kind))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS job_hours (
        job_id TEXT, sub TEXT, minutes INTEGER DEFAULT 0, PRIMARY KEY (job_id, sub))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS time_punches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, sub TEXT, in_at TEXT, in_lat REAL, in_lng REAL, in_acc REAL, in_ft REAL, in_flag TEXT,
        out_at TEXT, out_lat REAL, out_lng REAL, out_acc REAL, out_ft REAL, out_flag TEXT, minutes INTEGER DEFAULT 0, edit_note TEXT, edited_by TEXT, edited_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS daily_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, log_date TEXT, author TEXT, work_done TEXT, crew TEXT, hours REAL, materials TEXT, issues TEXT, weather TEXT,
        photos TEXT, created_at TEXT, updated_at TEXT, UNIQUE(job_id, log_date))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS safety_topics (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, points TEXT, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS safety_forms (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, kind TEXT, form_date TEXT, topic_name TEXT, topic_points TEXT, answers TEXT, hazards TEXT, crew TEXT, signer TEXT, signature TEXT, created_at TEXT)''')
    r=conn.execute("SELECT value FROM settings WHERE key='safety_seeded'").fetchone(); sseeded=set(json.loads(r['value'])) if r else set()
    have={(x['name'] or '').strip().lower() for x in conn.execute('SELECT name FROM safety_topics').fetchall()}
    for name, pts in BUILTIN_SAFETY_TOPICS:
        if name not in sseeded and name.lower() not in have:
            conn.execute('INSERT INTO safety_topics (name,points,created_at) VALUES (?,?,?)',(name,pts,datetime.utcnow().isoformat()))
        sseeded.add(name)
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('safety_seeded',?)",(json.dumps(sorted(sseeded)),))
    conn.execute('''CREATE TABLE IF NOT EXISTS checklist_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, job_type TEXT, items TEXT, created_at TEXT)''')
    r=conn.execute("SELECT value FROM settings WHERE key='tpl_seeded'").fetchone(); seeded=set(json.loads(r['value'])) if r else set()
    have={(x['name'] or '').strip().lower() for x in conn.execute('SELECT name FROM checklist_templates').fetchall()}
    for name, jt, items in BUILTIN_TEMPLATES:
        if name not in seeded and name.lower() not in have:
            conn.execute('INSERT INTO checklist_templates (name,job_type,items,created_at) VALUES (?,?,?,?)',(name,jt,json.dumps(items),datetime.utcnow().isoformat()))
        seeded.add(name)
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('tpl_seeded',?)",(json.dumps(sorted(seeded)),))
    conn.execute('''CREATE TABLE IF NOT EXISTS invoice_drafts (
        job_id TEXT PRIMARY KEY, package TEXT, status TEXT DEFAULT 'ready_for_review', created_at TEXT)''')
    for ix in ('photos(job_id)', 'photos(filename)', 'job_updates(job_id)', 'support_replies(job_id)', 'followups(job_id)', 'msg_drafts(job_id, kind, ref_date)',
               'msg_drafts(status)', 'jobs(status)', 'jobs(scheduled_date)', 'time_punches(job_id)', 'time_punches(sub, out_at)', 'safety_forms(job_id, form_date)', 'safety_forms(form_date)'):
        conn.execute('CREATE INDEX IF NOT EXISTS ix_%s ON %s' % (re.sub(r'\W+', '_', ix).strip('_'), ix))
    count = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    if count == 0:
        now = datetime.utcnow().isoformat()
        conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at) VALUES (?,?,?,?,?,?,?,?)',
                     ('TS-2026-0142','Access Control - Main Entrance','ABC Warehouse','1234 Industrial Blvd, Houston, TX','Install 2 Maglocks, 1 Aiphone, 1 Card Reader.','assigned',default_sub(),now))
        conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at) VALUES (?,?,?,?,?,?,?,?)',
                     ('TS-2026-0143','Camera Service Call','XYZ Retail','567 Commerce St, Houston, TX','Troubleshoot offline NVR.','assigned',default_sub(),now))
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
    try:
        import fieldops_users as _users
        _users.init_schema(conn)
        import fieldops_quote as _quote
        _quote.init_schema(conn)
        import fieldops_crm as _crm
        _crm.init_schema(conn)
        import fieldops_ops as _ops
        _ops.init_schema(conn)
        import fieldops_tickets as _tix
        _tix.init_schema(conn, backup_dir=BACKUP_DIR, db_path=DB_PATH)
        import fieldops_plans as _plans
        _plans.init_schema(conn)
        import fieldops_portal as _portal
        _portal.init_schema(conn)
        import fieldops_qbo_import as _qboimp
        _qboimp.init_schema(conn)
        import fieldops_security as _sec
        _sec.init_schema(conn)
        import fieldops_areas as _areas
        _areas.init_schema(conn)
    except Exception:
        app.logger.exception('users schema failed')
        raise
    try:
        import fieldops_platform as _plat
        _plat.init_and_migrate(conn, backup_dir=BACKUP_DIR, db_path=DB_PATH)
        snap = os.path.join(BACKUP_DIR, 'fieldops-pre-platform-v2.db')
        print('PLATFORM_MIGRATE backup=%s' % ('yes' if os.path.exists(snap) else 'no'), flush=True)
    except Exception:
        app.logger.exception('platform migrate failed')
        raise
    conn.commit(); conn.close()

init_db()
def data_report():
    try:
        c=get_db(); n={t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('jobs','qbo_monthly','stock_items','equipment','push_subs')}
        oldest=c.execute('SELECT MIN(month) FROM qbo_monthly').fetchone()[0]; c.close()
        weak=[k for k,v in USERS.items() if len(v)<10 or v.isdigit() or v.isalpha() or v.lower() in ('password','password1','changeme','truesense','fieldops')]
        if weak: print('SECURITY_WARNING weak password for: %s (use 10+ characters with letters and numbers)'%', '.join(sorted(weak)), flush=True)
        if not os.environ.get('SECRET_KEY'): print('SECURITY_WARNING SECRET_KEY is not set; sessions use a derived key', flush=True)
        print('DATA_CHECK db=%s uploads=%s counts=%s oldest_month=%s'%(DB_PATH,app.config['UPLOAD_FOLDER'],json.dumps(n),oldest), flush=True)
    except Exception as e: print('DATA_CHECK failed %s'%e, flush=True)
data_report()

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
.big-btn.green{background:#2d6a4f}.big-btn.stop{background:#b00020}
.hidden{display:none}#detail.guided>[id^="sec-"]:not(.focus){display:none}
#steps-box{margin:8px 0}.stp-bar{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}.stp{flex:1 1 30%;min-width:96px;padding:8px 6px;border-radius:8px;background:#eee;color:#333;font-size:.85rem;font-weight:600;text-align:center}
.stp.done{background:#2e7d32;color:#fff}.stp.cur{outline:3px solid #a49f2a;background:#fff}#next-btn{font-size:1.4rem;padding:22px}#toast{position:fixed;left:10px;right:10px;top:10px;z-index:60;pointer-events:none;background:#111;color:#fff;padding:16px;border-radius:12px;font-size:1.15rem;font-weight:700;text-align:center}.fs-row{display:flex;align-items:center;gap:8px;margin:6px 0}.fs-row span{flex:1;font-weight:600}.fs-row select{flex:0 0 120px;padding:10px;font-size:1rem}.fs-sig{width:100%;height:140px;border:2px solid #ddd;border-radius:10px;touch-action:none;background:#fff}#next-btn:disabled{background:#999}.stp-why{color:#b00020;font-weight:700;text-align:center;margin:4px 0;font-size:1.05rem}
.chk{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border);font-size:1rem}
.chk input{width:24px;height:24px;margin:0;accent-color:var(--accent)}
.day{font-weight:700;color:var(--accent);margin:16px 0 6px}
.badge{display:inline-block;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;background:#f0f0f0;color:#333;margin-left:6px}
.row{display:flex;gap:8px}.row>*{flex:1}.muted{color:#666}.ptitle{font-weight:600;font-size:.9rem;margin-top:8px}
.thumbs{display:flex;gap:8px;flex-wrap:wrap}.thumbs img{width:70px;height:70px;object-fit:cover;border-radius:8px}
.mat{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)}.mat input{width:24px;height:24px;margin:0;accent-color:var(--accent)}.mat span{flex:1}.mat button{background:none;border:none;color:#b00020;font-size:1.2rem}
textarea,input{width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem}
body.office{max-width:1100px;background:#fff}body.office #detail{max-width:480px;margin:auto}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
.tile{background:#fff;border:2px solid var(--border);border-radius:12px;padding:12px 8px;text-align:center;font-size:.95rem;font-weight:600;color:var(--text)}
.tile b{display:block;font-size:2rem;color:var(--accent)}.tile.on{border-color:var(--accent);background:#fbfaf0}.tile.red{border-color:#b00020}.tile.red b{color:#b00020}
.menu{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}.menu .big-btn{margin:0;padding:18px 8px;box-sizing:border-box;font-family:inherit}
.tech-h{font-weight:700;font-size:1.1rem;margin:18px 0 6px;padding-bottom:4px;border-bottom:2px solid var(--accent)}
.mgrid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}.mgrid .big-btn{margin:0;display:flex;align-items:center;justify-content:center;text-align:center;text-decoration:none;min-height:64px;box-sizing:border-box;cursor:pointer}.mgrid.small .big-btn{min-height:52px;font-size:1rem}
.tiles4{grid-template-columns:1fr 1fr!important}.tile{cursor:pointer}.more-box{margin:8px 0;border:1px solid var(--border);border-radius:10px;padding:0 10px;background:#fff}.more-box summary{cursor:pointer;padding:14px 4px;font-weight:600;min-height:24px}
body.office #home .tiles4{gap:10px;margin:4px 0 12px}
body.office #home .tiles4 .tile{position:relative;display:grid;grid-template-columns:auto 1fr;grid-template-rows:auto auto;column-gap:10px;align-items:center;text-align:left;padding:14px;border:1px solid var(--border);border-radius:16px;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.04),0 4px 14px rgba(0,0,0,.05);min-height:84px;overflow:hidden}
body.office #home .tiles4 .tile:before{content:'';position:absolute;left:0;top:0;bottom:0;width:5px;background:var(--accent)}
body.office #home .tiles4 .ti{grid-row:1/3;width:44px;height:44px;border-radius:12px;background:#f7f6ea;color:var(--accent);display:flex;align-items:center;justify-content:center}
body.office #home .tiles4 .tile b{font-size:1.9rem;line-height:1.1;color:var(--text)}
body.office #home .tiles4 .tl{font-size:.92rem;color:var(--muted);font-weight:600}
body.office #home .tiles4 .tile.red:before{background:#b00020}body.office #home .tiles4 .tile.red .ti{background:#fdecea;color:#b00020}body.office #home .tiles4 .tile.red b{color:#b00020}
body.office #home .tiles4 .tile.on{background:var(--accent);border-color:var(--accent)}body.office #home .tiles4 .tile.on b,body.office #home .tiles4 .tile.on .tl{color:#fff}body.office #home .tiles4 .tile.on .ti{background:rgba(255,255,255,.22);color:#fff}
.mgrid.main{gap:10px;margin:0 0 10px}
.mbtn{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;min-height:84px;padding:12px 6px;border-radius:16px;background:#fff;border:1px solid var(--border);box-shadow:0 1px 2px rgba(0,0,0,.04),0 4px 14px rgba(0,0,0,.05);color:var(--text);text-decoration:none;font-weight:700;font-size:1rem;text-align:center;cursor:pointer;box-sizing:border-box}
.mbtn svg{color:var(--accent)}.mbtn.prime{background:var(--accent);border-color:var(--accent);color:#fff}.mbtn.prime svg{color:#fff}
.list-h{font-weight:800;font-size:1.15rem;margin:16px 0 4px}.list-h span{background:#111;color:#fff;border-radius:10px;padding:1px 9px;font-size:.85rem;vertical-align:2px;margin-left:4px}
body.office #home .job{border-radius:14px;box-shadow:0 1px 2px rgba(0,0,0,.04),0 3px 10px rgba(0,0,0,.04)}
@media(min-width:900px){.mgrid.main{grid-template-columns:repeat(6,1fr)}.tgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 20px;align-items:start}}@media(min-width:600px){.mgrid{grid-template-columns:repeat(3,1fr)}
.tiles4{grid-template-columns:repeat(4,1fr)!important}}
@media(min-width:900px){.dash{display:flex;gap:20px;align-items:flex-start}.menu{display:flex;flex-direction:column;flex:0 0 210px;position:sticky;top:12px}.dash main{flex:1;min-width:0}.tiles{grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}}
.pal{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin:8px 0}.pal button{padding:12px 4px;border:2px solid var(--border);border-radius:10px;background:#fff;font-size:.95rem;font-weight:600;color:var(--text)}.pal button.on{border-color:var(--text);background:#fbfaf0}
.pal i,.pin{display:inline-block;color:#fff;font-style:normal;font-weight:700;border-radius:16px;text-align:center}.pal i{min-width:22px;padding:0 3px;line-height:22px;font-size:.75rem;margin-right:4px}
.plan{position:relative;margin:8px 0;border:1px solid var(--border);border-radius:8px;overflow:hidden}.plan img{width:100%;height:auto;display:block}
.dpin{position:absolute;width:34px;height:34px;margin:-17px 0 0 -17px;cursor:grab;z-index:2;touch-action:none;-webkit-user-select:none;user-select:none}.dpin::before{content:'';position:absolute;inset:-8px;border-radius:50%}.dpin.drag{z-index:5;cursor:grabbing;transform:scale(1.25)}.chd{position:absolute;width:44px;height:44px;margin:-22px 0 0 -22px;border-radius:50%;z-index:4;touch-action:none;cursor:grab;display:flex;align-items:center;justify-content:center}.chd i{display:block;width:20px;height:20px;border-radius:50%;border:3px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.45)}.chd.edge i{background:#fff;border-color:currentColor}.dpin .dsym{display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))}.dpin b{position:absolute;left:36px;top:50%;transform:translateY(-50%);font-size:.8rem;line-height:1;background:#fff;color:#111;border:1.5px solid currentColor;border-radius:6px;padding:2px 4px;white-space:nowrap}.dpin.sel .dsym{outline:3px solid #111;border-radius:50%}.dpin.lf b{left:auto;right:36px}
.plan .cov{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:1}.pal button svg{vertical-align:middle;margin-right:4px}.cam-ctl{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin:6px 0}.cam-ctl button{padding:12px 4px;border:2px solid var(--border);border-radius:10px;background:#fff;font-weight:700;font-size:.95rem;color:var(--text)}
.pin{position:absolute;transform:translate(-50%,-50%);min-width:32px;padding:0 4px;height:32px;line-height:28px;font-size:.85rem;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.5);box-sizing:border-box}.pin.sel{outline:3px solid #111}
#mp{position:fixed;inset:0;background:#fff;z-index:40;overflow:auto;padding:10px}#mp.hidden{display:none}#mp-map{height:55vh;border-radius:10px}
.mp-tools{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}.mp-tools button,.mp-b button{padding:14px 4px;border:2px solid var(--border);border-radius:10px;background:#fff;font-weight:700;font-size:1rem;color:var(--text)}.mp-tools button.on{border-color:var(--text);background:#fbfaf0}
.mp-b{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:6px 0}.mp-addr{display:inline-block;background:#111;color:#fff;font-weight:700;font-size:12px;padding:3px 6px;border-radius:6px;white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis;transform:translate(-50%,-100%);position:relative;top:-6px}.mp-addr:after{content:'';position:absolute;left:50%;bottom:-6px;margin-left:-6px;border:6px solid transparent;border-bottom:0;border-top-color:#111}
.mp-seg{background:rgba(0,0,0,.7);color:#fff;font-weight:700;font-size:12px;padding:2px 5px;border-radius:6px;white-space:nowrap}
#sk{position:fixed;inset:0;background:#fff;z-index:50;overflow:auto;padding:10px}#sk.hidden{display:none}
.sk-tools{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-bottom:6px}.sk-tools button{padding:14px 2px;border:2px solid var(--border);border-radius:10px;background:#fff;font-weight:700;font-size:.95rem;color:var(--text)}.sk-tools button.on{border-color:var(--text);background:#fbfaf0}

canvas{touch-action:none;-webkit-user-select:none;user-select:none;border:2px dashed var(--border);border-radius:10px;background:#fafafa;width:100%;height:160px}
</style></head><body class="{% if office %}office {% endif %}view-{{ view }}">
<div class="header">
  <img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px; margin:0 auto 4px; display:block;">
  <div class="sub">Field Ops</div>{% if office %}<div id="today-date" class="muted" style="font-size:.9rem;margin-top:2px"></div>{% endif %}
  <div style="font-size:.85rem;color:#666;margin-top:4px">{{ name }} &middot; <a href="/logout" style="color:#666" onclick="if(!confirm('Log out?'))return false;try{navigator.serviceWorker.controller&&navigator.serviceWorker.controller.postMessage('logout');localStorage.removeItem('fo-q')}catch(e){}">Log out</a></div>
</div>
<div id="home"{% if office %} class="dash"{% endif %}>
{% if office %}<main>
{% if office and view=='today' %}<div id="today">
<div class="fo-hello"><h1 id="fo-hello">Hello, {{ name }}</h1><div class="muted" id="fo-hello2">Loading your day...</div></div>
<div class="fo-prog" id="fo-prog"><div class="fo-skel" style="height:14px"></div></div>
<div class="fo-kpis" id="fo-kpis">{% for i in range(4) %}<div class="fo-kpi fo-skel" style="height:108px;border:0"></div>{% endfor %}</div>
<section><h2 class="fo-h">Needs you</h2><div id="fo-acts">{% for i in range(3) %}<div class="fo-skel" style="height:68px;margin-bottom:10px"></div>{% endfor %}</div>
<h2 class="fo-h">Today's jobs</h2><div id="fo-tjobs"><div class="fo-skel" style="height:96px"></div></div></section>
<section><h2 class="fo-h">Subs on the clock</h2><div id="fo-live"></div>
<h2 class="fo-h">Recent activity</h2><div id="fo-feed"></div></section>
</div>{% endif %}
<div{% if view in ('new','today') %} style="display:none"{% endif %}>
<div class="tiles tiles4"><button class="tile t-active" data-f="active" onclick="setF(this.dataset.f)" aria-pressed="false"><span class="ti"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1M9 10h6M9 14h6M9 18h3"/></svg></span><b id="t-active">0</b><span class="tl">Active Jobs</span></button><button class="tile t-help" data-f="help" onclick="setF(this.dataset.f)" aria-pressed="false"><span class="ti"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/></svg></span><b id="t-help">0</b><span class="tl">Needs Help</span></button><button class="tile t-surveys" data-f="surveys" onclick="setF(this.dataset.f)" aria-pressed="false"><span class="ti"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></span><b id="t-surveys">0</b><span class="tl">Surveys Pending</span></button><button class="tile t-today" data-f="today" onclick="setF(this.dataset.f)" aria-pressed="false"><span class="ti"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg></span><b id="t-today">0</b><span class="tl">Scheduled Today</span></button></div>
<button class="big-btn gray" id="f-all" style="display:none" onclick="setF('')">All Jobs</button>
<div class="mgrid main"><a href="/new-job" class="mbtn prime"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg><span>New Job</span></a><a href="/clients" class="mbtn"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5"/><circle cx="17" cy="9" r="2.5"/><path d="M17.5 14.5c2 .4 3.4 2 4 4.5"/></svg><span>Clients &amp; Surveys</span></a><a href="/calendar" class="mbtn"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4M8 14h2M14 14h2M8 17h2"/></svg><span>Calendar</span></a><a href="/to-order" class="mbtn"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/><path d="M2 3h3l2.7 12.5h11.6L22 7H6"/></svg><span>To Order</span></a><a href="/techs" class="mbtn"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21c1-4.5 4.2-7 8-7s7 2.5 8 7"/></svg><span>Subs</span></a><a href="/history" class="mbtn"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></svg><span>Job History</span></a></div>
<details class="more-box"><summary>Other lists</summary><div class="tiles"><button class="tile" data-f="progress" onclick="setF(this.dataset.f)"><b id="t-progress">0</b>In Progress</button><button class="tile" data-f="review" onclick="setF(this.dataset.f)"><b id="t-review">0</b>Needs Review</button><button class="tile" data-f="invoice" onclick="setF(this.dataset.f)"><b id="t-invoice">0</b>Ready to Invoice</button><button class="tile" data-f="follow" onclick="setF(this.dataset.f)"><b id="t-follow">0</b>Follow-ups Due</button><button class="tile" data-f="texts" onclick="setF(this.dataset.f)"><b id="t-texts">0</b>Texts to Review</button><button class="tile" id="tile-docs" onclick="location.href='/techs'"><b id="t-docs">0</b>Sub Docs Due</button><button class="tile" id="tile-sign" onclick="location.href='/agreement#pending'"><b id="t-sign">0</b>Needs Your Signature</button></div></details>
<details class="more-box"><summary>More tools</summary><div class="mgrid small"><a href="/schedule" class="big-btn gray">Schedule</a><a href="/checklists" class="big-btn gray">Checklists</a><a href="/agreement" class="big-btn gray">Agreement</a><a href="/revenue" class="big-btn gray">Revenue</a><a href="/timelog" class="big-btn gray">Time Log</a><a href="/safety" class="big-btn gray">Safety</a><a href="/qbo" class="big-btn gray">QuickBooks</a><a href="/reports" class="big-btn gray">Reports</a><a href="/plans" class="big-btn gray">Service Plans</a><a href="/equipment" class="big-btn gray">Equipment</a><a href="/stock" class="big-btn gray">Stock{% if low_n %} <span style="background:#c62828;color:#fff;border-radius:8px;padding:0 6px">{{ low_n }} low</span>{% endif %}</a></div></details>
</div>{% endif %}
{% if not office and agr %}{% if agr.signed %}<a href="/agreement" style="display:block;text-align:center;color:#666;margin:6px 0">My signed Subcontractor Agreement (v{{ agr.version }})</a>{% else %}<a href="/agreement" class="big-btn" style="display:block;text-align:center;text-decoration:none;box-sizing:border-box;margin:8px 0">Please sign the Subcontractor Agreement (v{{ agr.version }})</a>{% endif %}{% endif %}
{% if not office %}<button id="push-btn" class="big-btn hidden" onclick="pushOn()">Turn on job alerts</button><div id="push-hint" class="hidden" style="text-align:center;color:#666;margin:6px 0">For job alerts on iPhone: tap Share, then Add to Home Screen, then open Field Ops from your home screen.</div>{% endif %}
<div id="search-box" class="row" style="align-items:center{% if view=='new' %};display:none{% endif %}"><input type="search" id="q" placeholder="Search client, address, job, note, serial" onkeydown="if(event.key==='Enter')goSearch()"><button class="big-btn gray" style="flex:0 0 90px" onclick="goSearch()">Search</button></div>
{% if office and view=='new' %}<div id="office" class="job"><a href="/jobs" class="big-btn gray" style="display:block;text-align:center;text-decoration:none;box-sizing:border-box;margin:0 0 8px">&larr; Back</a><h2 style="margin:4px 0 8px">New Job</h2>
<div class="row" style="flex-wrap:wrap;gap:6px">{% for t in job_types %}<button class="big-btn gray qa" style="flex:1 1 45%" data-t="{{ t }}" onclick="quickType(this.dataset.t)">+ {{ t }}</button>{% endfor %}</div>
<datalist id="cl-list"></datalist>
<input type="text" id="n-id" placeholder="Job ID (e.g. TS-2026-0144)">
<input type="text" id="n-title" placeholder="Title" oninput="this.dataset.auto=''">
<input type="hidden" id="n-company_id" value="">
<select id="n-customer-pick" style="width:100%;padding:14px;border:1px solid #ccc;border-radius:10px;font-size:1rem;background:#fff;margin:4px 0" onchange="pickCompany(this)"><option value="">Customer: choose or type a new name</option></select>
<input type="text" id="n-customer" placeholder="New customer name" list="cl-list" onchange="pickClient()" oninput="if(this.value)document.getElementById('n-company_id').value=''">
<input type="text" id="n-address" placeholder="Address">
<input type="tel" id="n-phone" placeholder="Customer phone">
<select id="n-tech" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff">{% for v,l in techs %}<option value="{{ v }}">{{ l }}</option>{% else %}<option value="">No subs set in Render</option>{% endfor %}</select>
<select id="n-type" onchange="tplOpts()" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff">{% for t in job_types %}<option value="{{ t }}">{{ t }}</option>{% endfor %}</select>
<select id="n-tpl" onchange="tplPick()" style="width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem;background:#fff"></select>
<textarea id="n-scope" placeholder="Scope" rows="3"></textarea>
<div style="font-size:.85rem;color:#666">Scheduled date</div><input type="date" id="n-date">
<textarea id="n-checklist" placeholder="Checklist, one item per line (optional)" rows="3"></textarea>
<button class="big-btn" onclick="addJob()">Save Job</button><a href="/" class="big-btn gray" style="display:block;text-align:center;text-decoration:none;box-sizing:border-box">Cancel</a></div>{% else %}<div id="office" class="hidden"></div>{% endif %}
<div id="list"{% if view in ('new','today') %} style="display:none"{% endif %}></div>
{% if office %}</main>{% endif %}</div>
<div id="toast" class="hidden"></div>
<div id="detail" class="hidden">
<button class="big-btn gray" style="font-size:1.3rem;margin-bottom:8px" onclick="showList()">&larr; Back</button>
<div id="steps-box"><div class="stp-bar" id="stp-bar"></div><div class="stp-why" id="stp-why"></div><button class="big-btn" id="next-btn" onclick="nextStep()">Next Step</button><button class="big-btn gray" id="more-all" onclick="toggleGuided()">More</button></div>
<div class="job"><div class="job-id" id="d-id"></div><div id="d-title" style="font-weight:600;font-size:1.15rem"></div><div id="d-customer" style="color:#666"></div><div id="d-address" style="color:#666"></div><div id="d-scope" style="white-space:pre-wrap;margin-top:8px"></div><div id="d-created" style="color:#666;font-size:.85rem;margin-top:6px"></div></div>
<div id="mr-box" class="job hidden"></div>
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
<div id="sv-slot"><div id="markup"><div class="ptitle" style="font-size:1.2rem" id="mk-title">5. Floor Plan Devices</div><a class="big-btn" id="pd-link" href="/" onclick="this.href='/design/'+encodeURIComponent(currentJob.id)">Open Plan Designer</a>{% if office %}<a class="big-btn gray" id="bom-link" href="/" onclick="this.href='/job/'+encodeURIComponent(currentJob.id)+'/bom'">Bill of materials</a><a class="big-btn gray" id="share-link2" href="/" onclick="this.href='/job/'+encodeURIComponent(currentJob.id)+'/share'">Share view-only link</a>{% endif %}<a class="big-btn gray" id="pd-full-link" href="/" onclick="this.href='/design/'+encodeURIComponent(currentJob.id)+'?full=1'">&#x26F6; Full screen plan</a><div id="ab-copy"></div><div class="row" id="plan-add"><div class="big-btn" onclick="document.getElementById('plan-file').click()">Add Floor Plan or Photo</div><div class="big-btn gray" onclick="skOpen()">Draw Plan</div></div>
<input type="file" id="plan-file" accept="image/*" multiple style="display:none" onchange="uploadPlans(this)">
<div id="dev-palette" class="pal"></div><div id="dev-hint" style="font-weight:700;font-size:1.1rem;text-align:center;margin:6px 0"></div><button class="big-btn gray hidden" id="dev-undo" onclick="devUndoTap()">&#8630; Undo last change</button>
<div id="plans"></div>
<div id="dev-edit" class="job hidden" style="border:2px solid var(--accent)"><div id="dv-title" style="font-weight:800;font-size:1.3rem"></div><div class="muted" id="dv-movehint">Drag the device on the plan to move it, or tap Move.</div><button class="big-btn" id="dv-move" onclick="devMoveStart()">Move (tap the new spot)</button>
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
<div id="sec-start"><div class="job" id="start-box"></div></div>
<div id="sec-pn"><div class="job"><div style="font-weight:800;font-size:1.3rem">Photos and Notes</div><div class="muted">One save adds today's daily log, a job note and the photos.</div>
<label class="big-btn gray" style="display:block;cursor:pointer;margin-top:8px">&#128247; Add Photos<input type="file" id="pn-photos" accept="image/*" multiple style="display:none" onchange="pnPicked()"></label><div id="pn-picked" class="muted" style="text-align:center"></div>
<div style="font-weight:600;margin-top:8px">What did you do today?</div><textarea id="pn-work" rows="3" style="font-size:1.15rem" autocapitalize="sentences" placeholder="Say or type it"></textarea>
<div class="row" style="flex-wrap:wrap;gap:6px;margin-bottom:8px">{% for c in ['Ran cable','Mounted devices','Programmed','Tested all','Trained client','Cleaned up'] %}<button type="button" class="big-btn gray" style="flex:1 1 30%;padding:12px;font-size:1rem" data-c="{{ c }}" onclick="chip(this.dataset.c)">{{ c }}</button>{% endfor %}</div>
<div style="font-weight:600">Materials used</div><input type="text" id="pn-mat" placeholder="500 ft Cat6, 2 cameras" list="mat-list"><datalist id="mat-list"></datalist>
<div id="parts-box" class="hidden"><div style="font-weight:600">Parts from stock</div><select id="pt-item" style="width:100%;padding:14px;border:1px solid #ccc;border-radius:10px;font-size:1rem;background:#fff;margin:4px 0"></select>
<div class="row"><select id="pt-loc" style="flex:1 1 50%;padding:14px;border:1px solid #ccc;border-radius:10px;font-size:1rem;background:#fff"></select><input type="number" id="pt-qty" value="1" min="0" step="any" inputmode="decimal" style="flex:0 0 80px"><button type="button" class="big-btn" style="flex:0 0 90px" onclick="partUse()">Use</button></div><div id="pt-list"></div></div>
<div style="font-weight:600">Problems or delays</div><input type="text" id="pn-issues" placeholder="None">
<button class="big-btn" id="pn-save" style="font-size:1.25rem" onclick="pnSave()">Save</button><div id="pn-today" class="muted" style="text-align:center"></div></div></div>
<div id="sec-done"><div class="job"><button class="big-btn green" id="done-btn" style="font-size:1.4rem;padding:22px" onclick="finishTap()">Finish</button><div id="done-why" class="stp-why"></div><div class="muted" style="text-align:center" id="done-note">Clocks you out and marks the job done.</div></div></div>
<div id="sec-clock"><div class="job" style="text-align:center"><button id="clk-btn" class="big-btn green" style="font-size:1.4rem;padding:22px;width:100%" onclick="clockTap()">Clock In</button><div id="clk-info" style="font-size:1.05rem;margin-top:6px"></div><a class="big-btn gray" style="display:block;text-decoration:none;margin-top:8px" id="dl-link" href="#">Daily Log</a><a class="big-btn gray" style="display:block;text-decoration:none;margin-top:8px" id="sf-link" href="#">Safety</a>{% if office %}<a class="big-btn gray" style="display:block;text-decoration:none;margin-top:8px" id="clk-log" href="/timelog">Time Log</a>{% endif %}</div></div>
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
<div class="row"><button class="big-btn" onclick="saveTime('add')">Add</button><button class="big-btn gray" onclick="saveTime('set')">Set Totals</button></div>{% if office %}<div style="margin-top:12px;border-top:1px solid var(--border);padding-top:8px"><div style="font-weight:600">Invoice amount (office only)</div><div class="row" style="align-items:center"><input type="number" id="sale-amt" step="0.01" min="0" inputmode="decimal" placeholder="$ billed for this job"><button class="big-btn gray" style="flex:0 0 90px" onclick="saveSale()">Save</button></div>
<div style="font-weight:600;margin-top:8px">Labor cost (office only)</div><div id="labor"></div></div>{% endif %}</div></div>
<div id="sec-check"><div class="job"><div style="font-weight:600;margin-bottom:6px">Checklist</div><div id="checklist"></div>
<div id="punch-wrap"><div style="font-weight:600;margin:12px 0 4px">Punch items</div><div class="muted" style="font-size:.85rem">Office and assigned sub only. Not on client printouts.</div><div id="punch"></div>
<div class="row" style="align-items:center"><input type="text" id="punch-new" placeholder="Add punch item" onkeydown="if(event.key==='Enter')addPunch()"><button class="big-btn" style="flex:0 0 70px" onclick="addPunch()">Add</button></div></div></div></div>
<div id="sec-ab"><div class="job" id="ab-card"><div style="font-weight:800;font-size:1.4rem">As-Built</div><div id="ab-slot"></div><a id="asb-link" class="big-btn" href="/" onclick="this.href='/job/'+encodeURIComponent(currentJob.id)+'/asbuilt'">As-built PDF and client sign-off</a>{% if office %}<a id="sys-job-link" class="big-btn gray" href="/" onclick="this.href='/systems?client='+encodeURIComponent(currentJob.customer||'')+'&site='+encodeURIComponent(currentJob.address||'')">Installed systems at this site</a><a id="share-link" class="big-btn gray" href="/" onclick="this.href='/job/'+encodeURIComponent(currentJob.id)+'/share'">Share view-only link</a>{% endif %}<a id="eq-link" class="big-btn gray" href="/" onclick="this.href='/job/'+encodeURIComponent(currentJob.id)+'/equipment'">Equipment at this site</a>
<div class="ptitle" style="font-size:1.1rem;margin-top:14px">Blueprints</div><div id="bp-list"></div>
<div class="big-btn gray" onclick="document.getElementById('bp-file').click()">Upload Blueprint (PDF or image)</div>
<input type="file" id="bp-file" accept=".pdf,.png,.jpg,.jpeg,.webp,.heic,application/pdf,image/*" multiple style="display:none"></div></div>
<div id="sec-map"><button class="big-btn gray" onclick="mapOpen()">Site Map <span id="map-sum" style="font-weight:400;font-size:.9rem"></span></button></div>
<div id="sec-follow">{% if office %}<div class="job"><div style="font-weight:600">Follow-ups (office only)</div><div id="fu-list"></div>
<div style="font-size:.85rem;color:#666;margin-top:8px">Remind me on</div><input type="date" id="fu-date"><input type="text" id="fu-note" placeholder="What to follow up on (e.g. call about estimate)"><button class="big-btn" onclick="addFollow()">Add Follow-up</button></div>{% endif %}</div>
<div id="sec-hub">{% if office %}<div class="job"><div style="font-weight:600">Client Link (office only)</div><div class="muted" style="font-size:.9rem">Anyone with this link can see this job's summary, plan, photos and signature. Copy it and send it yourself.</div>
<input type="text" id="hub-url" readonly onclick="this.select()"><div id="hub-exp" style="font-weight:600"></div><div class="row"><button class="big-btn gray" onclick="copyHub()">Copy Link</button><button class="big-btn gray" onclick="newHub()">New Link</button></div><a class="big-btn gray" href="/" onclick="this.href='/job/'+encodeURIComponent(currentJob.id)+'/estimate'">Estimate</a><div id="hub-appr" style="font-weight:700;font-size:1.1rem;color:#2e7d32"></div></div>{% endif %}</div>
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
const OFFICE={{ office|tojson }};const VIEW={{ view|tojson }};let F=new URLSearchParams(location.search).get('f')||'';
const card=j=>`<div class="job"><div class="job-id">${esc(j.id)}${j.help_open?'<span class="badge" style="background:#b00020;color:#fff;font-size:.75rem;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px">Needs Help</span>':''}<span class="badge">${esc(j.status_label)}</span>${j.job_type?'<span class="badge">'+esc(j.job_type)+'</span>':''}</div><div style="font-weight:600">${esc(j.title)}</div><div style="color:#666">${esc(j.customer)}</div><div style="color:#666">${esc(j.address)}</div>${j.company?'<div style="color:#666">Sub: '+esc(j.tech)+' ('+esc(j.company)+')</div>':''}<button class="big-btn" data-id="${esc(j.id)}" onclick="openJob(this.dataset.id)">Open Job</button></div>`;
const dcard=j=>`<div class="job" style="cursor:pointer" data-id="${esc(j.id)}" onclick="openJob(this.dataset.id)"><div class="job-id" style="font-size:1rem">${esc(j.id)}${j.help_open?'<span class="badge" style="background:#b00020;color:#fff">Needs Help</span>':''}${j.job_type?'<span class="badge">'+esc(j.job_type)+'</span>':''}<span class="badge">${esc(j.status_label)}</span></div><div style="font-weight:600">${esc(j.customer||j.title)}</div><div class="muted">${esc(j.title)}</div>${j.company?'<div class="muted">Sub: '+esc(j.tech)+' ('+esc(j.company)+')</div>':''}<div class="muted">${j.scheduled_date?esc(dayLabel(j.scheduled_date)):'No date'}</div></div>`;
function setF(f){F=F===f?'':f;load()}
const $id=x=>document.getElementById(x);
function spark(a,money){const w=120,h=34,m=Math.max(...a,1),n=a.length-1;const pts=a.map((v,i)=>[(i*w/n).toFixed(1),(h-3-(v/m)*(h-6)).toFixed(1)]);
return '<svg class="fo-spark" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" aria-hidden="true"><polygon points="0,'+h+' '+pts.map(p=>p.join(',')).join(' ')+' '+w+','+h+'" fill="rgba(186,162,5,.16)"/><polyline points="'+pts.map(p=>p.join(',')).join(' ')+'" fill="none" stroke="#7a7a12" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>'}
function fwhen(s){if(!s)return'';const d=new Date(/Z|[+-]\d\d:?\d\d$/.test(s)||!/T/.test(s)?s:s+'Z');if(isNaN(d))return s;const t=new Date();const same=d.toDateString()===t.toDateString();return (same?'':d.toLocaleDateString([], {month:'short',day:'numeric'})+' ')+d.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})}
let liveMap=null;
async function loadToday(){let d;try{d=await (await fetch('/api/today')).json()}catch(e){return}
$id('fo-hello').textContent=d.hello+', '+d.name;const n=d.kpi.today;$id('fo-hello2').textContent=n?'You have '+n+' job'+(n===1?'':'s')+' today.':'No jobs on the schedule today.';
const p=d.prog,tot=p.togo+p.active+p.done||1;
$id('fo-prog').innerHTML='<div class="fo-bar"><i style="width:'+(p.done/tot*100)+'%;background:#2e7d32"></i><i style="width:'+(p.active/tot*100)+'%;background:var(--gold)"></i></div><div class="fo-legend"><span><b>'+p.togo+'</b> to go</span><span><b>'+p.active+'</b> active</span><span><b>'+p.done+'</b> done</span></div>';
const k=d.kpi;$id('fo-kpis').innerHTML=[['/jobs?f=today','Jobs today',k.today,spark(k.spark_jobs)+'<i class="fo-cap">Jobs done, 8 weeks</i>'],['/jobs?f=progress','In progress',k.progress,''],['#fo-acts','Waiting on you',k.waiting,''],['/money','Money in '+k.month,k.money,spark(k.spark_money)+'<i class="fo-cap">'+(k.rmr?('RMR '+k.rmr+'/mo. '):'')+esc(k.money_src)+'. Sales trend, 8 weeks</i>']]
.map(x=>'<a class="fo-kpi'+(x[1]==='Waiting on you'&&x[2]?' hot':'')+'" href="'+x[0]+'"><small>'+esc(x[1])+'</small><b>'+esc(String(x[2]))+'</b>'+x[3]+'</a>').join('');
$id('fo-acts').innerHTML=d.actions.length?d.actions.map(a=>'<a class="fo-act '+a.tone+'" href="'+esc(a.href)+'"><span class="fo-ai">'+a.icon+'</span><span class="fo-at"><b>'+esc(a.title)+' <em>'+a.n+'</em></b><small>'+esc(a.detail)+'</small></span><span class="fo-ab">'+esc(a.btn)+'</span></a>').join(''):'<div class="fo-act ok"><span class="fo-ai">&#10003;</span><span class="fo-at"><b>All caught up</b><small>Nothing needs you right now.</small></span></div>';
$id('fo-tjobs').innerHTML=d.today.length?d.today.map(dcard).join(''):'<div class="fo-empty">Nothing scheduled today. Tap + to add a job.</div>';
const lv=d.live.filter(x=>x.lat!=null&&x.lng!=null);
$id('fo-live').innerHTML=(d.live.length?(lv.length?'<div id="fo-map"></div>':'')+d.live.map(x=>'<div class="fo-row" data-id="'+esc(x.job)+'" onclick="openJob(this.dataset.id)"><span class="fo-pulse"></span><b>'+esc(x.sub)+'</b> <span class="muted">at '+esc(x.customer||x.title)+' since '+esc(fwhen(x.since))+(x.lat==null?' (no GPS)':'')+'</span></div>').join(''):'<div class="fo-empty">No one is clocked in right now.</div>');
if(lv.length&&await loadLeaflet()){if(liveMap){liveMap.remove();liveMap=null}liveMap=L.map('fo-map',{zoomControl:false,attributionControl:true});L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap'}).addTo(liveMap);
const b=[];lv.forEach(x=>{L.circleMarker([x.lat,x.lng],{radius:9,color:'#fff',weight:3,fillColor:'#7a7a12',fillOpacity:1}).addTo(liveMap).bindTooltip(x.sub,{permanent:true,direction:'top',offset:[0,-8]});b.push([x.lat,x.lng])});
if(b.length===1)liveMap.setView(b[0],14);else liveMap.fitBounds(b,{padding:[30,30]})}
$id('fo-feed').innerHTML=d.feed.length?d.feed.map(f=>'<div class="fo-row" data-id="'+esc(f.job)+'" onclick="openJob(this.dataset.id)"><b>'+esc(f.author)+'</b> <span class="muted">'+esc(f.customer||f.job)+' &middot; '+esc(fwhen(f.at))+'</span><div>'+esc(f.note)+'</div></div>').join(''):'<div class="fo-empty">No activity yet.</div>'}

const TPLS={{ tpls|tojson }};
function tplOpts(){const t=document.getElementById('n-type').value,s=document.getElementById('n-tpl');if(!s)return;s.innerHTML='';const o=new Option('Checklist: none (type your own below)','');s.add(o);
TPLS.filter(x=>!x.job_type||x.job_type===t).forEach(x=>s.add(new Option('Checklist: '+x.name,String(x.id))));
const c=document.getElementById('n-checklist');const d=TPLS.find(x=>x.name.toLowerCase()===(DEF_TPL[t]||'').toLowerCase()&&(!x.job_type||x.job_type===t));
if(d&&(!c.value.trim()||c.value===tplLast)){s.value=String(d.id);tplPick()}else if(c.value===tplLast){c.value='';tplLast=''}}
const DEF_TPL={{ def_tpl|tojson }};let tplLast='';
function tplPick(){const id=document.getElementById('n-tpl').value;const x=TPLS.find(k=>String(k.id)===id);if(x){const c=document.getElementById('n-checklist');c.value=x.items.join('\\n');tplLast=c.value}}
let QK=null;
async function quickFill(){try{QK=await (await fetch('/api/quick')).json()}catch(e){return}
const n=k=>document.getElementById('n-'+k);if(!n('id').value)n('id').value=QK.next_id;if(!n('date').value)n('date').value=QK.today;
const lt=localStorage.getItem('fo_last_tech');if(lt&&[...n('tech').options].some(o=>o.value===lt))n('tech').value=lt;
const ty=localStorage.getItem('fo_last_type');if(ty&&!n('title').value&&[...n('type').options].some(o=>o.value===ty)){n('type').value=ty;tplOpts()}
document.getElementById('cl-list').innerHTML=QK.clients.map(c=>'<option value="'+esc(c.name)+'"></option>').join('');
const sel=document.getElementById('n-customer-pick');
if(sel){const cur=sel.value;sel.innerHTML='<option value="">Customer: choose or type a new name</option>'+QK.clients.map(c=>'<option value="'+esc(String(c.id||c.name))+'" data-name="'+esc(c.name)+'">'+esc(c.name)+'</option>').join('');if(cur)sel.value=cur}
markQuick()}
function markQuick(){const t=document.getElementById('n-type').value;document.querySelectorAll('.qa').forEach(b=>{b.classList.toggle('gray',b.dataset.t!==t)})}
function quickType(t){const n=k=>document.getElementById('n-'+k);n('type').value=t;tplOpts();markQuick();autoTitle();(document.getElementById('n-customer-pick')||n('customer')).focus()}
function autoTitle(){const n=k=>document.getElementById('n-'+k);const t=n('title');if(!t.value||t.dataset.auto==='1'){const c=n('customer').value.trim();t.value=n('type').value+(c?' - '+c:'');t.dataset.auto='1'}}
function applyCompany(c){const n=k=>document.getElementById('n-'+k);if(!c)return;n('customer').value=c.name;n('company_id').value=c.id?String(c.id):'';if(!n('address').value)n('address').value=c.address||'';if(!n('phone').value)n('phone').value=c.phone||'';autoTitle()}
function pickCompany(sel){const id=sel.value;const c=(QK&&QK.clients||[]).find(x=>String(x.id)===String(id)||x.name===id);applyCompany(c);if(!id){document.getElementById('n-company_id').value=''}}
function pickClient(){const n=k=>document.getElementById('n-'+k);const c=(QK&&QK.clients||[]).find(x=>x.name.toLowerCase()===n('customer').value.trim().toLowerCase());
if(c){applyCompany(c);const sel=document.getElementById('n-customer-pick');if(sel&&c.id)sel.value=String(c.id)}else{n('company_id').value=''}autoTitle()}
function goSearch(){const q=(document.getElementById('q').value||'').trim();if(q.length>1)location.href='/search?q='+encodeURIComponent(q);else{document.getElementById('q').value='';load()}}
function toggleNew(){tplOpts();const o=document.getElementById('office');o.classList.toggle('hidden');if(!o.classList.contains('hidden')){quickFill();o.scrollIntoView({behavior:'smooth'});document.getElementById('n-id').focus()}}
async function load(){const q=(document.getElementById('q').value||'').trim();
if(OFFICE&&!q)return VIEW==='today'?loadToday():loadDash();
const r=await fetch(q?'/api/jobs?q='+encodeURIComponent(q):'/api/jobs?status=assigned');const jobs=await r.json();
const groups={};jobs.forEach(j=>{const k=j.scheduled_date||'';(groups[k]=groups[k]||[]).push(j)});
const keys=Object.keys(groups).sort((a,b)=>a===''?1:b===''?-1:a.localeCompare(b));
document.getElementById('list').innerHTML=(keys.map(k=>`<div class="day">${dayLabel(k)}</div>`+groups[k].map(card).join('')).join('')||'<div class="job">'+(q?'No matching jobs':'No jobs')+'</div>')}
async function loadDash(){const [a,c,fu,tx,dx,sg]=await Promise.all([fetch('/api/jobs?status=assigned').then(r=>r.json()),fetch('/api/jobs?status=completed').then(r=>r.json()),fetch('/api/followups').then(r=>r.json()),fetch('/api/drafts').then(r=>r.json()),fetch('/api/sub-docs/alerts').then(r=>r.json()),fetch('/api/agreements/pending').then(r=>r.json())]);
document.getElementById('t-sign').textContent=sg.length;document.getElementById('tile-sign').classList.toggle('red',sg.length>0);
document.getElementById('t-docs').textContent=dx.length;document.getElementById('tile-docs').classList.toggle('red',dx.length>0);document.getElementById('tile-docs').title=dx.map(x=>x.sub+' '+x.kind+(x.missing?' missing':(x.expired?' expired ':' expires ')+x.expires)).join(', ');
const today=ymd(new Date());const lists={active:a,surveys:a.filter(j=>j.job_type==='Survey'),today:a.filter(j=>j.scheduled_date===today),progress:a.filter(j=>j.status==='in_progress'),review:a.filter(j=>j.status==='review'),help:a.filter(j=>j.help_open),invoice:c.filter(j=>j.job_type!=='Survey'),follow:fu.filter(x=>x.due<=today),texts:tx.filter(x=>x.status==='pending')};
document.querySelector('.tile[data-f="follow"]').classList.toggle('red',fu.some(x=>x.due<today));document.querySelector('.tile[data-f="review"]').classList.toggle('red',lists.review.length>0);document.querySelector('.tile[data-f="texts"]').classList.toggle('red',lists.texts.length>0);
if(F==='follow'||F==='texts'){Object.keys(lists).forEach(k=>{document.getElementById('t-'+k).textContent=lists[k].length;document.querySelector('.tile[data-f="'+k+'"]').classList.toggle('on',F===k)});document.querySelector('.tile[data-f="help"]').classList.toggle('red',lists.help.length>0);return F==='follow'?renderFollowList(fu,today):renderTextList(tx)}
Object.keys(lists).forEach(k=>{document.getElementById('t-'+k).textContent=lists[k].length;document.querySelector('.tile[data-f="'+k+'"]').classList.toggle('on',F===k)});
document.querySelector('.tile[data-f="help"]').classList.toggle('red',lists.help.length>0);
document.getElementById('f-all').style.display=F?'':'none';const jobs=F?lists[F]:a;const g={};jobs.forEach(j=>{const k=(j.tech||'').trim()||'Unassigned';(g[k]=g[k]||[]).push(j)});
const keys=Object.keys(g).sort((x,y)=>x==='Unassigned'?-1:y==='Unassigned'?1:x.localeCompare(y));
keys.forEach(k=>g[k].sort((x,y)=>(x.scheduled_date||'9999').localeCompare(y.scheduled_date||'9999')));
const title=F?'<div class="day">'+esc(document.querySelector('.tile[data-f="'+F+'"]').textContent.replace(/^\d+/,''))+' <a href="#" onclick="setF(F);return false" style="color:#666;font-weight:400">(show all)</a></div>':'';
document.querySelectorAll('.tiles4 .tile').forEach(t=>t.setAttribute('aria-pressed',String(F===t.dataset.f)));
document.getElementById('list').innerHTML=(title||'<div class="list-h">Open jobs <span>'+a.length+'</span></div>')+(keys.length?'<div class="tgrid">'+keys.map(k=>`<section class="tg"><div class="tech-h">${esc(k)}${g[k][0].company?' ('+esc(g[k][0].company)+')':''} &middot; ${g[k].length} job${g[k].length===1?'':'s'}</div>`+g[k].map(dcard).join('')+'</section>').join('')+'</div>':'<div class="job">No jobs here.</div>')}
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
let tickChain=Promise.resolve();
function tick(i,v){currentJob.checklist[i].done=v;const id=currentJob.id;tickChain=tickChain.then(()=>fetch('/api/jobs/'+encodeURIComponent(id)+'/checklist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({checklist:currentJob.checklist})})).catch(()=>{});return tickChain}
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
const r=await (window.foSend||fetch)('/api/jobs/'+currentJob.id+'/updates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:n.value})},'Note for '+currentJob.id);
if(r.queued){n.value='';return}if(r.ok){n.value='';loadUpdates()}else alert('Error')}
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
const r=await (window.foSend||fetch)('/api/jobs/'+encodeURIComponent(currentJob.id)+'/photos',{method:'POST',body:fd},inp.files.length+' photo'+(inp.files.length>1?'s':'')+' for '+currentJob.id);inp.value='';if(r.queued)return;if(r.ok){currentJob.photos=(await r.json()).photos;renderPhotos()}else alert('Photo upload failed')}
['before','during','after','survey'].forEach(t=>document.getElementById(t).onchange=()=>uploadPhotos(t));
function renderMats(){const el=document.getElementById('matlist');el.innerHTML='';const m=currentJob.materials_list||[];
m.forEach((x,i)=>{const l=document.createElement('label');l.className='mat';const c=document.createElement('input');c.type='checkbox';c.checked=x.used;c.onchange=()=>{x.used=c.checked;saveMats()};
const s=document.createElement('span');s.textContent=x.text;const b=document.createElement('button');b.type='button';b.textContent='\u00d7';b.onclick=e=>{e.preventDefault();m.splice(i,1);saveMats();renderMats()};l.append(c,s,b);el.appendChild(l)});
if(!m.length)el.innerHTML='<div class="muted">No materials yet. Check items off as you use them.</div>'}
async function saveMats(){await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/materials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({materials:currentJob.materials_list})})}
function addMat(){const n=document.getElementById('mat-new');const t=n.value.trim();if(!t)return;(currentJob.materials_list=currentJob.materials_list||[]).push({text:t,used:false});n.value='';renderMats();saveMats()}
function renderTotals(){document.getElementById('totals').textContent='Total: '+(currentJob.hours||0)+' hrs';renderLabor()}
const money=n=>'$'+Number(n||0).toFixed(2);
async function saveSale(){const v=document.getElementById('sale-amt').value;const r=await post('/sale',{sale_amount:v});if(r.ok){currentJob.sale_amount=(await r.json()).sale_amount;alert('Saved')}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}}
let guided=true;
function stepNext(){const S=currentJob.steps||[];if(S.length&&S[S.length-1].done)return;let last=-1;S.forEach((s,i)=>{if(s.done)last=i});return S.slice(last+1).find(s=>!s.done)||S.find(s=>!s.done)}
function renderSteps(refocus){const S=currentJob.steps||[],nx=stepNext(),det=document.getElementById('detail');
const hd=document.getElementById('d-id').parentElement,box=document.getElementById('steps-box');if(hd.nextSibling!==box)hd.after(box);
document.getElementById('stp-bar').innerHTML=S.map((s,i)=>'<div class="stp'+(s.done?' done':'')+(s===nx?' cur':'')+'" role="button" tabindex="0" onclick="goStep('+i+')">'+(s.done?'&#10003; ':(i+1)+'. ')+esc(s.label)+'</div>').join('');
const b=document.getElementById('next-btn');b.disabled=!nx;b.textContent=nx?'Next Step: '+nx.label:'All steps done';
document.getElementById('stp-why').textContent=nx&&nx.why?nx.why:'';
det.classList.toggle('guided',guided);if({{ office|tojson }})['start','pn','done'].forEach(k=>document.getElementById('sec-'+k).classList.add('hidden'));else renderFast();document.getElementById('more-all').textContent=guided?'More':'Less';
const nk=nx?nx.label:'';if(refocus||(!{{ office|tojson }}&&nk!==lastNext)){lastNext=nk;focusSecs(currentJob.help_open?['help']:(!{{ office|tojson }}&&nx&&!nx.href?nx.go:[]))}}
let lastNext=null;
function toast(m,ms){const t=document.getElementById('toast');t.textContent=m;t.classList.remove('hidden');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.add('hidden'),ms||2500)}
function getLoc(){return new Promise(res=>{if(!navigator.geolocation)return res(null);navigator.geolocation.getCurrentPosition(p=>res({lat:p.coords.latitude,lng:p.coords.longitude,acc:p.coords.accuracy}),()=>res(null),{enableHighAccuracy:true,timeout:12000,maximumAge:60000})})}
const FS_ITEMS={{ safety_items|tojson }};
function renderFast(){const f=currentJob.flow||{},b=document.getElementById('start-box');if(!b)return;const sv=currentJob.job_type==='Survey';
const key=currentJob.id+':'+(!f.started?'start':(!sv&&!f.safety)?'safety':'done:'+(f.start_local||''));if(b.dataset.key===key){fastMisc(f);return}b.dataset.key=key;
if(!f.started){b.innerHTML='<button class="big-btn green" style="font-size:1.5rem;padding:24px" onclick="startTap(this)">Start</button><div class="muted" style="text-align:center">Tells the office you are on the way and clocks you in.</div>'}
else if(!sv&&!f.safety){b.innerHTML='<div style="font-weight:800;font-size:1.2rem">Safety Check</div><div class="muted">All OK? Change any line if not, then sign.</div>'+FS_ITEMS.map(([k,l])=>'<div class="fs-row"><span>'+esc(l)+'</span><select data-k="'+k+'" onchange="fsProb()"><option>OK</option><option>Problem</option><option>N/A</option></select></div>').join('')+
'<textarea id="fs-hz" class="hidden" rows="2" placeholder="What is the problem?"></textarea><div style="font-weight:600;margin-top:6px">Sign with your finger</div><canvas id="fs-sig" class="fs-sig" width="600" height="180"></canvas><div class="row"><button class="big-btn gray" style="flex:0 0 30%" onclick="fsClear()">Clear</button><button class="big-btn" style="flex:1" onclick="fsSubmit(this)">Submit</button></div>';fsPad()}
else b.innerHTML='<div class="ok" style="font-weight:700;text-align:center;color:#2e7d32">&#10003; Started '+esc(f.start_local||'')+(sv?'':' &middot; Safety signed')+'</div>';
fastMisc(f)}
let ptKey='';
async function partsLoad(force){if(!currentJob)return;if(!force&&ptKey===currentJob.id)return;ptKey=currentJob.id;
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/parts');if(!r.ok)return;const d=await r.json();const box=document.getElementById('parts-box');if(!box)return;
box.classList.toggle('hidden',!d.items.length);const sel=document.getElementById('pt-item'),lsel=document.getElementById('pt-loc');const keep=sel.value,lk=lsel.value;
lsel.innerHTML=d.locations.map(l=>'<option>'+esc(l)+'</option>').join('');if(lk)lsel.value=lk;if(!lsel.value)lsel.selectedIndex=0;
const L=lsel.value;sel.innerHTML='<option value="">Pick a part</option>'+d.items.map(i=>'<option value="'+i.id+'">'+esc(i.name)+' ('+(i.here[L]||0)+' '+esc(i.unit)+' here)</option>').join('');if(keep)sel.value=keep;
lsel.onchange=()=>{ptKey='';partsLoad(true)};
document.getElementById('pt-list').innerHTML=d.used.map(u=>'<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #eee"><div style="flex:1">'+esc(u.qty+' '+u.unit+' '+u.name)+'<div class="muted">'+esc(u.location+' - '+u.who)+'</div></div>'+(u.mine?'<button type="button" class="big-btn gray" style="flex:0 0 90px;padding:10px" data-m="'+u.id+'" onclick="partUndo(this.dataset.m)">Undo</button>':'')+'</div>').join('')}
async function partUse(){const id=document.getElementById('pt-item').value,q=document.getElementById('pt-qty').value;if(!id){toast('Pick a part');return}
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/parts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:id,qty:q,location:document.getElementById('pt-loc').value})});
const d=await r.json().catch(()=>({}));if(!r.ok){toast(d.error||'Error');return}if(d.queued)return;document.getElementById('pt-qty').value=1;toast('Part used');partsLoad(true)}
async function partUndo(m){if(!confirm('Undo this part?'))return;const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/parts/'+m,{method:'DELETE'});if(r.ok){toast('Undone');partsLoad(true)}}
function fastMisc(f){partsLoad();const pt=document.getElementById('pn-today');if(pt)pt.textContent=f.log_today?'Saved for today. Save again to add more.':'';
const st=(currentJob.steps||[]).find(s=>s.label==='Finish');document.getElementById('done-why').textContent=st&&!st.done&&st.why?st.why:''}
function fsProb(){const p=[...document.querySelectorAll('#start-box select')].some(s=>s.value==='Problem');document.getElementById('fs-hz').classList.toggle('hidden',!p)}
function fsPad(){const c=document.getElementById('fs-sig'),x=c.getContext('2d');x.lineWidth=3;x.lineCap='round';let d=false;c.dataset.ink='';const pt=e=>{const r=c.getBoundingClientRect();return[(e.clientX-r.left)*c.width/r.width,(e.clientY-r.top)*c.height/r.height]};
c.onpointerdown=e=>{d=true;c.setPointerCapture(e.pointerId);const[a,b]=pt(e);x.beginPath();x.moveTo(a,b)};c.onpointermove=e=>{if(!d)return;const[a,b]=pt(e);x.lineTo(a,b);x.stroke();c.dataset.ink='1'};c.onpointerup=()=>d=false}
function fsClear(){const c=document.getElementById('fs-sig');c.getContext('2d').clearRect(0,0,c.width,c.height);c.dataset.ink=''}
async function reloadJob(){const id=currentJob.id;const r=await fetch('/api/jobs/'+encodeURIComponent(id));if(!r.ok||currentJob.id!==id)return;const j=await r.json();Object.assign(currentJob,j);renderPhotos();renderClock();renderTotals();renderTicks&&0;renderSteps(false)}
async function startTap(b){b.disabled=true;b.textContent='Starting...';const loc=await getLoc();const r=await post('/clock',loc||{});const d=await r.json().catch(()=>({}));
if(!r.ok&&!(d.error||'').includes('still clocked in on job '+currentJob.id)){toast(d.error||'Error',4000);b.disabled=false;b.textContent='Start';return}
if(currentJob.job_type!=='Office Task')await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/onmyway',{method:'POST'});
toast('Started. Office will send your On My Way text.'+(d.clock&&d.clock.in_flag?' Flag: '+d.clock.in_flag:''),3500);await reloadJob()}
async function fsSubmit(b){const c=document.getElementById('fs-sig');if(!c.dataset.ink){toast('Please sign first');return}const fd=new FormData();
document.querySelectorAll('#start-box select').forEach(s=>fd.append(s.dataset.k,s.value));fd.append('hazards',document.getElementById('fs-hz').value);fd.append('signature',c.toDataURL('image/png'));
b.disabled=true;const r=await fetch('/job/'+encodeURIComponent(currentJob.id)+'/safety/daily?json=1',{method:'POST',body:fd});const d=await r.json().catch(()=>({}));b.disabled=false;
if(!r.ok){toast(d.error||'Error',4000);return}toast('Safety check saved');await reloadJob()}
function pnPicked(){const n=document.getElementById('pn-photos').files.length;document.getElementById('pn-picked').textContent=n?n+' photo'+(n>1?'s':'')+' ready. Tap Save.':''}
async function pnSave(){const f=document.getElementById('pn-photos'),w=document.getElementById('pn-work');if(!w.value.trim()&&!f.files.length){toast('Add a photo or say what you did');return}
const fd=new FormData();fd.append('work_done',w.value);fd.append('materials',document.getElementById('pn-mat').value);fd.append('issues',document.getElementById('pn-issues').value);[...f.files].forEach(x=>fd.append('photos',x));
const b=document.getElementById('pn-save');b.disabled=true;b.textContent='Saving...';const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/worklog',{method:'POST',body:fd});const d=await r.json().catch(()=>({}));b.disabled=false;b.textContent='Save';
if(!r.ok){toast(d.error||'Error',4000);return}matRemember(document.getElementById('pn-mat').value);f.value='';w.value='';document.getElementById('pn-mat').value='';document.getElementById('pn-issues').value='';pnPicked();toast('Saved');if(typeof loadUpdates==='function')loadUpdates();await reloadJob()}
async function finishTap(){const b=document.getElementById('done-btn');await refreshSteps();const S=currentJob.steps||[];const fin=S.find(s=>s.label==='Finish');
if(fin&&fin.why){toast(fin.why,4000);const e=focusSecs(fin.go||[]);if(e)e.scrollIntoView({behavior:'smooth',block:'start'});return}
b.disabled=true;b.textContent='Finishing...';if(currentJob.clock&&currentJob.clock.open){const loc=await getLoc();const r=await post('/clock',loc||{});if(!r.ok){const d=await r.json().catch(()=>({}));toast(d.error||'Clock out failed',4000);b.disabled=false;b.textContent='Finish';return}}
if(currentJob.job_type==='Survey'){b.disabled=false;b.textContent='Finish';document.getElementById('finish').onclick();return}
const fd=new FormData();fd.append('notes',currentJob.notes||'');fd.append('materials',currentJob.materials||'');fd.append('hours',0);fd.append('miles',0);fd.append('checklist',JSON.stringify(currentJob.checklist||[]));fd.append('signature',canvas.toDataURL());
const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/complete',{method:'POST',body:fd});const d=await r.json().catch(()=>({}));b.disabled=false;b.textContent='Finish';
if(!r.ok){toast(d.error||'Error',4000);await reloadJob();return}toast(d.status==='review'?'Done. The office will review it.':'Done');setTimeout(showList,1200)}
function focusSecs(keys){document.querySelectorAll('#detail .focus').forEach(e=>e.classList.remove('focus'));const mb=document.getElementById('more-box');let first=null;
keys.forEach(k=>{const e=document.getElementById('sec-'+k);if(!e)return;e.classList.add('focus');e.classList.remove('hidden');if(mb.contains(e)){document.getElementById('sec-more').classList.add('focus');mb.classList.remove('hidden')}first=first||e});return first}
function goStep(i){const s=(currentJob.steps||[])[i];if(!s)return;if(s.href){location.href=s.href;return}const e=focusSecs(s.go||[]);if(e)setTimeout(()=>e.scrollIntoView({behavior:'smooth',block:'start'}),50)}
function nextStep(){const nx=stepNext();if(!nx)return;if(nx.href){location.href=nx.href;return}
const e=focusSecs(nx.go||[]);if(e)setTimeout(()=>e.scrollIntoView({behavior:'smooth',block:'start'}),50)}
function toggleGuided(){guided=!guided;renderSteps(false);if(!guided)document.querySelectorAll('#detail .focus').forEach(e=>e.classList.remove('focus'))}
async function refreshSteps(){if(!currentJob)return;const id=currentJob.id;const r=await fetch('/api/jobs/'+encodeURIComponent(id)+'/steps');if(!r.ok||!currentJob||currentJob.id!==id)return;const sj=await r.json();currentJob.steps=sj.steps;currentJob.flow=sj.flow;renderSteps(false)}
let stepT=null;const _fetch=window.fetch.bind(window);
const QDB=()=>new Promise((res,rej)=>{const q=indexedDB.open('fo-queue',1);q.onupgradeneeded=()=>q.result.createObjectStore('q',{keyPath:'id',autoIncrement:true});q.onsuccess=()=>res(q.result);q.onerror=()=>rej(q.error)});
async function qAll(){const db=await QDB();return new Promise(r=>{const t=db.transaction('q').objectStore('q').getAll();t.onsuccess=()=>r(t.result||[])})}
async function qPut(v){const db=await QDB();return new Promise(r=>{const t=db.transaction('q','readwrite');t.objectStore('q').add(v);t.oncomplete=r})}
async function qDel(id){const db=await QDB();return new Promise(r=>{const t=db.transaction('q','readwrite');t.objectStore('q').delete(id);t.oncomplete=r})}
function qable(u,m){const s=String(u);return m!=='GET'&&(s.startsWith('/api/jobs/')||s.startsWith('/job/'))&&!s.endsWith('/steps')}
function b64u(x){const p='='.repeat((4-x.length%4)%4);const r=atob((x+p).replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from(r,c=>c.charCodeAt(0))}
async function pushInit(){const btn=document.getElementById('push-btn');if(!btn)return;
if(!('serviceWorker' in navigator)||!('PushManager' in window)){if(/iPhone|iPad/.test(navigator.userAgent))document.getElementById('push-hint').classList.remove('hidden');return}
try{const k=await (await fetch('/api/push/key')).json();if(!k.on)return;const reg=await navigator.serviceWorker.ready;const sub=await reg.pushManager.getSubscription();
if(sub&&Notification.permission==='granted'){fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sub)});return}
if(Notification.permission!=='denied')btn.classList.remove('hidden')}catch(e){}}
async function pushOn(){const btn=document.getElementById('push-btn');btn.disabled=true;
try{const perm=await Notification.requestPermission();if(perm!=='granted'){toast('Alerts are off. Turn them on in your phone settings.');btn.disabled=false;return}
const k=await (await fetch('/api/push/key')).json();const reg=await navigator.serviceWorker.ready;
const sub=(await reg.pushManager.getSubscription())||(await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64u(k.key)}));
const r=await fetch('/api/push/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sub)});
if(r.ok){btn.classList.add('hidden');toast('Job alerts on. You will get a ping for new jobs and schedule changes.')}else{toast('Could not turn on alerts. Try again.');btn.disabled=false}}
catch(e){toast('Could not turn on alerts. Try again.');btn.disabled=false}}
window.addEventListener('load',()=>setTimeout(pushInit,800));
function chip(c){const w=document.getElementById('pn-work');const v=w.value.trim();w.value=v?(v.replace(/[.,]?$/,'')+', '+c.toLowerCase()):c;w.focus()}
function matRemember(v){v=(v||'').trim();if(!v)return;try{let a=JSON.parse(localStorage.getItem('fo_mats')||'[]').filter(x=>x!==v);a.unshift(v);localStorage.setItem('fo_mats',JSON.stringify(a.slice(0,12)))}catch(e){}matList()}
function matList(){const d=document.getElementById('mat-list');if(!d)return;try{d.innerHTML=JSON.parse(localStorage.getItem('fo_mats')||'[]').map(x=>'<option value="'+esc(x)+'"></option>').join('')}catch(e){}}
window.addEventListener('load',matList);
async function qBadge(){const n=(await qAll().catch(()=>[])).length;let b=document.getElementById('q-bar');if(!b){b=document.createElement('div');b.id='q-bar';b.style.cssText='position:sticky;top:0;z-index:30;background:#b26a00;color:#fff;font-weight:700;text-align:center;padding:10px;border-radius:8px;margin:4px 0';document.body.prepend(b)}
b.textContent=n?(navigator.onLine?'Sending '+n+' saved item'+(n>1?'s':'')+'...':'Offline. '+n+' item'+(n>1?'s':'')+' saved on this phone. They send when you are back online.'):(navigator.onLine?'':'Offline. You can keep working.');b.style.display=b.textContent?'block':'none'}
let qBusy=false;async function qFlush(){if(qBusy||!navigator.onLine)return;qBusy=true;try{for(const it of await qAll()){let body=it.body;if(it.form){body=new FormData();it.form.forEach(([k,v,n])=>n?body.append(k,v,n):body.append(k,v))}
const h=Object.assign({},it.headers||{},{'X-Queued-At':it.at});let r;try{r=await _fetch(it.url,{method:it.method,headers:h,body})}catch(e){break}
if(r.ok||(r.status>=400&&r.status<500)){await qDel(it.id);if(!r.ok){const d=await r.json().catch(()=>({}));toast('Not saved: '+(d.error||r.status),5000)}}else break}}finally{qBusy=false;qBadge();if(currentJob)refreshSteps()}}
window.addEventListener('online',()=>{qBadge();qFlush()});window.addEventListener('offline',qBadge);setTimeout(()=>{qBadge();qFlush()},800);
async function qSave(u,o){const it={url:String(u),method:(o.method||'POST').toUpperCase(),headers:{},at:new Date().toISOString()};
if(o.headers)Object.entries(o.headers).forEach(([k,v])=>{if(/content-type/i.test(k))it.headers[k]=v});
if(o.body instanceof FormData){it.form=[];for(const [k,v] of o.body.entries())it.form.push(v instanceof File?[k,v,v.name]:[k,v])}else it.body=o.body||null;
await qPut(it);qBadge();return new Response(JSON.stringify({ok:true,queued:true,msg:'Saved on this phone. It will send when you are back online.'}),{status:200,headers:{'Content-Type':'application/json'}})}
window.fetch=async(u,o)=>{const m=((o&&o.method)||'GET').toUpperCase();if(qable(u,m)){if(!navigator.onLine)return qSave(u,o||{});try{const r0=await _fetch(u,o);afterFetch(u,o,r0);return r0}catch(e){return qSave(u,o||{})}}
const r=await _fetch(u,o);afterFetch(u,o,r);return r};
function afterFetch(u,o,r){try{const m=((o&&o.method)||'GET').toUpperCase(),s=String(u);
if(m!=='GET'&&currentJob&&(s.startsWith('/api/jobs/'+encodeURIComponent(currentJob.id)+'/')||s.startsWith('/api/jobs/'+currentJob.id+'/'))){clearTimeout(stepT);stepT=setTimeout(refreshSteps,400)}}catch(e){}}
function renderClock(){const c=currentJob.clock||{},b=document.getElementById('clk-btn'),i=document.getElementById('clk-info');if(!b)return;b.disabled=false;
b.textContent=c.open?'Clock Out':'Clock In';b.className='big-btn '+(c.open?'stop':'green');let h='';
if(c.open)h+='<b>Clocked in '+esc(c.in_local)+'</b>'+(c.in_dist?'<br>'+esc(c.in_dist):'')+(c.in_flag?'<br><b style="color:#b00020">'+esc(c.in_flag)+'</b>':'');
if(c.other)h+='<br><b style="color:#b00020">Still clocked in on job '+esc(c.other)+'</b>';if(c.today_hours)h+='<br>Today on this job: '+c.today_hours+' hrs';i.innerHTML=h;
document.getElementById('sf-link').href='/job/'+encodeURIComponent(currentJob.id)+'/safety';document.getElementById('dl-link').href='/job/'+encodeURIComponent(currentJob.id)+'/daily';const lg=document.getElementById('clk-log');if(lg)lg.href='/timelog?job='+encodeURIComponent(currentJob.id)}
async function clockTap(){const b=document.getElementById('clk-btn');b.disabled=true;b.textContent='Getting location...';
const loc=await new Promise(res=>{if(!navigator.geolocation)return res(null);navigator.geolocation.getCurrentPosition(p=>res({lat:p.coords.latitude,lng:p.coords.longitude,acc:p.coords.accuracy}),()=>res(null),{enableHighAccuracy:true,timeout:15000,maximumAge:30000})});
const r=await post('/clock',loc||{});const d=await r.json().catch(()=>({}));if(!r.ok){alert(d.error||'Error');renderClock();return}
alert(d.msg);currentJob.clock=d.clock;if(d.hours!=null){currentJob.hours=d.hours;currentJob.total_minutes=Math.round(d.hours*60);renderTotals()}if(d.labor){currentJob.labor=d.labor;renderLabor()}renderClock()}
function renderLabor(){const sa=document.getElementById('sale-amt');if(sa)sa.value=currentJob.sale_amount==null?'':currentJob.sale_amount;const el=document.getElementById('labor');if(!el||!currentJob.labor)return;const L=currentJob.labor;
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
const DEV_SYM={{ DEV_SYM|tojson }},CAM_DEF={{ CAM_DEF|tojson }},CAM_RANGE={{ CAM_RANGE|tojson }},CAM_FT={{ CAM_FT|tojson }};
function devSvg(t,col,solid,size){size=size||34;const bg=solid?col:'#fff',fg=solid?'#fff':col;return '<svg class="dsym" viewBox="0 0 24 24" width="'+size+'" height="'+size+'" aria-hidden="true"><circle cx="12" cy="12" r="11" fill="'+bg+'" stroke="'+(solid?'#fff':col)+'" stroke-width="1.6"'+(solid?'':' stroke-dasharray="3 2"')+'/><g transform="translate(12 12) scale(.66) translate(-12 -12)" fill="none" stroke="'+fg+'" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+(DEV_SYM[t]||'<circle cx="12" cy="12" r="3"/>')+'</g></svg>'}
function pinfo(fn){return (currentJob.plan_info||[]).find(p=>p.fn===fn)}
function camView(d){const p=pinfo(d.plan),v={dir:d.dir||0,fov:d.fov||CAM_DEF[d.type]||90,range:d.range||CAM_RANGE,ft:null};if(p&&p.ppf&&p.w){v.ft=d.rft||CAM_FT[d.type]||30;v.range=+(v.ft*p.ppf/p.w*100).toFixed(2)}return v}
function setRange(d,pct){d.range=Math.max(2,Math.min(100,+pct.toFixed(1)));const p=pinfo(d.plan);if(p&&p.ppf&&p.w)d.rft=Math.max(1,Math.min(2000,Math.round(pct/100*p.w/p.ppf)))}
function rangeStr(v){return v.ft?v.ft+' ft':v.range+'% of plan (set the scale for feet)'}
function conePath(cx,cy,r,dir,fov){if(fov>=360)return 'M'+cx+' '+cy+'m-'+r+' 0a'+r+' '+r+' 0 1 0 '+2*r+' 0a'+r+' '+r+' 0 1 0 -'+2*r+' 0z';const a1=(dir-fov/2)*Math.PI/180,a2=(dir+fov/2)*Math.PI/180;
return 'M'+cx+' '+cy+'L'+(cx+r*Math.sin(a1)).toFixed(1)+' '+(cy-r*Math.cos(a1)).toFixed(1)+'A'+r+' '+r+' 0 '+(fov>180?1:0)+' 1 '+(cx+r*Math.sin(a2)).toFixed(1)+' '+(cy-r*Math.cos(a2)).toFixed(1)+'Z'}
function covOn(){return localStorage.getItem('fo_cov')!=='0'}
function covToggle(){localStorage.setItem('fo_cov',covOn()?'0':'1');renderDevices()}
function drawCones(box,fn,devs){const img=box.querySelector('img');const go=()=>{box.querySelectorAll('.cov').forEach(e=>e.remove());if(!covOn())return;const W=img.naturalWidth||1000,H=img.naturalHeight||1000;
const p=devs.filter(d=>d.plan===fn&&CAM_DEF[d.type]).map(d=>{const v=camView(d),col=devColor(d.type);return '<path d="'+conePath(d.x*W/100,d.y*H/100,v.range*W/100,v.dir,v.fov)+'" fill="'+col+'" fill-opacity="'+(devSel===d.id?.24:.14)+'" stroke="'+col+'" stroke-opacity=".75" stroke-width="2" vector-effect="non-scaling-stroke"'+(d.type==='PTZ Cam'?' stroke-dasharray="6 4"':'')+(devSel===d.id&&!devLock()?' class="cone-sel" style="pointer-events:auto;cursor:move;touch-action:none"':'')+'/>'}).join('');
if(!p)return;box.insertAdjacentHTML('afterbegin','<svg class="cov" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" aria-hidden="true">'+p+'</svg>')};if(img.complete&&img.naturalWidth)go();else img.addEventListener('load',go,{once:true})}
function camCh(k,dv){const d=currentJob.devices.find(x=>x.id===devSel);if(!d)return;devSnap();const v=camView(d);
if(k==='dir')d.dir=((v.dir+dv)%360+360)%360;else if(k==='fov')d.fov=dv==='360'?360:Math.max(10,Math.min(360,v.fov+dv));else if(k==='range'){if(v.ft)d.rft=Math.max(5,Math.min(2000,v.ft+(dv>0?5:-5)));else d.range=Math.max(2,Math.min(100,+(v.range+dv).toFixed(1)))}
d.dir=d.dir??v.dir;d.fov=d.fov||v.fov;if(!v.ft)d.range=d.range||v.range;renderDevices();clearTimeout(window.camT);window.camT=setTimeout(saveDevices,500)}
function devLock(){return currentJob.job_type==='Survey'&&currentJob.status==='completed'&&!svEditing}
function isAB(){return currentJob.job_type!=='Survey'}
function devColor(t){const d=DEVS.find(x=>x[0]===t);return d?d[2]:'#333'}
function devPrefix(t){const d=DEVS.find(x=>x[0]===t);return d?d[1]:'?'}
function renderDevices(){if(!currentJob||currentJob.job_type==='Office Task')return;const ab=isAB();const lock=devLock();const plans=currentJob.plans||[];const devs=currentJob.devices=currentJob.devices||[];
if(lock){devType='';devMove=null}
document.getElementById('plan-add').classList.toggle('hidden',lock);
const pal=document.getElementById('dev-palette');pal.classList.toggle('hidden',lock||!plans.length);pal.innerHTML='';
DEVS.forEach(([n,p,c])=>{const b=document.createElement('button');b.className=devType===n?'on':'';b.innerHTML=devSvg(n,c,true,24);b.append(n);b.onclick=()=>{devType=devType===n?'':n;devMove=null;renderDevices()};pal.appendChild(b)});
document.getElementById('dev-hint').textContent=lock||!plans.length?'':devMove?'Tap the new spot on the plan':devType?'Tap the plan to drop a '+devType:'Pick a device, then tap the plan';
const w=document.getElementById('plans');w.innerHTML='';
plans.forEach((fn,pi)=>{const box=document.createElement('div');box.className='plan';const img=document.createElement('img');img.src='/uploads/'+encodeURIComponent(fn);img.alt='floor plan';box.appendChild(img);
devs.filter(d=>d.plan===fn).forEach(d=>{const pin=document.createElement('div');pin.className='dpin'+(devSel===d.id?' sel':'')+(d.x>82?' lf':'');pin.style.left=d.x+'%';pin.style.top=d.y+'%';pin.style.color=devColor(d.type);pin.title=d.type;pin.innerHTML=devSvg(d.type,devColor(d.type),!(ab&&!d.installed))+'<b>'+esc(d.label||devPrefix(d.type))+'</b>';
pin.onpointerdown=e=>pinDown(e,d,fn,box,pin);pin.onpointermove=pinMove;pin.onpointerup=pinUp;pin.onpointercancel=pinCancel;
pin.onclick=e=>{e.stopPropagation();if(pin._noclick){pin._noclick=false;return}if(devMove)return planTap(e,fn,box);devSel=d.id;devType='';renderDevices();document.getElementById('dev-edit').scrollIntoView({behavior:'smooth',block:'center'})};box.appendChild(pin)});
drawCones(box,fn,devs);camHandles(box,fn);box.onpointerdown=e=>{if(e.target.classList&&e.target.classList.contains('cone-sel'))coneDown(e,'rot',box,fn,box)};box.onpointermove=coneMove;box.onpointerup=coneUp;box.onpointercancel=coneUp;box.onclick=e=>{if(box._noclick){box._noclick=false;return}planTap(e,fn,box)};w.appendChild(box);
if(!lock){const rb=document.createElement('button');rb.className='big-btn gray';rb.textContent='Remove Plan '+(pi+1);rb.onclick=()=>removePlan(fn);w.appendChild(rb)}});
document.getElementById('dev-undo').classList.toggle('hidden',lock||!devUndo.length);document.getElementById('dv-move').classList.toggle('hidden',lock);document.getElementById('dv-movehint').classList.toggle('hidden',lock);
const d=devs.find(x=>x.id===devSel);const ed=document.getElementById('dev-edit');ed.classList.toggle('hidden',!d);
let cc=document.getElementById('cam-ctl');if(!cc){cc=document.createElement('div');cc.id='cam-ctl';document.getElementById('dv-title').after(cc)}cc.innerHTML='';
if(d&&CAM_DEF[d.type]){const v=camView(d),cmp=['N','NE','E','SE','S','SW','W','NW'][Math.round(v.dir/45)%8];cc.innerHTML='<div class="muted" style="margin:4px 0">Facing '+cmp+' ('+v.dir+'\u00b0), view '+v.fov+'\u00b0, range '+rangeStr(v)+'</div>'+(lock?'':'<div class="cam-ctl"><button onclick="camCh(&quot;dir&quot;,-15)">&#8634; Turn left</button><button onclick="camCh(&quot;dir&quot;,15)">Turn right &#8635;</button><button onclick="camCh(&quot;fov&quot;,&quot;360&quot;)">360&deg;</button><button onclick="camCh(&quot;fov&quot;,-10)">Narrower</button><button onclick="camCh(&quot;fov&quot;,10)">Wider</button><button onclick="camCh(&quot;range&quot;,-3)">Shorter</button><button onclick="camCh(&quot;range&quot;,3)">Longer</button></div>')}
if(d){document.getElementById('dv-title').textContent=d.label+' \u00b7 '+d.type;['note','height','cable'].forEach(k=>{const el=document.getElementById('dv-'+k);el.value=d[k]||'';el.disabled=lock});document.getElementById('dv-btns').classList.toggle('hidden',lock);
document.querySelectorAll('#dev-edit .ab-only').forEach(e=>e.classList.toggle('hidden',!ab));
if(ab){['mac','ip','port','clabel'].forEach(k=>document.getElementById('dv-'+k).value=d[k]||'');document.getElementById('dv-installed').checked=!!d.installed;
const ph=document.getElementById('dv-photo');ph.innerHTML='';if(d.photo){const a=document.createElement('a');a.href='/uploads/'+encodeURIComponent(d.photo);a.target='_blank';const i=document.createElement('img');i.src=a.href;i.style.cssText='width:120px;height:120px;object-fit:cover;border-radius:8px';a.appendChild(i);ph.appendChild(a)}
document.getElementById('dv-photo-btn').textContent=d.photo?'Replace Device Photo':'Add Device Photo'}}
const cp=document.getElementById('ab-copy');cp.innerHTML='';if(ab&&!lock&&!plans.length)(currentJob.survey_plan_sources||[]).forEach(x=>{const b=document.createElement('button');b.className='big-btn';b.style.background='#6a1b9a';b.textContent='Copy Survey Plan ('+x.id+')';b.onclick=()=>copyPlan(x.id);cp.appendChild(b)});
const cnt={};devs.forEach(x=>cnt[x.type]=(cnt[x.type]||0)+1);const c=document.getElementById('dev-counts');c.innerHTML='';
if(devs.length){const h=document.createElement('div');h.className='ptitle';h.textContent='Device Count ('+devs.length+' total'+(ab?', '+devs.filter(x=>x.installed).length+' installed':'')+')';c.appendChild(h);
if(devs.some(x=>CAM_DEF[x.type])){const t=document.createElement('button');t.className='big-btn gray';t.textContent='Show coverage: '+(covOn()?'On':'Off');t.onclick=covToggle;c.appendChild(t)}
DEVS.forEach(([n,p,col])=>{if(!cnt[n])return;const r=document.createElement('div');r.style.cssText='font-size:1.15rem;padding:6px 0;border-bottom:1px solid #e5e5e5;display:flex;align-items:center;gap:8px';const i=document.createElement('span');i.innerHTML=devSvg(n,col,true,28);i.style.lineHeight='0';const inst=devs.filter(x=>x.type===n&&x.installed).length;r.append(i,n+' x'+cnt[n]+(ab?' ('+inst+' installed, '+(cnt[n]-inst)+' planned)':''));c.appendChild(r)})}
else if(plans.length)c.innerHTML='<div class="muted">No devices yet</div>'}
let devDrag=null,devUndo=[];
function devSnap(){devUndo.push(JSON.stringify(currentJob.devices));if(devUndo.length>50)devUndo.shift()}
function devUndoTap(){if(!devUndo.length)return;currentJob.devices=JSON.parse(devUndo.pop());devSel=null;devMove=null;saveDevices()}
function pinDown(e,d,fn,box,pin){if(devLock()||devMove||devType||(e.pointerType==='mouse'&&e.button!==0))return;const pr=pin.getBoundingClientRect();if(devSel&&(e.clientX<pr.left-10||e.clientX>pr.right+10||e.clientY<pr.top-10||e.clientY>pr.bottom+10)){const under=document.elementsFromPoint(e.clientX,e.clientY).find(x=>x.classList&&(x.classList.contains('chd')||x.classList.contains('cone-sel')));if(under){pin._noclick=true;setTimeout(()=>{pin._noclick=false},500);const hd=under.classList.contains('chd')?under:null;coneDown(e,hd?hd.dataset.k:'rot',box,fn,hd||box);return}}try{pin.setPointerCapture(e.pointerId)}catch(_){}devDrag={id:d.id,pid:e.pointerId,x0:e.clientX,y0:e.clientY,moved:false,pin:pin,box:box,fn:fn}}
function pinMove(e){const g=devDrag;if(!g||e.pointerId!==g.pid)return;if(!g.moved&&Math.hypot(e.clientX-g.x0,e.clientY-g.y0)<6)return;g.moved=true;e.preventDefault();
const r=g.box.querySelector('img').getBoundingClientRect();g.x=Math.max(0,Math.min(100,(e.clientX-r.left)/r.width*100));g.y=Math.max(0,Math.min(100,(e.clientY-r.top)/r.height*100));g.pin.style.left=g.x+'%';g.pin.style.top=g.y+'%';g.pin.classList.add('drag')}
function pinUp(e){const g=devDrag;if(!g||e.pointerId!==g.pid)return;devDrag=null;if(!g.moved)return;g.pin._noclick=true;setTimeout(()=>{g.pin._noclick=false},400);
const d=currentJob.devices.find(k=>k.id===g.id);if(!d)return;devSnap();d.x=+g.x.toFixed(2);d.y=+g.y.toFixed(2);d.plan=g.fn;devSel=d.id;saveDevices()}
function pinCancel(e){if(devDrag&&e.pointerId===devDrag.pid){devDrag=null;renderDevices()}}
let coneDrag=null;
function angOf(d,e,box){const r=box.querySelector('img').getBoundingClientRect(),cx=r.left+d.x/100*r.width,cy=r.top+d.y/100*r.height,dx=e.clientX-cx,dy=e.clientY-cy;return {a:((Math.atan2(dx,-dy)*180/Math.PI)+360)%360,dist:Math.hypot(dx,dy)/r.width*100}}
function handlePos(d,ang,rad,box){const im=box.querySelector('img'),r=im.getBoundingClientRect(),q=ang*Math.PI/180,k=(im.naturalWidth&&im.naturalHeight)?im.naturalWidth/im.naturalHeight:(r.width&&r.height?r.width/r.height:1);return [d.x+rad*Math.sin(q),d.y-rad*Math.cos(q)*k]}
function camHandles(box,fn){box.querySelectorAll('.chd').forEach(e=>e.remove());const d=(currentJob.devices||[]).find(x=>x.id===devSel);
if(!d||d.plan!==fn||!CAM_DEF[d.type]||devLock()||!covOn())return;const im=box.querySelector('img');if(im&&!im.naturalWidth){im.addEventListener('load',()=>camHandles(box,fn),{once:true});return}const v=camView(d),c=devColor(d.type);
[['tip',v.dir,v.range,'Drag to turn and set range'],['edge',v.fov>=360?v.dir+90:v.dir+v.fov/2,v.range*.72,'Drag to widen or narrow']].forEach(([k,a,rad,tl])=>{const el=document.createElement('div');el.className='chd '+k;el.dataset.k=k;el.title=tl;el.style.color=c;el.innerHTML='<i style="background:'+(k==='tip'?c:'#fff')+'"></i>';
const [x,y]=handlePos(d,a,rad,box);el.style.left=x+'%';el.style.top=y+'%';el.onpointerdown=e=>coneDown(e,k,box,fn,el);el.onpointermove=coneMove;el.onpointerup=coneUp;el.onpointercancel=coneUp;el.onclick=e=>e.stopPropagation();box.appendChild(el)})}
function coneDown(e,k,box,fn,cap){const d=currentJob.devices.find(x=>x.id===devSel);if(!d||devLock())return;e.preventDefault();e.stopPropagation();try{cap.setPointerCapture(e.pointerId)}catch(_){}
const v=camView(d);coneDrag={k:k,pid:e.pointerId,box:box,fn:fn,d:d,before:JSON.stringify(currentJob.devices),moved:false};d.dir=v.dir;d.fov=v.fov;d.range=Math.min(100,v.range);if(v.ft)d.rft=v.ft}
function coneMove(e){const g=coneDrag;if(!g||e.pointerId!==g.pid)return;e.preventDefault();e.stopPropagation();g.moved=true;const d=g.d,o=angOf(d,e,g.box);
if(g.k==='tip'){d.dir=Math.round(o.a);setRange(d,Math.max(2,o.dist))}
else if(g.k==='edge'){let df=Math.abs(((o.a-d.dir)%360+540)%360-180);d.fov=Math.max(10,Math.min(359,Math.round(df*2)))}
else d.dir=Math.round(o.a);
drawCones(g.box,g.fn,currentJob.devices);const v=camView(d);g.box.querySelectorAll('.chd').forEach(el=>{const tip=el.dataset.k==='tip',[x,y]=handlePos(d,tip?v.dir:v.dir+v.fov/2,tip?v.range:v.range*.72,g.box);el.style.left=x+'%';el.style.top=y+'%'})}
function coneUp(e){const g=coneDrag;if(!g||e.pointerId!==g.pid)return;coneDrag=null;g.box._noclick=true;setTimeout(()=>{g.box._noclick=false},400);if(!g.moved)return;
devUndo.push(g.before);if(devUndo.length>50)devUndo.shift();saveDevices()}
function planTap(e,fn,box){if(devLock())return;const r=box.querySelector('img').getBoundingClientRect();const x=(e.clientX-r.left)/r.width*100,y=(e.clientY-r.top)/r.height*100;if(x<0||x>100||y<0||y>100)return;
if(devMove){const d=currentJob.devices.find(k=>k.id===devMove);devMove=null;if(d){devSnap();d.x=x;d.y=y;d.plan=fn;devSel=d.id}saveDevices();return}
if(!devType){alert('Pick a device first');return}
devSnap();currentJob.devices.push({type:devType,plan:fn,x:x,y:y});saveDevices()}
async function saveDevices(){const r=await post('/devices',{devices:currentJob.devices});if(r.ok){currentJob.devices=(await r.json()).devices}else{alert('Error, not saved');const g=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id));if(g.ok)currentJob.devices=(await g.json()).devices}renderDevices()}
function devSave(){const d=currentJob.devices.find(k=>k.id===devSel);if(!d)return;['note','height','cable'].concat(isAB()?['mac','ip','port','clabel']:[]).forEach(k=>d[k]=document.getElementById('dv-'+k).value.trim());if(isAB())d.installed=document.getElementById('dv-installed').checked;devSnap();devSel=null;saveDevices()}
function devMoveStart(){devMove=devSel;devSel=null;devType='';renderDevices();document.getElementById('dev-hint').scrollIntoView({behavior:'smooth',block:'center'})}
function devDelete(){const d=currentJob.devices.find(k=>k.id===devSel);if(!d||!confirm('Delete '+d.label+'?'))return;devSnap();currentJob.devices=currentJob.devices.filter(k=>k.id!==devSel);devSel=null;saveDevices()}
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
const mk=L.marker([c.lat,c.lng],{draggable:!lock,icon:L.divIcon({className:'',html:'<div class="dpin" style="position:static;margin:0;color:'+col+'">'+devSvg('Dome Cam',col,true,34)+'<b>'+esc(c.label||'C')+'</b></div>',iconSize:[34,34],iconAnchor:[17,17]})}).addTo(mpLay);
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
function fillSurvey(){svEditing=false;devType='';devSel=null;devMove=null;devUndo=[];renderKelly();const s=currentJob.survey||{};SV_TEXT.forEach(k=>document.getElementById('sv-'+k).value=s[k]||'');
if(!s.site_conditions)document.getElementById('sv-site_conditions').value=legacyCond(s);const o=document.getElementById('sv-office');if(o)o.value=currentJob.survey_office||'';const ex=document.getElementById('sv-expect');if(ex)ex.textContent=(s.timeline||s.budget||s.decision_maker)?'Earlier entry (office only): Timeline '+(s.timeline||'-')+' \u00b7 Budget '+(s.budget||'-')+' \u00b7 Decision maker '+(s.decision_maker||'-'):'';svCall()}
async function saveSurvey(quiet){const s=Object.assign({},currentJob.survey||{});SV_TEXT.forEach(k=>s[k]=document.getElementById('sv-'+k).value);
let ok=(await post('/survey',{survey:s})).ok;const o=document.getElementById('sv-office');if(ok&&o)ok=(await post('/survey-office',{survey_office:o.value})).ok;
if(ok){currentJob.survey=s;if(o)currentJob.survey_office=o.value;svCall();if(!quiet){alert('Survey saved');svEditing=false;renderKelly()}}else alert('Error');return ok}
const LAYOUT={Survey:{show:['help','links','type','start','done','omw','clock','survey','check','log','follow','hub','finish'],more:[]},
Install:{show:['help','review','links','type','start','pn','done','omw','clock','status','photos','mat','time','check','log','ab','map','follow','hub','private','sig','finish'],more:[]},
Service:{show:['help','review','links','type','start','pn','done','omw','clock','status','log','photos','mat','time','follow','private','sig','finish'],more:['ab','map','check','hub']},
'Office Task':{show:['type','status','log','check','follow','finish'],more:[]}};
function toggleMore(){const b=document.getElementById('more-box');b.classList.toggle('hidden');document.getElementById('more-btn').textContent=b.classList.contains('hidden')?'Show more':'Show less'}
function renderType(){const t=currentJob.job_type||'';const s=document.getElementById('jtype');if(s)s.value=t;const l=document.getElementById('jtype-label');if(l)l.textContent=t||'(not set)';
const sv=t==='Survey',L=LAYOUT[t]||LAYOUT.Install,det=document.getElementById('detail'),mb=document.getElementById('more-box'),before=document.getElementById('sec-admin');
['help','links','type','survey','status','log','photos','mat','time','clock','start','pn','done','omw','review','check','ab','map','follow','hub','private','sig','finish'].forEach(k=>{const e=document.getElementById('sec-'+k);e.classList.toggle('hidden',!L.show.includes(k)&&!L.more.includes(k));det.appendChild(e)});
L.show.forEach(k=>det.insertBefore(document.getElementById('sec-'+k),before));L.more.forEach(k=>mb.appendChild(document.getElementById('sec-'+k)));
if(sv){const m=document.getElementById('sec-map');m.classList.remove('hidden');document.getElementById('sv-mapslot').appendChild(m)}
det.insertBefore(document.getElementById('sec-more'),before);document.getElementById('sec-more').classList.toggle('hidden',!L.more.length);mb.classList.add('hidden');document.getElementById('more-btn').textContent='Show more';
document.getElementById(sv?'sv-slot':'ab-slot').appendChild(document.getElementById('markup'));document.getElementById('mk-title').textContent=sv?'4. Devices and Map':'Plan and Devices';
document.getElementById('log-title').textContent={Survey:'Notes',Service:'Problem & What Was Done','Office Task':'Notes'}[t]||'Notes & Updates';renderSteps(true);
document.getElementById('mat-title').textContent=t==='Service'?'Parts Used':'Materials';document.getElementById('mat-new').placeholder=t==='Service'?'Add part (e.g. 1x 4TB hard drive)':'Add material (e.g. 2x Maglock)';
document.getElementById('mat-legacy').textContent=currentJob.materials?'Earlier note: '+currentJob.materials:'';
document.getElementById('punch-wrap').classList.toggle('hidden',t==='Office Task'||t==='Survey');
const f=document.getElementById('finish');f.textContent=sv?'Complete Survey':t==='Office Task'?'Done':'Finish Job';f.classList.toggle('hidden',sv&&currentJob.status==='completed');mapSummary();renderDevices();if(!sv&&t!=='Office Task')resize()}
async function setType(t){if(!t)return;const r=await post('/type',{job_type:t});if(r.ok){const d=await r.json();currentJob.job_type=d.job_type;renderType()}else alert('Error')}
function addPunch(){const n=document.getElementById('punch-new');const t=n.value.trim();if(!t)return;(currentJob.punch_list=currentJob.punch_list||[]).push({text:t,done:false});n.value='';renderTicks('punch',currentJob.punch_list,savePunch,true);savePunch()}
async function assignMe(){const r=await post('/reassign',{tech:{{ name|tojson }}});if(r.ok){currentJob.tech={{ name|tojson }};const rt=document.getElementById('r-tech');if(rt)rt.value=currentJob.tech;alert('Assigned to you')}else alert('Error')}
async function copyJob(){const id=prompt('New Job ID (leave blank to auto-number)','');if(id===null)return;const sd=prompt('Scheduled date (YYYY-MM-DD, blank = today)','');if(sd===null)return;
const r=await post('/copy',{id:id.trim(),scheduled_date:sd.trim()});const d=await r.json().catch(()=>({}));if(r.ok){alert('Copied as '+d.id);openJob(d.id)}else alert(d.error||'Error')}
async function renderMR(){const b=document.getElementById('mr-box'),j=currentJob;if(!b||!j)return;if(j.job_type==='Office Task'){b.classList.add('hidden');return}b.classList.remove('hidden');
const u='/job/'+encodeURIComponent(j.id)+'/materials';b.innerHTML='<div style="font-weight:800;font-size:1.2rem">Materials from ADI</div><a class="big-btn" href="'+u+'">Request materials</a>';
let L=[];try{const r=await fetch('/api/jobs/'+encodeURIComponent(j.id)+'/material-requests');if(r.ok)L=(await r.json()).requests||[]}catch(e){}if(currentJob!==j)return;
b.innerHTML='<div style="font-weight:800;font-size:1.2rem">Materials from ADI</div>'+L.map(m=>'<div class="mr-st" style="border-top:1px solid var(--border);padding:8px 0"><div style="font-weight:700">'+esc(m.rid)+' &middot; '+esc(m.status_line)+'</div><div class="muted" style="font-size:.9rem">'+esc(m.items.map(i=>i.qty+' x '+(i.desc||i.part)).join(', '))+'</div>'+(m.can_pickup?'<a class="big-btn green" href="/materials/'+m.id+'/pickup">I picked it up</a>':'')+(OFFICE?'<a class="big-btn gray" href="/orders/'+m.id+'">Open order</a>':'')+'</div>').join('')+'<a class="big-btn" href="'+u+'">Request materials</a>'}
async function openJob(id){const r=await fetch('/api/jobs/'+encodeURIComponent(id));if(!r.ok){alert('Job not found');return}currentJob=await r.json();history.replaceState(null,'','/?job='+encodeURIComponent(currentJob.id));
guided=true;renderPhotos();renderBlueprints();renderHelp();renderClock();document.getElementById('support-note').value='';loadSupport();document.getElementById('help-form').classList.add('hidden');document.getElementById('help-note').value='';renderMats();renderTotals();fillSurvey();renderTicks('punch',currentJob.punch_list||[],savePunch,true);document.getElementById('punch-new').value='';document.getElementById('status').value=currentJob.status||'assigned';document.getElementById('hours').value='';
const pn=document.getElementById('private-notes');if(pn)pn.value=currentJob.private_notes||'';document.getElementById('print').href='/print/job/'+encodeURIComponent(currentJob.id);
document.getElementById('d-id').textContent=currentJob.id;document.getElementById('d-title').textContent=currentJob.title;
document.getElementById('d-customer').textContent=currentJob.customer;document.getElementById('d-address').textContent=currentJob.address;
document.getElementById('d-scope').textContent=currentJob.scope||'';document.getElementById('d-created').textContent=currentJob.created_by?'Created by '+currentJob.created_by:'';renderChecklist();loadUpdates();loadFollow();renderHub();renderReview();renderMR();document.getElementById('sec-omw').style.display=['completed','review'].includes(currentJob.status)?'none':'';const rt=document.getElementById('r-tech');if(rt)rt.value=currentJob.tech||'';document.getElementById('update-note').value='';
const c=document.getElementById('call');const ph=(currentJob.phone||'').replace(/[^0-9+]/g,'');if(ph){c.href='tel:'+ph;c.textContent='Call Customer ('+currentJob.phone+')';c.style.display='block'}else{c.removeAttribute('href');c.style.display='none'}
document.getElementById('history').href='/clients?name='+encodeURIComponent(currentJob.customer||'');
document.getElementById('directions').href='https://maps.google.com/?q='+encodeURIComponent(currentJob.address);
document.getElementById('home').classList.add('hidden');document.getElementById('detail').classList.remove('hidden');window.scrollTo(0,0);renderType();clearSig()}
function showList(){history.replaceState(null,'',VIEW==='jobs'?'/jobs':'/');document.getElementById('detail').classList.add('hidden');document.getElementById('home').classList.remove('hidden');load()}
async function flushNote(){const n=document.getElementById('update-note');if(!n.value.trim())return true;
const r=await (window.foSend||fetch)('/api/jobs/'+encodeURIComponent(currentJob.id)+'/updates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:n.value})},'Note for '+currentJob.id);if(r.queued){n.value='';return true}if(r.ok){n.value='';loadUpdates();return true}alert('Error saving note');return false}
document.getElementById('finish').onclick=async()=>{const fd=new FormData();
fd.append('notes',currentJob.notes||'');
if(currentJob.job_type==='Survey'){if(!(await saveSurvey(true)))return;if(!(await flushNote()))return;const r=await fetch('/api/jobs/'+encodeURIComponent(currentJob.id)+'/complete',{method:'POST',body:fd});if(r.ok){alert('Survey complete. Kelly has been told it is ready for an estimate.');openJob(currentJob.id)}else alert('Error');return}
if(!(await flushNote()))return;
fd.append('materials',currentJob.materials||'');
fd.append('hours',document.getElementById('hours').value||0);fd.append('miles',0);
fd.append('checklist',JSON.stringify(currentJob.checklist||[]));fd.append('signature',canvas.toDataURL());
const r=await fetch('/api/jobs/'+currentJob.id+'/complete',{method:'POST',body:fd});
if(r.ok){const d=await r.json().catch(()=>({}));alert(currentJob.job_type==='Office Task'?'Task done':d.status==='review'?'Job finished. The office will review it.':'Job finished');showList()}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}};
async function addJob(){const v=k=>document.getElementById('n-'+k).value.trim();
const body={id:v('id'),title:v('title'),customer:v('customer'),company_id:v('company_id'),address:v('address'),phone:v('phone'),tech:v('tech'),scope:v('scope'),scheduled_date:v('date'),checklist:v('checklist'),job_type:v('type')};
if(!body.id||!body.title){alert('Job ID and title are required');return}
if(!body.customer){alert('Pick or type a customer');return}
const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(r.ok){try{localStorage.setItem('fo_last_tech',body.tech);localStorage.setItem('fo_last_type',body.job_type)}catch(e){}
['id','title','customer','address','phone','scope','date','checklist'].forEach(k=>document.getElementById('n-'+k).value='');document.getElementById('n-title').dataset.auto='';location.href='/?job='+encodeURIComponent(body.id)}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}}
{const td=document.getElementById('today-date');if(td)td.textContent=new Date().toLocaleDateString([], {weekday:'long',month:'long',day:'numeric'})}
if(VIEW==='new'){tplOpts();quickFill().then(()=>{const QS=new URLSearchParams(location.search);['customer','address'].forEach(k=>{const v=QS.get(k);if(v)document.getElementById('n-'+k).value=v});const cid=QS.get('company_id');if(cid){document.getElementById('n-company_id').value=cid;const sel=document.getElementById('n-customer-pick');if(sel){sel.value=cid;pickCompany(sel)}}else if(QS.get('customer'))pickClient();const qt=QS.get('t');if(qt)quickType(qt);else document.getElementById('n-id').focus()})}else load();
const qj=new URLSearchParams(location.search).get('job');if(qj)openJob(qj);
</script></body></html>'''

@app.route('/')
def index(view='today'):
    techs=[(n,n) for n in sub_logins()]+office_assignees()
    tpls=[]
    if is_office():
        conn=get_db(); tpls=[{'id':r['id'],'name':r['name'],'job_type':r['job_type'] or '','items':json.loads(r['items'] or '[]')} for r in conn.execute('SELECT * FROM checklist_templates ORDER BY name').fetchall()]; conn.close()
    agr=None; low_n=0
    if is_office():
        conn=get_db(); low_n=len(low_stock(conn)); conn.close()
    if not is_office():
        conn=get_db(); a=current_agreement(conn); agr={'version':a['version'],'signed':signed_current(conn, me())}; conn.close()
    if view=='today' and not is_office(): view='home'
    return render_template_string(HTML, view=view, safety_items=SAFETY_ITEMS, def_tpl=DEFAULT_TEMPLATE, agr=agr, name=me(), office=is_office(), techs=techs, statuses=STATUSES, job_types=JOB_TYPES, obstacles=SURVEY_OBSTACLES, dev_types=DEVICE_TYPES, tpls=tpls, low_n=low_n)

@app.route('/new-job')
def new_job_page():
    if not is_office(): return redirect('/')
    return index(view='new')

@app.route('/history')
def job_history():
    if not is_office(): return redirect('/')
    conn=get_db(); rows=conn.execute("SELECT id,title,customer,job_type,tech,completed_at FROM jobs WHERE status='completed' ORDER BY completed_at DESC LIMIT 500").fetchall(); conn.close()
    q=(request.args.get('q') or '').strip().lower()
    if q: rows=[r for r in rows if q in ' '.join(str(r[k] or '') for k in ('id','title','customer','tech','job_type')).lower()]
    body='<form class="row"><input name="q" value="%s" placeholder="Search history"><button class="big-btn gray" style="flex:0 0 100px">Search</button></form>'%h(request.args.get('q') or '')
    last=''
    for r in rows:
        m=(r['completed_at'] or '')[:7]
        if m!=last:
            last=m
            try: body+='<h2>%s</h2>'%datetime.strptime(m,'%Y-%m').strftime('%B %Y')
            except ValueError: body+='<h2>No date</h2>'
        body+='<a class="card" href="/?job=%s"><b>%s</b> <span class="muted">%s</span><div>%s</div><div class="muted">%s%s</div></a>'%(urllib.parse.quote(r['id']),h(r['customer'] or r['title']),h(r['id']),h(r['title']),h(r['job_type'] or 'Job'),(' &middot; '+h(r['tech'])) if r['tech'] else '')
    if not rows: body+='<div class="muted">No completed jobs yet.</div>'
    return ops_page('Job History', body)

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
    sd = (d.get('scheduled_date') or '').strip() or today_local().isoformat()
    jt = (d.get('job_type') or '').strip(); jt = jt if jt in JOB_TYPES else 'Install'
    if not lines:
        t = conn.execute('SELECT items FROM checklist_templates WHERE LOWER(name)=? ORDER BY id LIMIT 1',(DEFAULT_TEMPLATE.get(jt,'').lower(),)).fetchone()
        try: lines = [x for x in json.loads(t['items']) if str(x).strip()] if t else []
        except Exception: lines = []
    chk = json.dumps([{'text': x, 'done': False} for x in (lines or (DEFAULT_CHECKLIST if jt not in NO_CLOSING_STEPS else []))])
    tech = nice_name(d.get('tech')) or default_sub()
    if not tech: conn.close(); return jsonify({'error':'Pick a sub for this job'}),400
    customer=(d.get('customer') or '').strip()
    company_id=None
    raw_cid=d.get('company_id')
    if raw_cid not in (None,''):
        try: company_id=int(raw_cid)
        except (TypeError,ValueError): company_id=None
    try:
        import fieldops_platform as _plat
        if company_id:
            co=conn.execute('SELECT * FROM companies WHERE id=?',(company_id,)).fetchone()
            if not co: conn.close(); return jsonify({'error':'Customer not found'}),400
            customer=co['name']
        elif customer:
            co=_plat.get_company_by_key(conn, _plat.nkey(customer))
            if co:
                company_id=co['id']
                customer=co['name']
    except Exception as e:
        app.logger.warning('resolve company failed: %s', e)
    conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,phone,checklist,scheduled_date,created_by,job_type,company_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (jid,title,customer,(d.get('address') or '').strip(),(d.get('scope') or '').strip(),'assigned',tech,datetime.utcnow().isoformat(),(d.get('phone') or '').strip(),chk,sd,me(),jt,company_id))
    try:
        import fieldops_platform as _plat
        _plat.link_job(conn, conn.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone(), company_id=company_id)
    except Exception as e:
        app.logger.warning('link job failed: %s', e)
    conn.commit(); conn.close()
    push_to_sub(tech,'New job: '+title,(d.get('customer') or '').strip()+' - '+nice_day(sd),'/?job='+jid)
    return jsonify({'ok':True,'id':jid}),201

@app.route('/api/quick')
def quick_info():
    if not is_office(): return jsonify({'error':'office only'}),403
    conn=get_db(); y=today_local().year; pre='TS-%d-'%y; top=0
    for r in conn.execute('SELECT id FROM jobs WHERE id LIKE ?',(pre+'%',)):
        try: top=max(top,int(r['id'][len(pre):]))
        except ValueError: pass
    cl=[]
    try:
        for r in conn.execute("SELECT id,name,billing_address,phone FROM companies WHERE IFNULL(archived,0)=0 ORDER BY name"):
            cl.append({'id':r['id'],'name':r['name'],'address':r['billing_address'] or '','phone':r['phone'] or ''})
    except Exception:
        cl=[{'id':None,'name':r['name'],'address':r['address'] or '','phone':r['phone'] or ''} for r in conn.execute('SELECT name,address,phone FROM clients ORDER BY name')]
    conn.close(); return jsonify({'next_id':pre+'%04d'%(top+1),'clients':cl,'today':today_local().isoformat()})

@app.route('/api/jobs/<job_id>/type', methods=['POST'])
def set_job_type(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    jt=((request.get_json(silent=True) or {}).get('job_type') or '').strip()
    if jt not in JOB_TYPES: return jsonify({'error':'bad type'}),400
    conn=get_db(); n=conn.execute('UPDATE jobs SET job_type=? WHERE id=?',(jt,job_id)).rowcount
    conn.commit(); conn.close()
    return (jsonify({'ok':True,'job_type':jt}) if n else ('',404))

def save_ticks(job_id, col, key):
    if col not in ('punch_list',):
        return jsonify({'error':'bad field'}),400
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
    conn=get_db(); photos=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto','daily','matreq','ticket') ORDER BY id",(job_id,)).fetchall()]; bps=blueprints(conn, job_id); plans=plan_files(conn, job_id); srcs=survey_plan_sources(conn, row)
    extra={}
    if is_office():
        n,c=(row['hub_nonce'],row['hub_created_at']) if row['hub_nonce'] and row['hub_created_at'] else hub_new(conn, job_id)
        extra=hub_info(job_id,n,c); extra['labor']=job_labor(conn,row)
    extra['clock']=clock_state(conn, job_id); extra['steps']=job_steps(conn, row); extra['flow']=flow_state(conn, row)
    conn.close()
    conn=get_db(); extra['plan_info']=plan_info(conn, job_id); conn.close()
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
    if needs_review(row) and not is_office():
        cur=conn.execute('SELECT checklist FROM jobs WHERE id=?',(job_id,)).fetchone()['checklist']
        left=sum(1 for i in parse_checklist(cur, row['job_type']) if not i['done'])
        pics=job_photo_count(conn, job_id)+sum(1 for t in PHOTO_TYPES for f in request.files.getlist(t) if f and f.filename)
        why=done_block(left, pics)
        if why: conn.close(); return jsonify({'error':why}),400
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
    pl=conn.execute('SELECT * FROM service_plans WHERE id=?',(j['plan_id'],)).fetchone() if j['plan_id'] else None
    if pl:
        pkg['service_plan']={'id':pl['id'],'title':pl['title'],'price':pl['price'] or 0,'every_months':pl['every_months']}
        pkg['line_items']=[{'description':(pl['title'] or 'Service plan visit')+' - visit '+(j['scheduled_date'] or ''),'qty':1,'rate':pl['price'] or 0,'amount':pl['price'] or 0}]
    pkg['material_requests']=[mr_dict(r, True) for r in conn.execute("SELECT * FROM material_requests WHERE job_id=? AND status!='canceled' ORDER BY id",(job_id,))]
    pkg['material_lines']=mr_job_lines(conn, job_id)
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
    photos=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto','daily','matreq','ticket') ORDER BY id",(job_id,)).fetchall()]; conn.close()
    return jsonify({'ok':True,'photos':photos})

def blueprints(conn, job_id):
    out=[]
    for b in conn.execute("SELECT filename,orig_name FROM photos WHERE job_id=? AND photo_type='blueprint' ORDER BY id",(job_id,)).fetchall():
        ext=os.path.splitext(b['filename'])[1].lower()
        out.append({'filename':b['filename'],'name':b['orig_name'] or b['filename'],'image':ext in ('.png','.jpg','.jpeg','.webp')})
    return out

def plan_files(conn, job_id):
    files=[r['filename'] for r in conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type='plan' ORDER BY id",(job_id,)).fetchall()]
    try:
        import fieldops_areas as _areas
        return _areas.ordered_files(conn, job_id, files)
    except Exception:
        return files

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
        nm=pm_next_name(conn, job_id)
        conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at,orig_name) VALUES (?,?,?,?,?)',(job_id,fn,'plan',now,os.path.basename(f.filename)[:200]))
        pm_add(conn, job_id, fn, nm, os.path.basename(f.filename)[:200])
    conn.commit(); plans=plan_files(conn, job_id); conn.close()
    return jsonify({'ok':True,'plans':plans})

@app.route('/api/jobs/<job_id>/plans/delete', methods=['POST'])
def delete_plan(job_id):
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    fn=secure_filename(str((request.get_json(silent=True) or {}).get('filename') or ''))
    if not fn or fn not in plan_files(conn, job_id): conn.close(); return ('',404)
    conn.execute("DELETE FROM photos WHERE job_id=? AND filename=? AND photo_type='plan'",(job_id,fn)); conn.execute('DELETE FROM plan_meta WHERE filename=?',(fn,))
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
        conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at,orig_name) VALUES (?,?,?,?,?)',(job_id,nf,'plan',now,'From survey '+src_id)); pm_copy(conn, fn, nf, job_id); mp[fn]=nf
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
        last=conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','matreq','ticket') ORDER BY id DESC LIMIT 1",(job_id,)).fetchone()
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
        last=conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','matreq','ticket') ORDER BY id DESC LIMIT 1",(job_id,)).fetchone()
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
    fns=[r['filename'] for r in conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto','daily','matreq','ticket') ORDER BY id",(job_id,)).fetchall()]
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
{% if office %}<a class="btn" id="sys-link" href="/systems?client={{ client|urlencode }}">Installed systems and open issues</a>{% endif %}<a class="btn" href="/print/client?name={{ client|urlencode }}" target="_blank">Print Summary</a>
{% if office %}<a class="btn" href="/clients/remove?name={{ client|urlencode }}" style="display:block;text-align:center;text-decoration:none;box-sizing:border-box;border:1px solid #b00020;color:#b00020;background:#fff">Remove company record</a>{% endif %}
<div class="folder">Active Jobs</div>{% for j in active %}{{ jobcard(j, true) }}{% else %}<div class="muted" style="margin-bottom:12px">None</div>{% endfor %}
<div class="folder">Completed Surveys</div>{% for j in surveys %}{{ jobcard(j, false) }}{% else %}<div class="muted" style="margin-bottom:12px">None</div>{% endfor %}
<div class="folder">Completed Jobs</div>{% for j in done %}{{ jobcard(j, false) }}{% else %}<div class="muted">None</div>{% endfor %}
{% endif %}
</body></html>'''

def ckey(name): return ' '.join((name or '').split()).lower()

SUB_DOCS = [('insurance', 'Insurance')]  # license rows stay in the DB but are hidden
def sub_names(conn):
    n=sub_logins()
    for r in conn.execute("SELECT DISTINCT TRIM(tech) t FROM jobs WHERE status!='completed' AND IFNULL(TRIM(tech),'')!=''").fetchall():
        if ckey(r['t']) not in [x.lower() for x in n] and ckey(r['t']) not in OFFICE_NAMES: n.append(nice_name(r['t']))
    return n
def doc_alerts(conn):
    soon=(today_local()+timedelta(days=30)).isoformat(); t=today_local().isoformat(); out=[]
    names={x.lower() for x in sub_names(conn)}
    for r in conn.execute("SELECT * FROM sub_docs WHERE IFNULL(expires,'')!='' AND expires<=? ORDER BY expires",(soon,)).fetchall():
        if r['sub'] in names and r['kind'] in dict(SUB_DOCS): out.append({'sub':r['sub'].title(),'kind':dict(SUB_DOCS)[r['kind']],'expires':r['expires'],'expired':r['expires']<t,'missing':False})
    have={(r['sub'],r['kind']) for r in conn.execute("SELECT sub,kind FROM sub_docs WHERE IFNULL(filename,'')!=''").fetchall()}
    for n in sub_names(conn):
        for k,l in SUB_DOCS:
            if (n.lower(),k) not in have: out.append({'sub':n,'kind':l,'expires':'','expired':False,'missing':True})
    return out

def sub_companies(conn): return {r['key']:r['company'] for r in conn.execute("SELECT key,company FROM subs WHERE IFNULL(company,'')!=''").fetchall()}

def tech_has_client(conn, customer):
    return any(can_see(r) for r in conn.execute('SELECT tech FROM jobs WHERE LOWER(TRIM(customer))=?',(ckey(customer),)).fetchall())

from markupsafe import escape as _esc, Markup
def h(v): return str(_esc('' if v is None else v))
# ---------- Device symbols (vector, drawn in code) and camera coverage ----------
DEV_SYM = {
 'Dome Cam':'<path d="M5 14a7 7 0 0 1 14 0z"/><path d="M3.5 14h17"/><circle cx="12" cy="11.5" r="1.6"/>',
 'Bullet Cam':'<path d="M3.5 8.5h11v6h-11z"/><path d="M14.5 9.5l5-2.5v9l-5-2.5"/><path d="M7 14.5V19h4"/>',
 'PTZ Cam':'<circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1.3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/>',
 'Card Reader':'<rect x="7" y="3" width="10" height="18" rx="1.5"/><path d="M9.5 7h5"/><circle cx="12" cy="14" r="2"/>',
 'Door Contact':'<rect x="3.5" y="7" width="7" height="10" rx="1"/><rect x="13.5" y="7" width="7" height="10" rx="1"/><path d="M10.5 12h3"/>',
 'Motion':'<path d="M7.5 5.5a9 9 0 0 0 0 13M16.5 5.5a9 9 0 0 1 0 13M10 8.5a4.5 4.5 0 0 0 0 7M14 8.5a4.5 4.5 0 0 1 0 7"/><circle cx="12" cy="12" r="1.2"/>',
 'Keypad':'<rect x="6" y="3" width="12" height="18" rx="1.5"/><path d="M9.5 8h.01M12 8h.01M14.5 8h.01M9.5 11.5h.01M12 11.5h.01M14.5 11.5h.01M9.5 15h.01M12 15h.01M14.5 15h.01" stroke-width="2.6"/>',
 'Panel':'<rect x="4" y="4" width="16" height="16" rx="1"/><path d="M7.5 8.5h9M7.5 12h9M7.5 15.5h5"/>',
 'NVR':'<rect x="2.5" y="8" width="19" height="8" rx="1"/><path d="M6 12h.01M9 12h.01" stroke-width="2.6"/><path d="M13 12h5.5"/>',
 'Intercom':'<rect x="6.5" y="2.5" width="11" height="19" rx="1.5"/><path d="M9.5 6.5h5M9.5 9h5M9.5 11.5h5"/><circle cx="12" cy="16.5" r="1.8"/>',
 'WAP':'<path d="M4 10a11 11 0 0 1 16 0M7 13.2a6.5 6.5 0 0 1 10 0M10 16.3a2.5 2.5 0 0 1 4 0"/><path d="M12 19.5h.01" stroke-width="3"/>',
 'Data Drop':'<path d="M12 4l8.5 15h-17z"/><path d="M12 10v4.5"/>',
 'Speaker':'<path d="M4 9.5h4l5-4.5v14l-5-4.5H4z"/><path d="M16.5 9a4.5 4.5 0 0 1 0 6M19 6.5a8 8 0 0 1 0 11"/>',
 'TV':'<rect x="2.5" y="5" width="19" height="12" rx="1"/><path d="M8 21h8M12 17v4"/>',
}
CAM_DEF = {'Dome Cam': 100, 'Bullet Cam': 60, 'PTZ Cam': 90}
CAM_RANGE = 15  # percent of plan width, used when the plan has no scale
CAM_FT = {'Dome Cam': 30, 'Bullet Cam': 60, 'PTZ Cam': 100}  # default range in feet once the plan is scaled
def cam_view(d, ppf=None, W=None):
    v = {'dir': d.get('dir', 0) or 0, 'fov': d.get('fov') or CAM_DEF.get(d['type'], 90), 'range': d.get('range') or CAM_RANGE, 'ft': None}
    if ppf and W:
        v['ft'] = d.get('rft') or CAM_FT.get(d['type'], 30); v['range'] = v['ft'] * ppf / W * 100
    return v

def dev_icon(t, color, solid=True, size=34, label=None):
    """Self-contained SVG marker: ring + symbol. Solid = installed / survey, white = planned."""
    sym = DEV_SYM.get(t, '<circle cx="12" cy="12" r="3"/>')
    bg, fg, dash = (color, '#fff', '') if solid else ('#fff', color, ' stroke-dasharray="3 2"')
    return ('<svg class="dsym" viewBox="0 0 24 24" width="%d" height="%d" aria-label="%s"><circle cx="12" cy="12" r="11" fill="%s" stroke="%s" stroke-width="1.6"%s/>'
            '<g transform="translate(12 12) scale(.66) translate(-12 -12)" fill="none" stroke="%s" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">%s</g></svg>') % (
            size, size, _esc(label or t), bg, color if not solid else '#fff', dash, fg, sym)

_IMG_SIZE = {}
def img_size(fn):
    """Width/height of a plan image from its header (PNG, JPEG, GIF, WEBP). No extra libraries."""
    if fn in _IMG_SIZE: return _IMG_SIZE[fn]
    wh = (1000, 1000)
    try:
        with open(os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(fn)), 'rb') as f: b = f.read(65536)
        import struct
        if b[:8] == b'\x89PNG\r\n\x1a\n': wh = struct.unpack('>II', b[16:24])
        elif b[:6] in (b'GIF87a', b'GIF89a'): wh = struct.unpack('<HH', b[6:10])
        elif b[:4] == b'RIFF' and b[8:12] == b'WEBP':
            if b[12:16] == b'VP8X': wh = (1 + int.from_bytes(b[24:27], 'little'), 1 + int.from_bytes(b[27:30], 'little'))
            elif b[12:16] == b'VP8 ': wh = (struct.unpack('<H', b[26:28])[0] & 0x3fff, struct.unpack('<H', b[28:30])[0] & 0x3fff)
            elif b[12:16] == b'VP8L':
                v = int.from_bytes(b[21:25], 'little'); wh = ((v & 0x3fff) + 1, ((v >> 14) & 0x3fff) + 1)
        elif b[:2] == b'\xff\xd8':
            i = 2
            while i < len(b) - 9:
                if b[i] != 0xFF: i += 1; continue
                m = b[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h_, w_ = struct.unpack('>HH', b[i + 5:i + 9]); wh = (w_, h_); break
                i += 2 + struct.unpack('>H', b[i + 2:i + 4])[0]
    except Exception: pass
    wh = (wh[0] or 1000, wh[1] or 1000); _IMG_SIZE[fn] = wh; return wh

def cone_path(cx, cy, r, d, fov):
    if fov >= 360: return 'M%.1f %.1fm-%.1f 0a%.1f %.1f 0 1 0 %.1f 0a%.1f %.1f 0 1 0 -%.1f 0z' % (cx, cy, r, r, r, 2 * r, r, r, 2 * r)
    a1, a2 = math.radians(d - fov / 2), math.radians(d + fov / 2)
    x1, y1 = cx + r * math.sin(a1), cy - r * math.cos(a1); x2, y2 = cx + r * math.sin(a2), cy - r * math.cos(a2)
    return 'M%.1f %.1fL%.1f %.1fA%.1f %.1f 0 %d 1 %.1f %.1fZ' % (cx, cy, x1, y1, r, r, 1 if fov > 180 else 0, x2, y2)

def plan_svg(fn, devs, ab=False, cones=True):
    """One vector overlay for print and client views: coverage cones, symbols and labels."""
    W, H = img_size(fn); s = W * 0.034; out = []; ppf = plan_scale_of(fn)[1] if cones else None
    colors = {n: c for n, p, c in DEVICE_TYPES}
    ds = [d for d in devs if d.get('plan') == fn]
    if cones:
        for d in ds:
            if d['type'] not in CAM_DEF: continue
            v = cam_view(d, ppf, W); col = colors[d['type']]; cx, cy = d['x'] * W / 100, d['y'] * H / 100
            out.append('<path d="%s" fill="%s" fill-opacity=".14" stroke="%s" stroke-opacity=".7" stroke-width="%.1f"%s/>' % (
                cone_path(cx, cy, v['range'] * W / 100, v['dir'], v['fov']), col, col, W * 0.002, ' stroke-dasharray="%.1f %.1f"' % (W * .008, W * .006) if d['type'] == 'PTZ Cam' else ''))
    for d in ds:
        col = colors[d['type']]; cx, cy = d['x'] * W / 100, d['y'] * H / 100; solid = not (ab and not d.get('installed'))
        out.append('<svg x="%.1f" y="%.1f" width="%.1f" height="%.1f" viewBox="0 0 24 24" overflow="visible">%s</svg>' % (cx - s / 2, cy - s / 2, s, s, dev_icon(d['type'], col, solid).split('>', 1)[1].rsplit('</svg>', 1)[0]))
        lab = _esc(d.get('label') or '')
        lf = d['x'] > 82
        out.append('<text x="%.1f" y="%.1f" font-size="%.1f" font-weight="700" font-family="Arial,sans-serif" fill="#111" stroke="#fff" stroke-width="%.1f" paint-order="stroke" dominant-baseline="middle" text-anchor="%s">%s</text>' % (cx - s * .62 if lf else cx + s * .62, cy, s * .62, s * .2, 'end' if lf else 'start', lab))
    return Markup('<svg class="plan-svg" viewBox="0 0 %d %d" preserveAspectRatio="none" style="position:absolute;inset:0;width:100%%;height:100%%;pointer-events:none;-webkit-print-color-adjust:exact;print-color-adjust:exact">%s</svg>' % (W, H, ''.join(out)))
app.jinja_env.globals.update(DEV_SYM=DEV_SYM, CAM_DEF=CAM_DEF, CAM_RANGE=CAM_RANGE, CAM_FT=CAM_FT, plan_svg=plan_svg, dev_icon=lambda t, c, solid=True, size=28: Markup(dev_icon(t, c, solid, size)))
# ---------- end device symbols ----------

OPS_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }}</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,Arial,sans-serif;background:#fff;color:#111;max-width:760px;margin:auto;padding:14px;overflow-wrap:anywhere}
h1{font-size:1.4rem;margin:8px 0}h2{font-size:1.15rem;color:#a49f2a;border-bottom:2px solid #a49f2a;margin:22px 0 8px}
.big-btn{display:block;width:100%;text-align:center;padding:16px;border:0;border-radius:10px;background:#a49f2a;color:#fff;font-size:1.1rem;font-weight:700;text-decoration:none;margin:6px 0}
.big-btn.gray{background:#eee;color:#111}.big-btn.red{background:#c62828}.row{display:flex;gap:8px;flex-wrap:wrap}.row>*{flex:1 1 140px}
input,select,textarea{width:100%;padding:13px;border:1px solid #ccc;border-radius:10px;font-size:1rem;margin:4px 0;background:#fff;font-family:inherit}
label{display:block;font-weight:600;margin-top:6px}.card{border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin:8px 0;background:#fff}
a.card{display:block;color:#111;text-decoration:none}.muted{color:#666;font-size:.9rem}.flag{background:#fdecea;color:#c62828;border-radius:8px;padding:2px 8px;font-weight:700}
.okf{background:#e8f5e9;color:#2e7d32;border-radius:8px;padding:2px 8px;font-weight:700}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #eee;padding:8px 6px;text-align:left;vertical-align:top}
th{font-size:.85rem;color:#666}td:first-child{min-width:120px}.num{text-align:right;white-space:nowrap}.msg{background:#e8f5e9;border-radius:10px;padding:10px;margin:8px 0;font-weight:600}</style></head><body>
{% if back is not sameas false %}<a href="{{ back or '/' }}" class="big-btn gray fo-back">&larr; Back</a>{% endif %}<h1>{{ title }}</h1>{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}{{ body }}</body></html>'''
def ops_page(title, body, back='/', msg=''): return render_template_string(OPS_HTML, title=title, body=Markup(body), back=back, msg=msg)
def money(v):
    try: x=float(v or 0)
    except Exception: return '$0.00'
    return ('-' if x<0 else '')+'${:,.2f}'.format(abs(x))

@app.route('/search')
def search_page():
    if not me(): return redirect('/login')
    q=(request.args.get('q') or '').strip()[:80]; office=is_office()
    form='<form action="/search"><input type="search" name="q" value="%s" placeholder="Client, address, job, note, serial" autofocus><button class="big-btn">Search</button></form>'%h(q)
    if len(q)<2: return ops_page('Search', form+'<div class="muted">Type 2 or more letters.</div>')
    like='%'+q.lower()+'%'; conn=get_db(); out=[]
    jobs={r['id']:r for r in conn.execute('SELECT * FROM jobs').fetchall() if can_see(r)}
    hits={}
    def hit(jid, why):
        if jid in jobs and why not in hits.setdefault(jid,[]): hits[jid].append(why)
    for jid,r in jobs.items():
        for col,lab in (('id','Job ID'),('title','Title'),('customer','Client'),('address','Address'),('scope','Scope'),('phone','Phone'),('survey_devices','Device')):
            if like[1:-1] in str(r[col] or '').lower(): hit(jid,lab)
        if office and like[1:-1] in str(r['private_notes'] or '').lower(): hit(jid,'Office note')
    for r in conn.execute('SELECT job_id,note FROM job_updates WHERE LOWER(note) LIKE ?',(like,)).fetchall(): hit(r['job_id'],'Note: '+str(r['note'])[:80])
    for r in conn.execute("SELECT job_id,log_date,work_done FROM daily_logs WHERE LOWER(IFNULL(work_done,'')||' '||IFNULL(materials,'')||' '||IFNULL(issues,'')) LIKE ?",(like,)).fetchall(): hit(r['job_id'],'Daily log '+(r['log_date'] or ''))
    for f in SEARCH_HOOKS: f(conn, like, hit, out, office)
    cl=[]
    if office:
        cl=conn.execute("SELECT name,address,phone FROM clients WHERE LOWER(name||' '||IFNULL(address,'')||' '||IFNULL(phone,'')) LIKE ? ORDER BY name LIMIT 30",(like,)).fetchall()
    conn.close()
    body=form
    if cl: body+='<h2>Clients</h2>'+''.join('<a class="card" href="/clients?name=%s"><b>%s</b><div class="muted">%s %s</div></a>'%(urllib.parse.quote(c['name']),h(c['name']),h(c['address'] or ''),h(c['phone'] or '')) for c in cl)
    if hits:
        body+='<h2>Jobs</h2>'+''.join('<a class="card" href="/?job=%s"><b>%s</b> <span class="muted">%s</span><div>%s</div><div class="muted">%s</div><div class="muted">Found in: %s</div></a>'%(
            urllib.parse.quote(j),h(jobs[j]['title'] or j),h(j),h(jobs[j]['customer'] or ''),h(jobs[j]['address'] or ''),h(', '.join(hits[j][:5]))) for j in sorted(hits,key=lambda k:(jobs[k]['status']=='completed',k)))
    body+=''.join(out)
    if not cl and not hits and not out: body+='<div class="card">Nothing found for "%s".</div>'%h(q)
    return ops_page('Search', body)
SEARCH_HOOKS = []

EQ_FIELDS = [('client','Client'),('site','Site address'),('device','Device'),('model','Model'),('serial','Serial'),('mac','MAC'),('ip','IP'),
             ('install_date','Install date'),('warranty_end','Warranty ends'),('asbuilt_label','As-built label'),('job_id','Job ID'),('notes','Notes')]
def warranty_tag(w):
    try: d=datetime.strptime(w,'%Y-%m-%d').date()
    except Exception: return ''
    left=(d-today_local()).days
    if left<0: return ' <span class="flag">Warranty ended</span>'
    if left<=60: return ' <span class="flag">Warranty ends in %d days</span>'%left
    return ' <span class="okf">Under warranty</span>'
def eq_card(r, office, link=True):
    bits=' &middot; '.join(h(x) for x in [r['model'] and 'Model '+r['model'], r['serial'] and 'SN '+r['serial'], r['mac'] and 'MAC '+r['mac'], r['ip'] and 'IP '+r['ip']] if x)
    meta=' &middot; '.join(h(x) for x in [r['asbuilt_label'] and 'As-built '+r['asbuilt_label'], r['install_date'] and 'Installed '+r['install_date'], r['warranty_end'] and 'Warranty to '+r['warranty_end']] if x)
    pw=(' &middot; Password: &bull;&bull;&bull;&bull;&bull;&bull;' if office and r['password'] else '')
    inner='<b>%s</b>%s<div>%s</div><div class="muted">%s%s</div>'%(h(r['device'] or 'Device'),warranty_tag(r['warranty_end']),bits,meta,pw)
    return ('<a class="card" href="/equipment/%d">%s</a>'%(r['id'],inner)) if (office and link) else '<div class="card">%s</div>'%inner

@app.route('/equipment')
def equipment_page():
    if not is_office(): return redirect('/')
    site=(request.args.get('site') or '').strip(); job=(request.args.get('job') or '').strip()
    conn=get_db(); rows=conn.execute('SELECT * FROM equipment ORDER BY LOWER(client),LOWER(site),device').fetchall()
    jrow=conn.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone() if job else None; conn.close()
    if site: rows=[r for r in rows if ckey(r['site'])==ckey(site)]
    body=''
    if jrow: body+='<form method="post" action="/equipment/import/%s"><button class="big-btn">Add devices from this job as-built</button></form>'%h(jrow['id'])
    body+='<a class="big-btn gray" href="/equipment/new?%s">+ Add Equipment</a>'%urllib.parse.urlencode({'site':site or (jrow['address'] if jrow else ''),'client':(jrow['customer'] if jrow else ''),'job_id':job})
    if site: body+='<a class="big-btn gray" href="/equipment">Show all sites</a>'
    groups={}
    for r in rows: groups.setdefault((r['client'] or '', r['site'] or ''),[]).append(r)
    for (c,st),rs in groups.items():
        body+='<h2>%s</h2><div class="muted">%s</div>'%(h(c or 'No client'),h(st))+''.join(eq_card(r,True) for r in rs)
    if not rows: body+='<div class="card">No equipment yet.</div>'
    return ops_page('Equipment'+(' at '+site if site else ''), body, back=('/?job='+urllib.parse.quote(job)) if job else '/', msg=request.args.get('msg',''))

@app.route('/equipment/<eid>', methods=['GET','POST'])
def equipment_edit(eid):
    if not is_office(): return redirect('/')
    conn=get_db(); r=conn.execute('SELECT * FROM equipment WHERE id=?',(eid,)).fetchone() if eid!='new' else None
    if eid!='new' and not r: conn.close(); return redirect('/equipment')
    if request.method=='POST':
        f=request.form
        if f.get('delete')=='1' and r:
            conn.execute('DELETE FROM equipment WHERE id=?',(eid,)); conn.commit(); conn.close(); return redirect('/equipment?msg=Deleted')
        vals={k:(f.get(k) or '').strip()[:300] for k,_ in EQ_FIELDS}
        for k in ('install_date','warranty_end'):
            if vals[k]:
                try: datetime.strptime(vals[k],'%Y-%m-%d')
                except ValueError: vals[k]=''
        pw=(f.get('password') or '')[:200]
        if r:
            if f.get('clear_pw')=='1': pw=''
            elif not pw: pw=r['password'] or ''
            conn.execute('UPDATE equipment SET %s,password=?,updated_at=? WHERE id=?'%','.join(k+'=?' for k,_ in EQ_FIELDS),tuple(vals.values())+(pw,now_local_str(),eid))
        else:
            conn.execute('INSERT INTO equipment (%s,password,created_at,updated_at) VALUES (%s)'%(','.join(k for k,_ in EQ_FIELDS),','.join('?'*(len(EQ_FIELDS)+3))),tuple(vals.values())+(pw,now_local_str(),now_local_str()))
        conn.commit(); conn.close(); return redirect('/equipment?'+urllib.parse.urlencode({'site':vals['site'],'msg':'Saved'}))
    conn.close(); src=r or request.args
    body='<form method="post">'
    for k,lab in EQ_FIELDS:
        typ='date' if k in ('install_date','warranty_end') else 'text'
        body+='<label>%s</label><input type="%s" name="%s" value="%s">'%(lab,typ,k,h((src[k] if r else src.get(k,'')) or ''))
    body+='<label>Device password (office only)</label>'
    if r and r['password']:
        if request.args.get('show')=='1': body+='<div class="card"><b>%s</b></div>'%h(r['password'])
        else: body+='<div class="card">&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull; <a href="/equipment/%s?show=1">Show</a></div>'%h(eid)
        body+='<input type="password" name="password" placeholder="Type a new password to change it" autocomplete="new-password"><label><input type="checkbox" name="clear_pw" value="1" style="width:auto"> Remove password</label>'
    else: body+='<input type="password" name="password" placeholder="Optional" autocomplete="new-password">'
    body+='<button class="big-btn">Save</button></form>'
    if r: body+='<form method="post" onsubmit="return confirm(\'Delete this device?\')"><input type="hidden" name="delete" value="1"><button class="big-btn red">Delete</button></form>'
    return ops_page('Edit Equipment' if r else 'Add Equipment', body, back='/equipment'+('?'+urllib.parse.urlencode({'site':r['site']}) if r else ''))

@app.route('/equipment/import/<job_id>', methods=['POST'])
def equipment_import(job_id):
    if not is_office(): return redirect('/')
    conn=get_db(); j=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not j: conn.close(); return redirect('/equipment')
    have={r['asbuilt_label'] for r in conn.execute('SELECT asbuilt_label FROM equipment WHERE job_id=?',(job_id,)).fetchall()}
    day=(j['completed_at'] or '')[:10] or today_local().isoformat(); n=0
    for d in parse_devices(j['survey_devices']):
        if d['label'] in have: continue
        conn.execute('INSERT INTO equipment (client,site,job_id,device,mac,ip,install_date,asbuilt_label,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (j['customer'] or '',j['address'] or '',job_id,d['type'],d['mac'],d['ip'],day,d['label'],' '.join(x for x in [d['port'] and 'Port '+d['port'], d['clabel'] and 'Cable '+d['clabel'], d['note']] if x),now_local_str(),now_local_str()))
        n+=1
    conn.commit(); conn.close()
    return redirect('/equipment?'+urllib.parse.urlencode({'site':j['address'] or '','job':job_id,'msg':'Added %d devices from the as-built'%n}))

@app.route('/job/<job_id>/equipment')
def job_equipment(job_id):
    conn=get_db(); j=page_job(conn, job_id)
    if not j: conn.close(); return redirect('/')
    if is_office(): conn.close(); return redirect('/equipment?'+urllib.parse.urlencode({'site':j['address'] or '','job':job_id}))
    rows=[r for r in conn.execute('SELECT * FROM equipment ORDER BY device').fetchall() if j['address'] and ckey(r['site'])==ckey(j['address'])]; conn.close()
    body=''.join(eq_card(r,False) for r in rows) or '<div class="card">No equipment saved for this site yet.</div>'
    return ops_page('Equipment at this site', '<div class="muted">%s</div>'%h(j['address'] or '')+body, back='/?job='+urllib.parse.quote(job_id))

def eq_search(conn, like, hit, out, office):
    rows=conn.execute("SELECT * FROM equipment WHERE LOWER(IFNULL(device,'')||' '||IFNULL(model,'')||' '||IFNULL(serial,'')||' '||IFNULL(mac,'')||' '||IFNULL(ip,'')||' '||IFNULL(asbuilt_label,'')) LIKE ? LIMIT 40",(like,)).fetchall()
    if office:
        if rows: out.append('<h2>Equipment</h2>'+''.join(eq_card(r,True) for r in rows))
    else:
        for r in rows:
            if r['job_id']: hit(r['job_id'],'Equipment: '+(r['device'] or '')+' '+(r['serial'] or ''))
SEARCH_HOOKS.append(eq_search)

PLAN_LEAD_DAYS = 14
def add_months(d, m):
    y, mo = divmod(d.month - 1 + m, 12); y += d.year; mo += 1
    import calendar as _c
    return d.replace(year=y, month=mo, day=min(d.day, _c.monthrange(y, mo)[1]))
def run_plans(conn):
    made=[]; horizon=(today_local()+timedelta(days=PLAN_LEAD_DAYS)).isoformat()
    for p in conn.execute("SELECT * FROM service_plans WHERE active=1 AND IFNULL(next_date,'')!='' AND next_date<=?",(horizon,)).fetchall():
        try: nd=datetime.strptime(p['next_date'],'%Y-%m-%d').date()
        except ValueError: continue
        jid='SP%d-%s'%(p['id'],nd.strftime('%Y%m%d'))
        if not conn.execute('SELECT 1 FROM jobs WHERE id=?',(jid,)).fetchone():
            try: lines=[x for x in json.loads(p['checklist'] or '[]') if str(x).strip()]
            except Exception: lines=[]
            if not lines:
                t=conn.execute('SELECT items FROM checklist_templates WHERE LOWER(name)=? ORDER BY id LIMIT 1',(DEFAULT_TEMPLATE.get('Service','').lower(),)).fetchone()
                try: lines=[x for x in json.loads(t['items']) if str(x).strip()] if t else []
                except Exception: lines=[]
            tech=nice_name(p['tech']) or default_sub()
            conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,phone,checklist,scheduled_date,created_by,job_type,sale_amount,plan_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (jid,(p['title'] or 'Service visit'),p['client'] or '',p['site'] or '','Service plan visit (every %d months)'%(p['every_months'] or 12),'assigned',tech,datetime.utcnow().isoformat(),p['phone'] or '',
                          json.dumps([{'text':x,'done':False} for x in (lines or DEFAULT_CHECKLIST)]),nd.isoformat(),'Service Plan','Service',p['price'] or None,p['id']))
            made.append((tech,p['title'] or 'Service visit',p['client'] or '',nd.isoformat(),jid))
        conn.execute('UPDATE service_plans SET next_date=?,last_job_id=? WHERE id=?',(add_months(nd,max(1,p['every_months'] or 12)).isoformat(),jid,p['id']))
    conn.commit()
    for tech,title,client,sd,jid in made: push_to_sub(tech,'New job: '+title,client+' - '+nice_day(sd),'/?job='+jid)
    return len(made)
_PLANS_AT=[0.0]
@app.before_request
def plans_tick():
    if time.time()-_PLANS_AT[0]<600 or request.path.startswith(('/static/','/sw.js','/healthz')): return
    _PLANS_AT[0]=time.time()
    try:
        conn=get_db(); run_plans(conn); conn.close()
    except Exception as e: app.logger.warning('plans run failed %s', e)

@app.route('/plans', methods=['GET','POST'])
def plans_page():
    if not is_office(): return redirect('/')
    conn=get_db(); msg=''
    if request.method=='POST':
        f=request.form; a=f.get('action')
        if a in ('add','edit'):
            nd=(f.get('next_date') or '').strip()
            try: nd=datetime.strptime(nd,'%Y-%m-%d').date().isoformat()
            except ValueError: nd=''
            try: em=max(1,min(60,int(f.get('every_months') or 12)))
            except ValueError: em=12
            chk=json.dumps([x.strip() for x in (f.get('checklist') or '').splitlines() if x.strip()])
            kind=(f.get('kind') or 'maintenance').strip()
            if kind not in ('monitoring','inspection','maintenance'): kind='maintenance'
            client=(f.get('client') or '').strip()[:120]; site=(f.get('site') or '').strip()[:200]
            cid=sid=None
            try:
                import fieldops_platform as plat
                if client:
                    co=plat.upsert_company(conn, client, phone=(f.get('phone') or ''))
                    cid=co['id'] if co else None
                    if cid and site:
                        srow=plat.upsert_site(conn, cid, address=site)
                        sid=srow['id'] if srow else None
            except Exception:
                pass
            vals=(client,site,(f.get('phone') or '').strip()[:40],(f.get('title') or 'Service visit').strip()[:120],em,nd,nice_name(f.get('tech') or '')[:60],max(0,fnum(f.get('price'))),chk,1 if f.get('active','1')=='1' else 0,kind,cid,sid)
            if a=='add': conn.execute('INSERT INTO service_plans (client,site,phone,title,every_months,next_date,tech,price,checklist,active,created_at,kind,company_id,site_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',vals[:10]+(now_local_str(),)+vals[10:]); msg='Plan added'
            else: conn.execute('UPDATE service_plans SET client=?,site=?,phone=?,title=?,every_months=?,next_date=?,tech=?,price=?,checklist=?,active=?,kind=?,company_id=?,site_id=? WHERE id=?',vals+(f.get('plan_id'),)); msg='Plan saved'
            conn.commit()
        elif a=='run': msg='Made %d visits'%run_plans(conn)
    plans=conn.execute('SELECT * FROM service_plans ORDER BY active DESC, next_date').fetchall()
    eid=request.args.get('edit'); ed=conn.execute('SELECT * FROM service_plans WHERE id=?',(eid,)).fetchone() if eid else None
    rmr_fn=globals().get('plans_rmr'); rmr=rmr_fn(conn) if callable(rmr_fn) else 0
    conn.close()
    subs=sub_logins()
    def form(p):
        g=lambda k,d='': h((p[k] if p else d) or d)
        try: chk='\n'.join(json.loads(p['checklist'] or '[]')) if p else ''
        except Exception: chk=''
        return ('<form method="post"><input type="hidden" name="action" value="%s"><input type="hidden" name="plan_id" value="%s">'%('edit' if p else 'add',g('id'))+
            '<label>Client</label><input name="client" value="%s" required><label>Site address</label><input name="site" value="%s"><label>Client phone</label><input name="phone" value="%s">'%(g('client'),g('site'),g('phone'))+
            '<label>Visit name</label><input name="title" value="%s" placeholder="Camera checkup">'%(g('title','Service visit'))+
            '<div class="row"><div><label>Kind</label><select name="kind">%s</select></div><div><label>Every how many months</label><input name="every_months" type="number" min="1" max="60" value="%s"></div><div><label>Next visit</label><input name="next_date" type="date" value="%s" required></div></div>'%(''.join('<option value="%s"%s>%s</option>'%(k,' selected' if (p and (p['kind'] if 'kind' in p.keys() else '') or 'maintenance')==k else '',lab) for k,lab in (('maintenance','Maintenance'),('inspection','Inspection'),('monitoring','Monitoring'))),g('every_months','12'),g('next_date'))+
            '<div class="row"><div><label>Sub</label><select name="tech">%s</select></div><div><label>Price per visit $</label><input name="price" type="number" step="0.01" min="0" value="%s"></div></div>'%(''.join('<option%s>%s</option>'%(' selected' if p and ckey(p['tech'])==ckey(n) else '',h(n)) for n in subs),('%g'%p['price']) if p and p['price'] else '')+
            '<label>Checklist, one per line (optional)</label><textarea name="checklist" rows="3">%s</textarea>'%h(chk)+
            '<label>Status</label><select name="active"><option value="1"%s>Active</option><option value="0"%s>Paused</option></select><button class="big-btn">%s</button></form>'%('' if p and not p['active'] else ' selected',' selected' if p and not p['active'] else '','Save Plan' if p else 'Add Plan'))
    body='<div class="muted">Due visits (within %d days) become unscheduled tickets in the schedule tray, and a QuickBooks invoice draft is queued at the plan price. Drafts only. Nothing is sent. Price stays in the office.</div>'%PLAN_LEAD_DAYS
    body+='<div class="plat-panel" style="margin:12px 0"><b>RMR</b> <span style="font-size:1.4rem;font-weight:800;margin-left:8px">%s</span> <span class="muted">/ month from active plans</span></div>'%money(rmr)
    if ed: body+='<h2>Edit plan</h2>'+form(ed)+'<a class="big-btn gray" href="/plans">Cancel</a>'
    body+='<h2>Plans</h2>'+(''.join('<a class="card" href="/plans?edit=%d"><b>%s</b> %s <span class="plat-pill">%s</span><div>%s</div><div class="muted">%s</div><div class="muted">Every %d months &middot; next %s &middot; %s &middot; %s%s</div></a>'%(
        p['id'],h(p['title']),'<span class="okf">Active</span>' if p['active'] else '<span class="flag">Paused</span>',h((p['kind'] if 'kind' in p.keys() and p['kind'] else 'maintenance') or 'maintenance'),h(p['client']),h(p['site']),p['every_months'] or 12,h(nice_day(p['next_date'] or '')),h(p['tech'] or 'any sub'),money(p['price']),(' &middot; last visit '+h(p['last_job_id'])) if p['last_job_id'] else '') for p in plans) or '<div class="card">No plans yet.</div>')
    body+='<form method="post"><input type="hidden" name="action" value="run"><button class="big-btn gray">Make due visits now</button></form>'
    if not ed: body+='<h2>New plan</h2>'+form(None)
    return ops_page('Service Plans', body, msg=msg)

def job_parts_cost(conn, job_id):
    r=conn.execute('SELECT SUM(-qty*IFNULL(unit_cost,0)) c FROM stock_moves WHERE job_id=? AND qty<0',(job_id,)).fetchone()
    return round(r['c'] or 0,2)

# ---------------- Office charts (inline SVG, no libraries) ----------------
CH_PAL=['#a49f2a','#5f5b16','#cfca72','#2f2d0c','#8f8a3a','#e6e3b5','#9e9e9e']
WORK_TYPES=['Cameras','Access','Intercom','Data','AV','Service','Other']
_WT_TPL={'Camera Install':'Cameras','Access Control Install':'Access','Intercom Install':'Intercom','Structured Cabling Install':'Data','Fiber Install':'Data','AV Install':'AV','Service Call':'Service'}
_WT_KW=[('Cameras',('camera','cctv','nvr','dvr','video surveillance')),('Access',('access','card reader','reader','door lock','maglock','strike','keypad')),('Intercom',('intercom','door station','buzzer')),
        ('Data',('cabling','cable','cat6','cat5','data drop','fiber','network','wap','wifi','rack')),('AV',('av ','audio','speaker','tv','display','projector','sound'))]
def work_type(row):
    if (row['job_type'] or '')=='Service': return 'Service'
    try: items={str(c.get('text') or '').strip().lower() for c in json.loads(row['checklist'] or '[]') if isinstance(c,dict)}
    except Exception: items=set()
    best,score=None,0
    if items:
        for name,_,steps in BUILTIN_TEMPLATES:
            if name not in _WT_TPL: continue
            n=len(items & {s.lower() for s in steps})
            if n>score: best,score=_WT_TPL[name],n
    if best and score>=2: return best
    txt=' %s %s '%((row['title'] or '').lower(),(row['scope'] or '').lower())
    for wt,kws in _WT_KW:
        if any(k in txt for k in kws): return wt
    return 'Other'
def ch_short(v):
    a=abs(v); s='-' if v<0 else ''
    return s+('$%.1fM'%(a/1e6) if a>=1e6 else '$%gk'%round(a/1000,1) if a>=1000 else '$%d'%round(a))
def ch_donut(items, empty, money_vals):
    items=[(n,v,c) for n,v,c in items if v>0]; tot=sum(v for _,v,_ in items)
    if not tot: return '<div class="ch-empty"><svg viewBox="0 0 120 120" width="96" height="96" aria-hidden="true"><circle cx="60" cy="60" r="44" fill="none" stroke="#eeede4" stroke-width="20"/></svg><div>%s</div></div>'%h(empty)
    C=2*math.pi*44; off=0; segs=''
    for n,v,c in items:
        L=C*v/tot; segs+='<circle cx="60" cy="60" r="44" fill="none" stroke="%s" stroke-width="20" stroke-dasharray="%.3f %.3f" stroke-dashoffset="%.3f" transform="rotate(-90 60 60)"><title>%s: %s</title></circle>'%(c,L,C-L,-off,h(n),h(money(v) if money_vals else '%d'%v)); off+=L
    center=(ch_short(tot) if money_vals else '%d'%tot)
    svg='<svg viewBox="0 0 120 120" class="ch-donut" role="img" aria-label="%s">%s<text x="60" y="58" text-anchor="middle" font-size="15" font-weight="800" fill="#111">%s</text><text x="60" y="74" text-anchor="middle" font-size="9" fill="#666">%s</text></svg>'%(h(', '.join('%s %s'%(n,money(v) if money_vals else v) for n,v,_ in items)),segs,h(center),'total' if money_vals else 'jobs')
    leg=''.join('<div class="ch-li"><i style="background:%s"></i><span>%s</span><b>%s</b><em>%d%%</em></div>'%(c,h(n),h(money(v) if money_vals else '%d'%v),round(v*100/tot)) for n,v,c in items)
    return '<div class="ch-pie">%s<div class="ch-leg">%s</div></div>'%(svg,leg)
def ch_hbars(items, empty):
    if not items: return '<div class="ch-empty"><div>%s</div></div>'%h(empty)
    mx=max(abs(v) for _,v in items) or 1; neg=any(v<0 for _,v in items); W=360; lw=96; vw=64; bw=W-lw-vw-8; zero=lw+(bw*min(1,max(abs(v) for _,v in items if v<0)/mx)/2 if neg else 0)
    span=bw-(zero-lw); rows=''; y=4
    for n,v in items:
        L=abs(v)/mx*(span if v>=0 else (zero-lw)); x=zero if v>=0 else zero-L
        rows+='<text x="%d" y="%.1f" font-size="12" fill="#111" text-anchor="end">%s</text><rect x="%.1f" y="%.1f" width="%.1f" height="18" rx="4" fill="%s"><title>%s: %s</title></rect><text x="%d" y="%.1f" font-size="12" font-weight="700" fill="%s" text-anchor="end">%s</text>'%(
            lw-6,y+13,h(n[:14]),x,y,max(L,1.5),'#a49f2a' if v>=0 else '#c62828',h(n),h(money(v)),W-4,y+13,'#111' if v>=0 else '#c62828',h(ch_short(v))); y+=26
    if neg: rows+='<line x1="%.1f" x2="%.1f" y1="0" y2="%d" stroke="#999" stroke-width="1"/>'%(zero,zero,y)
    return '<svg viewBox="0 0 %d %d" class="ch-svg" role="img" aria-label="Profit by sub">%s</svg>'%(W,y,rows)
def ch_months(rows):
    if not any(r['n'] for r in rows): return '<div class="ch-empty"><div>No finished jobs in the last 12 months yet.</div></div>'
    W,H,top,base=360,190,18,160; mx=max(r['n'] for r in rows) or 1; slot=(W-8)/12; out=''
    for g in range(0,5):
        y=base-(base-top)*g/4; out+='<line x1="4" x2="%d" y1="%.1f" y2="%.1f" stroke="#f0efe8"/>'%(W-4,y,y)
    for i,r in enumerate(rows):
        hh=(base-top)*r['n']/mx; x=4+slot*i+slot*.18; w=slot*.64
        out+='<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" fill="#a49f2a"><title>%s: %d jobs</title></rect>'%(x,base-hh,w,max(hh,0),h(r['label']),r['n'])
        if r['n']: out+='<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" font-weight="700" fill="#111">%d</text>'%(x+w/2,base-hh-4,r['n'])
        out+='<text x="%.1f" y="%d" text-anchor="middle" font-size="11" fill="#555">%s</text>'%(x+w/2,base+16,h(r['short'][0] if False else r['short']))
    out+='<line x1="4" x2="%d" y1="%d" y2="%d" stroke="#111"/>'%(W-4,base,base)
    return '<svg viewBox="0 0 %d %d" class="ch-svg" role="img" aria-label="Jobs done per month">%s</svg>'%(W,H,out)
def ch_months_data(conn):
    keys=month_keys(12); rows={k:{'month':k,'n':0} for k in keys}
    for j in conn.execute("SELECT completed_at FROM jobs WHERE status='completed' AND COALESCE(job_type,'')!='Office Task' AND completed_at IS NOT NULL").fetchall():
        k=local_month(j['completed_at'])
        if k in rows: rows[k]['n']+=1
    out=[rows[k] for k in keys]
    for r in out:
        d=datetime.strptime(r['month'],'%Y-%m'); r['label']=d.strftime('%b %Y'); r['short']=d.strftime('%b')[:3]
    return out
def est_stage(e):
    if e.get('signed_at'): return 'approved'
    if e.get('lost_at'): return 'lost'
    if e.get('ready'): return 'sent'
    return None
CH_CSS='''<style>.ch-grid{display:grid;grid-template-columns:1fr;gap:12px;margin:8px 0 14px}@media(min-width:820px){.ch-grid{grid-template-columns:1fr 1fr}}
.ch{border:1px solid #e5e5e5;border-radius:14px;padding:12px;background:#fff;min-width:0;overflow:hidden}.ch h3{margin:0 0 2px;font-size:1.05rem}.ch .muted{font-size:.85rem;margin-bottom:6px}
.ch-pie{display:flex;flex-direction:column;gap:8px;align-items:stretch}.ch-donut{width:160px;height:160px;flex:none;align-self:center}.ch-leg{min-width:0}
.ch-li{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #f2f1ea;font-size:.95rem}.ch-li i{width:12px;height:12px;border-radius:3px;flex:none}.ch-li span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ch-li b{white-space:nowrap}.ch-li em{font-style:normal;color:#777;font-size:.8rem;width:34px;text-align:right}
.ch-svg{width:100%;height:auto;display:block}.ch-empty{display:flex;flex-direction:column;align-items:center;gap:6px;color:#777;padding:14px 6px;text-align:center}
.pl{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:6px 0}.pl div{border-radius:12px;padding:10px 6px;text-align:center;border:2px solid #e5e5e5;min-width:0}.pl b{display:block;font-size:1.35rem}.pl span{font-size:.85rem;font-weight:700}.pl small{display:block;color:#555;font-size:.85rem;overflow-wrap:anywhere}</style>'''
def charts_html(conn, jobs_rows, jobs, subs, fr, to):
    wt={}
    for r in jobs_rows:
        s=r['sale_amount'] or 0
        if s>0: k=work_type(r); wt[k]=wt.get(k,0)+s
    pie1=ch_donut([(n,wt.get(n,0),CH_PAL[i]) for i,n in enumerate(WORK_TYPES)],'No sales in this date range yet.',True)
    stc={}
    for j in jobs: stc[j['status']]=stc.get(j['status'],0)+1
    pie2=ch_donut([(lab,stc.get(k,0),CH_PAL[i]) for i,(k,lab) in enumerate(STATUSES)],'No jobs in this date range yet.',False)
    sp=sorted([(x['name'],round(x['profit'],2)) for x in subs.values() if x['name'] and (x['sale'] or x['profit'])],key=lambda t:-t[1])[:12]
    bars=ch_hbars(sp,'No profit to show yet. Profit shows once jobs have a sale amount.')
    months=ch_months(ch_months_data(conn))
    pipe={'sent':[0,0.0],'approved':[0,0.0],'lost':[0,0.0]}
    for r in jobs_rows:
        e=load_estimate(r); st=est_stage(e)
        if st: pipe[st][0]+=1; pipe[st][1]+=e['price']
    if not any(v[0] for v in pipe.values()): pipe_html='<div class="ch-empty"><div>No estimates in this date range yet.</div></div>'
    else:
        tot=sum(v[1] for v in pipe.values()); cols={'sent':'#cfca72','approved':'#a49f2a','lost':'#9e9e9e'}; labs={'sent':'Sent','approved':'Approved','lost':'Lost'}
        pipe_html='<div class="pl">'+''.join('<div style="border-color:%s"><span>%s</span><b>%d</b><small>%s</small></div>'%(cols[k],labs[k],pipe[k][0],h(money(pipe[k][1]))) for k in ('sent','approved','lost'))+'</div>'
        if tot>0:
            x=0; segs=''
            for k in ('sent','approved','lost'):
                w=pipe[k][1]/tot*356
                if w>0: segs+='<rect x="%.1f" y="2" width="%.1f" height="22" fill="%s"><title>%s: %s</title></rect>'%(2+x,w,cols[k],labs[k],h(money(pipe[k][1]))); x+=w
            pipe_html+='<svg viewBox="0 0 360 26" class="ch-svg" role="img" aria-label="Estimate dollars by stage"><rect x="2" y="2" width="356" height="22" rx="6" fill="#f2f1ea"/>%s</svg>'%segs
        dec=pipe['approved'][0]+pipe['lost'][0]
        pipe_html+='<div class="muted" style="margin-top:6px">%s</div>'%('Win rate: %d%% of decided estimates (%d of %d).'%(round(pipe['approved'][0]*100/dec),pipe['approved'][0],dec) if dec else 'No estimates approved or lost yet.')
    rng='%s to %s'%(fr,to)
    card=lambda t,sub,inner:'<div class="ch"><h3>%s</h3><div class="muted">%s</div>%s</div>'%(t,sub,inner)
    return CH_CSS+'<h2 id="charts">Charts</h2><div class="ch-grid">'+card('Sales by work type','Sale amounts on jobs, %s'%rng,pie1)+card('Jobs by status','Jobs in %s'%rng,pie2)+\
        card('Profit by sub','Sale minus labor and parts on their jobs, %s'%rng,bars)+card('Jobs done per month','Finished jobs, last 12 months',months)+\
        card('Estimates pipeline','Sent, approved and lost, %s'%rng,pipe_html)+'</div>'

@app.route('/reports')
def reports_page():
    if not is_office(): return redirect('/')
    t=today_local(); fr=(request.args.get('from') or '%d-01-01'%t.year)[:10]; to=(request.args.get('to') or '%d-12-31'%t.year)[:10]
    conn=get_db(); jobs=[]; jrows=[]
    for r in conn.execute('SELECT * FROM jobs').fetchall():
        day=(r['scheduled_date'] or (r['created_at'] or '')[:10])
        if not (fr<=day<=to) or r['job_type']=='Office Task': continue
        jrows.append(r)
        lab=job_labor(conn, r); parts=job_parts_cost(conn, r['id']); sale=r['sale_amount'] or 0
        jobs.append({'id':r['id'],'title':r['title'] or '','client':r['customer'] or '','type':r['job_type'] or 'Install','tech':nice_name(r['tech']),'date':day,'status':r['status'],
                     'sale':sale,'labor':lab['total'],'parts':parts,'profit':round(sale-lab['total']-parts,2),'hours':round(sum(l['hours'] for l in lab['lines']),2),'lines':lab['lines'],'no_sale':not r['sale_amount']})
    subs={}; types={}
    for j in jobs:
        for l in j['lines']:
            s_=subs.setdefault(l['sub'],{'name':l['sub'],'jobs':set(),'hours':0,'labor':0,'sale':0,'profit':0}); s_['jobs'].add(j['id']); s_['hours']+=l['hours']; s_['labor']+=l['cost']
        if j['tech']:
            s_=subs.setdefault(j['tech'],{'name':j['tech'],'jobs':set(),'hours':0,'labor':0,'sale':0,'profit':0}); s_['jobs'].add(j['id']); s_['sale']+=j['sale']; s_['profit']+=j['profit']
        ty=types.setdefault(j['type'],{'name':j['type'],'jobs':0,'hours':0,'sale':0,'labor':0,'parts':0,'profit':0})
        ty['jobs']+=1; ty['hours']+=j['hours']; ty['sale']+=j['sale']; ty['labor']+=j['labor']; ty['parts']+=j['parts']; ty['profit']+=j['profit']
    kind=request.args.get('csv')
    charts='' if kind else charts_html(conn, jrows, jobs, subs, fr, to)
    conn.close()
    if kind in ('jobs','subs','types'):
        import csv, io
        b=io.StringIO(); w=csv.writer(b)
        if kind=='jobs':
            w.writerow(['Job ID','Date','Client','Title','Type','Sub','Status','Sale','Labor','Parts','Profit','Hours'])
            for j in jobs: w.writerow([j['id'],j['date'],j['client'],j['title'],j['type'],j['tech'],j['status'],j['sale'],j['labor'],j['parts'],j['profit'],j['hours']])
        elif kind=='subs':
            w.writerow(['Sub','Jobs','Hours','Labor cost','Sales on their jobs','Profit on their jobs'])
            for x in sorted(subs.values(),key=lambda x:x['name']): w.writerow([x['name'],len(x['jobs']),round(x['hours'],2),round(x['labor'],2),round(x['sale'],2),round(x['profit'],2)])
        else:
            w.writerow(['Job type','Jobs','Hours','Sales','Labor','Parts','Profit'])
            for x in types.values(): w.writerow([x['name'],x['jobs'],round(x['hours'],2),round(x['sale'],2),round(x['labor'],2),round(x['parts'],2),round(x['profit'],2)])
        from flask import Response
        return Response(b.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=fieldops-%s-%s-to-%s.csv'%(kind,fr,to)})
    q=urllib.parse.urlencode({'from':fr,'to':to})
    tot=lambda k:sum(j[k] for j in jobs)
    body='<form class="row"><div><label>From</label><input type="date" name="from" value="%s"></div><div><label>To</label><input type="date" name="to" value="%s"></div><button class="big-btn" style="flex:1 1 100%%">Show</button></form>'%(h(fr),h(to))
    body+='<div class="card"><table><tr><td>Sales</td><td class="num"><b>%s</b></td></tr><tr><td>Sub labor</td><td class="num">%s</td></tr><tr><td>Parts from stock</td><td class="num">%s</td></tr><tr><td><b>Profit</b></td><td class="num"><b>%s</b></td></tr><tr><td>Sub hours</td><td class="num">%s</td></tr></table></div>'%(money(tot('sale')),money(tot('labor')),money(tot('parts')),money(tot('profit')),'%g'%round(tot('hours'),2))
    miss=sum(1 for j in jobs if j['no_sale'])
    if miss: body+='<div class="muted">%d jobs have no sale amount yet, so they show $0 sales.</div>'%miss
    body+=charts
    body+='<h2>By sub</h2><table><tr><th>Sub</th><th class="num">Jobs</th><th class="num">Hours</th><th class="num">Labor</th><th class="num">Profit</th></tr>'+''.join('<tr><td>%s</td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td></tr>'%(h(x['name']),len(x['jobs']),'%g'%round(x['hours'],2),money(x['labor']),money(x['profit'])) for x in sorted(subs.values(),key=lambda x:-x['hours']))+'</table><a class="big-btn gray" href="/reports?%s&csv=subs">Download sub CSV</a>'%q
    body+='<h2>By job type</h2><table><tr><th>Type</th><th class="num">Jobs</th><th class="num">Sales</th><th class="num">Profit</th></tr>'+''.join('<tr><td>%s</td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td></tr>'%(h(x['name']),x['jobs'],money(x['sale']),money(x['profit'])) for x in types.values())+'</table><a class="big-btn gray" href="/reports?%s&csv=types">Download job type CSV</a>'%q
    body+='<h2>By job</h2><table><tr><th>Job</th><th class="num">Sale</th><th class="num">Cost</th><th class="num">Profit</th></tr>'+''.join('<tr><td><a href="/?job=%s">%s</a><div class="muted">%s %s %s</div></td><td class="num">%s</td><td class="num">%s</td><td class="num"%s><b>%s</b></td></tr>'%(
        urllib.parse.quote(j['id']),h(j['title'] or j['id']),h(j['date']),h(j['client']),h(j['tech']),money(j['sale']),money(j['labor']+j['parts']),' style="color:#c62828"' if j['profit']<0 else '',money(j['profit'])) for j in sorted(jobs,key=lambda j:j['date'],reverse=True))+'</table><a class="big-btn gray" href="/reports?%s&csv=jobs">Download job CSV</a>'%q
    return ops_page('Reports', body)

def qbo_lines(conn, row):
    return qbo_base_lines(conn, row)+mr_job_lines(conn, row['id'])
def qbo_base_lines(conn, row):
    try: pkg=json.loads((conn.execute('SELECT package FROM invoice_drafts WHERE job_id=?',(row['id'],)).fetchone() or {'package':'{}'})['package'] or '{}')
    except Exception: pkg={}
    if pkg.get('line_items'): return [{'description':str(l.get('description'))[:4000],'qty':fnum(l.get('qty'),1),'rate':round(fnum(l.get('rate')),2),'amount':round(fnum(l.get('amount')),2)} for l in pkg['line_items']]
    e=load_estimate(row)
    if e.get('signed_at') and e['ready']:
        return [{'description':e['desc']+(' - '+'; '.join(e['items']) if e['items'] else ''),'qty':1,'rate':e['price'],'amount':e['price']}]
    if row['sale_amount']: return [{'description':(row['title'] or row['id'])+(' - '+row['scope'] if row['scope'] else ''),'qty':1,'rate':round(row['sale_amount'],2),'amount':round(row['sale_amount'],2)}]
    return []
def qbo_payload(conn, row):
    lines=qbo_lines(conn, row)
    return {'job_id':row['id'],'doc_number':row['id'][:21],'txn_date':today_local().isoformat(),
            'customer':{'name':(row['customer'] or '').strip(),'phone':draft_phone(row),'address':row['address'] or ''},
            'line_items':lines,'total':round(sum(l['amount'] for l in lines),2),'customer_memo':row['title'] or '','private_note':'Field Ops job '+row['id']+'. Created as a draft. Do not send without Roland OK.'}
def sync_key_ok():
    key=os.environ.get('QBO_SYNC_KEY') or ''
    if not key: return jsonify({'error':'QBO_SYNC_KEY is not set'}),503
    if not hmac.compare_digest((request.headers.get('X-Sync-Key') or '').encode(), key.encode()): return jsonify({'error':'bad key'}),403
    return None

@app.route('/qbo', methods=['GET','POST'])
def qbo_page():
    if not is_office(): return redirect('/')
    conn=get_db(); msg=''
    if request.method=='POST':
        f=request.form; a=f.get('action')
        if a=='export':
            n=0
            for jid in f.getlist('job'):
                r=conn.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
                if not r or conn.execute("SELECT 1 FROM qbo_queue WHERE job_id=? AND status IN ('queued','created','paid')",(jid,)).fetchone(): continue
                p=qbo_payload(conn, r)
                if not p['line_items'] or not p['customer']['name']: continue
                conn.execute('INSERT OR REPLACE INTO qbo_queue (job_id,status,payload,queued_at,queued_by,updated_at) VALUES (?,?,?,?,?,?)',(jid,'queued',json.dumps(p),now_local_str(),me(),now_local_str())); n+=1
            msg='Queued %d for QuickBooks. Ask your bot to create the QuickBooks drafts. It checks each one with you first.'%n
        elif a=='unqueue':
            conn.execute("DELETE FROM qbo_queue WHERE job_id=? AND status IN ('queued','error')",(f.get('job'),)); msg='Removed from the queue'
        elif a=='paylink':
            link=(f.get('pay_link') or '').strip()[:500]
            if link and not link.startswith('https://'): msg='The pay link must start with https://'
            else: conn.execute('UPDATE jobs SET pay_link=? WHERE id=?',(link or None,f.get('job'))); msg='Pay link saved' if link else 'Pay link removed'
        elif a=='ops_draft':
            fn=globals().get('ops_qbo_post')
            if callable(fn):
                try: msg=fn(conn, f) or msg
                except Exception:
                    app.logger.exception('ops qbo draft failed'); msg='Could not save that draft.'
        conn.commit()
    drafts=conn.execute('SELECT d.job_id,d.created_at FROM invoice_drafts d ORDER BY d.created_at DESC').fetchall()
    q={r['job_id']:r for r in conn.execute('SELECT * FROM qbo_queue').fetchall()}
    jobs={r['id']:r for r in conn.execute('SELECT * FROM jobs').fetchall()}
    ready=[]
    for d in drafts:
        r=jobs.get(d['job_id'])
        if not r or (d['job_id'] in q and q[d['job_id']]['status']!='error'): continue
        ready.append((r,qbo_payload(conn, r)))
    conn.close()
    body='<div class="muted">Export puts approved invoice drafts in a queue. Your bot (it has QuickBooks) creates each one as a QuickBooks draft invoice only after you OK it. Nothing is sent to clients.</div>'
    body+='<h2>Ready to export</h2>'
    if ready:
        body+='<form method="post"><input type="hidden" name="action" value="export">'
        for r,p in ready:
            okp=bool(p['line_items']) and bool(p['customer']['name'])
            body+='<label class="card" style="display:flex;gap:10px;align-items:flex-start;font-weight:400"><input type="checkbox" name="job" value="%s" %s style="width:28px;height:28px;margin:0;flex:0 0 28px"><div style="flex:1"><b>%s</b> <span class="muted">%s</span><div>%s</div>%s<div style="font-weight:700">%s</div>%s</div></label>'%(
                h(r['id']),'checked' if okp else 'disabled',h(r['customer'] or 'No client'),h(r['id']),h(r['title']),''.join('<div class="muted">%s</div>'%h(l['description']) for l in p['line_items']),money(p['total']),'' if okp else '<div class="flag">Add a client and a sale amount first</div>')
        body+='<button class="big-btn">Export to QuickBooks</button></form>'
    else: body+='<div class="card">Nothing ready. Approved jobs show up here.</div>'
    def row_html(x):
        r=jobs.get(x['job_id']); p=json.loads(x['payload'] or '{}')
        tag={'queued':'<span class="flag" style="background:#fff8e1;color:#8d6e00">Waiting for bot</span>','created':'<span class="okf" style="background:#e3f2fd;color:#1565c0">In QuickBooks</span>','paid':'<span class="okf">Paid</span>','error':'<span class="flag">Problem</span>'}.get(x['status'],'')
        out='<div class="card"><b>%s</b> %s <span class="muted">%s</span><div>%s%s</div><div class="muted">Queued %s%s</div>'%(h((r['customer'] if r else '') or p.get('customer',{}).get('name','')),tag,h(x['job_id']),money(x['total'] if x['total'] is not None else p.get('total')),(' &middot; QuickBooks #'+h(x['doc_number'] or x['qbo_invoice_id'])) if (x['doc_number'] or x['qbo_invoice_id']) else '',h(x['queued_at']),(' &middot; '+h(x['note'])) if x['note'] else '')
        if x['status'] in ('queued','error'): out+='<form method="post"><input type="hidden" name="action" value="unqueue"><input type="hidden" name="job" value="%s"><button class="big-btn gray">Take off the queue</button></form>'%h(x['job_id'])
        if r and x['status'] in ('created','paid'): out+='<div class="muted">Pay Now button: <b>%s</b></div>'%('ON' if (r['pay_link'] or '').startswith('https://') else 'off. Paste the link in Pay Now links above.')
        return out+'</div>'
    payjobs=[jobs[x['job_id']] for x in q.values() if x['status']=='created' and x['job_id'] in jobs]
    body+='<h2 id="pay">Pay Now links</h2><div class="card" style="background:#fffdf0"><b>How to add a Pay Now button for a client</b><ol style="margin:6px 0 0 18px;padding:0;line-height:1.6"><li>In QuickBooks, open the invoice and tap <b>Share link</b> (or Send, then Copy link).</li><li>Tap <b>Paste</b> next to the job below.</li><li>Tap <b>Save</b>. The client portal then shows a Pay Now button.</li></ol><div class="muted" style="margin-top:6px">Nothing is sent to the client. They see it only when they open their portal link.</div></div>'
    if payjobs:
        for r in payjobs:
            on=(r['pay_link'] or '').startswith('https://')
            body+=('<form method="post" class="card"><input type="hidden" name="action" value="paylink"><input type="hidden" name="job" value="%s"><b>%s</b> <span class="muted">%s</span> %s'
                   '<input name="pay_link" id="pl-%s" value="%s" placeholder="Paste the QuickBooks invoice link" inputmode="url" autocomplete="off">'
                   '<div class="row"><button type="button" class="big-btn gray" onclick="plPaste(\'pl-%s\')">Paste</button><button class="big-btn">Save</button></div></form>')%(
                   h(r['id']),h(r['customer'] or r['id']),h(r['id']),'<span class="okf">Pay Now ON</span>' if on else '<span class="flag">No link yet</span>',h(r['id']),h(r['pay_link'] or ''),h(r['id']))
        body+='<script>async function plPaste(id){var i=document.getElementById(id);try{var t=(await navigator.clipboard.readText()||"").trim();if(t){i.value=t;return}}catch(e){}i.focus();alert("Long-press the box and choose Paste.")}</script>'
    else: body+='<div class="muted">No invoices in QuickBooks yet. After your bot creates one, it shows up here for its pay link.</div>'
    for st,title in (('queued','Waiting for the bot'),('error','Problems'),('created','In QuickBooks, not paid'),('paid','Paid')):
        xs=[x for x in q.values() if x['status']==st]
        if xs: body+='<h2>%s</h2>'%title+''.join(row_html(x) for x in sorted(xs,key=lambda x:x['updated_at'] or '',reverse=True))
    extra_fn=globals().get('ops_qbo_extra')
    if callable(extra_fn):
        try:
            conn2=get_db(); body+=extra_fn(conn2) or ''; conn2.close()
        except Exception:
            app.logger.exception('ops qbo extra failed')
    return ops_page('QuickBooks Export', body, msg=msg)

@app.route('/api/qbo/pending')
def qbo_pending():
    bad=sync_key_ok()
    if bad: return bad
    st=request.args.get('status','queued')
    if st not in ('queued','created'): return jsonify({'error':'status must be queued or created'}),400
    conn=get_db(); rows=conn.execute('SELECT * FROM qbo_queue WHERE status=? ORDER BY queued_at',(st,)).fetchall(); conn.close()
    existing=[dict(json.loads(r['payload'] or '{}'),status=r['status'],qbo_invoice_id=r['qbo_invoice_id'],queued_at=r['queued_at']) for r in rows]
    fn=globals().get('ops_qbo_pending')
    if callable(fn):
        try: existing=fn(request.args.get('kind') or 'final', st, existing)
        except Exception:
            app.logger.exception('ops qbo pending failed')
    return jsonify(existing)

@app.route('/api/qbo/result', methods=['POST'])
def qbo_result():
    bad=sync_key_ok()
    if bad: return bad
    d=request.get_json(silent=True) or {}; jid=str(d.get('job_id') or ''); st=d.get('status')
    fn=globals().get('ops_qbo_result')
    if callable(fn) and d.get('draft_id'):
        res=fn(d)
        if res=='ok': return jsonify({'ok':True})
        if res=='draft not found': return jsonify({'error':res}),404
        return jsonify({'error':res or 'bad request'}),400
    if st not in ('created','paid','error'): return jsonify({'error':'status must be created, paid or error'}),400
    conn=get_db(); x=conn.execute('SELECT * FROM qbo_queue WHERE job_id=?',(jid,)).fetchone()
    if not x: conn.close(); return jsonify({'error':'job not in queue'}),404
    pick=globals().get('pick_pay_link')
    link=pick(d) if callable(pick) else str(d.get('pay_link') or d.get('invoiceLink') or '').strip()[:500]
    if link is None or (link and not str(link).startswith('https://')): conn.close(); return jsonify({'error':'pay_link must be https'}),400
    link=link or ''
    tot=d.get('total'); bal=d.get('balance')
    conn.execute('UPDATE qbo_queue SET status=?,qbo_invoice_id=COALESCE(?,qbo_invoice_id),doc_number=COALESCE(?,doc_number),total=COALESCE(?,total),balance=COALESCE(?,balance),note=?,updated_at=? WHERE job_id=?',
                 (st,(str(d['qbo_invoice_id'])[:40] if d.get('qbo_invoice_id') else None),(str(d['doc_number'])[:40] if d.get('doc_number') else None),(fnum(tot) if tot is not None else None),(fnum(bal) if bal is not None else None),str(d.get('note') or '')[:300],now_local_str(),jid))
    if link: conn.execute('UPDATE jobs SET pay_link=? WHERE id=?',(link,jid))
    if st=='paid': conn.execute('UPDATE jobs SET paid_at=COALESCE(paid_at,?) WHERE id=?',(now_local_str(),jid))
    conn.execute('UPDATE invoice_drafts SET status=? WHERE job_id=?',({'created':'in_quickbooks','paid':'paid','error':'ready_for_review'}[st],jid))
    conn.commit(); conn.close(); return jsonify({'ok':True})

def stock_locations(): return ['Shop']+['Truck: '+n for n in sub_logins()]
def my_truck(): return 'Truck: '+me()
def stock_move(conn, item_id, loc, qty, job_id='', note=''):
    it=conn.execute('SELECT * FROM stock_items WHERE id=?',(item_id,)).fetchone()
    if not it or not qty: return False
    conn.execute('INSERT INTO stock_levels (item_id,location,qty) VALUES (?,?,0) ON CONFLICT(item_id,location) DO NOTHING',(item_id,loc))
    conn.execute('UPDATE stock_levels SET qty=qty+? WHERE item_id=? AND location=?',(qty,item_id,loc))
    conn.execute('INSERT INTO stock_moves (item_id,location,qty,job_id,who,note,unit_cost,created_at) VALUES (?,?,?,?,?,?,?,?)',(item_id,loc,qty,job_id or None,me(),note[:200],it['cost'] or 0,now_local_str()))
    return True
def low_stock(conn):
    tot={r['item_id']:r['t'] for r in conn.execute('SELECT item_id,SUM(qty) t FROM stock_levels GROUP BY item_id')}
    return [r for r in conn.execute('SELECT * FROM stock_items').fetchall() if (r['low_at'] or 0)>0 and (tot.get(r['id']) or 0)<=r['low_at']]
def fnum(v, d=0.0):
    try: x=float(v); return x if math.isfinite(x) else d
    except (TypeError, ValueError): return d

@app.route('/stock', methods=['GET','POST'])
def stock_page():
    if not is_office(): return redirect('/')
    conn=get_db(); msg=''; locs=stock_locations()
    if request.method=='POST':
        f=request.form; a=f.get('action')
        if a=='add' and (f.get('name') or '').strip():
            cur=conn.execute('INSERT INTO stock_items (name,sku,unit,cost,low_at,created_at) VALUES (?,?,?,?,?,?)',(f['name'].strip()[:120],(f.get('sku') or '').strip()[:60],(f.get('unit') or 'each').strip()[:20],max(0,fnum(f.get('cost'))),max(0,fnum(f.get('low_at'))),now_local_str()))
            q=fnum(f.get('qty'))
            if q: stock_move(conn,cur.lastrowid,'Shop',q,note='Starting stock')
            msg='Added '+f['name'].strip()
        elif a=='edit':
            conn.execute('UPDATE stock_items SET name=?,sku=?,unit=?,cost=?,low_at=? WHERE id=?',((f.get('name') or '').strip()[:120] or 'Item',(f.get('sku') or '').strip()[:60],(f.get('unit') or 'each').strip()[:20],max(0,fnum(f.get('cost'))),max(0,fnum(f.get('low_at'))),f.get('item_id')))
            msg='Saved'
        elif a=='adjust' and f.get('location') in locs:
            if stock_move(conn,f.get('item_id'),f['location'],fnum(f.get('qty')),note=(f.get('note') or 'Adjust').strip()): msg='Stock updated'
        elif a=='move' and f.get('from') in locs and f.get('to') in locs and f['from']!=f['to']:
            q=abs(fnum(f.get('qty')))
            if q and stock_move(conn,f.get('item_id'),f['from'],-q,note='Moved to '+f['to']): stock_move(conn,f.get('item_id'),f['to'],q,note='Moved from '+f['from']); msg='Moved'
        conn.commit()
    items=conn.execute('SELECT * FROM stock_items ORDER BY LOWER(name)').fetchall()
    lv={(r['item_id'],r['location']):r['qty'] for r in conn.execute('SELECT * FROM stock_levels')}
    for (_,l) in lv:
        if l not in locs: locs.append(l)
    low={r['id'] for r in low_stock(conn)}
    moves=conn.execute('SELECT m.*,i.name,i.unit FROM stock_moves m JOIN stock_items i ON i.id=m.item_id ORDER BY m.id DESC LIMIT 30').fetchall(); conn.close()
    fq=lambda x:('%g'%round(x or 0,2))
    opt=''.join('<option value="%d">%s</option>'%(i['id'],h(i['name'])) for i in items); lopt=''.join('<option>%s</option>'%h(l) for l in locs)
    body=''
    if low: body+='<div class="card"><span class="flag">Low stock</span> '+h(', '.join(i['name'] for i in items if i['id'] in low))+'</div>'
    body+='<div style="overflow-x:auto"><table><tr><th>Item</th>'+''.join('<th class="num">%s</th>'%h(l) for l in locs)+'<th class="num">Total</th><th>Cost</th></tr>'
    for i in items:
        tot=sum(lv.get((i['id'],l),0) for l in locs)
        body+='<tr><td><a href="/stock/%d"><b>%s</b></a>%s<div class="muted">%s</div></td>'%(i['id'],h(i['name']),' <span class="flag">Low</span>' if i['id'] in low else '',h(' '.join(x for x in [i['sku'] or '', i['unit'] or ''] if x)))
        body+=''.join('<td class="num"%s>%s</td>'%(' style="color:#c62828;font-weight:700"' if lv.get((i['id'],l),0)<0 else '',fq(lv.get((i['id'],l),0))) for l in locs)
        body+='<td class="num"><b>%s</b></td><td class="num">%s</td></tr>'%(fq(tot),money(i['cost']))
    body+='</table></div>' if items else '</table></div><div class="card">No stock items yet. Add one below.</div>'
    if items:
        body+='<h2>Add or remove stock</h2><form method="post"><input type="hidden" name="action" value="adjust"><select name="item_id">%s</select><select name="location">%s</select><input name="qty" type="number" step="any" inputmode="decimal" placeholder="Qty (use - to remove)" required><input name="note" placeholder="Note (bought, counted, broken)"><button class="big-btn">Update Stock</button></form>'%(opt,lopt)
        body+='<h2>Move stock</h2><form method="post"><input type="hidden" name="action" value="move"><select name="item_id">%s</select><div class="row"><select name="from">%s</select><select name="to">%s</select></div><input name="qty" type="number" step="any" min="0" inputmode="decimal" placeholder="Qty" required><button class="big-btn">Move</button></form>'%(opt,lopt,''.join('<option%s>%s</option>'%(' selected' if n==1 else '',h(l)) for n,l in enumerate(locs)))
    body+='<h2>New item</h2><form method="post"><input type="hidden" name="action" value="add"><input name="name" placeholder="Name (Cat6 cable, 4MP dome)" required><div class="row"><input name="sku" placeholder="Part number"><input name="unit" placeholder="Unit (each, ft, box)"></div><div class="row"><input name="cost" type="number" step="0.01" min="0" placeholder="Cost each $"><input name="low_at" type="number" step="any" min="0" placeholder="Low alert at"></div><input name="qty" type="number" step="any" placeholder="Starting qty in Shop"><button class="big-btn">Add Item</button></form>'
    if moves:
        body+='<h2>Recent</h2><table>'+''.join('<tr><td>%s<div class="muted">%s %s</div></td><td>%s %s<div class="muted">%s</div></td><td class="num"><b>%s</b></td></tr>'%(h(m['name']),h(m['created_at'] or ''),h(m['who'] or ''),h(m['location']),('Job '+h(m['job_id'])) if m['job_id'] else '',h(m['note'] or ''),('+' if m['qty']>0 else '')+fq(m['qty'])) for m in moves)+'</table>'
    return ops_page('Parts and Stock', body, msg=msg)

@app.route('/stock/<int:iid>', methods=['GET'])
def stock_item(iid):
    if not is_office(): return redirect('/')
    conn=get_db(); i=conn.execute('SELECT * FROM stock_items WHERE id=?',(iid,)).fetchone(); conn.close()
    if not i: return redirect('/stock')
    body='<form method="post" action="/stock"><input type="hidden" name="action" value="edit"><input type="hidden" name="item_id" value="%d"><label>Name</label><input name="name" value="%s"><label>Part number</label><input name="sku" value="%s"><label>Unit</label><input name="unit" value="%s"><label>Cost each ($)</label><input name="cost" type="number" step="0.01" min="0" value="%s"><label>Low alert at (total)</label><input name="low_at" type="number" step="any" min="0" value="%s"><button class="big-btn">Save</button></form>'%(
        i['id'],h(i['name']),h(i['sku'] or ''),h(i['unit'] or ''),'%g'%(i['cost'] or 0),'%g'%(i['low_at'] or 0))
    return ops_page('Edit Item', body, back='/stock')

@app.route('/api/jobs/<job_id>/parts', methods=['GET','POST'])
def job_parts(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    locs=stock_locations() if is_office() else [my_truck(),'Shop']
    if request.method=='POST':
        d=request.get_json(silent=True) or {}; q=abs(fnum(d.get('qty'))); loc=d.get('location') if d.get('location') in locs else locs[0]
        if not q or q>100000: conn.close(); return jsonify({'error':'Enter how many'}),400
        if not stock_move(conn,d.get('item_id'),loc,-q,job_id,'Used on job'): conn.close(); return jsonify({'error':'Pick a part'}),400
        conn.commit()
    lv={(r['item_id'],r['location']):r['qty'] for r in conn.execute('SELECT * FROM stock_levels')}
    items=[{'id':i['id'],'name':i['name'],'unit':i['unit'] or '','here':{l:round(lv.get((i['id'],l),0),2) for l in locs}} for i in conn.execute('SELECT * FROM stock_items ORDER BY LOWER(name)')]
    used=[{'id':m['id'],'name':m['name'],'unit':m['unit'] or '','qty':-m['qty'],'location':m['location'],'who':m['who'],'mine':is_office() or ckey(m['who'])==ckey(me())}
          for m in conn.execute('SELECT m.*,i.name,i.unit FROM stock_moves m JOIN stock_items i ON i.id=m.item_id WHERE m.job_id=? AND m.qty<0 ORDER BY m.id',(job_id,))]
    conn.close(); return jsonify({'items':items,'used':used,'locations':locs})

@app.route('/api/jobs/<job_id>/parts/<int:mid>', methods=['DELETE'])
def job_part_undo(job_id, mid):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    m=conn.execute('SELECT * FROM stock_moves WHERE id=? AND job_id=? AND qty<0',(mid,job_id)).fetchone()
    if not m or not (is_office() or ckey(m['who'])==ckey(me())): conn.close(); return jsonify({'error':'not found'}),404
    conn.execute('UPDATE stock_levels SET qty=qty-? WHERE item_id=? AND location=?',(m['qty'],m['item_id'],m['location']))
    conn.execute('DELETE FROM stock_moves WHERE id=?',(mid,)); conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/clients')
def clients_page():
    conn=get_db(); name=request.args.get('name')
    if not is_office() and (name is None or not tech_has_client(conn, name)): conn.close(); return redirect('/')
    if is_office() and name is None:
        conn.close(); return redirect('/customers')
    if is_office() and name:
        try:
            import fieldops_platform as _plat
            co=_plat.get_company_by_key(conn, ckey(name))
            if co: conn.close(); return redirect('/customers/%d'%co['id'])
        except Exception:
            pass
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
    d['photos']=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto','daily','matreq','ticket') ORDER BY id",(r['id'],)).fetchall()]
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
    fn=globals().get('ops_calendar_page')
    if callable(fn): return fn()
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
{% set ab = j.job_type != 'Survey' %}{% if j.job_type != 'Office Task' and j.plans %}<div class="sec">{{ 'As-Built' if ab else 'Floor Plan Devices' }}</div>{% if ab %}<div class="muted">Solid symbol = installed, white dashed symbol = planned. Shaded wedges show camera coverage.</div>{% endif %}{% for fn in j.plans %}<div style="position:relative;margin:6px 0;page-break-inside:avoid"><img src="/uploads/{{ fn|urlencode }}" alt="floor plan" style="width:100%;height:auto;object-fit:contain;border-radius:0;margin:0;display:block;border:1px solid #ddd">{{ plan_svg(fn, j.devices, ab) }}</div>{% endfor %}
{% if j.devices %}<div style="margin-top:6px"><b>Device count:</b> {% for t,n in dev_counts(j.devices).items() %}<span style="white-space:nowrap">{{ dev_icon(t, DEV_COLORS[t], true, 18) }} {{ t }} x{{ n }}</span>{% if ab %} ({{ j.devices|selectattr('type','equalto',t)|selectattr('installed')|list|length }} installed){% endif %}{% if not loop.last %}, {% endif %}{% endfor %} ({{ j.devices|length }} total{% if ab %}, {{ j.devices|selectattr('installed')|list|length }} installed{% endif %})</div>
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
    fn=globals().get('ops_schedule_page')
    if callable(fn): return fn()
    t=today_local()
    try: start=datetime.strptime(request.args.get('start',''),'%Y-%m-%d').date()
    except ValueError: start=t
    start=start-timedelta(days=start.weekday()); end=start+timedelta(days=6)
    conn=get_db(); rows=conn.execute("SELECT * FROM jobs WHERE status!='completed' AND (scheduled_date IS NULL OR scheduled_date='' OR scheduled_date BETWEEN ? AND ?) ORDER BY created_at",(start.isoformat(),end.isoformat())).fetchall(); co=sub_companies(conn); conn.close()
    jobs=[job_dict(r) for r in rows]
    techs=sub_logins()+[nice_name(k) for k in sorted(USERS) if k in OFFICE_NAMES]
    techs+=sorted({j['tech'] for j in jobs if j['tech'] and j['tech'].lower() not in [x.lower() for x in techs]})
    days=[{'iso':(start+timedelta(days=i)).isoformat(),'label':(start+timedelta(days=i)).strftime('%a %b ')+str((start+timedelta(days=i)).day),'today':start+timedelta(days=i)==t} for i in range(7)]
    return render_template_string(SCHED_HTML, co=co, days=days, techs=techs, jobs=jobs, prev=(start-timedelta(days=7)).isoformat(), next=(start+timedelta(days=7)).isoformat())

@app.route('/api/jobs/<job_id>/schedule', methods=['POST'])
def schedule_job(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    d=request.get_json(silent=True) or {}; sd=str(d.get('scheduled_date') or '').strip(); tech=nice_name(str(d.get('tech') or ''))[:60]
    if sd:
        try: sd=datetime.strptime(sd,'%Y-%m-%d').date().isoformat()
        except ValueError: return jsonify({'error':'bad date'}),400
    if not tech: return jsonify({'error':'tech required'}),400
    conn=get_db(); old=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    n=conn.execute('UPDATE jobs SET scheduled_date=?,tech=? WHERE id=?',(sd or None,tech,job_id)).rowcount
    try:
        import fieldops_ops as _ops
        existing=list(conn.execute('SELECT * FROM job_slots WHERE job_id=?',(job_id,)))
        if not sd:
            conn.execute('DELETE FROM job_slots WHERE job_id=?',(job_id,))
        elif len(existing)==1:
            _ops.upsert_slot(conn, job_id, sd, tech, existing[0]['start_time'], existing[0]['end_time'], existing[0]['id'])
        elif not existing:
            _ops.upsert_slot(conn, job_id, sd, tech)
    except Exception:
        app.logger.exception('slot sync from schedule API failed')
    conn.commit(); conn.close()
    if n and old:
        if ckey(old['tech'])!=ckey(tech):
            push_to_sub(tech,'New job: '+(old['title'] or job_id),(old['customer'] or '')+' - '+nice_day(sd),'/?job='+job_id)
            push_to_sub(old['tech'],'Job moved off your list',(old['title'] or job_id),'/')
        elif (old['scheduled_date'] or '')!=(sd or ''):
            push_to_sub(tech,'Schedule change: '+(old['title'] or job_id),'Now '+nice_day(sd),'/?job='+job_id)
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
{% if paid %}<div class="ok" style="margin-top:12px;text-align:center">&#10003; Paid. Thank you!</div>{% elif pay %}<a href="{{ pay }}" target="_blank" rel="noopener noreferrer" class="btn" style="display:block;text-align:center;text-decoration:none;margin-top:14px;font-size:1.4rem;padding:22px">Pay Now</a><div class="muted" style="text-align:center;margin-top:4px">Opens the secure QuickBooks payment page</div>{% endif %}
{% if j.job_type == 'Survey' %}<h2>Site Survey</h2><div class="card">{% if j.survey.want_desc %}<div><b>What you want:</b></div><div style="white-space:pre-wrap">{{ j.survey.want_desc }}</div>{% endif %}
{% if j.survey.conditions %}<div style="margin-top:8px"><b>Site conditions:</b></div><div style="white-space:pre-wrap">{{ j.survey.conditions }}</div>{% endif %}</div>{% endif %}
{% if j.plans %}<h2>{{ 'Plan and Devices' if j.job_type == 'Survey' else 'As-Built Plan' }}</h2>{% for fn in j.plans %}<div class="plan"><img src="{{ signed(fn) }}" alt="plan">{{ plan_svg(fn, j.devices, j.job_type != 'Survey') }}</div>{% endfor %}
{% if j.devices %}<div><b>Device count:</b> {% for t,n in dev_counts(j.devices).items() %}<span style="white-space:nowrap">{{ dev_icon(t, DEV_COLORS[t], true, 18) }} {{ t }} x{{ n }}</span>{% if not loop.last %}, {% endif %}{% endfor %}</div>
<table><tr><th>Label</th><th>Device</th><th>Note</th>{% if j.job_type != 'Survey' %}<th>Status</th>{% endif %}</tr>{% for d in j.devices %}<tr><td><b>{{ d.label }}</b></td><td>{{ d.type }}</td><td>{{ d.note }}</td>{% if j.job_type != 'Survey' %}<td>{{ 'Installed' if d.installed else 'Planned' }}</td>{% endif %}</tr>{% endfor %}</table>{% endif %}{% endif %}
{% if j.photos %}<h2>Photos</h2><div class="ph">{% for p in j.photos %}<a href="{{ signed(p.filename) }}" target="_blank"><img src="{{ signed(p.filename) }}" alt="photo"></a>{% endfor %}</div>{% endif %}
{% if j.job_type not in ('Survey','Office Task') and j.status == 'completed' and j.signature and j.signature.startswith('data:image/') %}<h2>Completion Signature</h2><img src="{{ j.signature }}" alt="signature" style="width:260px;max-width:100%;border:1px solid #ddd">{% if j.completed_at %}<div class="muted">Completed {{ j.completed_at[:10] }}</div>{% endif %}{% endif %}
{% if est.ready %}<h2>Estimate</h2><div class="card"><div style="white-space:pre-wrap;font-size:1.1rem">{{ est.desc }}</div>
{% if est['items'] %}<ul style="margin:8px 0 8px 18px;padding:0">{% for x in est['items'] %}<li>{{ x }}</li>{% endfor %}</ul>{% endif %}<div style="font-size:1.6rem;font-weight:800;margin-top:6px">{{ money(est.price) }}</div>
{% if est.note %}<div class="muted" style="white-space:pre-wrap;margin-top:6px">{{ est.note }}</div>{% endif %}</div>
{% if est.signed_at %}<div class="ok">&#10003; {{ est.signed_name }} approved this estimate ({{ money(est.price) }}) on {{ est.signed_at }}</div>
{% if est.signature and est.signature.startswith('data:image/png;base64,') %}<img src="{{ est.signature }}" alt="signature" style="width:260px;max-width:100%;border:1px solid #ddd;margin-top:6px">{% endif %}
{% else %}<form method="post" action="/hub/{{ j.id|urlencode }}/estimate" onsubmit="return estSubmit()"><input type="hidden" name="t" value="{{ t }}"><input type="hidden" name="signature" id="est-sig">
<h2>Sign to approve</h2><input name="name" placeholder="Your full name" required maxlength="100">
<div class="muted">Sign with your finger below</div><canvas id="est-pad" width="600" height="200" style="width:100%;height:160px;border:2px solid #ccc;border-radius:10px;background:#fff;touch-action:none"></canvas>
<button type="button" style="background:#eee;color:#111;border:0;border-radius:10px;padding:12px;width:100%;font-size:1rem" onclick="estClear()">Clear signature</button>
<label style="display:flex;gap:10px;align-items:center;margin:12px 0;font-size:1.05rem"><input type="checkbox" name="agree" value="1" required style="width:28px;height:28px;margin:0"> I approve this estimate and price.</label>
<button class="btn" type="submit">Approve and Sign</button></form>
<script>
var estDrawn=false;
(function(){var c=document.getElementById("est-pad"),x=c.getContext("2d"),on=false;x.lineWidth=4;x.lineCap="round";x.strokeStyle="#111";
function p(e){var r=c.getBoundingClientRect();return [(e.clientX-r.left)*c.width/r.width,(e.clientY-r.top)*c.height/r.height]}
c.addEventListener("pointerdown",function(e){on=true;var q=p(e);x.beginPath();x.moveTo(q[0],q[1]);c.setPointerCapture(e.pointerId)});
c.addEventListener("pointermove",function(e){if(!on)return;var q=p(e);x.lineTo(q[0],q[1]);x.stroke();estDrawn=true});
c.addEventListener("pointerup",function(){on=false})})();
function estClear(){var c=document.getElementById("est-pad");c.getContext("2d").clearRect(0,0,c.width,c.height);estDrawn=false}
function estSubmit(){if(!estDrawn){alert("Please sign in the box");return false}document.getElementById("est-sig").value=document.getElementById("est-pad").toDataURL("image/png");return true}
</script>{% endif %}{% endif %}
{% if j.job_type == 'Survey' and not est.ready %}<h2>Estimate</h2>{% if j.hub_approved_at %}<div class="ok">&#10003; Estimate approved by {{ j.hub_approved_name or 'client' }} on {{ j.hub_approved_at }}</div>
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
    j=job_full(conn,row); conn.close(); est=load_estimate(row)
    pay=row['pay_link'] if (row['pay_link'] or '').startswith('https://') else ''
    return render_template_string(HUB_HTML, j=j, est=est, money=money, pay=pay, paid=row['paid_at'], t=request.values.get('t',''), signed=lambda fn: '/hub/'+urllib.parse.quote(job_id)+'/f/'+urllib.parse.quote(fn)+'?t='+urllib.parse.quote(request.values.get('t','')))

@app.route('/hub/<job_id>/f/<path:fn>')
def hub_file(job_id, fn):
    conn,row=hub_job(job_id)
    if not row: return ('This link has expired.',410)
    fn=secure_filename(fn); ok=conn.execute('SELECT 1 FROM photos WHERE job_id=? AND filename=?',(job_id,fn)).fetchone(); conn.close()
    if not ok: return ('',404)
    r=send_from_directory(app.config['UPLOAD_FOLDER'], fn); r.headers['Cache-Control']='private, no-store'; return r

@app.route('/hub/<job_id>/approve', methods=['POST'])
def hub_approve(job_id):
    conn,row=hub_job(job_id)
    if not row: return (HUB_EXPIRED_HTML,410)
    if row['job_type']=='Survey' and not row['hub_approved_at']:
        conn.execute('UPDATE jobs SET hub_approved_at=?,hub_approved_name=? WHERE id=?',(now_local_str(),(request.form.get('name') or '').strip()[:100],job_id)); conn.commit()
    conn.close()
    return redirect('/hub/'+urllib.parse.quote(job_id)+'?t='+urllib.parse.quote(request.values.get('t','')))

def load_estimate(row):
    # One estimate per job. Older Good/Better/Best data converts to one: the signed pick, else the first filled option.
    try: e=json.loads(row['estimate_json'] or '{}')
    except Exception: e={}
    e=e if isinstance(e,dict) else {}
    if 'options' in e:
        opts=[o for o in (e.get('options') or []) if isinstance(o,dict) and str(o.get('name') or '').strip()]
        ch=e.get('chosen') if isinstance(e.get('chosen'),int) and 0<=e.get('chosen')<len(opts) else 0
        o=opts[ch] if opts else {}
        e={'desc':str(o.get('name') or ''),'items':o.get('items') or [],'price':o.get('price') or 0,'note':e.get('note',''),
           **{k:e[k] for k in ('signed_name','signature','signed_at','ip') if k in e}}
    e['desc']=str(e.get('desc') or '')[:4000]; e['items']=[str(x)[:200] for x in (e.get('items') or []) if str(x).strip()][:30]
    e['price']=round(fnum(e.get('price')),2); e['note']=str(e.get('note') or '')[:1000]
    e['ready']=bool(e['desc'].strip())
    return e
def save_estimate(conn, job_id, e):
    conn.execute('UPDATE jobs SET estimate_json=? WHERE id=?',(json.dumps({k:v for k,v in e.items() if k!='ready'}),job_id))

@app.route('/job/<job_id>/estimate', methods=['GET','POST'])
def estimate_edit(job_id):
    if not is_office(): return redirect('/')
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return redirect('/')
    e=load_estimate(row); msg=''
    if request.method=='POST':
        if request.form.get('action')=='clear':
            e={k:e[k] for k in ('desc','items','price','note')}; msg='Signature cleared. You can edit the estimate.'
        elif request.form.get('action') in ('lost','reopen') and not e.get('signed_at'):
            e={k:v for k,v in e.items() if k!='lost_at'}
            if request.form.get('action')=='lost': e['lost_at']=now_local_str(); msg='Marked as lost. It shows as lost on the Reports charts.'
            else: msg='Reopened. It counts as sent again.'
        elif e.get('signed_at'): msg='Already signed. Clear it first to change.'
        else:
            e={'desc':(request.form.get('desc') or '').strip()[:4000],'items':[x.strip() for x in (request.form.get('items') or '').splitlines() if x.strip()][:30],
               'price':max(0,round(fnum(request.form.get('price')),2)),'note':(request.form.get('note') or '').strip()[:1000],**({'lost_at':e['lost_at']} if e.get('lost_at') else {})}
            msg='Saved. Send the client their portal link when ready.'
        save_estimate(conn, job_id, e); conn.commit(); e=load_estimate(conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
    conn.close()
    body='<div class="muted">%s &middot; %s</div>'%(h(row['customer']),h(row['title']))
    if e.get('signed_at'):
        body+='<div class="card"><span class="okf">Approved</span> <b>%s</b><div style="white-space:pre-wrap">%s</div><div>Signed by %s on %s</div><img src="%s" alt="signature" style="width:260px;max-width:100%%;border:1px solid #ddd;margin-top:6px"></div>'%(money(e['price']),h(e['desc']),h(e.get('signed_name')),h(e.get('signed_at')),h(e.get('signature') if str(e.get('signature','')).startswith('data:image/png;base64,') else ''))
        body+='<form method="post" onsubmit="return confirm(\'Clear the client signature?\')"><input type="hidden" name="action" value="clear"><button class="big-btn gray">Clear signature to edit</button></form>'
    else:
        body+='<form method="post"><label>Description and scope</label><textarea name="desc" rows="5" placeholder="Install 6 cameras, NVR and phone app setup">%s</textarea>'%h(e['desc'])
        body+='<label>Line items, one per line (optional)</label><textarea name="items" rows="4" placeholder="6 x 4MP dome camera">%s</textarea>'%h('\n'.join(e['items']))
        body+='<label>Price $</label><input name="price" type="number" step="0.01" min="0" value="%s"><label>Note to client (optional)</label><textarea name="note" rows="3">%s</textarea><button class="big-btn">Save Estimate</button></form>'%(('%g'%e['price']) if e['price'] else '',h(e['note']))
        body+='<div class="muted">The client sees this on their portal link, signs with a finger and approves. Nothing is sent from here.</div>'
        if e.get('ready'):
            if e.get('lost_at'): body+='<div class="card"><b>Marked lost</b> on %s.<form method="post"><input type="hidden" name="action" value="reopen"><button class="big-btn gray">Reopen estimate</button></form></div>'%h(e['lost_at'])
            else: body+='<form method="post" onsubmit="return confirm(&quot;Mark this estimate as lost?&quot;)"><input type="hidden" name="action" value="lost"><button class="big-btn gray">Mark as lost</button></form>'
    return ops_page('Estimate', body, back='/?job='+urllib.parse.quote(job_id), msg=msg)

@app.route('/hub/<job_id>/estimate', methods=['POST'])
def hub_estimate_sign(job_id):
    conn,row=hub_job(job_id)
    if not row: return (HUB_EXPIRED_HTML,410)
    e=load_estimate(row); f=request.form; sig=(f.get('signature') or ''); name=(f.get('name') or '').strip()[:100]
    if e['ready'] and not e.get('signed_at') and name and sig.startswith('data:image/png;base64,') and len(sig)<300000 and f.get('agree')=='1':
        e.update({'signed_name':name,'signature':sig,'signed_at':now_local_str(),'ip':(request.headers.get('X-Forwarded-For') or request.remote_addr or '')[:60]})
        save_estimate(conn, job_id, e)
        conn.execute('UPDATE jobs SET hub_approved_at=?,hub_approved_name=?,sale_amount=? WHERE id=?',(now_local_str(),name,e['price'] or None,job_id))
        conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,'Client portal','%s approved and signed the estimate'%name,now_local_str()))
        conn.commit()
    conn.close()
    return redirect('/hub/'+urllib.parse.quote(job_id)+'?t='+urllib.parse.quote(f.get('t','')))

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

REMIND_LAST=[0.0]
def make_reminders(conn):
    if time.time()-REMIND_LAST[0]<60: return
    REMIND_LAST[0]=time.time(); added=False
    tm=(today_local()+timedelta(days=1)).isoformat()
    for r in conn.execute("SELECT * FROM jobs WHERE scheduled_date=? AND status!='completed' AND COALESCE(job_type,'')!='Office Task'",(tm,)).fetchall():
        ph=draft_phone(r)
        if not ph or conn.execute("SELECT 1 FROM msg_drafts WHERE job_id=? AND kind='reminder' AND ref_date=?",(r['id'],tm)).fetchone(): continue
        when=datetime.strptime(tm,'%Y-%m-%d').strftime('%A, %b %d').replace(' 0',' ')
        body=f"Hi{(' '+r['customer'].strip()) if (r['customer'] or '').strip() else ''}, this is True Sense Security. Reminder: we will see you tomorrow, {when}{(' at '+r['address']) if r['address'] else ''}. Reply to this text if you need to change it."
        conn.execute("INSERT INTO msg_drafts (job_id,kind,phone,body,status,ref_date,created_by,created_at) VALUES (?,?,?,?,'pending',?,?,?)",(r['id'],'reminder',ph,body,tm,'Auto',now_local_str())); added=True
    if added: conn.commit()

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

AGR_TOKEN = re.compile(r'\[(SUB\?|SUB-BOX|SUB|OFFICE|AUTO):\s*([^\]]*)\]')
def agr_parse(body):
    segs=[]; pos=0; n=0
    for m in AGR_TOKEN.finditer(body or ''):
        if m.start()>pos: segs.append({'t':'text','v':body[pos:m.start()]})
        kind,label=m.group(1),m.group(2).strip()
        if kind in ('SUB','SUB?'): n+=1; segs.append({'t':'sub','key':'f%d'%n,'label':label,'req':kind=='SUB'})
        elif kind=='SUB-BOX': n+=1; segs.append({'t':'box','key':'f%d'%n,'label':label})
        elif kind=='OFFICE':
            l,_,d=label.partition('='); segs.append({'t':'office','label':l.strip(),'default':d.strip(),'raw':m.group(0)})
        else: segs.append({'t':'auto','label':label.lower()})
        pos=m.end()
    if pos<len(body or ''): segs.append({'t':'text','v':body[pos:]})
    return segs

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
.text{white-space:pre-wrap;font-family:Arial,Helvetica,sans-serif;font-size:1rem;line-height:1.5;background:#fff;border:1px solid #ddd;border-radius:10px;padding:12px}
input[type=text],textarea{width:100%;padding:14px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:6px 0}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #eee;padding:8px 4px;text-align:left}
.fill{display:inline-block;width:15em;max-width:100%;padding:10px;font-size:16px;font-family:Arial,Helvetica,sans-serif;background:#fff59d;border:2px solid #d4b000;border-radius:8px;margin:3px 0;white-space:normal}
.box{width:28px;height:28px;vertical-align:middle;accent-color:#a49f2a;outline:3px solid #fff59d}.ofc{background:#ffe0b2;border-radius:4px;padding:1px 4px}.auto{background:#eee;border-radius:4px;padding:1px 4px;color:#555}
.val{text-decoration:underline;font-weight:700}
@media print{.noprint{display:none}.ofc,.auto{background:none}body{max-width:none;padding:0}.text{border:0;padding:0;max-height:none!important;overflow:visible!important}}</style></head><body>
<img src="LOGO_URL" alt="True Sense Security" style="height:56px;margin:0 auto 4px;display:block">""".replace('LOGO_URL', "https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w")

AGR_MACRO = """{% macro agr(segs, mode, vals={}, s=None) %}{% for g in segs %}{% if g.t == 'text' %}{{ g.v }}{% elif g.t == 'sub' %}{% if mode == 'sign' %}<input class="fill" type="text" name="f_{{ g.key }}" maxlength="200" placeholder="{{ g.label }}" aria-label="{{ g.label }}" {{ 'required' if g.req else '' }}>{% elif mode == 'copy' %}<span class="val">{{ vals.get(g.key) or '-' }}</span>{% else %}<span class="fill" style="width:auto;display:inline;padding:1px 6px">{{ g.label }}{{ '' if g.req else ' (optional)' }}</span>{% endif %}{% elif g.t == 'box' %}{% if mode == 'sign' %}<input class="box sbox" type="checkbox" name="f_{{ g.key }}" value="1" aria-label="{{ g.label }}">{% elif mode == 'copy' %}{{ '\u2611' if vals.get(g.key) else '\u2610' }}{% else %}<span class="fill" style="width:auto;display:inline;padding:1px 6px">[ ]</span>{% endif %}{% elif g.t == 'office' %}<span class="ofc" title="To be set by office">{{ g.default or '(to be set by office: ' ~ g.label ~ ')' }}</span>{% elif g.t == 'auto' %}{% if mode == 'copy' and s %}{% if g.label == 'name' %}<span class="val">{{ s.typed_name }}</span>{% elif g.label == 'agree' %}\u2611{% elif g.label == 'date' %}<span class="val">{{ s.signed_at }}</span>{% elif g.label == 'signature' %}{% if s.signature and s.signature.startswith('data:image/') %}<img src="{{ s.signature }}" alt="signature" style="height:60px;vertical-align:middle">{% endif %}{% endif %}{% else %}<span class="auto">{{ '[ ]' if g.label == 'agree' else '(filled when signed)' }}</span>{% endif %}{% endif %}{% endfor %}{% endmacro %}"""

AGR_SUB_HTML = AGR_HEAD + AGR_MACRO + """<h2>Subcontractor Agreement</h2><div class="muted" style="text-align:center">Version {{ a.version }}</div>
{% if need %}<div class="job" style="border:2px solid #b00020"><b>Please read and sign before you can open jobs.</b></div>{% endif %}
{% if mine %}<div class="job" style="border:2px solid #2e7d32"><b style="color:#2e7d32">You signed version {{ mine.version }} on {{ mine.signed_at }}.</b> {% if mine.co_signed_at %}<b>Fully signed by True Sense on {{ mine.co_signed_at }}.</b>{% else %}<b style="color:#b26a00">Awaiting company signature.</b>{% endif %}{% if not current %} <b style="color:#b00020">A new version needs your signature.</b>{% endif %}
<a class="btn" href="/agreement/copy/{{ mine.id }}">View my signed copy</a></div>{% endif %}
{% if not current %}<form method="post" action="/agreement/sign" onsubmit="return go()"><div class="job" style="background:#fffde7;border:2px solid #d4b000"><b>Fill in every yellow box in the agreement below.</b> Orange parts are set by True Sense. Then type your name, check I AGREE, and sign.</div>
<div class="text" style="max-height:65vh;overflow-y:auto">{{ agr(segs, 'sign') }}</div><div class="job"><input type="hidden" name="agreement_id" value="{{ a.id }}"><input type="hidden" name="signature" id="sigdata">
<label style="font-weight:600">Type your full legal name<input type="text" name="typed_name" required maxlength="100" autocomplete="name"></label>
<label style="display:flex;gap:10px;align-items:center;font-weight:700;font-size:1.1rem;margin:10px 0"><input type="checkbox" name="agree" value="1" required style="width:28px;height:28px"> I AGREE to version {{ a.version }}</label>
<div style="font-weight:600">Sign with your finger</div><canvas id="pad" style="width:100%;height:180px;border:2px solid #ccc;border-radius:10px;background:#fff;touch-action:none"></canvas>
<button type="button" class="btn" onclick="clr()">Clear Signature</button><button type="submit" class="btn go" style="font-size:1.2rem">Sign</button></div></form>
<script>
const c=document.getElementById('pad'),x=c.getContext('2d');let d=false,inked=false;
function fit(){c.width=c.clientWidth*2;c.height=c.clientHeight*2;x.scale(2,2);x.lineWidth=2.5;x.lineCap='round';x.strokeStyle='#111';inked=false}fit();
function pt(e){const r=c.getBoundingClientRect();return [e.clientX-r.left,e.clientY-r.top]}
c.addEventListener('pointerdown',e=>{d=true;c.setPointerCapture(e.pointerId);const [a,b]=pt(e);x.beginPath();x.moveTo(a,b)});
c.addEventListener('pointermove',e=>{if(!d)return;const [a,b]=pt(e);x.lineTo(a,b);x.stroke();inked=true});
c.addEventListener('pointerup',()=>d=false);c.addEventListener('pointercancel',()=>d=false);
function clr(){x.setTransform(1,0,0,1,0,0);x.clearRect(0,0,c.width,c.height);fit()}
function go(){const bx=document.querySelectorAll('.sbox');if(bx.length&&![...bx].some(b=>b.checked)){alert('Check your entity type box');bx[0].scrollIntoView({block:'center'});return false}if(!inked){alert('Please sign with your finger');return false}document.getElementById('sigdata').value=c.toDataURL('image/png');return true}
</script>{% endif %}
{% if not need %}<a class="btn noprint" href="/">Back to Jobs</a>{% endif %}<a class="btn noprint" href="/logout">Log out</a></body></html>"""

AGR_OFFICE_HTML = AGR_HEAD + AGR_MACRO + """<h2>Subcontractor Agreement</h2><a class="btn" href="/">Back to Jobs</a>
<div class="job" id="pending" style="border:2px solid {{ '#b00020' if pend else '#ddd' }}"><div style="font-weight:700;font-size:1.1rem">Needs Your Signature ({{ pend|length }})</div>
{% for p in pend %}<div style="border-top:1px solid #eee;padding:8px 0"><b>{{ p.sub|title }}</b> signed v{{ p.version }} on {{ p.signed_at }}<a class="btn go" href="/agreement/countersign/{{ p.id }}">Review and Sign</a></div>{% else %}<div class="muted">Nothing waiting.</div>{% endfor %}</div>
{% if done %}<div class="job"><div style="font-weight:700">Fully signed copies</div>{% if not smtp %}<div style="color:#b00020;font-weight:700;margin:4px 0">Email not set up. Use Download / Print to save or print a signed copy.</div>{% endif %}
{% for d in done %}<div style="border-top:1px solid #eee;padding:8px 0"><b>{{ d.sub|title }}</b> v{{ d.version }} &middot; fully signed {{ d.co_signed_at }}<div class="muted">Email: {{ d.email_status or 'Not sent' }}{% if d.email_at %} ({{ d.email_at }}){% endif %}</div>
<div style="display:flex;gap:8px"><a class="btn" href="/agreement/copy/{{ d.id }}">Open Copy</a><a class="btn gold" href="/agreement/copy/{{ d.id }}?print=1">Download / Print</a>{% if smtp %}<form method="post" action="/agreement/email/{{ d.id }}" style="flex:1;margin:0" onsubmit="return confirm('Email the signed copy to the sub and to you?')"><button class="btn gold" type="submit">Email copy</button></form>{% endif %}</div></div>{% endfor %}</div>{% endif %}
<form method="post" action="/agreement/require" class="job" style="border:2px solid {{ '#2e7d32' if required else '#ddd' }}"><div style="font-weight:700;font-size:1.1rem">Require subs to sign: {{ 'ON' if required else 'OFF' }}</div>
<div class="muted">{{ 'Subs must sign the current version before they can open jobs.' if required else 'Subs are not blocked. They can still sign from a link on their home screen.' }}</div>
<input type="hidden" name="on" value="{{ '0' if required else '1' }}"><button class="btn {{ '' if required else 'gold' }}" type="submit">{{ 'Turn OFF' if required else 'Turn ON' }}</button></form>
<div class="job"><div style="font-weight:700">Sub status (current version {{ a.version }})</div><table>{% for n, st, sid in rows %}<tr><td><b>{{ n }}</b></td><td style="color:{{ '#2e7d32' if st.startswith('Fully') else ('#b26a00' if st.startswith('Awaiting') else '#b00020') }};font-weight:600">{{ st }}</td><td>{% if sid %}<a href="/agreement/copy/{{ sid }}">Signed copy</a>{% endif %}</td></tr>{% else %}<tr><td>No subs set in Render</td></tr>{% endfor %}</table></div>
<div class="job"><div style="font-weight:700">Current version {{ a.version }}</div><div class="muted">Added {{ a.created_at }} by {{ a.created_by }}</div>
<div class="muted" style="margin-top:6px">Yellow = the sub fills in. Orange = set by office. Grey = filled when the sub signs.</div>
<div class="text" style="max-height:50vh;overflow-y:auto;margin-top:8px">{{ agr(segs, 'office') }}</div></div>
{% if office_fields %}<form method="post" action="/agreement/fill" class="job" onsubmit="return confirm('Save these as a new version? Every sub will need to sign again.')"><div style="font-weight:700">Fill office blanks</div>
<div class="muted">Fills the orange parts. Saves as a new version. Leave a box empty to keep it open.</div>
{% for f in office_fields %}<label style="font-weight:600;display:block">{{ f.label }}<input type="text" name="o_{{ loop.index0 }}" value="{{ f.default }}" maxlength="200"></label>{% endfor %}
<label style="font-weight:600">Version number<input type="text" name="version" value="{{ next_v }}" maxlength="20"></label><button class="btn gold" type="submit">Save Office Blanks</button></form>{% endif %}
<form method="post" action="/agreement/new" enctype="multipart/form-data" class="job" onsubmit="return confirm('Make this the new version? Every sub will need to sign again.')"><div style="font-weight:700">Add a new version</div>
<div class="muted">Paste the new text or pick a .txt file. All subs will need to sign again.</div>
<div class="muted" style="font-size:.9rem">Blanks: [SUB: label] sub must fill, [SUB?: label] sub may fill, [SUB-BOX: label] sub checkbox, [OFFICE: label = value] set by office, [AUTO: name], [AUTO: agree], [AUTO: signature], [AUTO: date] filled when signed.</div>
<label style="font-weight:600">Version number<input type="text" name="version" value="{{ next_v }}" maxlength="20"></label>
<textarea name="body" rows="8" placeholder="Paste the full agreement text here"></textarea><input type="file" name="file" accept=".txt,text/plain" style="display:block;margin:6px 0">
<button class="btn gold" type="submit">Save New Version</button></form></body></html>"""

AGR_COPY_HTML = AGR_HEAD + AGR_MACRO + """<h2>Subcontractor Agreement: Signed Copy</h2>
{% if autoprint %}<script>addEventListener('load',function(){setTimeout(function(){print()},300)})</script>{% endif %}{% if not standalone %}<div class="noprint"><div class="muted" style="margin:4px 0">To save a PDF: tap Print, then choose Save as PDF.</div><button class="btn gold" onclick="print()">Print or Save PDF</button><a class="btn" href="/agreement">Back</a></div>{% endif %}
<div class="job"><div style="font-weight:700;font-size:1.1rem;color:{{ '#2e7d32' if s.co_signed_at else '#b26a00' }}">{{ 'Fully signed' if s.co_signed_at else 'Awaiting company signature' }}</div><div><b>Version:</b> {{ s.version }}</div><div><b>Signed by:</b> {{ s.typed_name }} (login: {{ s.sub|title }})</div><div><b>Date and time:</b> {{ s.signed_at }}</div></div>
<div class="text">{{ agr(segs, 'copy', vals, s) }}</div>
<div class="job" style="page-break-inside:avoid"><div style="font-weight:700">Subcontractor Signature</div><div><b>Typed name:</b> {{ s.typed_name }}</div><div>&#9745; I AGREE to version {{ s.version }}</div>
{% if s.signature and s.signature.startswith('data:image/') %}<img src="{{ s.signature }}" alt="signature" style="width:320px;max-width:100%;border-bottom:2px solid #111;display:block;margin:8px 0">{% endif %}
<div><b>Signed:</b> {{ s.signed_at }}</div><div class="muted" style="font-size:.8rem">Recorded by True Sense Field Ops. IP {{ s.ip or '-' }}. Device: {{ s.user_agent or '-' }}</div></div>
<div class="job" style="page-break-inside:avoid"><div style="font-weight:700">Company Signature (True Sense Security)</div>{% if s.co_signed_at %}<div><b>Typed name:</b> {{ s.co_name }}</div><div><b>Title:</b> {{ s.co_title or '-' }}</div><div>&#9745; I AGREE to version {{ s.version }}</div>
{% if s.co_signature and s.co_signature.startswith('data:image/') %}<img src="{{ s.co_signature }}" alt="company signature" style="width:320px;max-width:100%;border-bottom:2px solid #111;display:block;margin:8px 0">{% endif %}
<div><b>Signed:</b> {{ s.co_signed_at }}</div><div class="muted" style="font-size:.8rem">Recorded by True Sense Field Ops. IP {{ s.co_ip or '-' }}. Device: {{ s.co_user_agent or '-' }}</div>{% else %}<div style="color:#b26a00;font-weight:600">Awaiting company signature</div>{% endif %}</div>
</body></html>"""

AGR_CO_HTML = AGR_HEAD + AGR_MACRO + """<h2>Company Countersign</h2><a class="btn" href="/agreement">Back</a>
<div class="job"><div><b>Sub:</b> {{ s.typed_name }} (login: {{ s.sub|title }})</div><div><b>Version:</b> {{ s.version }}</div><div><b>Sub signed:</b> {{ s.signed_at }}</div></div>
<div class="text" style="max-height:55vh;overflow-y:auto">{{ agr(segs, 'copy', vals, s) }}</div>
<div class="job"><div style="font-weight:700">Sub Signature</div>{% if s.signature and s.signature.startswith('data:image/') %}<img src="{{ s.signature }}" alt="signature" style="width:260px;max-width:100%;border-bottom:2px solid #111;display:block;margin:8px 0">{% endif %}</div>
<form method="post" class="job" onsubmit="return go()"><div style="font-weight:700;font-size:1.1rem">Sign for True Sense Security</div><input type="hidden" name="signature" id="sigdata">
<label style="font-weight:600">Your full name<input type="text" name="co_name" value="{{ pre_name }}" required maxlength="100"></label>
<label style="font-weight:600">Title<input type="text" name="co_title" value="{{ pre_title }}" maxlength="100"></label>
<label style="display:flex;gap:10px;align-items:center;font-weight:700;font-size:1.1rem;margin:10px 0"><input type="checkbox" name="agree" value="1" required style="width:28px;height:28px"> I AGREE to version {{ s.version }}</label>
<div style="font-weight:600">Sign with your finger</div><canvas id="pad" style="width:100%;height:180px;border:2px solid #ccc;border-radius:10px;background:#fff;touch-action:none"></canvas>
<button type="button" class="btn" onclick="clr()">Clear Signature</button><button type="submit" class="btn go" style="font-size:1.2rem">Sign</button>
<div class="muted">When you sign, the fully signed copy is emailed to the sub and to you.</div></form>
<script>
const c=document.getElementById('pad'),x=c.getContext('2d');let d=false,inked=false;
function fit(){c.width=c.clientWidth*2;c.height=c.clientHeight*2;x.scale(2,2);x.lineWidth=2.5;x.lineCap='round';x.strokeStyle='#111';inked=false}fit();
function pt(e){const r=c.getBoundingClientRect();return [e.clientX-r.left,e.clientY-r.top]}
c.addEventListener('pointerdown',e=>{d=true;c.setPointerCapture(e.pointerId);const [a,b]=pt(e);x.beginPath();x.moveTo(a,b)});
c.addEventListener('pointermove',e=>{if(!d)return;const [a,b]=pt(e);x.lineTo(a,b);x.stroke();inked=true});
c.addEventListener('pointerup',()=>d=false);c.addEventListener('pointercancel',()=>d=false);
function clr(){x.setTransform(1,0,0,1,0,0);x.clearRect(0,0,c.width,c.height);fit()}
function go(){if(!inked){alert('Please sign with your finger');return false}document.getElementById('sigdata').value=c.toDataURL('image/png');return true}
</script></body></html>"""

def agr_sign_data(conn, sid):
    s=conn.execute('SELECT * FROM agreement_signs WHERE id=?',(sid,)).fetchone()
    if not s: return None, None, None, None
    a=conn.execute('SELECT body FROM agreements WHERE id=?',(s['agreement_id'],)).fetchone(); body=a['body'] if a else ''
    try: vals=json.loads(s['fields'] or '{}')
    except Exception: vals={}
    return s, body, agr_parse(body), vals

def pending_countersign(conn):
    return conn.execute('SELECT * FROM agreement_signs s WHERE co_signed_at IS NULL AND id=(SELECT MAX(id) FROM agreement_signs WHERE sub=s.sub) ORDER BY id').fetchall()

def agr_view_token(sid, co_at): return hmac.new(app.secret_key.encode(), ('agr:%s:%s'%(sid,co_at)).encode(), hashlib.sha256).hexdigest()[:32]

SMTP_VARS = ['SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASS', 'SMTP_FROM', 'AGREEMENT_NOTIFY_TO']
def smtp_ready(): return bool(os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'))

def sub_email(segs, vals):
    for g in segs:
        if g['t']=='sub' and g['label'].strip().lower()=='email':
            v=(vals.get(g['key']) or '').strip()
            if re.fullmatch(r'[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+', v): return v
    return ''

def queue_agreement_email(conn, sid, base):
    s, body, segs, vals = agr_sign_data(conn, sid)
    if not s or not s['co_signed_at']: return 'not fully signed'
    if not smtp_ready():
        app.logger.warning('AGREEMENT_EMAIL_SKIPPED %s: SMTP not set up', sid)
        conn.execute("UPDATE agreement_signs SET email_status='Email not set up',email_at=? WHERE id=?",(now_local_str(),sid)); conn.commit(); return 'Email not set up'
    to=[x for x in [sub_email(segs, vals), (os.environ.get('AGREEMENT_NOTIFY_TO') or '').strip()] if x]
    if not to:
        conn.execute("UPDATE agreement_signs SET email_status='Failed: no email address',email_at=? WHERE id=?",(now_local_str(),sid)); conn.commit(); return 'no address'
    html=render_template_string(AGR_COPY_HTML, s=s, segs=segs, vals=vals, standalone=True)
    link=base.rstrip('/')+'/agreement/view/%d?t=%s'%(sid, agr_view_token(sid, s['co_signed_at']))
    fname=secure_filename('TrueSense_Subcontractor_Agreement_%s_v%s.html'%(s['typed_name'] or s['sub'], s['version'])) or 'TrueSense_Subcontractor_Agreement.html'
    conn.execute("UPDATE agreement_signs SET email_status='Sending',email_at=? WHERE id=?",(now_local_str(),sid)); conn.commit()
    threading.Thread(target=send_agreement_email, args=(sid, to, html, link, fname, s['version']), daemon=True).start()
    return 'Sending'

def send_agreement_email(sid, to, html, link, fname, version):
    import smtplib
    from email.message import EmailMessage
    sent, errs = [], []
    host=os.environ.get('SMTP_HOST'); port=int(os.environ.get('SMTP_PORT') or 587); user=os.environ.get('SMTP_USER'); pw=os.environ.get('SMTP_PASS') or ''
    for addr in to:
        try:
            m=EmailMessage(); m['Subject']='TrueSense Subcontractor Agreement, fully signed'; m['From']=os.environ.get('SMTP_FROM'); m['To']=addr
            m.set_content('Hello,\n\nThe True Sense Security Subcontractor Agreement (version %s) is now signed by both parties.\n\nA copy is attached. You can also view it here:\n%s\n\nTrue Sense Security\n' % (version, link))
            m.add_attachment(html.encode('utf-8'), maintype='text', subtype='html', filename=fname)
            if port==465: srv=smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                srv=smtplib.SMTP(host, port, timeout=20); srv.ehlo()
                if srv.has_extn('starttls'): srv.starttls(); srv.ehlo()
            if user: srv.login(user, pw)
            srv.send_message(m); srv.quit(); sent.append(addr)
        except Exception as e:
            errs.append('%s (%s)' % (addr, type(e).__name__)); app.logger.warning('AGREEMENT_EMAIL_FAILED %s %s %s', sid, addr, type(e).__name__)
    st=('Sent to '+', '.join(sent) if sent else '')+(('; ' if sent else '')+'Failed: '+', '.join(errs) if errs else '')
    try:
        conn=get_db(); conn.execute('UPDATE agreement_signs SET email_status=?,email_at=? WHERE id=?',(st[:300],now_local_str(),sid)); conn.commit(); conn.close()
    except Exception: pass

def base_url(): return (os.environ.get('RENDER_EXTERNAL_URL') or request.host_url).rstrip('/')

@app.route('/agreement/countersign/<int:sid>', methods=['GET','POST'])
def agreement_countersign(sid):
    if not is_office(): return ('Office only',403)
    conn=get_db(); s, body, segs, vals = agr_sign_data(conn, sid)
    if not s: conn.close(); return ('Not found',404)
    if s['co_signed_at']: conn.close(); return redirect(f'/agreement/copy/{sid}')
    if request.method=='POST':
        f=request.form; name=(f.get('co_name') or '').strip()[:100]; sig=f.get('signature') or ''
        if not name or f.get('agree')!='1' or not sig.startswith('data:image/png;base64,') or len(sig)>600000: conn.close(); return ('Type your name, check I agree, and sign.',400)
        ip=(request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()[:60]
        n=conn.execute('UPDATE agreement_signs SET co_name=?,co_title=?,co_signature=?,co_signed_at=?,co_by=?,co_ip=?,co_user_agent=? WHERE id=? AND co_signed_at IS NULL',
                       (name,(f.get('co_title') or '').strip()[:100],sig,now_local_str(),me(),ip,(request.headers.get('User-Agent') or '')[:300],sid)).rowcount
        conn.commit()
        if not n: conn.close(); return ('Already countersigned',409)
        queue_agreement_email(conn, sid, base_url()); conn.close()
        return redirect(f'/agreement/copy/{sid}')
    cur_body=current_agreement(conn)['body']; conn.close()
    def pre(lbl):
        for b in (body, cur_body):
            m1=re.search(r'COMPANY SIGNER(.*)', b, re.S); m=re.search(r'^\s*'+lbl+r':\s*(.+)$', m1.group(1) if m1 else '', re.M); v=(m.group(1).strip() if m else '')
            if v and '[' not in v: return v
        return ''
    return render_template_string(AGR_CO_HTML, s=s, segs=segs, vals=vals, pre_name=pre('Typed name') or me(), pre_title=pre('Title'))

@app.route('/agreement/email/<int:sid>', methods=['POST'])
def agreement_email(sid):
    if not is_office(): return ('',403)
    conn=get_db(); st=queue_agreement_email(conn, sid, base_url()); conn.close()
    return redirect('/agreement#pending')

@app.route('/agreement/view/<int:sid>')
def agreement_view(sid):
    conn=get_db(); s, body, segs, vals = agr_sign_data(conn, sid); conn.close()
    if not s or not s['co_signed_at'] or not hmac.compare_digest(request.args.get('t',''), agr_view_token(sid, s['co_signed_at'])): return ('Link not valid',404)
    return render_template_string(AGR_COPY_HTML, s=s, segs=segs, vals=vals, standalone=True)

@app.route('/api/agreements/pending')
def agreements_pending():
    if not is_office(): return jsonify({'error':'Office only'}),403
    conn=get_db(); rows=[{'id':r['id'],'sub':nice_name(r['sub']),'version':r['version'],'signed_at':r['signed_at']} for r in pending_countersign(conn)]; conn.close()
    return jsonify(rows)

@app.route('/agreement')
def agreement_page():
    conn=get_db(); a=current_agreement(conn)
    if is_office():
        rows=[]
        for n in sub_names(conn):
            ls=last_sign(conn, n)
            if not ls: rows.append((n,'Not signed',None))
            elif ls['agreement_id']==a['id']: rows.append((n,('Fully signed v'+ls['version']+' on '+ls['co_signed_at']) if ls['co_signed_at'] else ('Awaiting company signature (sub signed v'+ls['version']+' on '+ls['signed_at']+')'),ls['id']))
            else: rows.append((n,'Needs re-sign (signed v'+ls['version']+')',ls['id']))
        req=agreement_required(conn)
        pend_rows=pending_countersign(conn)
        done_rows=conn.execute('SELECT s.* FROM agreement_signs s WHERE co_signed_at IS NOT NULL AND id=(SELECT MAX(id) FROM agreement_signs WHERE sub=s.sub) ORDER BY id DESC').fetchall(); conn.close()
        segs=agr_parse(a['body'])
        return render_template_string(AGR_OFFICE_HTML, pend=pend_rows, done=done_rows, smtp=smtp_ready(), a=a, segs=segs, office_fields=[g for g in segs if g['t']=='office'], rows=rows, required=req, next_v=bump_version(a['version']))
    mine=last_sign(conn, me()); cur=signed_current(conn, me()); need=agreement_required(conn) and not cur; conn.close()
    return render_template_string(AGR_SUB_HTML, a=a, segs=agr_parse(a['body']), mine=mine, current=cur, need=need)

@app.route('/agreement/sign', methods=['POST'])
def agreement_sign():
    if is_office(): return redirect('/agreement')
    f=request.form; name=(f.get('typed_name') or '').strip()[:100]; sig=f.get('signature') or ''
    conn=get_db(); a=current_agreement(conn)
    if str(a['id'])!=(f.get('agreement_id') or ''): conn.close(); return ('The agreement changed. Go back and read the new version.',409)
    if not name or f.get('agree')!='1' or not sig.startswith('data:image/png;base64,') or len(sig)>600000: conn.close(); return ('Type your name, check I agree, and sign.',400)
    vals={}; segs=agr_parse(a['body']); boxes=[g for g in segs if g['t']=='box']
    for g in segs:
        if g['t']=='sub':
            v=(f.get('f_'+g['key']) or '').strip()[:200]
            if g['req'] and not v: conn.close(); return ('Fill in every yellow box: '+g['label'],400)
            vals[g['key']]=v
        elif g['t']=='box': vals[g['key']]=f.get('f_'+g['key'])=='1'
    if boxes and not any(vals[g['key']] for g in boxes): conn.close(); return ('Check your entity type box',400)
    ip=(request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()[:60]
    cur=conn.execute('INSERT INTO agreement_signs (sub,agreement_id,version,typed_name,signature,signed_at,ip,user_agent,fields) VALUES (?,?,?,?,?,?,?,?,?)',
                     (ckey(me()),a['id'],a['version'],name,sig,now_local_str(),ip,(request.headers.get('User-Agent') or '')[:300],json.dumps(vals)))
    conn.commit(); sid=cur.lastrowid; conn.close()
    return redirect(f'/agreement/copy/{sid}')

@app.route('/agreement/copy/<int:sid>')
def agreement_copy(sid):
    conn=get_db(); s=conn.execute('SELECT * FROM agreement_signs WHERE id=?',(sid,)).fetchone()
    if not s or not (is_office() or s['sub']==ckey(me())): conn.close(); return ('Not allowed',403)
    a=conn.execute('SELECT body FROM agreements WHERE id=?',(s['agreement_id'],)).fetchone(); conn.close()
    try: vals=json.loads(s['fields'] or '{}')
    except Exception: vals={}
    return render_template_string(AGR_COPY_HTML, s=s, segs=agr_parse(a['body'] if a else ''), vals=vals, standalone=False, autoprint=request.args.get('print')=='1')

@app.route('/agreement/require', methods=['POST'])
def agreement_require():
    if not is_office(): return ('',403)
    conn=get_db(); conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('require_agreement',?)",('1' if request.form.get('on')=='1' else '0',)); conn.commit(); conn.close()
    return redirect('/agreement')

@app.route('/agreement/fill', methods=['POST'])
def agreement_fill():
    if not is_office(): return ('',403)
    conn=get_db(); a=current_agreement(conn); out=[]; i=0
    for g in agr_parse(a['body']):
        if g['t']=='text': out.append(g['v'])
        elif g['t']=='office':
            v=(request.form.get('o_%d'%i) or '').strip()[:200]; i+=1
            out.append(v.replace('[','(').replace(']',')') if v else g['raw'])
        elif g['t']=='sub': out.append('[SUB%s: %s]'%('' if g['req'] else '?', g['label']))
        elif g['t']=='box': out.append('[SUB-BOX: %s]'%g['label'])
        else: out.append('[AUTO: %s]'%g['label'])
    v=(request.form.get('version') or '').strip()[:20] or bump_version(a['version'])
    conn.execute('INSERT INTO agreements (version,body,created_at,created_by) VALUES (?,?,?,?)',(v,''.join(out),now_local_str(),me())); conn.commit(); conn.close()
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

@app.route('/api/jobs/<job_id>/sale', methods=['POST'])
def save_sale(job_id):
    if not is_office(): return jsonify({'error':'Office only'}),403
    v=str((request.get_json(silent=True) or {}).get('sale_amount') or '').replace('$','').replace(',','').strip()
    try: amt=round(float(v),2) if v else None
    except ValueError: return jsonify({'error':'Numbers only'}),400
    if amt is not None and not (0<=amt<1e8): return jsonify({'error':'Numbers only'}),400
    conn=get_db(); n=conn.execute('UPDATE jobs SET sale_amount=? WHERE id=?',(amt,job_id)).rowcount; conn.commit(); conn.close()
    return (jsonify({'ok':True,'sale_amount':amt}) if n else ('',404))

def month_keys(n=12):
    t=today_local(); y,m=t.year,t.month; out=[]
    for _ in range(n): out.append('%04d-%02d'%(y,m)); m-=1; y,m=(y-1,12) if m==0 else (y,m)
    return out[::-1]

def local_month(iso):
    try:
        dt=datetime.fromisoformat(iso[:19])
        from zoneinfo import ZoneInfo
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('America/Chicago')).strftime('%Y-%m')
    except Exception: return (iso or '')[:7]

def parse_qbo_rows(rows):
    out=[]
    for r in rows:
        mo=str(r.get('month') or '').strip().strip('"\'').strip()[:7]
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', mo): raise ValueError('Bad month: '+mo)
        vals=[]
        for k in ('invoiced','paid'):
            v=str(r.get(k) if r.get(k) is not None else '').replace('$','').replace(',','').strip().strip('"\'').strip()
            try: vals.append(round(float(v),2) if v else None)
            except ValueError: raise ValueError('Numbers only for '+mo)
        out.append((mo,vals[0],vals[1]))
    return out

def save_qbo_rows(rows, source):
    conn=get_db(); now=now_local_str()
    for mo,inv,paid in rows:
        if inv is None and paid is None: conn.execute('DELETE FROM qbo_monthly WHERE month=?',(mo,)); continue  # both blank clears the month
        conn.execute('INSERT OR REPLACE INTO qbo_monthly (month,invoiced,paid,synced_at,source) VALUES (?,?,?,?,?)',(mo,inv,paid,now,source))
    conn.commit(); conn.close(); return now

@app.route('/api/qbo-monthly', methods=['POST'])
def qbo_monthly_api():
    key=os.environ.get('QBO_SYNC_KEY') or ''
    if not key: return jsonify({'error':'QBO_SYNC_KEY is not set'}),503
    if not hmac.compare_digest((request.headers.get('X-Sync-Key') or '').encode(), key.encode()): return jsonify({'error':'bad key'}),403
    d=request.get_json(silent=True) or {}
    try: rows=parse_qbo_rows(d.get('months') or [])
    except (ValueError, TypeError) as e: return jsonify({'error':str(e)[:100]}),400
    if not rows: return jsonify({'error':'months required'}),400
    return jsonify({'ok':True,'saved':len(rows),'synced_at':save_qbo_rows(rows,'api')})

REV_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Revenue</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,Arial,sans-serif;background:#fff;color:#111;max-width:900px;margin:auto;padding:12px}h2{color:#a49f2a;margin:8px 0;text-align:center}
.job{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin:10px 0}.muted{color:#666}.btn{display:block;width:100%;background:#f0f0f0;color:#111;padding:16px;border:0;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;font-size:1.05rem;margin:8px 0}.gold{background:#a49f2a;color:#fff}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}table{border-collapse:collapse;width:100%;font-size:1rem}td,th{border-bottom:1px solid #eee;padding:10px 6px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}tr.tot td{font-weight:700;border-top:2px solid #111}
.big{display:flex;gap:8px;flex-wrap:wrap}.big div{flex:1 1 140px;border:2px solid #e5e5e5;border-radius:12px;padding:12px;text-align:center}.big b{display:block;font-size:1.5rem}
.key{display:flex;gap:14px;flex-wrap:wrap;font-weight:600;margin:6px 0}.key span:before{content:"";display:inline-block;width:14px;height:14px;border-radius:3px;margin-right:6px;vertical-align:-2px;background:var(--c)}
textarea{width:100%;padding:12px;border:1px solid #ddd;border-radius:10px;font-size:1rem}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;margin:0 auto 4px;display:block">
<h2>Revenue (office only)</h2><a class="btn" href="/">Back to Jobs</a><a class="btn" href="/reports#charts">Charts: work type, status, profit by sub, jobs per month, estimates</a>
<div class="big"><div>Field Ops sales {{ year }}<b>{{ money(ytd.fo) }}</b></div><div>QuickBooks invoiced {{ year }}<b>{{ money(ytd.qi) }}</b></div><div>QuickBooks paid {{ year }}<b>{{ money(ytd.qp) }}</b></div><div>Field Ops margin {{ year }}<b>{{ money(ytd.fo - ytd.lab) }}</b></div></div>
<div class="job"><div style="font-weight:700;font-size:1.1rem">Last 12 months</div>
<div class="key"><span style="--c:#a49f2a">Field Ops sales</span><span style="--c:#1565c0">QuickBooks invoiced</span><span style="--c:#2e7d32">QuickBooks paid</span></div>
<div class="wrap"><svg viewBox="0 0 {{ W }} {{ H }}" style="min-width:640px;width:100%;height:auto" role="img" aria-label="Monthly revenue chart">
{% for g in grid %}<line x1="70" x2="{{ W-10 }}" y1="{{ g.y }}" y2="{{ g.y }}" stroke="#eee"/><text x="64" y="{{ g.y+5 }}" text-anchor="end" font-size="14" fill="#666">{{ g.label }}</text>{% endfor %}
{% for b in bars %}<rect x="{{ b.x }}" y="{{ b.y }}" width="{{ b.w }}" height="{{ b.h }}" fill="{{ b.c }}"><title>{{ b.t }}</title></rect>{% endfor %}
{% for m in rows %}<text x="{{ m.cx }}" y="{{ H-12 }}" text-anchor="middle" font-size="15" fill="#111">{{ m.short }}</text>{% endfor %}
<line x1="70" x2="{{ W-10 }}" y1="{{ base }}" y2="{{ base }}" stroke="#111"/></svg></div>
<div class="muted">QuickBooks last synced: {{ synced or 'never' }}</div></div>
<div class="job wrap"><table><tr><th>Month</th><th>Field Ops sales</th><th>Sub labor</th><th>Margin</th><th>Jobs</th><th>QB invoiced</th><th>QB paid</th></tr>
{% for m in rows %}<tr><td>{{ m.label }}</td><td>{{ money(m.fo) }}</td><td>{{ money(m.lab) }}</td><td>{{ money(m.fo - m.lab) }}</td><td>{{ m.n }}{% if m.missing %} <span style="color:#b00020" title="Approved jobs with no invoice amount">({{ m.missing }} no $)</span>{% endif %}</td><td>{{ '-' if m.qi is none else money(m.qi) }}</td><td>{{ '-' if m.qp is none else money(m.qp) }}</td></tr>{% endfor %}
<tr class="tot"><td>12 months</td><td>{{ money(tot.fo) }}</td><td>{{ money(tot.lab) }}</td><td>{{ money(tot.fo - tot.lab) }}</td><td>{{ tot.n }}</td><td>{{ money(tot.qi) }}</td><td>{{ money(tot.qp) }}</td></tr>
<tr class="tot"><td>{{ year }} so far</td><td>{{ money(ytd.fo) }}</td><td>{{ money(ytd.lab) }}</td><td>{{ money(ytd.fo - ytd.lab) }}</td><td>{{ ytd.n }}</td><td>{{ money(ytd.qi) }}</td><td>{{ money(ytd.qp) }}</td></tr></table>
<div class="muted" style="margin-top:6px">Field Ops sales = the invoice amount on each approved job, by month the work was finished. Sub labor uses rates locked at approval.</div></div>
<form method="post" action="/revenue/qbo-import" class="job"><div style="font-weight:700">Update QuickBooks numbers</div><div class="muted">Paste one line per month: month,invoiced,paid (example: 2026-05,10793.83,5250.13). Lines replace that month.</div>
<textarea name="csv" rows="6" placeholder="2026-05,10793.83,5250.13"></textarea>{% if msg %}<div style="font-weight:700;color:{{ '#2e7d32' if ok else '#b00020' }}">{{ msg }}</div>{% endif %}<button class="btn gold" type="submit">Save QuickBooks Numbers</button></form>
</body></html>"""

def revenue_data():
    keys=month_keys(12); year=today_local().year; conn=get_db()
    rows={k:{'month':k,'fo':0.0,'lab':0.0,'n':0,'missing':0,'qi':None,'qp':None} for k in keys}
    for j in conn.execute("SELECT * FROM jobs WHERE status='completed' AND COALESCE(job_type,'') NOT IN ('Survey','Office Task') AND completed_at IS NOT NULL").fetchall():
        k=local_month(j['completed_at'])
        if k in rows:
            r=rows[k]; r['n']+=1; r['fo']+=j['sale_amount'] or 0; r['lab']+=job_labor(conn,j)['total']
            if j['sale_amount'] is None: r['missing']+=1
    synced=None
    for q in conn.execute('SELECT * FROM qbo_monthly').fetchall():
        if q['month'] in rows: rows[q['month']]['qi']=q['invoiced']; rows[q['month']]['qp']=q['paid']
        if q['synced_at'] and (synced is None or q['synced_at']>synced): synced=q['synced_at']
    conn.close()
    out=[rows[k] for k in keys]
    for r in out:
        d=datetime.strptime(r['month'],'%Y-%m'); r['label']=d.strftime('%b %Y'); r['short']=d.strftime('%b')
    def total(rs): return {'fo':sum(r['fo'] for r in rs),'lab':sum(r['lab'] for r in rs),'n':sum(r['n'] for r in rs),'qi':sum(r['qi'] or 0 for r in rs),'qp':sum(r['qp'] or 0 for r in rs)}
    return out, total(out), total([r for r in out if r['month'].startswith(str(year))]), year, synced

@app.route('/revenue', methods=['GET'])
def revenue_page(msg=None, ok=True):
    if not is_office(): return redirect('/')
    rows,tot,ytd,year,synced=revenue_data()
    W,H,base,top=760,330,290,20; mx=max([r['fo'] for r in rows]+[r['qi'] or 0 for r in rows]+[r['qp'] or 0 for r in rows]+[1])
    step=10**max(0,len(str(int(mx)))-1); nice=next(m*step for m in (1,2,2.5,5,10) if m*step>=mx); sc=(base-top)/nice
    def short(v): return '$%gk'%(v/1000) if v>=1000 else '$%d'%v
    grid=[{'y':base-sc*nice*i/4,'label':short(nice*i/4)} for i in range(5)]
    slot=(W-80)/12; bw=slot/4; bars=[]
    for i,r in enumerate(rows):
        x0=80+slot*i+bw*0.5; r['cx']=80+slot*i+slot/2
        for k,(v,c,nm) in enumerate([(r['fo'],'#a49f2a','Field Ops sales'),(r['qi'] or 0,'#1565c0','QuickBooks invoiced'),(r['qp'] or 0,'#2e7d32','QuickBooks paid')]):
            h=v*sc; bars.append({'x':round(x0+bw*k,1),'y':round(base-h,1),'w':round(bw*0.9,1),'h':round(h,1),'c':c,'t':'%s %s: $%s'%(r['label'],nm,format(v,',.2f'))})
    return render_template_string(REV_HTML, rows=rows, tot=tot, ytd=ytd, year=year, synced=synced, grid=grid, bars=bars, W=W, H=H, base=base, msg=msg, ok=ok, money=lambda v: '$'+format(v or 0,',.2f'))

@app.route('/revenue/qbo-import', methods=['POST'])
def revenue_qbo_import():
    if not is_office(): return ('',403)
    lines=[l.strip() for l in (request.form.get('csv') or '').splitlines() if l.strip() and not l.strip().lower().startswith('month')]
    import csv
    try: rows=parse_qbo_rows([dict(zip(('month','invoiced','paid'),[c.strip() for c in cells]+['',''])) for cells in csv.reader(lines) if cells])
    except (ValueError, TypeError) as e: return revenue_page(msg='Not saved. '+str(e)[:80], ok=False)
    if not rows: return revenue_page(msg='Paste at least one line', ok=False)
    save_qbo_rows(rows,'paste'); return revenue_page(msg='Saved %d month%s'%(len(rows),'' if len(rows)==1 else 's'))

FAR_FT=1320

def queued_time():
    v=(request.headers.get('X-Queued-At') or '').strip()
    try:
        from datetime import timezone
        t=datetime.fromisoformat(v.replace('Z','+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception: return None
    now=datetime.utcnow()
    return t.isoformat(timespec='seconds') if now-timedelta(days=3)<=t<=now+timedelta(minutes=2) else None

def utc_to_local(iso, fmt='%m/%d %I:%M %p'):
    if not iso: return ''
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone
        return datetime.fromisoformat(iso[:19]).replace(tzinfo=timezone.utc).astimezone(ZoneInfo('America/Chicago')).strftime(fmt)
    except Exception: return iso[:16]

def local_to_utc(s):
    from zoneinfo import ZoneInfo
    from datetime import timezone
    return datetime.fromisoformat(s.strip()[:16]).replace(tzinfo=ZoneInfo('America/Chicago')).astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')

def site_point(conn, row):
    g=parse_map(row['map_data'])['geo']
    if g and g.get('lat') is not None and g.get('addr','')==(row['address'] or '').strip() or (g and g.get('lat') is not None and g.get('q')=='My location'): return (g['lat'],g['lng'])
    addr=(row['address'] or '').strip()
    if not addr or (g and g.get('addr')==addr and g.get('lat') is None): return None
    try: hit=nominatim(addr)
    except Exception: return (g['lat'],g['lng']) if g and g.get('lat') is not None else None
    g={'addr':addr,'q':addr,'lat':hit[0] if hit else None,'lng':hit[1] if hit else None}; set_geo(conn, row, g)
    return (g['lat'],g['lng']) if hit else None

def punch_check(conn, row, d):
    lat=_num(d.get('lat'),-90,90,None); lng=_num(d.get('lng'),-180,180,None); acc=_num(d.get('acc'),0,1e6,None)
    if lat is None or lng is None: return None,None,None,None,'No location (blocked or off)'
    site=site_point(conn, row)
    if not site: return lat,lng,acc,None,'No site pin to compare'
    ft=round(feet_between((lat,lng),site)); return lat,lng,acc,ft,('Far from site (%s)'%dist_text(ft) if ft>FAR_FT else '')

def dist_text(ft):
    if ft is None: return ''
    return ('%d ft'%ft) if ft<1000 else ('%.1f mi'%(ft/5280))

def add_hours_for(conn, row, sub, delta):
    if not delta: return
    if not conn.execute('SELECT 1 FROM job_hours WHERE job_id=?',(row['id'],)).fetchone() and (row['total_minutes'] or 0) and ckey(row['tech']):
        conn.execute('INSERT INTO job_hours (job_id,sub,minutes) VALUES (?,?,?)',(row['id'],ckey(row['tech']),row['total_minutes']))
    conn.execute('UPDATE jobs SET total_minutes=MAX(0,IFNULL(total_minutes,0)+?) WHERE id=?',(int(delta),row['id']))
    conn.execute('INSERT INTO job_hours (job_id,sub,minutes) VALUES (?,?,0) ON CONFLICT(job_id,sub) DO NOTHING',(row['id'],sub))
    conn.execute('UPDATE job_hours SET minutes=MAX(0,minutes+?) WHERE job_id=? AND sub=?',(int(delta),row['id'],sub))

def punch_minutes(a, b):
    try: return max(0, int(round((datetime.fromisoformat(b[:19])-datetime.fromisoformat(a[:19])).total_seconds()/60)))
    except Exception: return 0

def clock_state(conn, job_id):
    who=ckey(me()); o=conn.execute('SELECT * FROM time_punches WHERE sub=? AND out_at IS NULL ORDER BY id DESC LIMIT 1',(who,)).fetchone()
    day=today_local().isoformat(); mins=sum(p['minutes'] or 0 for p in conn.execute('SELECT in_at,minutes FROM time_punches WHERE job_id=? AND sub=? AND out_at IS NOT NULL',(job_id,who)).fetchall() if utc_to_local(p['in_at'],'%Y-%m-%d')==day)
    st={'open':bool(o and o['job_id']==job_id),'other':o['job_id'] if o and o['job_id']!=job_id else '','today_hours':round(mins/60,2)}
    if st['open']: st.update(in_local=utc_to_local(o['in_at'],'%I:%M %p'),in_dist=(dist_text(o['in_ft'])+' from site') if o['in_ft'] is not None else '',in_flag=o['in_flag'] or '')
    return st

@app.route('/api/jobs/<job_id>/clock', methods=['POST'])
def clock_tap(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    who=ckey(me()); d=request.get_json(silent=True) or {}; now=queued_time() or datetime.utcnow().isoformat(timespec='seconds')
    o=conn.execute('SELECT * FROM time_punches WHERE sub=? AND out_at IS NULL ORDER BY id DESC LIMIT 1',(who,)).fetchone()
    if o and o['job_id']!=job_id: conn.close(); return jsonify({'error':'You are still clocked in on job %s. Clock out there first.'%o['job_id']}),409
    lat,lng,acc,ft,flag=punch_check(conn, row, d); out={'ok':True}
    if o:
        mins=punch_minutes(o['in_at'],now)
        conn.execute('UPDATE time_punches SET out_at=?,out_lat=?,out_lng=?,out_acc=?,out_ft=?,out_flag=?,minutes=? WHERE id=?',(now,lat,lng,acc,ft,flag,mins,o['id']))
        add_hours_for(conn, row, who, mins); out['msg']='Clocked out. %s hrs added.'%round(mins/60,2)
    else:
        if row['status'] in ('completed','review') and not is_office(): conn.close(); return jsonify({'error':'This job is finished. Ask the office.'}),409
        conn.execute('INSERT INTO time_punches (job_id,sub,in_at,in_lat,in_lng,in_acc,in_ft,in_flag) VALUES (?,?,?,?,?,?,?,?)',(job_id,who,now,lat,lng,acc,ft,flag))
        out['msg']='Clocked in.'
    if ft is not None: out['msg']+=' You are %s from the site.'%dist_text(ft)
    if flag: out['msg']+=' Flag: '+flag+'.'
    conn.commit(); j=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    out['clock']=clock_state(conn, job_id); out['hours']=round((j['total_minutes'] or 0)/60,2)
    if is_office(): out['labor']=job_labor(conn, j)
    conn.close(); return jsonify(out)

TIMELOG_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time Log</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,Arial,sans-serif;background:#fff;color:#111;max-width:900px;margin:auto;padding:12px;font-size:1.05rem}h2{color:#a49f2a;text-align:center;margin:8px 0}
.job{border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin:10px 0}.flag{color:#b00020;font-weight:700}.muted{color:#666}.btn{display:block;width:100%;background:#f0f0f0;color:#111;padding:16px;border:0;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;font-size:1.05rem;margin:8px 0}.gold{background:#a49f2a;color:#fff}
input,select{width:100%;padding:12px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:4px 0}label{font-weight:600;display:block;margin-top:6px}details summary{cursor:pointer;font-weight:600;padding:8px 0}</style></head><body>
<h2>Time Log (office only)</h2><a class="btn" href="{{ '/?job='+job if job else '/' }}">Back</a>
<form method="get"><div style="display:flex;gap:8px"><input name="job" value="{{ job }}" placeholder="Job ID (blank = all)"><select name="flag"><option value="">All</option><option value="1"{% if flagged %} selected{% endif %}>Flagged only</option></select></div><button class="btn" type="submit">Show</button></form>
{% if msg %}<div class="job" style="font-weight:700;color:{{ '#2e7d32' if ok else '#b00020' }}">{{ msg }}</div>{% endif %}
{% for p in punches %}<div class="job"><div><b>{{ p.sub_name }}</b> &middot; Job <a href="/?job={{ p.job_id }}">{{ p.job_id }}</a> &middot; {{ p.hours }} hrs{% if not p.out_at %} <span class="flag">(still clocked in)</span>{% endif %}</div>
<div>In: {{ p.in_local }}{% if p.in_dist %} &middot; {{ p.in_dist }} from site{% endif %}{% if p.in_flag %} <span class="flag">{{ p.in_flag }}</span>{% endif %}</div>
<div>Out: {{ p.out_local or '-' }}{% if p.out_dist %} &middot; {{ p.out_dist }} from site{% endif %}{% if p.out_flag %} <span class="flag">{{ p.out_flag }}</span>{% endif %}</div>
{% if p.edit_note %}<div class="muted">Edited by {{ p.edited_by }} {{ p.edited_local }}: {{ p.edit_note }}</div>{% endif %}
<details><summary>Edit</summary><form method="post" action="/timelog/edit/{{ p.id }}"><input type="hidden" name="back" value="{{ job }}"><label>In</label><input type="datetime-local" name="in_at" value="{{ p.in_val }}" required><label>Out</label><input type="datetime-local" name="out_at" value="{{ p.out_val }}"><label>Why (required)</label><input name="note" required maxlength="300" placeholder="Forgot to clock out"><button class="btn gold" type="submit">Save Change</button></form></details></div>
{% else %}<div class="job muted">No clock ins yet.</div>{% endfor %}</body></html>"""

@app.route('/timelog')
def timelog_page(msg=None, ok=True):
    if not is_office(): return redirect('/')
    job=(request.values.get('job') or request.values.get('back') or '').strip()[:60]; flagged=request.args.get('flag')=='1'
    q='SELECT * FROM time_punches'; a=[]
    w=[]
    if job: w.append('job_id=?'); a.append(job)
    if flagged: w.append("(IFNULL(in_flag,'')!='' OR IFNULL(out_flag,'')!='' OR out_at IS NULL)")
    if w: q+=' WHERE '+' AND '.join(w)
    conn=get_db(); rows=conn.execute(q+' ORDER BY id DESC LIMIT 300',a).fetchall(); conn.close(); ps=[]
    for r in rows:
        p=dict(r); p.update(sub_name=nice_name(r['sub']),hours=round((r['minutes'] or 0)/60,2),in_local=utc_to_local(r['in_at']),out_local=utc_to_local(r['out_at']),
            in_dist=dist_text(r['in_ft']),out_dist=dist_text(r['out_ft']),edited_local=utc_to_local(r['edited_at']),
            in_val=utc_to_local(r['in_at'],'%Y-%m-%dT%H:%M'),out_val=utc_to_local(r['out_at'],'%Y-%m-%dT%H:%M')); ps.append(p)
    return render_template_string(TIMELOG_HTML, punches=ps, job=job, flagged=flagged, msg=msg, ok=ok)

@app.route('/timelog/edit/<int:pid>', methods=['POST'])
def timelog_edit(pid):
    if not is_office(): return ('',403)
    note=(request.form.get('note') or '').strip()[:300]
    if not note: return timelog_page(msg='Not saved. Add a reason.', ok=False)
    try:
        ia=local_to_utc(request.form.get('in_at') or ''); oa=(request.form.get('out_at') or '').strip(); oa=local_to_utc(oa) if oa else None
    except Exception: return timelog_page(msg='Not saved. Check the times.', ok=False)
    if oa and oa<ia: return timelog_page(msg='Not saved. Out is before In.', ok=False)
    conn=get_db(); p=conn.execute('SELECT * FROM time_punches WHERE id=?',(pid,)).fetchone()
    if not p: conn.close(); return ('',404)
    mins=punch_minutes(ia,oa) if oa else 0
    conn.execute('UPDATE time_punches SET in_at=?,out_at=?,minutes=?,edit_note=?,edited_by=?,edited_at=? WHERE id=?',(ia,oa,mins,note,me(),datetime.utcnow().isoformat(timespec='seconds'),pid))
    row=conn.execute('SELECT * FROM jobs WHERE id=?',(p['job_id'],)).fetchone()
    if row: add_hours_for(conn, row, p['sub'], mins-(p['minutes'] or 0))
    conn.commit(); conn.close()
    return timelog_page(msg='Saved. Hours updated.')

FIELD_HEAD = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }}</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,Arial,sans-serif;background:#fff;color:#111;max-width:700px;margin:auto;padding:12px;font-size:1.05rem}h2{color:#a49f2a;text-align:center;margin:8px 0}
.job{border:1px solid #e5e5e5;border-radius:12px;padding:12px;margin:10px 0}.muted{color:#666}.flag{color:#b00020;font-weight:700}.ok{color:#2e7d32;font-weight:700}
.btn{display:block;width:100%;background:#f0f0f0;color:#111;padding:16px;border:0;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;font-size:1.1rem;margin:8px 0;font-family:inherit}.gold{background:#a49f2a;color:#fff}
input,select,textarea{width:100%;padding:12px;border:1px solid #ddd;border-radius:10px;font-size:1rem;margin:4px 0;font-family:inherit}label{font-weight:600;display:block;margin-top:10px}
.pics img{width:31%;margin:1%;border-radius:8px;aspect-ratio:1;object-fit:cover}.pre{white-space:pre-wrap}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:44px;margin:0 auto 4px;display:block">"""

DAILY_HTML = FIELD_HEAD + """<h2>Daily Log</h2><div style="text-align:center;font-weight:600">Job {{ j.id }} &middot; {{ j.title }}</div><div class="muted" style="text-align:center">{{ j.customer or '' }}</div>
<a class="btn" href="/?job={{ j.id }}">Back to Job</a>
{% if msg %}<div class="job {{ 'ok' if ok else 'flag' }}">{{ msg }}</div>{% endif %}
<form method="post" enctype="multipart/form-data" class="job"><label>Date</label><input type="date" name="log_date" value="{{ cur.log_date }}" max="{{ today }}" onchange="location='?date='+this.value" required>
<div class="muted">{% if cur.id %}Editing the log for this day{% if cur.author %} (by {{ cur.author }}){% endif %}.{% else %}One log per day. Fill it before you leave.{% endif %}</div>
<label>Work done today</label><textarea name="work_done" rows="4" required maxlength="3000" placeholder="Ran 6 cables, hung 2 cameras">{{ cur.work_done or '' }}</textarea>
<label>Crew on site</label><input name="crew" maxlength="300" value="{{ cur.crew or '' }}" placeholder="Mike, Joe">
<label>Crew hours (total)</label><input type="number" name="hours" step="0.25" min="0" max="200" inputmode="decimal" value="{{ '' if cur.hours is none else cur.hours }}" placeholder="8">
<label>Materials used</label><textarea name="materials" rows="2" maxlength="2000" placeholder="500 ft Cat6, 2 cameras">{{ cur.materials or '' }}</textarea>
<label>Delays or problems</label><textarea name="issues" rows="2" maxlength="2000" placeholder="None">{{ cur.issues or '' }}</textarea>
<label>Weather</label><input name="weather" maxlength="100" value="{{ cur.weather or '' }}" placeholder="Sunny, 75">
<label>Add photos</label><input type="file" name="photos" accept="image/*" multiple>
{% if cur.pics %}<div class="pics">{% for f in cur.pics %}<a href="/uploads/{{ f }}" target="_blank"><img src="/uploads/{{ f }}" alt="photo"></a>{% endfor %}</div>{% endif %}
<button class="btn gold" type="submit">Save Daily Log</button></form>
<h3>Past logs</h3>{% for l in logs %}<div class="job"><div><b><a href="?date={{ l.log_date }}">{{ l.nice_date }}</a></b> &middot; {{ l.author }}{% if l.hours is not none %} &middot; {{ l.hours }} hrs{% endif %}{% if l.weather %} &middot; {{ l.weather }}{% endif %}</div>
<div class="pre">{{ l.work_done }}</div>{% if l.crew %}<div><b>Crew:</b> {{ l.crew }}</div>{% endif %}{% if l.materials %}<div class="pre"><b>Materials:</b> {{ l.materials }}</div>{% endif %}{% if l.issues %}<div class="pre"><b>Problems:</b> {{ l.issues }}</div>{% endif %}
{% if l.pics %}<div class="pics">{% for f in l.pics %}<a href="/uploads/{{ f }}" target="_blank"><img src="/uploads/{{ f }}" alt="photo"></a>{% endfor %}</div>{% endif %}</div>{% else %}<div class="muted">No logs yet.</div>{% endfor %}
</body></html>"""

def page_job(conn, job_id):
    row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    return row if row and can_see(row) else None

def log_dict(r):
    d=dict(r)
    try: d['pics']=json.loads(r['photos'] or '[]')
    except Exception: d['pics']=[]
    try: d['nice_date']=datetime.strptime(r['log_date'],'%Y-%m-%d').strftime('%a %b %d, %Y')
    except Exception: d['nice_date']=r['log_date']
    return d

@app.route('/job/<job_id>/daily', methods=['GET','POST'])
def daily_log(job_id):
    conn=get_db(); row=page_job(conn, job_id)
    if not row: conn.close(); return redirect('/')
    today=today_local().isoformat(); msg=None; ok=True
    day=(request.form.get('log_date') if request.method=='POST' else request.args.get('date')) or today
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or day>today: day=today
    if request.method=='POST':
        f=lambda k,n: (request.form.get(k) or '').strip()[:n]
        work=f('work_done',3000)
        try: hrs=round(float(f('hours',10)),2) if f('hours',10) else None
        except ValueError: hrs=None
        if hrs is not None and not (0<=hrs<=200): hrs=None
        if not work: msg='Not saved. Fill in Work done today.'; ok=False
        else:
            now=datetime.utcnow().isoformat(timespec='seconds')
            old=conn.execute('SELECT photos FROM daily_logs WHERE job_id=? AND log_date=?',(job_id,day)).fetchone()
            pics=json.loads(old['photos'] or '[]') if old else []
            for up in request.files.getlist('photos')[:20]:
                if up and up.filename:
                    fn=secure_filename(f"{job_id}_daily_{uuid.uuid4().hex[:8]}.jpg"); up.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
                    conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at) VALUES (?,?,?,?)',(job_id,fn,'daily',now)); pics.append(fn)
            vals=(work,f('crew',300),hrs,f('materials',2000),f('issues',2000),f('weather',100),json.dumps(pics))
            if old: conn.execute('UPDATE daily_logs SET work_done=?,crew=?,hours=?,materials=?,issues=?,weather=?,photos=?,author=?,updated_at=? WHERE job_id=? AND log_date=?',vals+(me(),now,job_id,day))
            else: conn.execute('INSERT INTO daily_logs (work_done,crew,hours,materials,issues,weather,photos,author,job_id,log_date,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',vals+(me(),job_id,day,now,now))
            conn.commit(); msg='Saved.'
    cur=conn.execute('SELECT * FROM daily_logs WHERE job_id=? AND log_date=?',(job_id,day)).fetchone()
    cur=log_dict(cur) if cur else {'log_date':day,'pics':[]}
    if not ok: cur.update({k:(request.form.get(k) or '') for k in ('work_done','crew','materials','issues','weather')})
    logs=[log_dict(r) for r in conn.execute('SELECT * FROM daily_logs WHERE job_id=? ORDER BY log_date DESC LIMIT 60',(job_id,)).fetchall()]; conn.close()
    return render_template_string(DAILY_HTML, title='Daily Log', j=row, cur=cur, logs=logs, today=today, msg=msg, ok=ok)

SIGPAD = """<label>Sign with your finger</label><canvas class="sig" width="600" height="200" style="width:100%;height:160px;border:2px solid #ddd;border-radius:10px;touch-action:none;background:#fff"></canvas>
<input type="hidden" name="signature"><button type="button" class="btn" onclick="sigClear(this)">Clear Signature</button>"""
SIG_JS = """<script>
document.querySelectorAll('canvas.sig').forEach(c=>{const x=c.getContext('2d');x.lineWidth=3;x.lineCap='round';x.strokeStyle='#111';let d=false;c.dataset.ink='';
const pt=e=>{const r=c.getBoundingClientRect();return[(e.clientX-r.left)*c.width/r.width,(e.clientY-r.top)*c.height/r.height]};
c.addEventListener('pointerdown',e=>{d=true;c.setPointerCapture(e.pointerId);const[a,b]=pt(e);x.beginPath();x.moveTo(a,b)});
c.addEventListener('pointermove',e=>{if(!d)return;const[a,b]=pt(e);x.lineTo(a,b);x.stroke();c.dataset.ink='1'});c.addEventListener('pointerup',()=>d=false)});
function sigClear(b){const c=b.form.querySelector('canvas.sig');c.getContext('2d').clearRect(0,0,c.width,c.height);c.dataset.ink=''}
document.querySelectorAll('form.sigform').forEach(f=>f.addEventListener('submit',e=>{const c=f.querySelector('canvas.sig');if(!c.dataset.ink){e.preventDefault();alert('Please sign first');return}f.querySelector('[name=signature]').value=c.toDataURL('image/png')}));
</script>"""

SAFETY_HTML = FIELD_HEAD + """<h2>Safety</h2><div style="text-align:center;font-weight:600">Job {{ j.id }} &middot; {{ j.title }}</div>
<a class="btn" href="/?job={{ j.id }}">Back to Job</a>
{% if msg %}<div class="job {{ 'ok' if ok else 'flag' }}">{{ msg }}</div>{% endif %}
<div class="job"><b>Today</b><div>Daily Safety Check: {% if done.daily %}<span class="ok">Done ({{ done.daily|join(', ') }})</span>{% else %}<span class="flag">Not done</span>{% endif %}</div>
<div>Toolbox Talk: {% if done.talk %}<span class="ok">Done ({{ done.talk|join(', ') }})</span>{% else %}<span class="flag">Not done</span>{% endif %}</div></div>
<details class="job"{% if not done.daily %} open{% endif %}><summary style="font-weight:700;font-size:1.15rem;cursor:pointer">Daily Safety Check</summary>
<form method="post" action="/job/{{ j.id }}/safety/daily" class="sigform">{% for k,label in items %}<label>{{ label }}</label><select name="{{ k }}" required><option value="">Pick one</option><option>OK</option><option>Problem</option><option>N/A</option></select>{% endfor %}
<label>Hazards or problems found</label><textarea name="hazards" rows="2" maxlength="1000" placeholder="None (required if you picked Problem)"></textarea>
""" + SIGPAD + """<button class="btn gold" type="submit">Submit Safety Check</button></form></details>
<details class="job"{% if done.daily and not done.talk %} open{% endif %}><summary style="font-weight:700;font-size:1.15rem;cursor:pointer">Toolbox Talk</summary>
<form method="post" action="/job/{{ j.id }}/safety/talk" class="sigform"><label>Topic</label><select name="topic" required onchange="document.querySelectorAll('.tp').forEach(e=>e.style.display=e.dataset.id===this.value?'block':'none')"><option value="">Pick a topic</option>{% for t in topics %}<option value="{{ t.id }}">{{ t.name }}</option>{% endfor %}</select>
{% for t in topics %}<div class="tp pre job" data-id="{{ t.id }}" style="display:none">{{ t.points }}</div>{% endfor %}
<label>Who was there</label><input name="crew" maxlength="300" required placeholder="Mike, Joe">
""" + SIGPAD + """<button class="btn gold" type="submit">Sign Toolbox Talk</button></form></details>
<h3>Past forms</h3>{% for f in forms %}<a class="btn" style="text-align:left" href="/safety/form/{{ f.id }}">{{ f.form_date }} &middot; {{ 'Safety Check' if f.kind=='daily' else 'Talk: '+f.topic_name }} &middot; {{ f.signer }}{% if f.problem %} <span class="flag">Problem</span>{% endif %}</a>{% else %}<div class="muted">None yet.</div>{% endfor %}
""" + SIG_JS + "</body></html>"

SAFETY_FORM_HTML = FIELD_HEAD + """<h2>{{ 'Daily Safety Check' if f.kind=='daily' else 'Toolbox Talk' }}</h2><a class="btn" href="{{ back }}">Back</a>
<div class="job"><div><b>Job:</b> {{ f.job_id }} {{ j.title if j else '' }}</div><div><b>Date:</b> {{ f.form_date }}</div><div><b>Signed by:</b> {{ f.signer }} at {{ when }}</div>
{% if f.kind=='daily' %}{% for k,label in items %}<div>{{ label }}: <b class="{{ 'flag' if ans.get(k)=='Problem' else '' }}">{{ ans.get(k,'-') }}</b></div>{% endfor %}<div class="pre"><b>Hazards:</b> {{ f.hazards or 'None' }}</div>
{% else %}<div><b>Topic:</b> {{ f.topic_name }}</div><div class="pre job">{{ f.topic_points }}</div><div><b>Who was there:</b> {{ f.crew }}</div>{% endif %}
<img src="{{ f.signature }}" alt="signature" style="width:100%;max-width:400px;border-bottom:1px solid #111;margin-top:8px"></div></body></html>"""

SAFETY_OFFICE_HTML = FIELD_HEAD + """<h2>Safety (office only)</h2><a class="btn" href="/">Back to Jobs</a>
{% if msg %}<div class="job {{ 'ok' if ok else 'flag' }}">{{ msg }}</div>{% endif %}
<h3>Last 14 days</h3>{% for r in days %}<div class="job"><b>{{ r.date }} &middot; Job <a href="/job/{{ r.job_id }}/safety">{{ r.job_id }}</a></b> {{ r.title }}
<div>Safety Check: {% if r.daily %}<span class="ok">{{ r.daily|join(', ') }}</span>{% else %}<span class="flag">Not done</span>{% endif %}{% if r.problem %} <span class="flag">Problem reported</span>{% endif %}</div>
<div>Toolbox Talk: {% if r.talk %}<span class="ok">{{ r.talk|join(', ') }}</span>{% else %}<span class="flag">Not done</span>{% endif %}</div></div>{% else %}<div class="muted">No safety forms yet.</div>{% endfor %}
<div class="muted">Shows jobs with a clock in, daily log, or safety form that day.</div>
<h3>Toolbox talk topics</h3>{% for t in topics %}<div class="job"><b>{{ t.name }}</b><div class="pre muted">{{ t.points }}</div><form method="post" action="/safety/topic/delete/{{ t.id }}" onsubmit="return confirm('Delete this topic?')"><button class="btn" type="submit">Delete</button></form></div>{% endfor %}
<form method="post" action="/safety/topic" class="job"><b>Add a topic</b><input name="name" required maxlength="80" placeholder="Topic name"><textarea name="points" rows="4" required maxlength="2000" placeholder="One talking point per line"></textarea><button class="btn gold" type="submit">Add Topic</button></form>
</body></html>"""

def sig_ok(v): return isinstance(v,str) and v.startswith('data:image/png;base64,') and 200<len(v)<400000

def safety_done(conn, job_id, day):
    out={'daily':[], 'talk':[]}
    for f in conn.execute('SELECT kind,signer FROM safety_forms WHERE job_id=? AND form_date=? ORDER BY id',(job_id,day)).fetchall():
        if f['signer'] not in out.get(f['kind'],[]): out.setdefault(f['kind'],[]).append(f['signer'])
    return out

@app.route('/job/<job_id>/safety', methods=['GET'])
def safety_page(job_id, msg=None, ok=True):
    conn=get_db(); row=page_job(conn, job_id)
    if not row: conn.close(); return redirect('/')
    day=today_local().isoformat(); forms=[]
    for f in conn.execute('SELECT id,kind,form_date,topic_name,signer,answers FROM safety_forms WHERE job_id=? ORDER BY id DESC LIMIT 100',(job_id,)).fetchall():
        d=dict(f); d['problem']='Problem' in (f['answers'] or ''); forms.append(d)
    topics=[dict(t) for t in conn.execute('SELECT * FROM safety_topics ORDER BY id').fetchall()]
    done=safety_done(conn, job_id, day); conn.close()
    return render_template_string(SAFETY_HTML, title='Safety', j=row, items=SAFETY_ITEMS, topics=topics, forms=forms, done=done, msg=msg, ok=ok)

@app.route('/job/<job_id>/safety/<kind>', methods=['POST'])
def safety_submit(job_id, kind):
    if kind not in ('daily','talk'): return ('',404)
    conn=get_db(); row=page_job(conn, job_id)
    if not row: conn.close(); return ('',403)
    sig=request.form.get('signature') or ''; js=request.args.get('json')=='1'
    def fail(m):
        conn.close(); return (jsonify({'error':m}),400) if js else safety_page(job_id, msg='Not saved. '+m, ok=False)
    if not sig_ok(sig): return fail('Please sign.')
    now=datetime.utcnow().isoformat(timespec='seconds'); day=today_local().isoformat()
    if kind=='daily':
        ans={k:request.form.get(k) for k,_ in SAFETY_ITEMS}
        if any(v not in ('OK','Problem','N/A') for v in ans.values()): return fail('Answer every line.')
        hz=(request.form.get('hazards') or '').strip()[:1000]
        if 'Problem' in ans.values() and not hz: return fail('Tell us the problem.')
        conn.execute('INSERT INTO safety_forms (job_id,kind,form_date,answers,hazards,signer,signature,created_at) VALUES (?,?,?,?,?,?,?,?)',(job_id,'daily',day,json.dumps(ans),hz,me(),sig,now))
    else:
        try: t=conn.execute('SELECT * FROM safety_topics WHERE id=?',(int(request.form.get('topic') or 0),)).fetchone()
        except ValueError: t=None
        crew=(request.form.get('crew') or '').strip()[:300]
        if not t or not crew: return fail('Pick a topic and list who was there.')
        conn.execute('INSERT INTO safety_forms (job_id,kind,form_date,topic_name,topic_points,crew,signer,signature,created_at) VALUES (?,?,?,?,?,?,?,?,?)',(job_id,'talk',day,t['name'],t['points'],crew,me(),sig,now))
    conn.commit(); conn.close()
    return jsonify({'ok':True}) if js else safety_page(job_id, msg='Saved. Thank you.')

@app.route('/safety/form/<int:fid>')
def safety_form_view(fid):
    conn=get_db(); f=conn.execute('SELECT * FROM safety_forms WHERE id=?',(fid,)).fetchone()
    j=page_job(conn, f['job_id']) if f else None; conn.close()
    if not f or not j: return redirect('/')
    try: ans=json.loads(f['answers'] or '{}')
    except Exception: ans={}
    return render_template_string(SAFETY_FORM_HTML, title='Safety Form', f=f, j=j, ans=ans, items=SAFETY_ITEMS, when=utc_to_local(f['created_at'],'%I:%M %p'), back='/job/%s/safety'%f['job_id'])

@app.route('/safety')
def safety_office(msg=None, ok=True):
    if not is_office(): return redirect('/')
    conn=get_db(); start=(today_local()-timedelta(days=13)).isoformat(); keys={}
    for f in conn.execute('SELECT job_id,kind,form_date,signer,answers FROM safety_forms WHERE form_date>=?',(start,)).fetchall():
        r=keys.setdefault((f['form_date'],f['job_id']),{'daily':[],'talk':[],'problem':False})
        if f['signer'] not in r[f['kind']]: r[f['kind']].append(f['signer'])
        if 'Problem' in (f['answers'] or ''): r['problem']=True
    for l in conn.execute('SELECT job_id,log_date FROM daily_logs WHERE log_date>=?',(start,)).fetchall(): keys.setdefault((l['log_date'],l['job_id']),{'daily':[],'talk':[],'problem':False})
    for p in conn.execute('SELECT job_id,in_at FROM time_punches WHERE in_at>=?',(start,)).fetchall():
        d=utc_to_local(p['in_at'],'%Y-%m-%d')
        if d>=start: keys.setdefault((d,p['job_id']),{'daily':[],'talk':[],'problem':False})
    titles={r['id']:r['title'] for r in conn.execute('SELECT id,title FROM jobs').fetchall()}
    days=[dict(v,date=k[0],job_id=k[1],title=titles.get(k[1],'')) for k,v in sorted(keys.items(),key=lambda kv:(kv[0][0],kv[0][1]),reverse=True)]
    topics=[dict(t) for t in conn.execute('SELECT * FROM safety_topics ORDER BY id').fetchall()]; conn.close()
    return render_template_string(SAFETY_OFFICE_HTML, title='Safety', days=days, topics=topics, msg=msg, ok=ok)

@app.route('/safety/topic', methods=['POST'])
def safety_topic_add():
    if not is_office(): return ('',403)
    n=(request.form.get('name') or '').strip()[:80]; pts=(request.form.get('points') or '').strip()[:2000]
    if not n or not pts: return safety_office(msg='Not saved. Fill in name and points.', ok=False)
    conn=get_db(); conn.execute('INSERT INTO safety_topics (name,points,created_at) VALUES (?,?,?)',(n,pts,datetime.utcnow().isoformat())); conn.commit(); conn.close()
    return redirect('/safety')

@app.route('/safety/topic/delete/<int:tid>', methods=['POST'])
def safety_topic_delete(tid):
    if not is_office(): return ('',403)
    conn=get_db(); conn.execute('DELETE FROM safety_topics WHERE id=?',(tid,)); conn.commit(); conn.close()
    return redirect('/safety')

def job_photo_count(conn, job_id, types=('before','during','after')):
    return conn.execute('SELECT COUNT(*) FROM photos WHERE job_id=? AND photo_type IN (%s)' % ','.join('?'*len(types)),(job_id,)+tuple(types)).fetchone()[0]

def done_block(left, pics):
    if left: return 'Finish the checklist first (%d left)' % left
    if not pics: return 'Add at least one photo first'
    return ''

def step(label, done, go=None, href=None, why=''):
    return {'label':label,'done':bool(done),'go':go or [],'href':href,'why':'' if done else why}

def job_steps(conn, row):
    jid=row['id']; t=row['job_type'] or 'Install'; st=row['status'] or 'assigned'; day=today_local().isoformat(); who=ckey(me())
    q=lambda sql,*a: conn.execute(sql,a).fetchone()[0]
    left=sum(1 for i in parse_checklist(row['checklist'], row['job_type']) if not i['done'])
    notes=bool((row['notes'] or '').strip()) or q('SELECT COUNT(*) FROM job_updates WHERE job_id=?',jid)>0
    started=st in ('in_progress','review','completed')
    finished=st in ('review','completed') if needs_review(row) else st=='completed'
    enc=urllib.parse.quote(jid, safe='')
    if t=='Office Task':
        return [step('Checklist', not left, ['check'], why='%d checklist items left' % left if left else ''),
                step('Notes', notes, ['log']),
                step('Done', st=='completed', ['check'] if left else ['finish'], why='Finish the checklist first (%d left)' % left if left else '')]
    if is_office():
        sched=bool(row['scheduled_date'] and ckey(row['tech']))
        if t=='Survey':
            return [step('Schedule and Assign Sub', sched, href='/schedule'),
                    step('Survey Done', st=='completed', ['survey'], why='' if st=='completed' else 'Waiting on the sub to finish the survey'),
                    step('Estimate (Kelly)', row['kelly_at'], ['survey'], why='Finish the survey first' if st!='completed' else 'Waiting on Kelly'),
                    step('Client Approves', row['hub_approved_at'], ['hub'], why='Send the client link for approval')]
        cust=ckey(row['customer']); addr=(row['address'] or '').strip().lower()
        sv=conn.execute("SELECT status,kelly_at FROM jobs WHERE job_type='Survey' AND id!=? AND ((?!='' AND LOWER(TRIM(IFNULL(customer,'')))=?) OR (?!='' AND LOWER(TRIM(IFNULL(address,'')))=?))",(jid,cust,cust,addr,addr)).fetchall()
        sv_done=any(r['status']=='completed' for r in sv) or bool(conn.execute("SELECT 1 FROM photos WHERE job_id=? AND photo_type='plan'",(jid,)).fetchone())
        draft=conn.execute('SELECT 1 FROM invoice_drafts WHERE job_id=?',(jid,)).fetchone()
        punched=conn.execute('SELECT 1 FROM time_punches WHERE job_id=?',(jid,)).fetchone()
        return [step('Survey', sv_done, ['ab'], why='No finished survey for this client yet'),
                step('Estimate (Kelly)', any(r['kelly_at'] for r in sv), ['links'], why='Finish the survey first' if not sv_done else 'Waiting on Kelly'),
                step('Client Approves', row['hub_approved_at'], ['hub'], why='Send the client link for approval'),
                step('Schedule and Assign Sub', sched, href='/schedule'),
                step('Work in Progress', started or punched, ['status'], why='Waiting on the sub to start'),
                step('Review', st=='completed', ['review'], why='' if st=='review' else 'Waiting on the sub to finish'),
                step('Invoice Draft', draft, ['review'], why='Approve the job in Review first' if st!='completed' else 'No invoice draft yet'),
                step('Done and Archived', st=='completed' and draft and row['sale_amount'] is not None, ['time'], why='Enter the invoice amount' if st=='completed' else 'Approve the job first')]
    f=flow_state(conn, row); pun_open=f['is_in']
    if t=='Survey':
        sv=clean_survey(row['survey_form']); filled=any(v for k,v in sv.items() if k!='obstacles' and isinstance(v,str))
        pics=job_photo_count(conn, jid, ('survey',))
        return [step('Start', f['started'] or finished, ['start']),
                step('Fill Survey and Photos', filled and pics, ['survey'], why='Fill the survey' if not filled else 'Add at least one photo'),
                step('Finish', finished, ['done'])]
    pics=job_photo_count(conn, jid)
    why_done=done_block(left, pics)
    return [step('Start', (f['started'] and f['safety']) or finished, ['start'], why='Sign the safety check' if f['started'] and not f['safety'] else ''),
            step('Checklist', not left, ['check'], why='%d checklist items left' % left if left else ''),
            step('Photos and Notes', (pics and f['log_today']) or finished, ['pn'], why='Add a photo' if not pics else 'Say what you did today'),
            step('Finish', finished, ['check'] if left else ['pn'] if not pics else ['done'], why=why_done)]

def flow_state(conn, row):
    jid=row['id']; day=today_local().isoformat(); who=ckey(me())
    pun=[p for p in conn.execute('SELECT in_at,out_at FROM time_punches WHERE job_id=? AND sub=? ORDER BY id',(jid,who)).fetchall() if utc_to_local(p['in_at'],'%Y-%m-%d')==day]
    safety=conn.execute("SELECT 1 FROM safety_forms WHERE job_id=? AND kind='daily' AND form_date=? AND signer=?",(jid,day,me())).fetchone()
    dlog=conn.execute('SELECT 1 FROM daily_logs WHERE job_id=? AND log_date=?',(jid,day)).fetchone()
    return {'started':bool(pun),'is_in':any(p['out_at'] is None for p in pun),'start_local':utc_to_local(pun[0]['in_at'],'%I:%M %p') if pun else '','safety':bool(safety),'log_today':bool(dlog)}

@app.route('/api/jobs/<job_id>/worklog', methods=['POST'])
def worklog(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    if row['status'] in ('review','completed') and not is_office(): conn.close(); return jsonify({'error':'This job is finished. Ask the office.'}),409
    g=lambda k,n: (request.form.get(k) or '').strip()[:n]
    work,mat,iss=g('work_done',3000),g('materials',2000),g('issues',2000)
    files=[f for f in request.files.getlist('photos')[:20] if f and f.filename]
    if not work and not files: conn.close(); return jsonify({'error':'Add a photo or say what you did'}),400
    day=today_local().isoformat(); now=datetime.utcnow().isoformat(timespec='seconds'); new=[]
    for f in files:
        fn=secure_filename(f"{job_id}_during_{uuid.uuid4().hex[:8]}.jpg"); f.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
        conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at) VALUES (?,?,?,?)',(job_id,fn,'during',now)); new.append(fn)
    if work: conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,me(),work,now_local_str()))
    old=conn.execute('SELECT * FROM daily_logs WHERE job_id=? AND log_date=?',(job_id,day)).fetchone()
    join=lambda a,b: '\n'.join(x for x in ((a or '').strip(), b) if x)[:3000]
    if old:
        pics=json.loads(old['photos'] or '[]')+new
        conn.execute('UPDATE daily_logs SET work_done=?,materials=?,issues=?,photos=?,author=?,updated_at=? WHERE id=?',
                     (join(old['work_done'],work) or 'Photos added',join(old['materials'],mat),join(old['issues'],iss),json.dumps(pics),me(),now,old['id']))
    else:
        conn.execute('INSERT INTO daily_logs (job_id,log_date,author,work_done,crew,materials,issues,photos,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (job_id,day,me(),work or 'Photos added',me(),mat,iss,json.dumps(new),now,now))
    conn.commit(); conn.close(); return jsonify({'ok':True,'photos':len(new)})

@app.route('/api/jobs/<job_id>/steps')
def job_steps_api(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    out=job_steps(conn, row); fl=flow_state(conn, row); conn.close(); return jsonify({'steps':out,'flow':fl})

@app.route('/to-order')
def to_order():
    if not is_office(): return redirect('/')
    conn=get_db(); rows=conn.execute("SELECT * FROM jobs WHERE status!='completed' ORDER BY COALESCE(scheduled_date, created_at)").fetchall(); conn.close()
    jobs=[]
    for r in rows:
        d=job_dict(r); d['need']=[m for m in d['materials_list'] if not m['used']]
        if d['need']: jobs.append(d)
    return render_template_string(ORDER_HTML, jobs=jobs, total=sum(len(j['need']) for j in jobs))

@app.route('/clients/delete', methods=['GET','POST'])
@app.route('/clients/remove', methods=['GET','POST'])
def delete_client():
    """Remove the company/client record only. Jobs, photos and notes are kept."""
    if not is_office(): return redirect('/')
    name=(request.values.get('name') or '').strip()
    k=ckey(name)
    if not k: return redirect('/customers')
    conn=get_db()
    co=None
    try:
        import fieldops_platform as _plat
        co=_plat.get_company_by_key(conn, k)
    except Exception:
        co=None
    jobs=conn.execute('SELECT COUNT(*) AS n FROM jobs WHERE company_id=? OR (company_id IS NULL AND LOWER(TRIM(customer))=?)',(co['id'] if co else -1, k)).fetchone()['n']
    if request.method=='GET' or request.form.get('confirm')!='yes':
        conn.close()
        body='<div class="fo-card"><p>This removes the company record for <b>%s</b>.</p><p>Jobs stay in Field Ops. Nothing is cascade-deleted: this will not delete the %d job%s, photos or notes.</p>'%(h(name), jobs, '' if jobs==1 else 's')
        if not jobs:
            body+='<p class="muted">There are no jobs linked to this company.</p>'
        body+='<form method="post" action="/clients/remove"><input type="hidden" name="name" value="%s"><input type="hidden" name="confirm" value="yes"><button class="big-btn" type="submit" style="background:#b00020">Remove company record</button></form>'%h(name)
        body+='<a class="big-btn gray" href="%s">Cancel</a></div>'%('/customers/%d'%co['id'] if co else '/customers')
        return ops_page('Remove company', body, back='/customers')
    # Confirmed: archive company, drop legacy clients row, keep jobs.
    try:
        import fieldops_platform as _plat
        if co:
            _plat.archive_company(conn, co['id'])
    except Exception as e:
        app.logger.warning('archive company failed: %s', e)
    conn.execute('DELETE FROM clients WHERE LOWER(TRIM(name))=?',(k,))
    conn.commit(); conn.close()
    return redirect('/customers')

@app.route('/clients/add', methods=['POST'])
def add_client():
    if not is_office(): return redirect('/')
    n=(request.form.get('name') or '').strip()
    if n:
        addr=(request.form.get('address') or '').strip(); phone=(request.form.get('phone') or '').strip()
        conn=get_db()
        if not conn.execute('SELECT 1 FROM clients WHERE LOWER(TRIM(name))=?',(ckey(n),)).fetchone():
            conn.execute('INSERT INTO clients (name,address,phone,created_at) VALUES (?,?,?,?)',(n,addr,phone,datetime.utcnow().isoformat()))
        try:
            import fieldops_platform as _plat
            co=_plat.upsert_company(conn, n, phone=phone, billing_address=addr)
            if co and addr: _plat.upsert_site(conn, co['id'], address=addr, phone=phone)
            if co and phone: _plat.ensure_contact(conn, co['id'], n, role='bill_to', phone=phone)
        except Exception as e:
            app.logger.warning('add client company sync failed: %s', e)
        conn.commit(); conn.close()
        try:
            import fieldops_platform as _plat
            conn=get_db(); co=_plat.get_company_by_key(conn, ckey(n)); conn.close()
            if co: return redirect('/customers/%d'%co['id'])
        except Exception:
            pass
    return redirect('/customers')

@app.route('/api/jobs/<job_id>/reassign', methods=['POST'])
def reassign_job(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    tech=nice_name((request.get_json(silent=True) or {}).get('tech'))
    if not tech: return jsonify({'error':'tech required'}),400
    conn=get_db(); old=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    n=conn.execute('UPDATE jobs SET tech=? WHERE id=?',(tech,job_id)).rowcount; conn.commit(); conn.close()
    if n and old and ckey(old['tech'])!=ckey(tech):
        push_to_sub(tech,'New job: '+(old['title'] or job_id),(old['customer'] or '')+' - '+nice_day(old['scheduled_date'] or ''),'/?job='+job_id)
        push_to_sub(old['tech'],'Job moved off your list',(old['title'] or job_id),'/')
    return (jsonify({'ok':True,'tech':tech}) if n else ('',404))

@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    if not is_office(): return jsonify({'error':'office only'}),403
    conn=get_db()
    for p in conn.execute('SELECT filename FROM photos WHERE job_id=?',(job_id,)).fetchall():
        try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'],secure_filename(p['filename'])))
        except OSError: pass
    for t in ('photos','job_updates','invoice_drafts','followups','msg_drafts','job_hours','material_requests'): conn.execute(f'DELETE FROM {t} WHERE job_id=?',(job_id,))
    n=conn.execute('DELETE FROM jobs WHERE id=?',(job_id,)).rowcount; conn.commit(); conn.close()
    return (jsonify({'ok':True}) if n else ('',404))

@app.route('/uploads/<path:fn>')
def uploads(fn):
    if not is_office():
        conn=get_db(); r=conn.execute('SELECT j.tech FROM photos p JOIN jobs j ON j.id=p.job_id WHERE p.filename=?',(fn,)).fetchone()
        ok=bool(r) and can_see(r); conn.close()
        if not ok: return ('',403)
    r=send_from_directory(app.config['UPLOAD_FOLDER'], secure_filename(fn), max_age=7*86400)
    r.headers['Cache-Control']='private, max-age=604800'; return r

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

# ---------- Material requests (ADI will call) ----------
MR_SHIP_TO='ADI Northwest, 7425 Pinemont Dr, Houston, TX 77040 (will call)'
MR_ACCOUNT_DEFAULT='463208'  # ADI customer account #, not a secret
MR_MARKUP=1.25
def mr_sell(cost): return round(cost*MR_MARKUP+1e-9,2)
MR_STATUS=[('submitted','Submitted'),('waiting_roland','Waiting on Roland'),('ordered','Ordered'),('ready','Ready for pickup'),('picked_up','Picked up'),('backordered','Backordered'),('canceled','Canceled')]
MR_LABEL=dict(MR_STATUS)
MR_PENDING=('submitted','waiting_roland')
MR_PICKUP_OK=('ordered','ready')

def mr_init():
    conn=get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS material_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, customer TEXT, account_no TEXT, sub TEXT, requested_by TEXT,
        ship_to TEXT, needed_by TEXT, urgency TEXT, note TEXT, photo TEXT, items TEXT, status TEXT DEFAULT 'submitted', status_at TEXT, status_by TEXT, history TEXT,
        bill INTEGER DEFAULT 1, office_note TEXT, pickup_photo TEXT, summary_text TEXT, email_draft TEXT, summary_at TEXT, webhook TEXT, webhook_tries INTEGER DEFAULT 0,
        created_at TEXT, updated_at TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_material_requests_job ON material_requests(job_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_material_requests_status ON material_requests(status)')
    conn.commit(); conn.close()
mr_init()

def mr_account(conn):
    r=conn.execute("SELECT value FROM settings WHERE key='adi_account'").fetchone()
    return ((r['value'] if r else '') or '').strip() or MR_ACCOUNT_DEFAULT
def mr_items(s):
    try: v=json.loads(s or '[]')
    except Exception: v=[]
    out=[]
    for x in (v if isinstance(v,list) else []):
        if not isinstance(x,dict): continue
        uc=x.get('unit_cost')
        out.append({'part':str(x.get('part') or '')[:80],'desc':str(x.get('desc') or '')[:300],'qty':fnum(x.get('qty'),1),'notes':str(x.get('notes') or '')[:300],
                    'unit_cost':(round(fnum(uc),2) if uc not in (None,'') else None)})
    return out
def mr_day(iso):
    d=utc_day(iso)
    try: return datetime.strptime(d,'%Y-%m-%d').strftime('%b %-d')
    except Exception: return ''
def mr_when(iso):
    try: return datetime.fromisoformat(str(iso)[:19]).replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo('America/Chicago')).strftime('%b %-d, %-I:%M %p CT')
    except Exception: return ''
def mr_line(d):
    s=d['status']; day=mr_day(d['status_at'] or d['created_at'])
    t={'submitted':'Submitted %s. The office will order it.','waiting_roland':'Waiting on Roland since %s.','ordered':'Ordered %s, will call at ADI Northwest, 7425 Pinemont Dr.',
       'ready':'Ready for pickup %s, will call at ADI Northwest, 7425 Pinemont Dr.','picked_up':'Picked up %s.','backordered':'Backordered %s. The office will update you.','canceled':'Canceled %s.'}.get(s,s+' %s')%day
    if s=='picked_up' and d.get('status_by'): t=t[:-1]+' by %s.'%d['status_by']
    return t
def mr_qty(q): return '%g'%q
def mr_dict(r, prices):
    d={k:r[k] for k in ('id','job_id','customer','account_no','sub','requested_by','ship_to','needed_by','urgency','note','photo','status','status_at','status_by','pickup_photo','created_at')}
    d['ship_to']=MR_SHIP_TO; d['account_no']=d['account_no'] or MR_ACCOUNT_DEFAULT
    d['rid']='MR-%d'%r['id']; d['status_label']=MR_LABEL.get(r['status'],r['status']); d['status_line']=mr_line(d); d['created_local']=mr_when(r['created_at'])
    items=mr_items(r['items'])
    if prices:
        for i in items:
            i['total']=round(i['unit_cost']*i['qty'],2) if i['unit_cost'] is not None else None
            i['sell']=mr_sell(i['unit_cost']) if i['unit_cost'] is not None else None
            i['sell_total']=round(i['sell']*i['qty'],2) if i['sell'] is not None else None
        d['total']=round(sum(i['total'] or 0 for i in items),2); d['sell_total']=round(sum(i['sell_total'] or 0 for i in items),2); d['markup_pct']=round((MR_MARKUP-1)*100); d['priced']=all(i['unit_cost'] is not None for i in items) and bool(items)
        d.update({'bill':bool(r['bill']),'office_note':r['office_note'] or '','summary_text':r['summary_text'] or '','email_draft':r['email_draft'] or '','summary_at':r['summary_at'] or '',
                  'webhook':r['webhook'] or '','history':json.loads(r['history'] or '[]')})
    else:
        for i in items: i.pop('unit_cost',None)
    d['items']=items; d['can_pickup']=r['status'] in MR_PICKUP_OK
    return d
def mr_set(conn, rid, st, by, note=''):
    r=conn.execute('SELECT history FROM material_requests WHERE id=?',(rid,)).fetchone()
    hist=json.loads((r['history'] if r else '') or '[]'); now=datetime.utcnow().isoformat(timespec='seconds')
    hist.append({'status':st,'label':MR_LABEL[st],'at':now_local_str(),'by':by,'note':(note or '')[:500]})
    conn.execute('UPDATE material_requests SET status=?,status_at=?,status_by=?,history=?,updated_at=? WHERE id=?',(st,now,by,json.dumps(hist),now,rid))
def mr_status_key(v):
    v=(v or '').strip().lower()
    for k,l in MR_STATUS:
        if v in (k,l.lower()): return k
    return None
def mr_save_photo(conn, job_id, f, ptype):
    if not (f and f.filename): return ''
    fn=secure_filename(f"{job_id}_{ptype}_{uuid.uuid4().hex[:8]}.jpg"); f.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
    conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at) VALUES (?,?,?,?)',(job_id,fn,ptype,datetime.utcnow().isoformat()))
    return fn

def mr_payload(conn, rid):
    r=conn.execute('SELECT * FROM material_requests WHERE id=?',(rid,)).fetchone()
    if not r: return None
    d=mr_dict(r, False); j=conn.execute('SELECT title,address FROM jobs WHERE id=?',(r['job_id'],)).fetchone()
    base=(os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
    d.update({'event':'material_request.submitted','job_title':j['title'] if j else '','site_address':j['address'] if j else '',
              'office_url':base+'/orders/%d'%rid if base else '','pending_api':base+'/api/material-requests/pending' if base else '','has_photo':bool(r['photo']),'photo':''})
    return d
def mr_webhook_send(rid):
    url=os.environ.get('MATREQ_WEBHOOK_URL') or ''; auth=os.environ.get('MATREQ_WEBHOOK_AUTH') or ''
    if not (url and auth): return
    ok=False
    try:
        c=get_db(); p=mr_payload(c, rid); c.close()
        if p is None: return
        req=urllib.request.Request(url, data=json.dumps(p).encode(), method='POST', headers={'Content-Type':'application/json','Authorization':auth,'User-Agent':'TrueSenseFieldOps/1'})
        with urllib.request.urlopen(req, timeout=10) as resp: ok=200<=resp.status<300
    except Exception as e: print('MATREQ_WEBHOOK failed MR-%s %s' % (rid, type(e).__name__), flush=True)
    try:
        c=get_db(); c.execute('UPDATE material_requests SET webhook=?,webhook_tries=COALESCE(webhook_tries,0)+1 WHERE id=?',('sent' if ok else 'failed',rid)); c.commit(); c.close()
    except Exception: pass
def mr_webhook(rid):
    if os.environ.get('MATREQ_WEBHOOK_URL') and os.environ.get('MATREQ_WEBHOOK_AUTH'):
        threading.Thread(target=mr_webhook_send, args=(rid,), daemon=True).start()
def mr_webhook_retry():
    if not (os.environ.get('MATREQ_WEBHOOK_URL') and os.environ.get('MATREQ_WEBHOOK_AUTH')): return
    c=get_db(); ids=[r['id'] for r in c.execute("SELECT id FROM material_requests WHERE webhook='failed' AND COALESCE(webhook_tries,0)<4 AND status IN ('submitted','waiting_roland')")]; c.close()
    for i in ids: mr_webhook_send(i)

def mr_job_lines(conn, job_id):
    lines=[]
    for r in conn.execute("SELECT * FROM material_requests WHERE job_id=? AND bill=1 AND status!='canceled' ORDER BY id",(job_id,)):
        for i in mr_items(r['items']):
            if i['unit_cost'] is None or i['unit_cost']<=0: continue
            lines.append({'description':('Materials: '+' '.join(x for x in (i['part'],i['desc']) if x)+' (MR-%d)'%r['id'])[:4000],'qty':i['qty'],'rate':mr_sell(i['unit_cost']),'amount':round(mr_sell(i['unit_cost'])*i['qty'],2)})
    return lines

MR_FORM_CSS='''<style>.mr-line{border:2px solid #e5e5e5;border-radius:12px;padding:10px;margin:8px 0;background:#fafaf5}.mr-line .row>*{flex:1 1 120px}
.seg2{display:flex;gap:8px}.seg2 label{flex:1;margin:0}.seg2 input{display:none}.seg2 span{display:block;text-align:center;padding:16px;border:2px solid #ccc;border-radius:10px;font-weight:700;font-size:1.1rem;cursor:pointer}
.seg2 input:checked+span{border-color:#a49f2a;background:#a49f2a;color:#fff}.seg2 input.urg:checked+span{border-color:#c62828;background:#c62828}
.auto{background:#f6f6f0;border-radius:12px;padding:10px 12px;margin:8px 0}.auto div{padding:2px 0}button,.big-btn{cursor:pointer}</style>'''
MR_LINE='''<div class="mr-line"><div class="row"><div><label>Part #</label><input name="part" maxlength="80" placeholder="Part # (if you know it)"></div><div><label>Qty</label><input name="qty" type="number" min="1" step="any" inputmode="decimal" value="1" onfocus="this.select()"></div></div>
<label>Description</label><input name="desc" maxlength="300" placeholder="What is it? e.g. Cat6 cable, 1000 ft box, blue">
<label>Notes</label><input name="lnote" maxlength="300" placeholder="Color, size, brand OK to swap? (optional)"><button type="button" class="big-btn gray" onclick="mrDel(this)">Remove this line</button></div>'''

def mr_line_filled(l):
    s=MR_LINE.replace('name="part" maxlength="80"','name="part" maxlength="80" value="%s"'%h(l['part'])).replace('value="1" onfocus','value="%s" onfocus'%h(mr_qty(l['qty'])))
    s=s.replace('name="desc" maxlength="300"','name="desc" maxlength="300" value="%s"'%h(l['desc']))
    return s.replace('name="lnote" maxlength="300"','name="lnote" maxlength="300" value="%s"'%h(('Plan: '+l['floors']) if l.get('floors') else ''))
def fo_ok(html):
    r=app.make_response(html); r.headers['X-FO-OK']='1'; return r

@app.route('/job/<job_id>/materials', methods=['GET','POST'])
def mr_new(job_id):
    conn=get_db(); row=page_job(conn, job_id)
    if not row: conn.close(); return redirect('/')
    acct=mr_account(conn); back='/?job='+urllib.parse.quote(job_id); err=''
    sub=nice_name(row['tech']) if is_office() else me()
    if request.method=='POST':
        f=request.form; items=[]
        for p,d,q,n in zip(f.getlist('part'),f.getlist('desc'),f.getlist('qty'),f.getlist('lnote')):
            p,d,n=p.strip()[:80],d.strip()[:300],n.strip()[:300]
            if not (p or d): continue
            qv=fnum(q,0)
            if qv<=0: err='Each line needs a quantity of 1 or more.'; break
            items.append({'part':p,'desc':d,'qty':qv,'notes':n,'unit_cost':None})
        nb=(f.get('needed_by') or '').strip()
        if not err and not items: err='Add at least one item (part # or description).'
        if not err and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', nb): err='Pick the date you need it by.'
        if not err:
            now=datetime.utcnow().isoformat(timespec='seconds'); urg='Urgent' if f.get('urgency')=='Urgent' else 'Normal'
            photo=mr_save_photo(conn, job_id, request.files.get('photo'), 'matreq')
            cur=conn.execute('INSERT INTO material_requests (job_id,customer,account_no,sub,requested_by,ship_to,needed_by,urgency,note,photo,items,status,status_at,status_by,history,bill,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id,row['customer'] or '',acct,sub,me(),MR_SHIP_TO,nb,urg,(f.get('note') or '').strip()[:2000],photo,json.dumps(items),'submitted',now,me(),
                 json.dumps([{'status':'submitted','label':'Submitted','at':now_local_str(),'by':me(),'note':''}]),1,now,now))
            rid=cur.lastrowid
            conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,me(),'Requested materials MR-%d: %s'%(rid,', '.join('%s x %s'%(mr_qty(i['qty']),i['desc'] or i['part']) for i in items))[:1000],now_local_str()))
            conn.commit(); conn.close(); mr_webhook(rid)
            return fo_ok(ops_page('Request sent', '<div class="card" style="text-align:center"><div style="font-size:3rem;color:#2e7d32">&#10003;</div><div style="font-size:1.3rem;font-weight:800">MR-%d sent to the office</div><div class="muted" style="font-size:1rem;margin-top:6px">Status: Submitted. You will see the status on the job.</div></div><a class="big-btn" href="%s" style="font-size:1.3rem;padding:20px">Back to Job</a>'%(rid,h(back)), back=False))
    pre=[]
    if request.method=='GET' and request.args.get('from')=='bom':
        _,bl=bom_build(conn,row,False); pre=bl[:60]
    conn.close()
    auto='<div class="auto"><div><b>Job:</b> %s %s</div><div><b>Customer:</b> %s</div><div><b>ADI account #:</b> %s</div><div><b>Sub:</b> %s</div><div><b>Ship to:</b> %s</div></div>'%(
        h(job_id),h(row['title'] or ''),h(row['customer'] or ''),h(acct) if acct else '<span class="muted">Office fills this in</span>',h(sub or '-'),h(MR_SHIP_TO))
    body=MR_FORM_CSS+auto+('<div class="flag" style="display:block;padding:10px;margin:8px 0">%s</div>'%h(err) if err else '')+'''<form method="post" enctype="multipart/form-data" data-outbox="Material request for '''+h(job_id)+'''" onsubmit="this.querySelector('[type=submit]').disabled=true">
<h2>Items</h2>'''+('<div class="muted">Filled in from the floor plan. Change anything you need.</div>' if pre else '')+'''<div id="mr-lines">'''+(''.join(mr_line_filled(l) for l in pre) if pre else MR_LINE)+'''</div><button type="button" class="big-btn gray" style="font-size:1.15rem" onclick="mrAdd()">+ Add another item</button>
<h2>When</h2><label>Needed by</label><input type="date" name="needed_by" required min="%s" value="%s">
<label>Urgency</label><div class="seg2"><label><input type="radio" name="urgency" value="Normal" checked><span>Normal</span></label><label><input type="radio" class="urg" name="urgency" value="Urgent"><span>Urgent</span></label></div>
<h2>Note and photo</h2><textarea name="note" rows="3" maxlength="2000" placeholder="Anything else the office should know (optional)"></textarea>
<label>Photo (optional)</label><input type="file" name="photo" accept="image/*">
<button type="submit" class="big-btn" style="font-size:1.35rem;padding:22px;margin-top:14px">Send Request</button>
<div class="muted" style="text-align:center">Goes to the office. Nothing is ordered until the office OKs it.</div></form>
<template id="mr-tpl">'''%(today_local().isoformat(),h(request.form.get('needed_by') or ''))+MR_LINE+'''</template>
<script>function mrAdd(){const t=document.getElementById('mr-tpl').content.cloneNode(true);document.getElementById('mr-lines').appendChild(t);const L=document.querySelectorAll('#mr-lines .mr-line');L[L.length-1].querySelector('[name=desc]').focus()}
function mrDel(b){const L=document.querySelectorAll('#mr-lines .mr-line');if(L.length<2){b.closest('.mr-line').querySelectorAll('input').forEach(i=>i.value=i.name==='qty'?'1':'');return}b.closest('.mr-line').remove()}</script>'''
    return ops_page('Request materials', body, back=back)

@app.route('/api/jobs/<job_id>/material-requests')
def mr_for_job(job_id):
    conn=get_db(); row,err=own_job(conn, job_id)
    if err: conn.close(); return err
    rows=conn.execute('SELECT * FROM material_requests WHERE job_id=? ORDER BY id DESC LIMIT 10',(job_id,)).fetchall(); off=is_office()
    out=[mr_dict(r, off) for r in rows]; conn.close()
    return jsonify({'requests':out,'ship_to':MR_SHIP_TO})

@app.route('/materials/<int:rid>/pickup', methods=['GET','POST'])
def mr_pickup(rid):
    conn=get_db(); r=conn.execute('SELECT * FROM material_requests WHERE id=?',(rid,)).fetchone()
    row=page_job(conn, r['job_id']) if r else None
    if not row: conn.close(); return redirect('/')
    back='/?job='+urllib.parse.quote(r['job_id']); err=''
    if r['status'] not in MR_PICKUP_OK:
        conn.close(); return ops_page('I picked it up','<div class="card">MR-%d is %s. Pickup can be marked once it is Ordered or Ready for pickup.</div><a class="big-btn" href="%s">Back to Job</a>'%(rid,h(MR_LABEL.get(r['status'],r['status'])),h(back)), back=back)
    if request.method=='POST':
        f=request.files.get('ticket')
        if not (f and f.filename): err='Take a photo of the pickup ticket first.'
        else:
            fn=mr_save_photo(conn, r['job_id'], f, 'ticket')
            conn.execute('UPDATE material_requests SET pickup_photo=? WHERE id=?',(fn,rid)); mr_set(conn, rid, 'picked_up', me(), 'Ticket photo added')
            conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(r['job_id'],me(),'Picked up materials MR-%d (ticket photo added)'%rid,now_local_str()))
            conn.commit(); conn.close()
            return ops_page('Picked up', '<div class="card" style="text-align:center"><div style="font-size:3rem;color:#2e7d32">&#10003;</div><div style="font-size:1.3rem;font-weight:800">MR-%d marked Picked up</div></div><a class="big-btn" href="%s" style="font-size:1.3rem;padding:20px">Back to Job</a>'%(rid,h(back)), back=False)
    d=mr_dict(r, False); conn.close()
    body=('<div class="flag" style="display:block;padding:10px;margin:8px 0">%s</div>'%h(err) if err else '')+'<div class="card"><b>%s</b> &middot; %s<div class="muted">%s</div><ul>%s</ul></div>'%(
        h(d['rid']),h(d['status_label']),h(MR_SHIP_TO),''.join('<li>%s x %s</li>'%(mr_qty(i['qty']),h(' '.join(x for x in (i['part'],i['desc']) if x))) for i in d['items']))
    body+='''<form method="post" enctype="multipart/form-data" onsubmit="if(!this.ticket.value){alert('Take a photo of the ticket first');return false}this.querySelector('[type=submit]').disabled=true">
<label style="font-size:1.15rem">Photo of the pickup ticket</label><input type="file" name="ticket" accept="image/*" capture="environment" required>
<button type="submit" class="big-btn" style="font-size:1.35rem;padding:22px;margin-top:12px">I picked it up</button></form>'''
    return ops_page('I picked it up', body, back=back)

def mr_card(d):
    urg=' <span class="flag">Urgent</span>' if d['urgency']=='Urgent' else ''
    return '<a class="card" href="/orders/%d"><b>%s</b>%s &middot; Job %s &middot; %s<div class="muted">%s &middot; Needed by %s &middot; %d item%s &middot; %s</div><div>%s</div></a>'%(
        d['id'],h(d['rid']),urg,h(d['job_id']),h(d['customer'] or ''),h(d['sub'] or ''),h(d['needed_by'] or '-'),len(d['items']),'' if len(d['items'])==1 else 's',
        ('Cost %s, sell %s'%(money(d['total']),money(d['sell_total']))) if d['priced'] else ('%s cost so far, prices missing'%money(d['total']) if d['total'] else 'No prices yet'),
        h(', '.join('%s x %s'%(mr_qty(i['qty']),i['desc'] or i['part']) for i in d['items'])[:200]))

@app.route('/orders', methods=['GET','POST'])
def mr_orders():
    if not is_office(): return redirect('/')
    conn=get_db(); msg=''
    if request.method=='POST':
        conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('adi_account',?)",((request.form.get('adi_account') or '').strip()[:40],)); conn.commit(); msg='ADI account # saved.'
    acct=mr_account(conn); rows=[mr_dict(r, True) for r in conn.execute('SELECT * FROM material_requests ORDER BY id DESC LIMIT 300')]; conn.close()
    show_all=request.args.get('all')=='1'; body='<div class="card"><b>Ship to:</b> %s<form method="post" class="row" style="margin-top:6px"><div><label>ADI account #</label><input name="adi_account" value="%s" placeholder="463208 (default)"></div><button class="big-btn gray" style="flex:0 0 120px;align-self:end">Save</button></form></div>'%(h(MR_SHIP_TO),h(acct))
    if not rows: body+='<div class="card muted" style="text-align:center">No material requests yet. Subs send them from the job screen with Request materials.</div>'
    for k,l in MR_STATUS:
        g=[d for d in rows if d['status']==k]
        if not g: continue
        if k in ('picked_up','canceled') and not show_all: g=g[:5]
        body+='<h2>%s (%d)</h2>'%(h(l),len([d for d in rows if d['status']==k]))+''.join(mr_card(d) for d in g)
    if any(d['status'] in ('picked_up','canceled') for d in rows) and not show_all: body+='<a class="big-btn gray" href="/orders?all=1">Show all done and canceled</a>'
    return ops_page('Orders', body, back='/more', msg=msg)

@app.route('/orders/<int:rid>', methods=['GET','POST'])
def mr_order(rid):
    if not is_office(): return redirect('/')
    conn=get_db(); r=conn.execute('SELECT * FROM material_requests WHERE id=?',(rid,)).fetchone()
    if not r: conn.close(); return redirect('/orders')
    msg=''
    if request.method=='POST':
        f=request.form; items=mr_items(r['items'])
        for n,i in enumerate(items):
            v=(f.get('cost_%d'%n) or '').replace('$','').replace(',','').strip()
            i['unit_cost']=round(fnum(v),2) if v and fnum(v,-1)>=0 else None
            q=fnum(f.get('qty_%d'%n),0)
            if q>0: i['qty']=q
        now=datetime.utcnow().isoformat(timespec='seconds')
        conn.execute('UPDATE material_requests SET items=?,bill=?,office_note=?,updated_at=? WHERE id=?',(json.dumps(items),1 if f.get('bill') else 0,(f.get('office_note') or '').strip()[:2000],now,rid))
        st=mr_status_key(f.get('status'))
        if st and st!=r['status']: mr_set(conn, rid, st, me(), (f.get('status_note') or '').strip())
        conn.commit(); msg='Saved.'; r=conn.execute('SELECT * FROM material_requests WHERE id=?',(rid,)).fetchone()
    d=mr_dict(r, True); j=conn.execute('SELECT title,address FROM jobs WHERE id=?',(r['job_id'],)).fetchone(); conn.close()
    pic=lambda fn,lbl: '<div><b>%s</b><br><a href="/uploads/%s" target="_blank"><img src="/uploads/%s" style="width:140px;height:140px;object-fit:cover;border-radius:8px"></a></div>'%(lbl,urllib.parse.quote(fn),urllib.parse.quote(fn)) if fn else ''
    body='<div class="card"><b>%s</b> &middot; <span class="okf">%s</span>%s<div>Job <a href="/?job=%s">%s</a> %s</div><div class="muted">%s</div><div><b>Customer:</b> %s &middot; <b>ADI account #:</b> %s</div><div><b>Sub:</b> %s &middot; <b>Sent:</b> %s</div><div><b>Ship to:</b> %s</div><div><b>Needed by:</b> %s &middot; <b>Urgency:</b> %s</div>%s</div>'%(
        h(d['rid']),h(d['status_label']),' <span class="flag">Urgent</span>' if d['urgency']=='Urgent' else '',urllib.parse.quote(d['job_id']),h(d['job_id']),h(j['title'] if j else ''),h(j['address'] if j else ''),
        h(d['customer'] or ''),h(d['account_no'] or 'not set'),h(d['sub'] or ''),h(d['created_local']),h(d['ship_to'] or MR_SHIP_TO),h(d['needed_by'] or '-'),h(d['urgency'] or 'Normal'),
        '<div><b>Note:</b> %s</div>'%h(d['note']) if d['note'] else '')
    body+='<div class="row">%s%s</div>'%(pic(d['photo'],'Sub photo'),pic(d['pickup_photo'],'Pickup ticket'))
    body+='<form method="post"><h2>Items and prices (office only)</h2>'
    for n,i in enumerate(d['items']):
        body+='<div class="card"><b>%s</b> %s<div class="muted">%s</div><div class="row"><div><label>Qty</label><input name="qty_%d" value="%s" inputmode="decimal" onfocus="this.select()"></div><div><label>Unit cost</label><input name="cost_%d" value="%s" inputmode="decimal" placeholder="0.00" onfocus="this.select()"></div></div><div style="text-align:right">Cost total: <b>%s</b> &middot; Sell each: <b>%s</b> &middot; Sell total: <b>%s</b></div></div>'%(
            h(i['part']),h(i['desc']),h(i['notes']),n,mr_qty(i['qty']),n,'%.2f'%i['unit_cost'] if i['unit_cost'] is not None else '',money(i['total']) if i['total'] is not None else '-',money(i['sell']) if i['sell'] is not None else '-',money(i['sell_total']) if i['sell_total'] is not None else '-')
    body+='<div class="card" style="text-align:right;font-size:1.15rem">Cost: <b id="mr-total">%s</b> &middot; Sell (cost + %d%%): <b id="mr-sell">%s</b><div class="muted">Sell price goes on the QuickBooks invoice draft.</div></div>'%(money(d['total']),d['markup_pct'],money(d['sell_total']))
    body+='<label style="display:flex;gap:10px;align-items:center;font-weight:600"><input type="checkbox" name="bill" value="1"%s style="width:26px;height:26px"> Add these parts to the QuickBooks invoice draft (at sell price)</label>'%(' checked' if d['bill'] else '')
    body+='<h2>Status</h2><select name="status">%s</select><input name="status_note" placeholder="Status note (optional, e.g. ADI order #)"><label>Office note</label><textarea name="office_note" rows="2">%s</textarea><button class="big-btn" style="font-size:1.2rem">Save</button></form>'%(
        ''.join('<option value="%s"%s>%s</option>'%(k,' selected' if k==d['status'] else '',h(l)) for k,l in MR_STATUS),h(d['office_note']))
    body+='<a class="big-btn gray" href="/print/order/%d" target="_blank">Order summary (print)</a>'%rid
    if d['summary_text'] or d['email_draft']:
        body+='<h2>From the bot</h2><div class="muted">Saved %s. Draft only. Nothing is sent by the app.</div>'%h(mr_when(d['summary_at']))
        if d['summary_text']: body+='<label>Order summary</label><textarea rows="8" readonly>%s</textarea>'%h(d['summary_text'])
        if d['email_draft']: body+='<label>Email draft to ADI</label><textarea rows="12" readonly>%s</textarea>'%h(d['email_draft'])
    body+='<h2>History</h2><table>%s</table>'%''.join('<tr><td>%s</td><td>%s</td><td class="muted">%s%s</td></tr>'%(h(x.get('label','')),h(x.get('by','')),h(x.get('at','')),(' &middot; '+h(x['note'])) if x.get('note') else '') for x in d['history'])
    return ops_page('Order '+d['rid'], body, back='/orders', msg=msg)

@app.route('/orders/<int:rid>/summary')
def mr_summary_old(rid): return redirect('/print/order/%d'%rid)

@app.route('/print/order/<int:rid>')
def mr_summary(rid):
    if not is_office(): return redirect('/')
    conn=get_db(); r=conn.execute('SELECT * FROM material_requests WHERE id=?',(rid,)).fetchone()
    if not r: conn.close(); return redirect('/orders')
    d=mr_dict(r, True); j=conn.execute('SELECT title,address FROM jobs WHERE id=?',(r['job_id'],)).fetchone(); conn.close()
    pr=request.args.get('prices')=='1'
    rows=''.join('<tr><td>%s</td><td>%s</td><td class="num">%s</td><td>%s</td>%s</tr>'%(h(i['part'] or '-'),h(i['desc']),mr_qty(i['qty']),h(i['notes']),
        ('<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'%(money(i['unit_cost']) if i['unit_cost'] is not None else '-',money(i['total']) if i['total'] is not None else '-',money(i['sell']) if i['sell'] is not None else '-',money(i['sell_total']) if i['sell_total'] is not None else '-')) if pr else '') for i in d['items'])
    html='''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Order summary %s</title>
<style>body{font-family:-apple-system,Arial,sans-serif;max-width:800px;margin:auto;padding:16px;color:#111}h1{font-size:1.4rem;margin:4px 0}table{width:100%%;border-collapse:collapse;margin:10px 0}td,th{border:1px solid #ccc;padding:7px;text-align:left;vertical-align:top}th{background:#f4f4ec}.num{text-align:right}
.kv div{padding:2px 0}.btns a,.btns button{display:inline-block;padding:12px 16px;margin:4px 4px 4px 0;border-radius:10px;border:0;background:#a49f2a;color:#fff;font-size:1rem;font-weight:700;text-decoration:none;cursor:pointer}.btns .g{background:#eee;color:#111}@media print{.btns{display:none}}</style></head><body>
<div class="btns"><button onclick="print()">Print</button><a class="g" href="/orders/%d">Back</a><a class="g" href="?prices=%s">%s</a></div>
<img src="/static/logo.png" alt="True Sense Security" style="height:44px"><h1>Order Request %s</h1>
<div class="kv"><div><b>Job:</b> %s %s</div><div><b>Site:</b> %s</div><div><b>Customer:</b> %s</div><div><b>ADI account #:</b> %s</div><div><b>Ship to:</b> %s</div><div><b>Needed by:</b> %s &middot; <b>Urgency:</b> %s</div><div><b>Requested by:</b> %s, %s</div></div>
<table><tr><th>Part #</th><th>Description</th><th class="num">Qty</th><th>Notes</th>%s</tr>%s%s</table>%s<div style="font-size:.85rem;color:#666">Status: %s. Printed %s.</div></body></html>'''%(
        h(d['rid']),rid,'0' if pr else '1','Hide prices' if pr else 'Show prices (office copy)',h(d['rid']),h(d['job_id']),h(j['title'] if j else ''),h(j['address'] if j else ''),h(d['customer'] or ''),
        h(d['account_no'] or 'not set'),h(d['ship_to'] or MR_SHIP_TO),h(d['needed_by'] or '-'),h(d['urgency'] or 'Normal'),h(d['sub'] or ''),h(d['created_local']),
        '<th class="num">Unit cost</th><th class="num">Cost total</th><th class="num">Sell each</th><th class="num">Sell total</th>' if pr else '',rows,'<tr><td colspan="5"><b>Totals</b></td><td class="num"><b>%s</b></td><td></td><td class="num"><b>%s</b></td></tr>'%(money(d['total']),money(d['sell_total'])) if pr else '',
        '<div><b>Notes:</b> %s</div>'%h(d['note']) if d['note'] else '',h(d['status_label']),h(now_local_str()))
    return html

@app.route('/api/material-requests/pending')
def mr_api_pending():
    bad=sync_key_ok()
    if bad: return bad
    want=[mr_status_key(s) for s in (request.args.get('status') or ','.join(MR_PENDING)).split(',')]
    want=[s for s in want if s] or list(MR_PENDING)
    conn=get_db(); rows=conn.execute('SELECT m.*,j.title AS job_title,j.address AS site_address FROM material_requests m LEFT JOIN jobs j ON j.id=m.job_id WHERE m.status IN (%s) ORDER BY m.id'%','.join('?'*len(want)),want).fetchall()
    out=[]
    for r in rows:
        d=mr_dict(r, True); d['job_title']=r['job_title'] or ''; d['site_address']=r['site_address'] or ''; out.append(d)
    acct=mr_account(conn); conn.close()
    return jsonify({'requests':out,'ship_to':MR_SHIP_TO,'account_no':acct,'statuses':[{'key':k,'label':l} for k,l in MR_STATUS]})

@app.route('/api/material-requests/<int:rid>/status', methods=['POST'])
def mr_api_status(rid):
    bad=sync_key_ok()
    if bad: return bad
    d=request.get_json(silent=True) or {}; st=mr_status_key(d.get('status'))
    if not st: return jsonify({'error':'status must be one of: '+', '.join(l for k,l in MR_STATUS)}),400
    conn=get_db()
    if not conn.execute('SELECT 1 FROM material_requests WHERE id=?',(rid,)).fetchone(): conn.close(); return ('',404)
    mr_set(conn, rid, st, (str(d.get('by') or 'Bot'))[:40], str(d.get('note') or '')); conn.commit()
    out=mr_dict(conn.execute('SELECT * FROM material_requests WHERE id=?',(rid,)).fetchone(), True); conn.close()
    return jsonify({'ok':True,'request':out})

@app.route('/api/material-requests/<int:rid>/summary', methods=['POST'])
def mr_api_summary(rid):
    bad=sync_key_ok()
    if bad: return bad
    d=request.get_json(silent=True) or {}
    s=str(d.get('summary') or d.get('summary_text') or '')[:20000]; e=str(d.get('email_draft') or '')[:20000]
    if not (s or e): return jsonify({'error':'send summary and/or email_draft'}),400
    conn=get_db()
    if not conn.execute('SELECT 1 FROM material_requests WHERE id=?',(rid,)).fetchone(): conn.close(); return ('',404)
    now=datetime.utcnow().isoformat(timespec='seconds')
    if s: conn.execute('UPDATE material_requests SET summary_text=? WHERE id=?',(s,rid))
    if e: conn.execute('UPDATE material_requests SET email_draft=? WHERE id=?',(e,rid))
    conn.execute('UPDATE material_requests SET summary_at=?,updated_at=? WHERE id=?',(now,now,rid)); conn.commit(); conn.close()
    return jsonify({'ok':True,'id':rid,'saved':[k for k,v in (('summary',s),('email_draft',e)) if v],'note':'Saved as draft text. The app never sends email.'})


# ---------- Plan designer: plans, PDF pages, names, scale ----------
PLAN_PDF_DIR = os.path.join(app.config['UPLOAD_FOLDER'], 'pdftmp')
PLAN_PX = 4000  # long side of a plan rendered from a PDF page
def pm_init():
    conn=get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS plan_meta (filename TEXT PRIMARY KEY, job_id TEXT, name TEXT, scale TEXT, src TEXT, created_at TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_plan_meta_job ON plan_meta(job_id)'); conn.commit(); conn.close()
pm_init()
def pm_scale(raw):
    try: s=json.loads(raw) if isinstance(raw,str) else raw
    except Exception: return None
    if not isinstance(s,dict): return None
    try:
        v={k:min(100.0,max(0.0,float(s[k]))) for k in ('ax','ay','bx','by')}; ft=float(s['ft'])
    except (KeyError,TypeError,ValueError): return None
    if not (0<ft<=100000) or (v['ax'],v['ay'])==(v['bx'],v['by']): return None
    v['ft']=round(ft,3); return v
def pm_ppf(fn, s):
    if not s: return None
    W,H=img_size(fn); px=math.hypot((s['bx']-s['ax'])/100*W,(s['by']-s['ay'])/100*H)
    return round(px/s['ft'],5) if px>0 else None
def plan_info(conn, job_id):
    meta={r['filename']:r for r in conn.execute('SELECT * FROM plan_meta WHERE job_id=?',(job_id,))}
    out=[]
    for i,fn in enumerate(plan_files(conn, job_id)):
        m=meta.get(fn); s=pm_scale(m['scale']) if m else None; W,H=img_size(fn)
        out.append({'fn':fn,'name':(m['name'] if m and m['name'] else 'Floor %d'%(i+1)),'scale':s,'ppf':pm_ppf(fn,s),'w':W,'h':H,'src':(m['src'] if m else '') or ''})
    return out
def plan_scale_of(fn):
    try:
        c=get_db(); r=c.execute('SELECT scale FROM plan_meta WHERE filename=?',(fn,)).fetchone(); c.close()
        s=pm_scale(r['scale']) if r else None; return s, pm_ppf(fn, s)
    except Exception: return None, None
def pm_add(conn, job_id, fn, name, src=''):
    conn.execute('INSERT OR REPLACE INTO plan_meta (filename,job_id,name,scale,src,created_at) VALUES (?,?,?,?,?,?)',(fn,job_id,name[:60],None,src[:200],datetime.utcnow().isoformat()))
def pm_next_name(conn, job_id): return 'Floor %d'%(len(plan_files(conn, job_id))+1)
def pm_copy(conn, src_fn, new_fn, job_id):
    m=conn.execute('SELECT * FROM plan_meta WHERE filename=?',(src_fn,)).fetchone()
    if m: conn.execute('INSERT OR REPLACE INTO plan_meta (filename,job_id,name,scale,src,created_at) VALUES (?,?,?,?,?,?)',(new_fn,job_id,m['name'],m['scale'],m['src'],datetime.utcnow().isoformat()))
def pdf_tmp_clean():
    try:
        os.makedirs(PLAN_PDF_DIR, exist_ok=True); cut=time.time()-86400
        for f in os.listdir(PLAN_PDF_DIR):
            p=os.path.join(PLAN_PDF_DIR,f)
            if os.path.getmtime(p)<cut: os.remove(p)
    except OSError: pass
def pdf_tok_ok(job_id, tok): return bool(re.fullmatch(re.escape(secure_filename(job_id))+r'_[0-9a-f]{12}', tok or ''))

@app.route('/api/jobs/<job_id>/plans/pdf', methods=['POST'])
def plan_pdf_upload(job_id):
    conn=get_db(); row,err=survey_edit_row(conn, job_id); conn.close()
    if err: return err
    f=request.files.get('file')
    if not (f and f.filename): return jsonify({'error':'no file'}),400
    data=f.read()
    if not data.startswith(b'%PDF'): return jsonify({'error':'That file is not a PDF'}),400
    try: import pymupdf
    except ImportError: return jsonify({'error':'PDF support is not installed on the server'}),503
    pdf_tmp_clean(); tok='%s_%s'%(secure_filename(job_id),uuid.uuid4().hex[:12]); path=os.path.join(PLAN_PDF_DIR,tok+'.pdf')
    with open(path,'wb') as o: o.write(data)
    try:
        doc=pymupdf.open(path); n=doc.page_count
        if doc.needs_pass: doc.close(); os.remove(path); return jsonify({'error':'That PDF has a password. Save it without one and try again.'}),400
        for i in range(min(n,60)):
            pg=doc[i]; z=360/max(pg.rect.width,pg.rect.height); pg.get_pixmap(matrix=pymupdf.Matrix(z,z),alpha=False).save(os.path.join(PLAN_PDF_DIR,'%s_%d.png'%(tok,i)))
        doc.close()
    except Exception as e:
        app.logger.warning('PDF plan failed %s: %s', job_id, e); return jsonify({'error':'Could not read that PDF'}),400
    return jsonify({'ok':True,'token':tok,'pages':n,'shown':min(n,60),'name':os.path.basename(f.filename)[:120]})

@app.route('/api/jobs/<job_id>/plans/pdf/<tok>/<int:i>.png')
def plan_pdf_thumb(job_id, tok, i):
    conn=get_db(); row,err=own_job(conn, job_id); conn.close()
    if err: return err
    if not pdf_tok_ok(job_id, tok): return ('',404)
    return send_from_directory(PLAN_PDF_DIR, '%s_%d.png'%(tok,i), max_age=600)

@app.route('/api/jobs/<job_id>/plans/pdf/<tok>/add', methods=['POST'])
def plan_pdf_add(job_id, tok):
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    path=os.path.join(PLAN_PDF_DIR,tok+'.pdf')
    if not pdf_tok_ok(job_id, tok) or not os.path.exists(path): conn.close(); return jsonify({'error':'That upload expired. Pick the PDF again.'}),400
    d=request.get_json(silent=True) or {}; pages=[]
    for p in (d.get('pages') or [])[:20]:
        try: pages.append(int(p))
        except (TypeError,ValueError): pass
    import pymupdf
    doc=pymupdf.open(path); pages=[p for p in dict.fromkeys(pages) if 0<=p<doc.page_count]
    if not pages: doc.close(); conn.close(); return jsonify({'error':'Pick at least one page'}),400
    now=datetime.utcnow().isoformat(); src=str(d.get('name') or 'PDF')[:120]; added=[]
    for p in pages:
        pg=doc[p]; z=min(300/72, PLAN_PX/max(pg.rect.width,pg.rect.height))
        fn=secure_filename('%s_plan_%s.png'%(job_id,uuid.uuid4().hex[:8]))
        pg.get_pixmap(matrix=pymupdf.Matrix(z,z),alpha=False).save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
        name=pm_next_name(conn, job_id)
        conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at,orig_name) VALUES (?,?,?,?,?)',(job_id,fn,'plan',now,('%s page %d'%(src,p+1))[:200]))
        pm_add(conn, job_id, fn, name, '%s page %d'%(src,p+1)); added.append(fn)
    doc.close(); conn.commit(); info=plan_info(conn, job_id); conn.close()
    return jsonify({'ok':True,'added':added,'plans':[x['fn'] for x in info],'plan_info':info})

@app.route('/api/jobs/<job_id>/plans/meta', methods=['POST'])
def plan_meta_save(job_id):
    conn=get_db(); row,err=survey_edit_row(conn, job_id)
    if err: conn.close(); return err
    d=request.get_json(silent=True) or {}; fn=secure_filename(str(d.get('filename') or ''))
    info={x['fn']:x for x in plan_info(conn, job_id)}
    if fn not in info: conn.close(); return ('',404)
    if not conn.execute('SELECT 1 FROM plan_meta WHERE filename=?',(fn,)).fetchone(): pm_add(conn, job_id, fn, info[fn]['name'])
    if 'name' in d:
        nm=' '.join(str(d.get('name') or '').split())[:60]
        if nm: conn.execute('UPDATE plan_meta SET name=? WHERE filename=?',(nm,fn))
    if 'scale' in d:
        s=pm_scale(d.get('scale')) if d.get('scale') else None
        if d.get('scale') and not s: conn.close(); return jsonify({'error':'Tap two different points and enter feet above 0'}),400
        conn.execute('UPDATE plan_meta SET scale=? WHERE filename=?',(json.dumps(s) if s else None,fn))
    conn.commit(); out=plan_info(conn, job_id); conn.close()
    return jsonify({'ok':True,'plan_info':out})

def design_can_edit(row):
    if row['job_type']=='Office Task': return False
    if row['job_type']=='Survey' and row['status']=='completed' and not is_office(): return False
    return True

DESIGN_CSS = r'''<style>
*{box-sizing:border-box}html,body{margin:0;background:#fff;color:#111;font-family:Inter,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.hidden{display:none!important}
.pd-top{display:flex;gap:8px;align-items:center;padding:8px 10px;border-bottom:1px solid #ecebe4;background:#fff;position:sticky;top:0;z-index:6;overflow-x:auto;scrollbar-width:none}
.pd-top::-webkit-scrollbar{display:none}
.pd-btn{min-height:44px;min-width:44px;padding:0 14px;border:1px solid #e3e2d8;border-radius:12px;background:#fff;font-weight:600;font-size:15px;line-height:1;font-family:inherit;color:#111;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px;text-decoration:none;white-space:nowrap;flex:none}
.pd-btn:active{transform:scale(.97)}.pd-btn.on,.pd-btn.gold{background:#a49f2a;border-color:#a49f2a;color:#fff}.pd-btn.red{color:#b00020;border-color:#f1c9cf}
.pd-btn[disabled]{opacity:.35;cursor:default}
.pd-title{font-weight:800;font-size:16px;white-space:nowrap;padding:0 4px}
.pd-tabs{display:flex;gap:6px}
.pd-main{display:flex;flex-direction:column}
.pd-view{position:relative;height:58vh;min-height:320px;background:#f4f4ef;overflow:hidden;border-bottom:1px solid #ecebe4;touch-action:none}
#pd-svg{width:100%;height:100%;display:block;touch-action:none;cursor:grab;user-select:none;-webkit-user-select:none}
#pd-svg.place{cursor:crosshair}
.pd-scale{position:absolute;left:10px;bottom:10px;background:rgba(255,255,255,.94);border:1px solid #e3e2d8;border-radius:10px;padding:6px 10px;font-size:12px;font-weight:700;display:flex;align-items:center;gap:8px;pointer-events:none}
.pd-scale i{display:block;height:8px;border:1.5px solid #111;background:repeating-linear-gradient(90deg,#111 0 25%,#fff 25% 50%)}
.pd-zoom{position:absolute;right:10px;bottom:10px;display:flex;flex-direction:column;gap:8px}
.pd-zoom .pd-btn{background:rgba(255,255,255,.96);font-size:22px;padding:0}
.pd-hint{position:absolute;left:50%;top:10px;transform:translateX(-50%);background:#111;color:#fff;border-radius:12px;padding:10px 14px;font-weight:700;font-size:14px;pointer-events:none;max-width:92%;text-align:center}
.pd-empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;text-align:center;padding:20px;color:#555}
.pd-side{padding:10px 12px 40px}
.pd-card{border:1px solid #ecebe4;border-radius:14px;padding:12px;margin:0 0 10px;background:#fff}
.pd-card h3{margin:0 0 8px;font-size:16px}
.pd-row{display:flex;gap:8px;flex-wrap:wrap}.pd-row>.pd-btn{flex:1 1 auto}
.pd-in{width:100%;min-height:44px;padding:10px 12px;border:1px solid #d9d8ce;border-radius:10px;font-size:16px;font-family:inherit;margin:4px 0 8px}
label.pd-l{display:block;font-weight:600;font-size:14px;margin-top:4px}
.pd-muted{color:#666;font-size:13px}
.pd-leg{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #f0efe8;font-size:15px}.pd-leg b{margin-left:auto}
.pd-modal{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:20;display:flex;align-items:flex-end;justify-content:center}
.pd-sheet{background:#fff;border-radius:18px 18px 0 0;width:100%;max-width:820px;max-height:88vh;overflow:auto;padding:14px}
@media(min-width:700px){.pd-modal{align-items:center}.pd-sheet{border-radius:18px}}
.pd-pages{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin:10px 0}
.pd-page{border:2px solid #e3e2d8;border-radius:12px;padding:6px;background:#fff;cursor:pointer;text-align:center;font-weight:600;font-size:14px;font-family:inherit}
.pd-page img{width:100%;height:120px;object-fit:contain;background:#f6f6f2;border-radius:8px;display:block;margin-bottom:4px}
.pd-page.on{border-color:#a49f2a;box-shadow:0 0 0 3px rgba(164,159,42,.25)}
@media(min-width:768px){.pd-main{flex-direction:row}.pd-view{flex:1;height:calc(100vh - 61px);height:calc(100dvh - 61px);border-bottom:0}.pd-side{width:290px;flex:none;height:calc(100vh - 61px);height:calc(100dvh - 61px);overflow:auto;border-left:1px solid #ecebe4}}
@media(min-width:1024px){.pd-side{width:340px}}@media(min-width:1280px){.pd-side{width:380px}}
.pd-fs{position:absolute;right:10px;top:10px;background:rgba(255,255,255,.96);font-weight:800}
.pd-isz{font-size:13px!important}
body.pd-full{overflow:hidden}body.pd-full .pd-top{display:none}
body.pd-full .pd-view{position:fixed;inset:0;width:100vw;height:100vh;height:100dvh;min-height:0;z-index:15;border:0}
body.pd-full .pd-fs{background:#a49f2a;border-color:#a49f2a;color:#fff;padding:0 20px}
body.pd-full .pd-side{height:0;padding:0;overflow:visible;border:0;width:0}
body.pd-full .pd-side>.pd-card{display:none}
body.pd-full #pd-traycard,body.pd-full #pd-sel:not(.hidden),body.pd-full #pd-sc:not(.hidden){display:block;position:fixed;left:8px;right:8px;bottom:calc(8px + env(safe-area-inset-bottom));z-index:17;margin:0;box-shadow:0 6px 24px rgba(0,0,0,.22);padding:8px}
body.pd-full #pd-sel,body.pd-full #pd-sc{max-height:46vh;overflow:auto;padding:12px}
body.pd-full.pd-hassel #pd-traycard,body.pd-full.pd-scaling #pd-traycard{display:none}
body.pd-full #pd-traycard h3,body.pd-full #pd-traycard .pd-muted{display:none}
body.pd-full .pd-zoom{bottom:calc(112px + env(safe-area-inset-bottom))}
body.pd-full.pd-hassel .pd-zoom,body.pd-full.pd-scaling .pd-zoom{display:none}
body.pd-full .pd-scale{bottom:calc(112px + env(safe-area-inset-bottom))}
@media(min-width:768px){body.pd-full #pd-traycard,body.pd-full #pd-sel:not(.hidden),body.pd-full #pd-sc:not(.hidden){left:auto;right:10px;top:64px;bottom:10px;width:300px;max-height:none}
body.pd-full .pd-zoom{right:320px;bottom:10px}body.pd-full.pd-hassel .pd-zoom{display:flex}body.pd-full .pd-scale{bottom:10px}body.pd-full.pd-hassel #pd-traycard{display:none}}
@media(orientation:landscape) and (max-height:520px){body.pd-full #pd-traycard,body.pd-full #pd-sel:not(.hidden),body.pd-full #pd-sc:not(.hidden){left:auto;right:8px;top:60px;bottom:8px;width:118px;max-height:none}
body.pd-full #pd-sel:not(.hidden),body.pd-full #pd-sc:not(.hidden){width:300px}
body.pd-full .pd-zoom{right:136px;bottom:8px}body.pd-full.pd-hassel .pd-zoom{display:flex;right:318px}body.pd-full .pd-scale{bottom:8px}}

</style>'''

DESIGN_BODY = r'''<div class="pd-top"><a class="pd-btn" href="__BACK__">&larr; Job</a><span class="pd-title">__TITLE__</span><div class="pd-tabs" id="pd-tabs"></div><span id="pd-topx"></span></div>
<div class="pd-main"><div class="pd-view" id="pd-view"><svg id="pd-svg" xmlns="http://www.w3.org/2000/svg"><image id="pd-img" x="0" y="0" preserveAspectRatio="none"/><g id="pd-ov"></g></svg>
<div class="pd-empty hidden" id="pd-empty"><div style="font-size:18px;font-weight:700">No floor plan yet</div><div>Add a photo, image or PDF of the floor plan.</div><button class="pd-btn gold edit-only" onclick="pickFile()">+ Add Floor Plan</button></div>
<button class="pd-btn pd-fs" id="pd-fs" onclick="fsToggle()">&#x26F6; Full screen</button><div class="pd-hint hidden" id="pd-hint"></div><div class="pd-scale hidden" id="pd-scale"></div>
<div class="pd-zoom"><button class="pd-btn" aria-label="Zoom in" onclick="zoomBtn(1/1.3)">+</button><button class="pd-btn" aria-label="Zoom out" onclick="zoomBtn(1.3)">&minus;</button><button class="pd-btn" style="font-size:13px" onclick="fit();draw()">Fit</button><button class="pd-btn pd-isz" id="pd-iszb" aria-label="Icon size" onclick="iszCycle()">M</button></div></div>
<div class="pd-side" id="pd-side">
<div id="pd-x1"></div>
<div class="pd-card hidden" id="pd-sc"><h3>Set scale</h3><div class="pd-muted">Real distance between the two points you tapped</div><label class="pd-l" for="pd-ft">Feet</label><input class="pd-in" id="pd-ft" type="number" inputmode="decimal" min="0.1" step="any" placeholder="e.g. 20"><div class="pd-row"><button class="pd-btn gold" onclick="scaleSave()">Save scale</button><button class="pd-btn" onclick="scaleCancel()">Cancel</button></div></div>
<div class="pd-card" id="pd-plancard"><h3 id="pd-pname">Plan</h3><div class="pd-muted" id="pd-pinfo"></div><div class="pd-row" style="margin-top:8px"><button class="pd-btn" id="pd-meas" onclick="measToggle()">Measure</button></div><div class="pd-row edit-only" style="margin-top:8px"><button class="pd-btn" onclick="scaleStart()">Set scale</button><button class="pd-btn" onclick="renamePlan()">Rename</button><button class="pd-btn" onclick="pickFile()">+ Add plan</button><button class="pd-btn red" onclick="removePlan()">Remove</button></div>
<div class="pd-row" style="margin-top:8px;align-items:center"><span style="font-weight:600;font-size:14px">Icon size</span><button class="pd-btn" data-isz="S" onclick="iszSet(&quot;S&quot;)">S</button><button class="pd-btn" data-isz="M" onclick="iszSet(&quot;M&quot;)">M</button><button class="pd-btn" data-isz="L" onclick="iszSet(&quot;L&quot;)">L</button></div><div class="pd-row" style="margin-top:8px"><button class="pd-btn" id="pd-cov" onclick="covToggle()">Coverage: On</button><span id="pd-x2"></span></div></div>
<div class="pd-card" id="pd-legend"></div></div></div>
<input type="file" id="pd-file" accept="image/*,.pdf,application/pdf" multiple style="display:none" onchange="upload(this)">
<div class="pd-modal hidden" id="pd-pdf"><div class="pd-sheet"><h3 style="margin:0">Pick the pages to add</h3><div class="pd-muted" id="pd-pdfinfo"></div><div class="pd-pages" id="pd-pages"></div><div class="pd-row"><button class="pd-btn gold" id="pd-pdfadd" onclick="pdfAdd()">Add pages</button><button class="pd-btn" onclick="pdfClose()">Cancel</button></div></div></div>
<div class="pd-modal hidden" id="pd-busy"><div class="pd-sheet" style="text-align:center;font-weight:700" id="pd-busytxt">Working...</div></div>'''

DESIGN_JS_CORE = r'''
const D=JSON.parse(document.getElementById('pd-data').textContent);
const JID=D.job.id,API='/api/jobs/'+encodeURIComponent(JID),EDIT=D.edit;
let P=D.plans,DEV=D.devices,cur=0,vb=null,sel=null,tool='',scaleMode=0,scalePts=[];
const $=id=>document.getElementById(id),svg=$('pd-svg');
const TYPES=D.types,SYM=D.sym,CAM_DEF=D.camdef,CAM_RANGE=D.camrange,CAM_FT=D.camft||{};let measMode=0,measPts=[];
function esc(v){const d=document.createElement('div');d.textContent=v==null?'':String(v);return d.innerHTML}
function plan(){return P[cur]}
function col(t){const d=TYPES.find(x=>x[0]===t);return d?d[2]:'#333'}
function pre(t){const d=TYPES.find(x=>x[0]===t);return d?d[1]:'?'}
function symG(t,x,y,s,solid){const c=col(t),u=s/24;return '<g transform="translate('+(x-s/2)+' '+(y-s/2)+') scale('+u+')"><circle cx="12" cy="12" r="11" fill="'+(solid===false?'#fff':c)+'" stroke="#fff" stroke-width="1.6"/><g transform="translate(12 12) scale(.66) translate(-12 -12)" fill="none" stroke="'+(solid===false?c:'#fff')+'" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+(SYM[t]||'<circle cx="12" cy="12" r="3"/>')+'</g></g>'}
function symHtml(t,size){return '<svg viewBox="0 0 24 24" width="'+size+'" height="'+size+'" aria-hidden="true">'+symG(t,12,12,24)+'</svg>'}
function covOn(){return localStorage.getItem('fo_cov')!=='0'}
function covToggle(){localStorage.setItem('fo_cov',covOn()?'0':'1');draw();side()}
const ISZ={S:[30,13],M:[42,15],L:[56,18]};function iszK(){const v=localStorage.getItem('fo_isz');return ISZ[v]?v:'M'}
function iszSet(v){localStorage.setItem('fo_isz',v);iszMark();draw()}function iszCycle(){const o=['S','M','L'];iszSet(o[(o.indexOf(iszK())+1)%3])}
function iszMark(){const v=iszK();$('pd-iszb').textContent=v;document.querySelectorAll('[data-isz]').forEach(b=>b.classList.toggle('on',b.dataset.isz===v))}
let FS=false,FSreal=false;
function fsToggle(on){FS=on===undefined?!FS:!!on;document.body.classList.toggle('pd-full',FS);$('pd-fs').innerHTML=FS?'Done':'&#x26F6; Full screen';
if(FS&&on===undefined&&document.documentElement.requestFullscreen&&!document.fullscreenElement){document.documentElement.requestFullscreen().then(()=>{FSreal=true}).catch(()=>{})}
if(!FS&&FSreal&&document.fullscreenElement){FSreal=false;document.exitFullscreen().catch(()=>{})}
requestAnimationFrame(()=>window.dispatchEvent(new Event('resize')));setTimeout(()=>window.dispatchEvent(new Event('resize')),250)}
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement&&FSreal){FSreal=false;if(FS)fsToggle(false)}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&FS&&!sel&&!tool)fsToggle(false)});
function pinfo(fn){return P.find(p=>p.fn===fn)}
function camView(d){const p=pinfo(d.plan),v={dir:d.dir||0,fov:d.fov||CAM_DEF[d.type]||90,range:d.range||CAM_RANGE,ft:null};if(p&&p.ppf&&p.w){v.ft=d.rft||CAM_FT[d.type]||30;v.range=+(v.ft*p.ppf/p.w*100).toFixed(2)}return v}
function rangePct(d){return camView(d).range}
function measFt(){const p=plan();if(!p||!p.ppf||measPts.length<2)return null;const [a,b]=measPts;return Math.hypot((b.x-a.x)/100*p.w,(b.y-a.y)/100*p.h)/p.ppf}
function ftStr(f){if(f<100){const ft=Math.floor(f),inch=Math.round((f-ft)*12);return inch===12?(ft+1)+' ft':ft+' ft '+inch+' in'}return Math.round(f)+' ft'}
function measToggle(){const p=plan();if(!p)return;if(!p.ppf&&!measMode){alert('Set the scale first. Tap Set scale and tap two points of a known distance.');return}measMode=measMode?0:1;measPts=[];tool='';sel=null;if(window.trayMark)trayMark();$('pd-meas').classList.toggle('on',!!measMode);$('pd-meas').textContent=measMode?'Stop measuring':'Measure';draw();if(window.PD_SIDE)PD_SIDE()}
function conePath(cx,cy,r,dir,fov){if(fov>=360)return 'M'+cx+' '+cy+'m-'+r+' 0a'+r+' '+r+' 0 1 0 '+2*r+' 0a'+r+' '+r+' 0 1 0 -'+2*r+' 0z';const a1=(dir-fov/2)*Math.PI/180,a2=(dir+fov/2)*Math.PI/180;
return 'M'+cx+' '+cy+'L'+(cx+r*Math.sin(a1)).toFixed(1)+' '+(cy-r*Math.cos(a1)).toFixed(1)+'A'+r+' '+r+' 0 '+(fov>180?1:0)+' 1 '+(cx+r*Math.sin(a2)).toFixed(1)+' '+(cy-r*Math.cos(a2)).toFixed(1)+'Z'}
function box(){return svg.getBoundingClientRect()}
function fit(){const p=plan();if(!p)return;const r=box(),a=(r.height||1)/(r.width||1),W=p.w*1.06,H=p.h*1.06;
if(W*a>=H)vb={w:W,h:W*a};else vb={h:H,w:H/a};vb.x=(p.w-vb.w)/2;vb.y=(p.h-vb.h)/2}
function toPlan(e){const r=box();return {x:vb.x+(e.clientX-r.left)/r.width*vb.w,y:vb.y+(e.clientY-r.top)/r.height*vb.h}}
function clampZoom(){const p=plan(),r=box(),a=r.height/r.width,maxW=Math.max(p.w,p.h/a)*3,minW=Math.max(p.w,p.h)/30;if(vb.w>maxW){vb.h*=maxW/vb.w;vb.w=maxW}if(vb.w<minW){vb.h*=minW/vb.w;vb.w=minW}}
function zoomAt(f,cx,cy){if(!vb)return;const r=box(),px=vb.x+(cx-r.left)/r.width*vb.w,py=vb.y+(cy-r.top)/r.height*vb.h;vb.w*=f;vb.h*=f;clampZoom();vb.x=px-(cx-r.left)/r.width*vb.w;vb.y=py-(cy-r.top)/r.height*vb.h;draw()}
function zoomBtn(f){const r=box();zoomAt(f,r.left+r.width/2,r.top+r.height/2)}
function k(){return vb.w/(box().width||1)}
function planDevs(){const p=plan();return p?DEV.filter(d=>d.plan===p.fn):[]}
function draw(){const p=plan();$('pd-empty').classList.toggle('hidden',!!p);const img=$('pd-img'),ov=$('pd-ov');
if(!p){img.removeAttribute('href');ov.innerHTML='';svg.removeAttribute('viewBox');$('pd-scale').classList.add('hidden');hint();return}
const href='/uploads/'+encodeURIComponent(p.fn);if(img.getAttribute('href')!==href){img.setAttribute('href',href)}img.setAttribute('width',p.w);img.setAttribute('height',p.h);
if(!vb)fit();svg.setAttribute('viewBox',vb.x+' '+vb.y+' '+vb.w+' '+vb.h);const K=k(),IZ=ISZ[iszK()],s=IZ[0]*K,fz=IZ[1]*K,W=p.w,H=p.h;let h='';
if(covOn())planDevs().filter(d=>CAM_DEF[d.type]).forEach(d=>{const v=camView(d),c=col(d.type);h+='<path d="'+conePath(d.x*W/100,d.y*H/100,rangePct(d)*W/100,v.dir,v.fov)+'" fill="'+c+'" fill-opacity="'+(sel===d.id?.2:.11)+'" stroke="'+c+'" stroke-opacity=".55" stroke-width="1.5" vector-effect="non-scaling-stroke"'+(d.type==='PTZ Cam'?' stroke-dasharray="6 4"':'')+' pointer-events="none"/>'});
planDevs().forEach(d=>{const x=d.x*W/100,y=d.y*H/100,lf=d.x>82;h+='<g data-id="'+esc(d.id)+'" style="cursor:pointer">'+(sel===d.id?'<circle cx="'+x+'" cy="'+y+'" r="'+(s*.78)+'" fill="#a49f2a" fill-opacity=".22" stroke="#a49f2a" stroke-width="2.5" vector-effect="non-scaling-stroke"/>':'')+
'<circle cx="'+x+'" cy="'+y+'" r="'+Math.max(s*.75,24*K)+'" fill="transparent"/>'+symG(d.type,x,y,s)+'<text x="'+(lf?x-s*.62:x+s*.62)+'" y="'+y+'" font-size="'+fz+'" font-weight="800" fill="#111" stroke="#fff" stroke-width="'+(4.5*K)+'" stroke-linejoin="round" paint-order="stroke" dominant-baseline="middle" text-anchor="'+(lf?'end':'start')+'" pointer-events="none">'+esc(d.label||pre(d.type))+'</text></g>'});
if(window.PD_HANDLES)h+=PD_HANDLES(W,H,K);
scalePts.forEach((q,i)=>{const x=q.x*W/100,y=q.y*H/100;h+='<circle cx="'+x+'" cy="'+y+'" r="'+(7*K)+'" fill="#d32f2f" stroke="#fff" stroke-width="'+(2*K)+'"/>'});
if(scalePts.length===2)h+='<line x1="'+scalePts[0].x*W/100+'" y1="'+scalePts[0].y*H/100+'" x2="'+scalePts[1].x*W/100+'" y2="'+scalePts[1].y*H/100+'" stroke="#d32f2f" stroke-width="3" stroke-dasharray="8 5" vector-effect="non-scaling-stroke"/>';
measPts.forEach(q=>{h+='<circle cx="'+q.x*W/100+'" cy="'+q.y*H/100+'" r="'+(7*K)+'" fill="#111" stroke="#fff" stroke-width="'+(2*K)+'"/>'});
if(measPts.length===2){const [a,b]=measPts,x1=a.x*W/100,y1=a.y*H/100,x2=b.x*W/100,y2=b.y*H/100,f=measFt();h+='<line x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" stroke="#111" stroke-width="3" vector-effect="non-scaling-stroke"/>';
if(f!=null)h+='<text x="'+(x1+x2)/2+'" y="'+((y1+y2)/2-12*K)+'" font-size="'+(16*K)+'" font-weight="800" fill="#111" stroke="#fff" stroke-width="'+(4*K)+'" paint-order="stroke" text-anchor="middle">'+ftStr(f)+'</text>'}
ov.innerHTML=h;scalebar();hint();svg.classList.toggle('place',!!tool||scaleMode>0||measMode>0)}
function niceFt(pxFt){const n=[1,2,5,10,20,25,50,100,200,250,500,1000,2000,5000];return n.find(v=>v*pxFt>=64)||n[n.length-1]}
function scalebar(){const p=plan(),el=$('pd-scale');if(!p){el.classList.add('hidden');return}el.classList.remove('hidden');
if(!p.ppf){el.innerHTML=EDIT?'Not to scale yet':'Not to scale';return}const pxFt=p.ppf/k(),ft=niceFt(pxFt);el.innerHTML='<i style="width:'+Math.round(ft*pxFt)+'px"></i><span>'+ft+' ft</span>'}
function hintText(){if(scaleMode===1)return 'Tap the first point of a known distance';if(scaleMode===2)return 'Tap the second point';if(scaleMode===3)return 'Enter the real distance below';if(measMode)return measPts.length<2?(measPts.length?'Tap the second point':'Tap two points to measure'):ftStr(measFt()||0)+'. Tap again to measure something else.';return window.PD_HINT?PD_HINT():''}
function hint(){const t=hintText(),el=$('pd-hint');el.textContent=t;el.classList.toggle('hidden',!t)}
function tabs(){$('pd-tabs').innerHTML=P.map((p,i)=>'<button class="pd-btn'+(i===cur?' on':'')+'" onclick="go('+i+')">'+esc(p.name)+'</button>').join('')+(EDIT?'<button class="pd-btn" onclick="pickFile()">+ Plan</button>':'')}
function go(i){cur=i;vb=null;sel=null;scaleCancel(true);if(measMode)measToggle();render()}
function legend(){const L=$('pd-legend'),all={},here={};DEV.forEach(d=>all[d.type]=(all[d.type]||0)+1);planDevs().forEach(d=>here[d.type]=(here[d.type]||0)+1);
const p=plan();if(!DEV.length){L.innerHTML='<h3>Legend</h3><div class="pd-muted">No devices yet</div>';return}
L.innerHTML='<h3>Legend</h3><div class="pd-muted">'+(p?esc(p.name)+': '+planDevs().length+' &middot; ':'')+'All plans: '+DEV.length+'</div>'+TYPES.filter(t=>all[t[0]]).map(t=>'<div class="pd-leg">'+symHtml(t[0],26)+'<span>'+esc(t[0])+'</span><b>'+(here[t[0]]||0)+(P.length>1?' / '+all[t[0]]:'')+'</b></div>').join('')}
function side(){const p=plan();$('pd-plancard').classList.toggle('hidden',!p);if(p){$('pd-pname').textContent=p.name;$('pd-pinfo').textContent=(p.ppf?'Scale set: '+p.scale.ft+' ft between two points':'Not to scale yet. Tap Set scale and tap two points of a known distance.')+(p.src?' \u00b7 '+p.src:'')}
$('pd-cov').textContent='Coverage: '+(covOn()?'On':'Off');legend();if(window.PD_SIDE)PD_SIDE();document.body.classList.toggle('pd-hassel',!!(sel&&DEV.find(d=>d.id===sel)));document.body.classList.toggle('pd-scaling',scaleMode===3)}
function render(){tabs();draw();side()}
// gestures
const ptr=new Map();let g=null;
function dist(a,b){return Math.hypot(a.x-b.x,a.y-b.y)}function mid(a,b){return {x:(a.x+b.x)/2,y:(a.y+b.y)/2}}
svg.addEventListener('pointerdown',e=>{if(!plan())return;svg.setPointerCapture(e.pointerId);ptr.set(e.pointerId,{x:e.clientX,y:e.clientY});
if(ptr.size===2){const [a,b]=[...ptr.values()];if(g&&g.dev&&g.moved&&window.PD_DRAGEND)PD_DRAGEND(g);g={pinch:true,d0:dist(a,b)||1,m0:mid(a,b),vb0:Object.assign({},vb)};return}
if(ptr.size>2)return;const t=e.target.closest&&e.target.closest('[data-id]'),th=e.target.closest&&e.target.closest('[data-h]');g={x0:e.clientX,y0:e.clientY,vb0:Object.assign({},vb),dev:t?t.getAttribute('data-id'):null,hd:th?th.getAttribute('data-h'):null,moved:false}});
svg.addEventListener('pointermove',e=>{if(!ptr.has(e.pointerId))return;ptr.set(e.pointerId,{x:e.clientX,y:e.clientY});if(!g)return;const r=box();
if(g.pinch){if(ptr.size<2)return;const [a,b]=[...ptr.values()],m=mid(a,b),f=g.d0/(dist(a,b)||1),px=g.vb0.x+(g.m0.x-r.left)/r.width*g.vb0.w,py=g.vb0.y+(g.m0.y-r.top)/r.height*g.vb0.h;
vb.w=g.vb0.w*f;vb.h=g.vb0.h*f;clampZoom();vb.x=px-(m.x-r.left)/r.width*vb.w;vb.y=py-(m.y-r.top)/r.height*vb.h;draw();return}
const dx=e.clientX-g.x0,dy=e.clientY-g.y0;if(!g.moved&&Math.hypot(dx,dy)>(g.hd?2:6)){g.moved=true;if(g.dev&&window.PD_DRAGSTART&&!scaleMode&&!measMode)PD_DRAGSTART(g)}
if(!g.moved)return;if(g.hd&&window.PD_CONE){PD_CONE(g,e);return}if(g.drag&&window.PD_DRAG){PD_DRAG(g,e);return}vb.x=g.vb0.x-dx/r.width*vb.w;vb.y=g.vb0.y-dy/r.height*vb.h;draw()});
function up(e){if(!ptr.has(e.pointerId))return;ptr.delete(e.pointerId);if(!g)return;
if(g.hd&&!g.pinch){if(g.moved&&window.PD_CONEEND)PD_CONEEND(g)}else if(!g.pinch){if(!g.moved&&e.type==='pointerup')tap(e,g.dev);else if(g.drag&&window.PD_DRAGEND)PD_DRAGEND(g)}
if(ptr.size===0)g=null;else if(g.pinch&&ptr.size===1){const [a]=[...ptr.values()];g={x0:a.x,y0:a.y,vb0:Object.assign({},vb),dev:null,moved:true}}}
svg.addEventListener('pointerup',up);svg.addEventListener('pointercancel',up);
svg.addEventListener('wheel',e=>{e.preventDefault();zoomAt(e.deltaY>0?1.15:1/1.15,e.clientX,e.clientY)},{passive:false});
window.addEventListener('resize',()=>{if(!vb)return;const r=box(),cx=vb.x+vb.w/2,cy=vb.y+vb.h/2;vb.h=vb.w*r.height/r.width;vb.x=cx-vb.w/2;vb.y=cy-vb.h/2;draw()});
function tap(e,id){const p=plan();if(!p)return;const q=toPlan(e),pc={x:Math.max(0,Math.min(100,q.x/p.w*100)),y:Math.max(0,Math.min(100,q.y/p.h*100))};
if(scaleMode===1||scaleMode===2){if(q.x<0||q.y<0||q.x>p.w||q.y>p.h)return;scalePts.push(pc);scaleMode++;if(scaleMode===3){$('pd-sc').classList.remove('hidden');document.body.classList.add('pd-scaling');$('pd-ft').value='';setTimeout(()=>{$('pd-sc').scrollIntoView({behavior:'smooth',block:'nearest'});$('pd-ft').focus()},50)}draw();return}
if(measMode){if(q.x<0||q.y<0||q.x>p.w||q.y>p.h)return;if(measPts.length>=2)measPts=[];measPts.push(pc);draw();return}
if(window.PD_TAP){PD_TAP(e,id,pc,q);return}}
// plans
function busy(t){$('pd-busy').classList.toggle('hidden',!t);if(t)$('pd-busytxt').textContent=t}
async function reload(){const r=await fetch(API);if(!r.ok)return;const j=await r.json();P=j.plan_info||[];DEV=j.devices||[];if(cur>=P.length)cur=Math.max(0,P.length-1)}
function pickFile(){if(EDIT)$('pd-file').click()}
let PDF=null;
async function upload(inp){const files=[...inp.files];inp.value='';if(!files.length)return;const imgs=files.filter(f=>!/\.pdf$/i.test(f.name)&&f.type!=='application/pdf'),pdfs=files.filter(f=>!imgs.includes(f));const before=P.length;
if(imgs.length){busy('Uploading plan...');const fd=new FormData();imgs.forEach(f=>fd.append('files',f));const r=await fetch(API+'/plans',{method:'POST',body:fd});busy('');if(!r.ok){const e=await r.json().catch(()=>({}));alert(e.error||'Upload failed');return}await reload();cur=before;vb=null;render()}
if(pdfs.length){busy('Reading PDF...');const fd=new FormData();fd.append('file',pdfs[0]);const r=await fetch(API+'/plans/pdf',{method:'POST',body:fd});const d=await r.json().catch(()=>({}));busy('');if(!r.ok){alert(d.error||'Could not read that PDF');return}
PDF={token:d.token,name:d.name,on:new Set([0])};if(d.pages===1){pdfAdd();return}
$('pd-pdfinfo').textContent=d.name+': '+d.pages+' pages'+(d.pages>d.shown?' (first '+d.shown+' shown)':'')+'. Tap each floor you want.';
$('pd-pages').innerHTML=Array.from({length:d.shown},(_,i)=>'<button class="pd-page'+(i===0?' on':'')+'" data-i="'+i+'" onclick="pdfPick(this)"><img loading="lazy" src="'+API+'/plans/pdf/'+d.token+'/'+i+'.png" alt="page '+(i+1)+'">Page '+(i+1)+'</button>').join('');pdfBtn();$('pd-pdf').classList.remove('hidden')}}
function pdfPick(b){const i=+b.dataset.i;if(PDF.on.has(i))PDF.on.delete(i);else PDF.on.add(i);b.classList.toggle('on',PDF.on.has(i));pdfBtn()}
function pdfBtn(){const n=PDF.on.size;$('pd-pdfadd').textContent=n?'Add '+n+' page'+(n>1?'s':''):'Pick a page';$('pd-pdfadd').disabled=!n}
function pdfClose(){$('pd-pdf').classList.add('hidden');PDF=null}
async function pdfAdd(){if(!PDF||!PDF.on.size)return;const before=P.length;$('pd-pdf').classList.add('hidden');busy('Making the plan from the PDF...');
const r=await fetch(API+'/plans/pdf/'+PDF.token+'/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pages:[...PDF.on].sort((a,b)=>a-b),name:PDF.name})});const d=await r.json().catch(()=>({}));busy('');PDF=null;
if(!r.ok){alert(d.error||'Could not add the pages');return}await reload();cur=before;vb=null;render()}
async function meta(body){const r=await fetch(API+'/plans/meta',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign({filename:plan().fn},body))});const d=await r.json().catch(()=>({}));if(!r.ok){alert(d.error||'Not saved');return false}P=d.plan_info;return true}
async function renamePlan(){const p=plan();if(!p)return;const n=prompt('Plan name (for example Floor 1)',p.name);if(n===null||!n.trim())return;if(await meta({name:n.trim()}))render()}
async function removePlan(){const p=plan();if(!p||!confirm('Remove '+p.name+' and the devices on it?'))return;const r=await fetch(API+'/plans/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:p.fn})});if(!r.ok){alert('Not removed');return}await reload();cur=Math.max(0,cur-1);vb=null;sel=null;render()}
function scaleStart(){if(!plan())return;if(measMode)measToggle();sel=null;tool='';scalePts=[];scaleMode=1;if(window.PD_SIDE)PD_SIDE();draw();$('pd-view').scrollIntoView({behavior:'smooth',block:'nearest'})}
function scaleCancel(quiet){scaleMode=0;scalePts=[];$('pd-sc').classList.add('hidden');document.body.classList.remove('pd-scaling');if(!quiet)draw()}
async function scaleSave(){const ft=parseFloat($('pd-ft').value);if(!(ft>0)){alert('Enter the real distance in feet');return}const [a,b]=scalePts;
if(await meta({scale:{ax:a.x,ay:a.y,bx:b.x,by:b.y,ft:ft}})){scaleCancel(true);render()}}
document.querySelectorAll('.edit-only').forEach(e=>e.classList.toggle('hidden',!EDIT));
'''
DESIGN_JS_INIT = r'''
iszMark();if(new URLSearchParams(location.search).get('full')==='1')fsToggle(true);
render();requestAnimationFrame(()=>{vb=null;draw()});
'''
def design_page(row, conn, js_extra='', body_extra=''):
    data={'job':{'id':row['id'],'title':row['title'] or '','customer':row['customer'] or '','type':row['job_type'] or ''},
          'plans':plan_info(conn, row['id']),'devices':parse_devices(row['survey_devices']),'types':DEVICE_TYPES,'sym':DEV_SYM,'camdef':CAM_DEF,'camft':CAM_FT,'camrange':CAM_RANGE,
          'edit':design_can_edit(row),'office':is_office()}
    data.update(design_data_extra(conn, row))
    js=json.dumps(data).replace('</','<\\/')
    body=DESIGN_BODY.replace('__BACK__',h('/?job='+urllib.parse.quote(row['id']))).replace('__TITLE__',h('Plan Designer'))
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Plan Designer %s</title>%s</head><body>%s%s'
            '<script id="pd-data" type="application/json">%s</script><script>%s%s%s</script></body></html>')%(h(row['id']),DESIGN_CSS,body,body_extra,js,DESIGN_JS_CORE,js_extra,DESIGN_JS_INIT)
def design_data_extra(conn, row): return {}
DESIGN_CSS_EDIT = r'''<style>
.pd-tray{display:flex;gap:8px;overflow-x:auto;padding:2px 2px 6px;touch-action:pan-x;scrollbar-width:thin}
.pd-tool{flex:none;width:80px;min-height:74px;border:1.5px solid #e3e2d8;border-radius:12px;background:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;font-size:11.5px;font-weight:600;cursor:grab;padding:6px 4px;font-family:inherit;color:#111;text-align:center;line-height:1.15;touch-action:pan-x;user-select:none;-webkit-user-select:none}
.pd-tool.on{border-color:#a49f2a;background:#f8f7ea;box-shadow:0 0 0 3px rgba(164,159,42,.28)}
@media(min-width:768px){.pd-tray{display:grid;grid-template-columns:repeat(3,1fr);overflow:visible;touch-action:pan-y}.pd-tool{width:auto;touch-action:pan-y}}@media(min-width:1024px){.pd-tray{grid-template-columns:repeat(4,1fr)}}
body.pd-full .pd-tray{display:flex;overflow-x:auto;overflow-y:hidden}body.pd-full .pd-tool{width:76px;min-height:68px}
@media(min-width:768px){body.pd-full .pd-tray{display:grid;grid-template-columns:repeat(3,1fr);overflow:auto;max-height:100%;touch-action:pan-y}body.pd-full .pd-tool{width:auto;touch-action:pan-y}}
@media(orientation:landscape) and (max-height:520px){body.pd-full .pd-tray{display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;height:100%;touch-action:pan-y}body.pd-full .pd-tool{width:100%;touch-action:pan-y}}
.pd-ghost{position:fixed;z-index:30;pointer-events:none;transform:translate(-50%,-50%);filter:drop-shadow(0 4px 8px rgba(0,0,0,.3))}
.pd-ur{position:absolute;left:10px;top:10px;display:flex;gap:8px}.pd-ur .pd-btn{background:rgba(255,255,255,.96)}
.pd-save{position:absolute;right:10px;top:62px;background:rgba(255,255,255,.94);border:1px solid #e3e2d8;border-radius:10px;padding:6px 10px;font-size:12px;font-weight:700;color:#2e7d32;pointer-events:none}
.pd-save.bad{color:#b00020}
.pd-hint{top:64px}
.pd-cam{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:6px 0 8px}.pd-cam .pd-btn{font-size:13px;padding:0 6px}
.pd-sel{border:2px solid #a49f2a}
@media(max-width:767px){.pd-view{height:60vh}}
</style>'''
DESIGN_BODY_EDIT = r'''<div id="pd-editx"></div>'''
DESIGN_JS_EDIT = r'''
let UNDO=[],REDO=[],saveT=null,saving=false,again=false;
const view=$('pd-view');
if(EDIT){view.insertAdjacentHTML('beforeend','<div class="pd-ur"><button class="pd-btn" id="pd-undo" onclick="undo()" aria-label="Undo">&#8630; Undo</button><button class="pd-btn" id="pd-redo" onclick="redo()" aria-label="Redo">Redo &#8631;</button></div><div class="pd-save hidden" id="pd-save"></div>');
$('pd-x1').insertAdjacentHTML('beforebegin','<div class="pd-card pd-sel hidden" id="pd-sel"></div><div class="pd-card" id="pd-traycard"><h3>Devices</h3><div class="pd-muted" style="margin-bottom:8px">Tap a device, then tap the plan. Or drag it onto the plan.</div><div class="pd-tray" id="pd-tray"></div></div>')}
else $('pd-x1').insertAdjacentHTML('beforebegin','<div class="pd-card pd-sel hidden" id="pd-sel"></div>');
function snap(){return JSON.stringify(DEV)}
function newId(){return 'd'+Math.random().toString(16).slice(2,12)}
function undoBtns(){if(!EDIT)return;$('pd-undo').disabled=!UNDO.length;$('pd-redo').disabled=!REDO.length}
function commit(before){if(before===snap())return;UNDO.push(before);if(UNDO.length>150)UNDO.shift();REDO=[];undoBtns();queueSave()}
function fixSel(){if(sel&&!DEV.find(d=>d.id===sel))sel=null}
function undo(){if(!UNDO.length)return;REDO.push(snap());DEV=JSON.parse(UNDO.pop());fixSel();undoBtns();queueSave();draw();side()}
function redo(){if(!REDO.length)return;UNDO.push(snap());DEV=JSON.parse(REDO.pop());fixSel();undoBtns();queueSave();draw();side()}
function status(t,bad){const s=$('pd-save');if(!s)return;s.textContent=t;s.classList.toggle('bad',!!bad);s.classList.toggle('hidden',!t);clearTimeout(status.t);if(t==='Saved')status.t=setTimeout(()=>s.classList.add('hidden'),1500)}
function queueSave(){clearTimeout(saveT);status('Saving...');saveT=setTimeout(save,450)}
async function save(){if(saving){again=true;return}saving=true;try{const r=await fetch(API+'/devices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({devices:DEV})});
if(r.ok){const d=await r.json(),lab={};(d.devices||[]).forEach(x=>lab[x.id]=x.label);DEV.forEach(x=>{if(lab[x.id])x.label=lab[x.id]});status('Saved')}else{const e=await r.json().catch(()=>({}));status(e.error||'Not saved',true)}}catch(e){status('No connection. Not saved yet.',true)}
saving=false;if(again){again=false;save()}draw();side()}
window.addEventListener('beforeunload',e=>{if(saving||saveT&&$('pd-save').textContent==='Saving...'){e.preventDefault();e.returnValue=''}});
function trayTA(){const tr=$('pd-tray');if(!tr)return;const sx=tr.scrollWidth>tr.clientWidth+2,sy=tr.scrollHeight>tr.clientHeight+2,v=sx?'pan-x':sy?'pan-y':'none';tr.style.touchAction=v;tr.querySelectorAll('.pd-tool').forEach(b=>b.style.touchAction=v)}
window.addEventListener('resize',()=>setTimeout(trayTA,60));
function trayMark(){document.querySelectorAll('.pd-tool').forEach(b=>b.classList.toggle('on',b.dataset.t===tool))}
function trayInit(){const t=$('pd-tray');if(!t)return;t.innerHTML=TYPES.map(x=>'<button class="pd-tool" data-t="'+esc(x[0])+'">'+symHtml(x[0],34)+'<span>'+esc(x[0])+'</span></button>').join('');t.querySelectorAll('.pd-tool').forEach(b=>b.addEventListener('pointerdown',e=>trayDown(e,b)))}
let TD=null;
function trayDown(e,b){if(!plan())return;TD={t:b.dataset.t,x0:e.clientX,y0:e.clientY,id:e.pointerId,drag:false,ghost:null,mouse:e.pointerType==='mouse',ta:getComputedStyle(b).touchAction||''};try{b.setPointerCapture(e.pointerId)}catch(_){}}
document.addEventListener('pointermove',e=>{if(!TD||e.pointerId!==TD.id)return;const dx=e.clientX-TD.x0,dy=e.clientY-TD.y0;
if(!TD.drag){const ta=TD.ta;if(TD.mouse||ta==='none'?Math.hypot(dx,dy)>8:ta.indexOf('pan-y')>=0?(Math.abs(dx)>12&&Math.abs(dx)>Math.abs(dy)):(dy<-12&&Math.abs(dy)>Math.abs(dx))){TD.drag=true;TD.ghost=document.createElement('div');TD.ghost.className='pd-ghost';TD.ghost.innerHTML=symHtml(TD.t,46);document.body.appendChild(TD.ghost)}else return}
TD.ghost.style.left=e.clientX+'px';TD.ghost.style.top=e.clientY+'px';e.preventDefault()},{passive:false});
document.addEventListener('pointerup',e=>{if(!TD||e.pointerId!==TD.id)return;const T=TD;TD=null;if(T.ghost)T.ghost.remove();
if(!T.drag){tool=tool===T.t?'':T.t;sel=null;trayMark();draw();side();return}
const r=box();if(e.clientX>=r.left&&e.clientX<=r.right&&e.clientY>=r.top&&e.clientY<=r.bottom){const q=toPlan(e),p=plan();place(T.t,q.x/p.w*100,q.y/p.h*100)}});
document.addEventListener('pointercancel',e=>{if(TD&&e.pointerId===TD.id){if(TD.ghost)TD.ghost.remove();TD=null}});
function place(t,x,y){const p=plan();if(!p||x<0||x>100||y<0||y>100)return;const before=snap(),id=newId();DEV.push({id:id,type:t,plan:p.fn,x:+x.toFixed(2),y:+y.toFixed(2),label:''});sel=id;commit(before);draw();side()}
window.PD_TAP=function(e,id,pc,q){if(measMode)return;const p=plan();if(id){sel=id;tool='';trayMark()}else if(tool&&EDIT){if(q.x<0||q.y<0||q.x>p.w||q.y>p.h)return;place(tool,pc.x,pc.y);return}else sel=null;draw();side()};
window.PD_DRAGSTART=function(g){if(!EDIT)return;g.drag=true;g.before=snap();sel=g.dev;tool='';trayMark()};
window.PD_DRAG=function(g,e){const d=DEV.find(x=>x.id===g.dev),p=plan();if(!d)return;const q=toPlan(e);d.x=+Math.max(0,Math.min(100,q.x/p.w*100)).toFixed(2);d.y=+Math.max(0,Math.min(100,q.y/p.h*100)).toFixed(2);draw()};
window.PD_HANDLES=function(W,H,K){const d=selDev(),p=plan();if(!EDIT||!d||!p||d.plan!==p.fn||!CAM_DEF[d.type]||!covOn()||scaleMode||measMode)return '';
const v=camView(d),c=col(d.type),cx=d.x*W/100,cy=d.y*H/100,R=v.range*W/100,out=[];
[['tip',v.dir,R,c,'#fff'],['edge',v.fov>=360?v.dir+90:v.dir+v.fov/2,R*.72,'#fff',c]].forEach(([k,a,rad,f,s])=>{const q=a*Math.PI/180,x=cx+rad*Math.sin(q),y=cy-rad*Math.cos(q);
out.push('<g data-h="'+k+'" class="pd-hd" style="cursor:grab"><circle cx="'+x+'" cy="'+y+'" r="'+(24*K)+'" fill="#fff" fill-opacity=".01"/><circle cx="'+x+'" cy="'+y+'" r="'+(11*K)+'" fill="'+f+'" stroke="'+s+'" stroke-width="'+(3.5*K)+'"/>'+(k==='edge'?'<circle cx="'+x+'" cy="'+y+'" r="'+(4*K)+'" fill="'+c+'"/>':'')+'</g>')});return out.join('')};
window.PD_CONE=function(g,e){const d=selDev(),p=plan();if(!d||!p||!EDIT)return;if(!g.before){g.before=snap();const v=camView(d);d.dir=v.dir;d.fov=v.fov}
const q=toPlan(e),dx=q.x-d.x*p.w/100,dy=q.y-d.y*p.h/100,a=((Math.atan2(dx,-dy)*180/Math.PI)+360)%360,dist=Math.hypot(dx,dy);
if(g.hd==='tip'){d.dir=Math.round(a);if(p.ppf){d.rft=Math.max(1,Math.min(2000,Math.round(dist/p.ppf)))}else d.range=Math.max(2,Math.min(100,+(dist/p.w*100).toFixed(1)))}
else{const df=Math.abs(((a-d.dir)%360+540)%360-180);d.fov=Math.max(10,Math.min(359,Math.round(df*2)))}draw()};
window.PD_CONEEND=function(g){if(g.before)commit(g.before);g.before=null;side()};
window.PD_DRAGEND=function(g){if(g.before)commit(g.before);g.before=null;g.drag=false;side()};
window.PD_HINT=function(){if(!EDIT||!plan())return '';if(tool)return 'Tap the plan to drop a '+tool+'. Tap '+tool+' again to stop.';if(!DEV.length)return 'Pick a device, then tap the plan';return ''};
function selDev(){return DEV.find(d=>d.id===sel)}
function dup(){const d=selDev();if(!d)return;const before=snap(),c=Object.assign({},d,{id:newId(),label:'',x:Math.min(100,d.x+3),y:Math.min(100,d.y+3),photo:'',installed:false,mac:'',ip:'',port:'',clabel:''});DEV.push(c);sel=c.id;commit(before);draw();side()}
function del(){const d=selDev();if(!d)return;const before=snap();DEV=DEV.filter(x=>x.id!==d.id);sel=null;commit(before);status('Deleted. Tap Undo to bring it back.');draw();side()}
function done(){sel=null;draw();side()}
function setField(k,v){const d=selDev();if(!d)return;v=v.trim();if((d[k]||'')===v)return;const before=snap();d[k]=v;commit(before)}
function camCh(kk,dv){const d=selDev();if(!d||!EDIT)return;const before=snap(),v=camView(d);
if(kk==='dir')d.dir=((v.dir+dv)%360+360)%360;else if(kk==='fov')d.fov=dv==='360'?360:Math.max(10,Math.min(360,v.fov+dv));else if(kk==='range')rangeStep(d,dv);
d.dir=d.dir??v.dir;d.fov=d.fov||v.fov;commit(before);draw();side()}
function rangeStep(d,dv){const v=camView(d);if(v.ft){d.rft=Math.max(5,Math.min(2000,v.ft+dv*5));return}d.range=Math.max(2,Math.min(100,+(v.range+dv*3).toFixed(1)))}
function rangeText(d){const v=camView(d);return v.ft?'range '+v.ft+' ft':'range '+v.range+'% of plan width (set the scale for feet)'}
window.PD_SIDE=function(){const c=$('pd-sel'),d=selDev();c.classList.toggle('hidden',!d);if(!d)return;const cam=!!CAM_DEF[d.type],v=camView(d);
const cmp=['N','NE','E','SE','S','SW','W','NW'][Math.round(v.dir/45)%8];
c.innerHTML='<div style="display:flex;align-items:center;gap:10px">'+symHtml(d.type,36)+'<h3 style="margin:0">'+esc(d.label||'New')+' &middot; '+esc(d.type)+'</h3></div>'+
(EDIT?'<div class="pd-row" style="margin:10px 0"><button class="pd-btn" onclick="dup()">Duplicate</button><button class="pd-btn red" onclick="del()">Delete</button><button class="pd-btn gold" onclick="done()">Done</button></div>':'')+
(cam?'<div class="pd-muted">Facing '+cmp+' ('+v.dir+'&deg;), view '+v.fov+'&deg;, '+esc(rangeText(d))+'</div>'+(EDIT?'<div class="pd-cam"><button class="pd-btn" onclick="camCh(&quot;dir&quot;,-15)">&#8634; Turn left</button><button class="pd-btn" onclick="camCh(&quot;dir&quot;,15)">Turn right &#8635;</button><button class="pd-btn" onclick="camCh(&quot;fov&quot;,&quot;360&quot;)">360&deg;</button><button class="pd-btn" onclick="camCh(&quot;fov&quot;,-10)">Narrower</button><button class="pd-btn" onclick="camCh(&quot;fov&quot;,10)">Wider</button><button class="pd-btn" onclick="camCh(&quot;range&quot;,-1)">Shorter</button><button class="pd-btn" onclick="camCh(&quot;range&quot;,1)">Longer</button></div>':''):'')+
(EDIT?'<label class="pd-l" for="pd-part">Part # or model (optional)</label><input class="pd-in" id="pd-part" maxlength="80" placeholder="e.g. Axis P3265-LVE" onchange="setField(&quot;part&quot;,this.value)"><label class="pd-l" for="pd-note">Notes (optional)</label><input class="pd-in" id="pd-note" maxlength="500" placeholder="e.g. 10 ft mount, ceiling" onchange="setField(&quot;note&quot;,this.value)">'
:'<div class="pd-muted" style="margin-top:6px">'+(d.part?'Part: '+esc(d.part)+'<br>':'')+(d.note?'Notes: '+esc(d.note):'')+'</div>');
if(EDIT){$('pd-part').value=d.part||'';$('pd-note').value=d.note||''}};
document.addEventListener('keydown',e=>{if(!EDIT||/INPUT|TEXTAREA/.test(document.activeElement.tagName))return;const m=e.ctrlKey||e.metaKey;
if(m&&e.key.toLowerCase()==='z'){e.preventDefault();e.shiftKey?redo():undo()}else if(m&&e.key.toLowerCase()==='y'){e.preventDefault();redo()}else if((e.key==='Delete'||e.key==='Backspace')&&sel){e.preventDefault();del()}else if(e.key==='Escape'){tool='';sel=null;trayMark();draw();side()}});
trayInit();undoBtns();setTimeout(trayTA,50);
(function(){const u=encodeURIComponent(JID);let h='';if(D.office)h+='<a class="pd-btn gold" href="/job/'+u+'/bom">Bill of materials</a>';if(EDIT)h+='<a class="pd-btn" href="/job/'+u+'/materials?from=bom">Request materials from plan</a>';
if(h)$('pd-legend').insertAdjacentHTML('afterend','<div class="pd-card" id="pd-bomcard"><h3>Materials</h3><div class="pd-row">'+h+'</div></div>')})();
(function(){const u=encodeURIComponent(JID);$('pd-legend').insertAdjacentHTML('afterend','<div class="pd-card" id="pd-pdfcard"><h3>Plan PDF</h3><div class="pd-muted" style="margin-bottom:8px">One page per floor with the title block, legend and bill of materials.</div><div class="pd-row"><a class="pd-btn gold" id="pd-pdf-client" target="_blank" href="/job/'+u+'/plan.pdf">Client PDF</a><a class="pd-btn" target="_blank" href="/job/'+u+'/plan.pdf?cov=0">Without coverage</a>'+(D.office?'<a class="pd-btn" target="_blank" href="/job/'+u+'/plan.pdf?copy=office">Office PDF with prices</a>':'')+'</div><div class="pd-muted" style="margin-top:6px">The client PDF never shows prices.</div></div>')})();
'''

# ---------------- Bill of materials from plan devices (office edits; subs never see prices) ----------------
def bom_load(row):
    try: b=json.loads(row['bom_json'] or '{}')
    except Exception: b={}
    b=b if isinstance(b,dict) else {}
    def clean(x):
        x=x if isinstance(x,dict) else {}
        q=x.get('qty'); c=x.get('cost')
        return {'part':str(x.get('part') or '').strip()[:80],'desc':str(x.get('desc') or '').strip()[:200],
                'qty':(round(fnum(q),2) if q not in (None,'') and fnum(q)>0 else None),'cost':(round(fnum(c),2) if c not in (None,'') and fnum(c)>=0 else None)}
    return {'defaults':{t:clean(v) for t,v in (b.get('defaults') or {}).items() if t in DEV_PREFIX} if isinstance(b.get('defaults'),dict) else {},
            'lines':{str(k)[:120]:clean(v) for k,v in (b.get('lines') or {}).items()} if isinstance(b.get('lines'),dict) else {},
            'extra':[e for e in (clean(x) for x in (b.get('extra') or [])[:60]) if e['part'] or e['desc']]}
def bom_build(conn, row, prices=False):
    b=bom_load(row); info=plan_info(conn, row['id']); names={p['fn']:p['name'] for p in info}
    groups={}; order=[t for t,_,_ in DEVICE_TYPES]
    for d in parse_devices(row['survey_devices']):
        if d['plan'] not in names: continue
        df=b['defaults'].get(d['type'],{}); part=d.get('part') or df.get('part') or ''
        key=d['type']+'|'+part.lower(); g=groups.setdefault(key,{'key':key,'type':d['type'],'auto_part':part,'count':0,'floors':{}})
        g['count']+=1; g['floors'][names[d['plan']]]=g['floors'].get(names[d['plan']],0)+1
    lines=[]
    for g in sorted(groups.values(), key=lambda g:(order.index(g['type']),g['auto_part'])):
        o=b['lines'].get(g['key'],{}); df=b['defaults'].get(g['type'],{})
        ln={'key':g['key'],'src':'plan','type':g['type'],'part':o.get('part') or g['auto_part'],'desc':o.get('desc') or df.get('desc') or g['type'],
            'auto_qty':g['count'],'qty':o.get('qty') or g['count'],'qty_set':o.get('qty') is not None,'floors':', '.join('%s: %d'%(n,g['floors'][n]) for n in [p['name'] for p in info] if n in g['floors'])}
        c=o.get('cost') if o.get('cost') is not None else df.get('cost')
        if prices: ln['cost']=c; ln['cost_set']=o.get('cost') is not None
        lines.append(ln)
    for i,e in enumerate(b['extra']):
        ln={'key':'x%d'%i,'src':'extra','type':'','part':e['part'],'desc':e['desc'] or e['part'],'qty':e['qty'] or 1,'auto_qty':None,'qty_set':True,'floors':''}
        if prices: ln['cost']=e['cost']
        lines.append(ln)
    if prices:
        for ln in lines:
            ln['sell']=mr_sell(ln['cost']) if ln.get('cost') is not None else None
            ln['cost_total']=round((ln['cost'] or 0)*ln['qty'],2); ln['sell_total']=round((ln['sell'] or 0)*ln['qty'],2)
    return b, lines
def bom_item_text(ln, prices):
    t='%s x %s'%(mr_qty(ln['qty']), ln['desc'] or ln['type'])
    if ln['part'] and ln['part'].lower()!=(ln['desc'] or '').lower(): t+=' (%s)'%ln['part']
    if prices and ln.get('sell') is not None: t+=' @ %s ea'%money(ln['sell'])
    return t[:200]

BOM_CSS='''<style>.bl{border:2px solid #e5e5e5;border-radius:12px;padding:10px;margin:8px 0;background:#fafaf5}.bl .row{display:flex;gap:8px;flex-wrap:wrap}.bl .row>div{flex:1 1 120px;min-width:0}
.bl h3{margin:0 0 4px;font-size:1.05rem;display:flex;align-items:center;gap:8px}.bl .fl{color:#666;font-size:.9rem;margin-bottom:4px}.sellv{font-weight:700;color:#2e7d32;padding:6px 0}
.tot{background:#f6f6f0;border-radius:12px;padding:10px 12px;margin:10px 0;font-size:1.05rem}.tot div{display:flex;justify-content:space-between;padding:2px 0}
details.dfl{border:1px solid #e5e5e5;border-radius:12px;padding:8px 10px;margin:10px 0}details.dfl summary{font-weight:700;cursor:pointer;min-height:44px;display:flex;align-items:center}
.chk{display:flex;align-items:center;gap:10px;font-weight:600;min-height:44px}.chk input{width:24px;height:24px}</style>'''

@app.route('/job/<job_id>/bom', methods=['GET','POST'])
def bom_page(job_id):
    if not is_office(): return redirect('/?job='+urllib.parse.quote(job_id))
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return redirect('/')
    msg=''
    if request.method=='POST':
        f=request.form; b=bom_load(row)
        lines={}
        for k,p,d,q,c in zip(f.getlist('key'),f.getlist('part'),f.getlist('desc'),f.getlist('qty'),f.getlist('cost')):
            o={'part':p.strip()[:80],'desc':d.strip()[:200],'qty':(fnum(q) if q.strip() and fnum(q)>0 else None),'cost':(round(fnum(c),2) if c.strip() and fnum(c)>=0 else None)}
            ty=k.split('|',1)[0]; auto_part=k.split('|',1)[1] if '|' in k else ''
            if o['part'].lower()==auto_part: o['part']=''
            if o['desc'] in (ty, (b['defaults'].get(ty) or {}).get('desc')): o['desc']=''
            if any(v not in (None,'') for v in o.values()): lines[k[:120]]=o
        extra=[]
        for p,d,q,c in zip(f.getlist('x_part'),f.getlist('x_desc'),f.getlist('x_qty'),f.getlist('x_cost')):
            if not (p.strip() or d.strip()): continue
            extra.append({'part':p.strip()[:80],'desc':d.strip()[:200],'qty':max(fnum(q,1),0.01) if q.strip() else 1,'cost':(round(fnum(c),2) if c.strip() and fnum(c)>=0 else None)})
        defaults={}
        for t_,p,d,c in zip(f.getlist('d_type'),f.getlist('d_part'),f.getlist('d_desc'),f.getlist('d_cost')):
            if t_ in DEV_PREFIX and (p.strip() or d.strip() or c.strip()):
                defaults[t_]={'part':p.strip()[:80],'desc':d.strip()[:200],'qty':None,'cost':(round(fnum(c),2) if c.strip() and fnum(c)>=0 else None)}
        conn.execute('UPDATE jobs SET bom_json=? WHERE id=?',(json.dumps({'defaults':defaults,'lines':lines,'extra':extra[:60]}),job_id)); conn.commit()
        row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone(); msg='Saved.'
        act=f.get('action')
        if act=='materials': conn.close(); return redirect('/job/%s/materials?from=bom'%urllib.parse.quote(job_id))
        if act=='estimate':
            e=load_estimate(row); _,ls=bom_build(conn,row,True)
            if e.get('signed_at'): msg='Saved. The estimate is already signed, so it was not changed. Clear the signature on the estimate first.'
            elif not ls: msg='Saved. No devices or lines to send yet.'
            else:
                pr=f.get('prices')=='1'; items=[bom_item_text(l,pr) for l in ls]
                e={k:e[k] for k in ('desc','items','price','note')}
                e['items']=items[:30]
                if not e['desc'].strip(): e['desc']='Supply and install the devices shown on the floor plan.'
                save_estimate(conn, job_id, e); conn.commit()
                tot=sum(l['sell_total'] for l in ls)
                msg='Sent %d lines to the estimate%s.%s Materials total at sell: %s. The estimate price was not changed.'%(min(len(items),30),' with prices' if pr else '',' Only the first 30 lines fit.' if len(items)>30 else '',money(tot))
    b,ls=bom_build(conn,row,True); e=load_estimate(row); conn.close()
    present=[t for t,_,_ in DEVICE_TYPES if any(l['type']==t for l in ls)]
    colors={n:c for n,p,c in DEVICE_TYPES}
    body=BOM_CSS+'<div class="muted">%s &middot; %s</div>'%(h(row['customer']),h(row['title']))
    body+='<div class="muted" style="margin:6px 0">Built from the devices on all floor plans. Part # and qty can be changed here. Costs and sell prices are office only. Sell = cost + 25%.</div>'
    body+='<form method="post" id="bomf"><h2>From the plans</h2>'
    if not any(l['src']=='plan' for l in ls): body+='<div class="card muted">No devices on the plans yet. Place devices in the Plan Designer and they show up here.</div>'
    for l in ls:
        if l['src']!='plan': continue
        body+=('<div class="bl"><input type="hidden" name="key" value="%s"><h3>%s %s <span style="margin-left:auto">%s</span></h3><div class="fl">%s</div>'
               '<div class="row"><div><label>Part #</label><input name="part" maxlength="80" value="%s" placeholder="Part # or model"></div><div><label>Qty (plan: %d)</label><input name="qty" type="number" min="0" step="any" inputmode="decimal" value="%s" placeholder="%d"></div></div>'
               '<label>Description</label><input name="desc" maxlength="200" value="%s">'
               '<div class="row"><div><label>Unit cost $</label><input name="cost" type="number" min="0" step="0.01" inputmode="decimal" value="%s" placeholder="%s"></div><div><label>Sell each</label><div class="sellv">%s</div></div></div></div>')%(
               h(l['key']),dev_icon(l['type'],colors[l['type']],True,26),h(l['type']),h('%s'%mr_qty(l['qty'])),h(l['floors']),h(l['part']),l['auto_qty'],h(mr_qty(l['qty'])) if l['qty_set'] else '',l['auto_qty'],
               h(l['desc']),('%g'%l['cost']) if l.get('cost_set') else '',('%g'%l['cost']) if l.get('cost') is not None else '0.00',money(l['sell']) if l['sell'] is not None else '<span class="muted">add a cost</span>')
    XL='''<div class="bl xl"><div class="row"><div><label>Part #</label><input name="x_part" maxlength="80" value="%s" placeholder="Part #"></div><div><label>Qty</label><input name="x_qty" type="number" min="0" step="any" inputmode="decimal" value="%s"></div></div>
<label>Description</label><input name="x_desc" maxlength="200" value="%s" placeholder="e.g. Cat6 cable, 1000 ft box"><div class="row"><div><label>Unit cost $</label><input name="x_cost" type="number" min="0" step="0.01" inputmode="decimal" value="%s"></div><div><label>Sell each</label><div class="sellv">%s</div></div></div>
<button type="button" class="big-btn gray" onclick="this.closest('.xl').remove()">Remove this line</button></div>'''
    body+='<h2>Extra lines</h2><div class="muted">Cable, mounts, boxes, labor items. Anything not on the plan.</div><div id="xls">'
    for l in ls:
        if l['src']=='extra': body+=XL%(h(l['part']),h(mr_qty(l['qty'])),h(l['desc']),('%g'%l['cost']) if l.get('cost') is not None else '',money(l['sell']) if l['sell'] is not None else '-')
    body+='</div><button type="button" class="big-btn gray" onclick="bomAdd()">+ Add a line</button>'
    body+='<details class="dfl"><summary>Default part # for each device type</summary><div class="muted">Used for any device that has no part # of its own.</div>'
    for t_,_,c_ in DEVICE_TYPES:
        dv=b['defaults'].get(t_,{})
        if t_ not in present and not dv: continue
        body+=('<div class="bl"><input type="hidden" name="d_type" value="%s"><h3>%s %s</h3><div class="row"><div><label>Default part #</label><input name="d_part" maxlength="80" value="%s"></div><div><label>Default cost $</label><input name="d_cost" type="number" min="0" step="0.01" inputmode="decimal" value="%s"></div></div>'
               '<label>Default description</label><input name="d_desc" maxlength="200" value="%s" placeholder="%s"></div>')%(h(t_),dev_icon(t_,c_,True,24),h(t_),h(dv.get('part','')),('%g'%dv['cost']) if dv.get('cost') is not None else '',h(dv.get('desc','')),h(t_))
    if not present and not b['defaults']: body+='<div class="muted">Place devices first.</div>'
    body+='</details>'
    ct=sum(l['cost_total'] for l in ls); st=sum(l['sell_total'] for l in ls); miss=sum(1 for l in ls if l.get('cost') is None)
    body+='<div class="tot"><div><span>Lines</span><b>%d</b></div><div><span>Cost total</span><b>%s</b></div><div><span>Sell total (+25%%)</span><b>%s</b></div>%s</div>'%(len(ls),money(ct),money(st),'<div class="muted" style="display:block">%d %s no cost yet.</div>'%(miss,'line has' if miss==1 else 'lines have') if miss else '')
    body+='<button class="big-btn" name="action" value="save">Save</button>'
    if e.get('signed_at'): body+='<div class="card muted">The estimate is signed. Clear the signature on the estimate page to send new lines.</div>'
    else: body+='<label class="chk"><input type="checkbox" name="prices" value="1"> Show sell price on each estimate line</label><button class="big-btn gold" name="action" value="estimate" onclick="return confirm(&quot;Replace the line items on the estimate with these lines?&quot;)">Send to estimate</button>'
    body+='<button class="big-btn gray" name="action" value="materials">Start a material request</button><div class="muted">The material request opens filled in with these parts and quantities, with no prices. Nothing is ordered or sent until you OK it.</div>'
    body+='<a class="big-btn gray" href="/job/%s/estimate">Open estimate</a></form>'%h(urllib.parse.quote(job_id))
    body+='<template id="xtpl">'+XL%('','1','','','-')+'</template><script>function bomAdd(){const t=document.getElementById("xtpl").content.cloneNode(true);document.getElementById("xls").appendChild(t);const L=document.querySelectorAll("#xls .xl");L[L.length-1].querySelector("[name=x_desc]").focus()}</script>'
    return ops_page('Bill of materials', body, back='/?job='+urllib.parse.quote(job_id), msg=msg)

# ---------------- Branded plan PDF and as-built PDF. Client copies never have prices. ----------------
PDF_GOLD=(0.643,0.624,0.165)
def pdf_sym_g(t, col, cx, cy, s, solid=True):
    sym=DEV_SYM.get(t,'<circle cx="12" cy="12" r="3"/>'); bg,fg=(col,'#ffffff') if solid else ('#ffffff',col)
    return ('<g transform="translate(%.2f %.2f) scale(%.4f)"><circle cx="12" cy="12" r="11" fill="%s" stroke="%s" stroke-width="1.6"%s/>'
            '<g transform="translate(12 12) scale(.66) translate(-12 -12)" fill="none" stroke="%s" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">%s</g></g>')%(
            cx-s/2,cy-s/2,s/24,bg,'#ffffff' if solid else col,'' if solid else ' stroke-dasharray="3 2"',fg,sym)
def pdf_overlay_svg(fn, devs, W, H, ppf, cones, ab=False):
    colors={n:c for n,p,c in DEVICE_TYPES}; ds=[d for d in devs if d.get('plan')==fn]; s=max(W,H)*0.028; out=[]
    if cones:
        for d in ds:
            if d['type'] not in CAM_DEF: continue
            v=cam_view(d, ppf, W); col=colors[d['type']]
            out.append('<path d="%s" fill="%s" fill-opacity="%s" stroke="%s" stroke-opacity="0.7" stroke-width="%.1f"%s/>'%(cone_path(d['x']*W/100,d['y']*H/100,v['range']*W/100,v['dir'],v['fov']),col,'0.06' if v['fov']>=360 else '0.12',col,max(W,H)*0.0014,
                       ' stroke-dasharray="%.1f %.1f"'%(W*.008,W*.006) if d['type']=='PTZ Cam' else ''))
    for d in ds:
        out.append(pdf_sym_g(d['type'],colors[d['type']],d['x']*W/100,d['y']*H/100,s,(not ab) or d.get('installed')))
    for d in ds:
        cx,cy=d['x']*W/100,d['y']*H/100; lf=d['x']>82; lab=_esc(d.get('label') or '')
        x=cx-s*.62 if lf else cx+s*.62; anc='end' if lf else 'start'
        for paint in ('fill="#ffffff" stroke="#ffffff" stroke-width="%.1f"'%(s*.22), 'fill="#111111"'):
            out.append('<text x="%.1f" y="%.1f" font-size="%.1f" font-weight="bold" font-family="Helvetica, Arial, sans-serif" text-anchor="%s" %s>%s</text>'%(x,cy+s*.22,s*.62,anc,paint,lab))
    return '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">%s</svg>'%(W,H,W,H,''.join(out))
def pdf_plan_image(pg, r, path, keep=False):
    import pymupdf
    try:
        pix=pymupdf.Pixmap(path); sh=0
        while max(pix.width,pix.height)/(2**sh)>2600: sh+=1
        if sh: pix.shrink(sh)
        if pix.alpha: pix=pymupdf.Pixmap(pix,0)
        pg.insert_image(r, pixmap=pix, keep_proportion=keep); return
    except Exception:
        pass
    from PIL import Image  # WEBP / GIF images that MuPDF cannot read
    import io
    im=Image.open(path); im.thumbnail((2600,2600))
    if im.mode!='RGB':
        im=im.convert('RGBA'); bg=Image.new('RGB',im.size,(255,255,255)); bg.paste(im,mask=im.split()[3]); im=bg
    b=io.BytesIO(); im.save(b,'PNG'); pg.insert_image(r, stream=b.getvalue(), keep_proportion=keep)
def pdf_svg_doc(svg):
    import pymupdf
    d=pymupdf.open(stream=svg.encode('utf-8'), filetype='svg'); b=d.convert_to_pdf(); d.close(); return pymupdf.open('pdf', b)
def pdf_nice_ft(pt_per_ft):
    for v in (1,2,5,10,20,25,50,100,200,250,500,1000):
        if v*pt_per_ft>=70: return v
    return 1000
def ab_load(row):
    try: a=json.loads(row['asbuilt_json'] or '{}')
    except Exception: a={}
    return a if isinstance(a,dict) else {}
def job_photos(conn, job_id):
    return [dict(p) for p in conn.execute("SELECT filename,photo_type,uploaded_at FROM photos WHERE job_id=? AND photo_type IN ('before','during','after','survey') ORDER BY id",(job_id,)).fetchall()]

class FoPdf:
    PW,PH,M=792,612,24
    def __init__(self, conn, row, prices, who):
        import pymupdf
        self.pm=pymupdf; self.conn=conn; self.row=row; self.prices=prices; self.who=who
        self.doc=pymupdf.open(); self.titles=[]; self.icons={}
        self.info=plan_info(conn,row['id']); self.devs=parse_devices(row['survey_devices']); self.colors={n:c for n,p,c in DEVICE_TYPES}
        self.date=today_local().strftime('%b %-d, %Y'); self.copy='Office copy (with prices)' if prices else 'Client copy'
        self.logo=os.path.join(os.path.dirname(os.path.abspath(__file__)),'static','logo-pdf.png')
    def page(self, title):
        pg=self.doc.new_page(width=self.PW,height=self.PH); self.titles.append(title); return pg
    def html(self, pg, rect, s): pg.insert_htmlbox(self.pm.Rect(*rect), s)
    def icon(self, t, solid=True):
        k=(t,solid)
        if k not in self.icons: self.icons[k]=pdf_svg_doc('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">%s</svg>'%pdf_sym_g(t,self.colors[t],12,12,24,solid))
        return self.icons[k]
    def finish(self, footnote):
        pm=self.pm; row=self.row; n=len(self.doc); PW,PH,M=self.PW,self.PH,self.M
        for i,pg in enumerate(self.doc):
            pg.draw_rect(pm.Rect(0,0,PW,4), color=None, fill=PDF_GOLD)
            if os.path.exists(self.logo):
                try: pg.insert_image(pm.Rect(M,12,M+110,44), filename=self.logo, keep_proportion=True)
                except Exception: pass
            self.html(pg,(M+120,14,PW-160,46),'<div style="font-family:sans-serif;font-size:15px;font-weight:bold;color:#111">%s</div><div style="font-family:sans-serif;font-size:9px;color:#555">%s &middot; %s</div>'%(h(self.titles[i]),h(row['customer'] or ''),h(row['title'] or '')))
            self.html(pg,(PW-160,16,PW-M,46),'<div style="font-family:sans-serif;font-size:9px;color:#555;text-align:right">Page %d of %d<br>%s</div>'%(i+1,n,self.copy))
            pg.draw_line((M,52),(PW-M,52), color=(0.88,0.87,0.82), width=0.8)
            self.html(pg,(M,PH-20,PW-M,PH-6),'<div style="font-family:sans-serif;font-size:7.5px;color:#777">True Sense Security &middot; Job %s &middot; Prepared by %s on %s &middot; %s</div>'%(h(row['id']),h(self.who),self.date,h(footnote)))
        out=self.doc.tobytes(garbage=3, deflate=True); self.doc.close(); return out
    def title_block(self, pg, x0, y0, x1, rows):
        html='<table style="font-family:sans-serif;font-size:8.5px;width:100%;border-collapse:collapse">'+''.join('<tr><td style="color:#666;padding:2px 4px 2px 0;vertical-align:top;width:62px">%s</td><td style="font-weight:bold;padding:2px 0">%s</td></tr>'%(h(k),h(v)) for k,v in rows)+'</table>'
        hgt=16.5*len(rows)+12; pg.draw_rect(self.pm.Rect(x0,y0,x1,y0+hgt), color=PDF_GOLD, width=1.2)
        self.html(pg,(x0+6,y0+5,x1-6,y0+hgt-2),html); return y0+hgt
    def base_rows(self, p, extra=()):
        r=self.row
        return [('Job ID',r['id']),('Client',r['customer'] or '-'),('Site',r['address'] or '-'),('Date',self.date),('Prepared by',self.who)]+list(extra)+([('Floor',p['name']),('Scale',('Set (%g ft reference)'%p['scale']['ft']) if p.get('ppf') else 'Not to scale')] if p else [])
    def plan_pages(self, cones, ab=False, prefix='Floor Plan'):
        pm=self.pm; PW,PH,M=self.PW,self.PH,self.M; devs=self.devs; info=self.info
        for p in info:
            pg=self.page('%s: %s'%(prefix,p['name']))
            area=pm.Rect(M,60,PW-M-190,PH-26); W,H=p['w'],p['h']; k=min(area.width/W,area.height/H)
            r=pm.Rect(area.x0,area.y0,area.x0+W*k,area.y0+H*k)
            try: pdf_plan_image(pg, r, os.path.join(app.config['UPLOAD_FOLDER'],secure_filename(p['fn'])))
            except Exception as ex:
                app.logger.warning('pdf plan image: %s', ex); self.html(pg,tuple(r),'<div style="font-family:sans-serif;color:#b00020">Plan image could not be read.</div>')
            pg.draw_rect(r, color=(0.85,0.85,0.8), width=0.6)
            try: pg.show_pdf_page(r, pdf_svg_doc(pdf_overlay_svg(p['fn'],devs,W,H,p.get('ppf'),cones,ab)), 0)
            except Exception as ex: app.logger.warning('pdf overlay: %s', ex)
            if p.get('ppf'):
                ptft=p['ppf']*k; ft=pdf_nice_ft(ptft); L=ft*ptft; bx,by=r.x0+8,r.y1-16
                pg.draw_rect(pm.Rect(bx-4,by-10,bx+L+50,by+8), color=None, fill=(1,1,1), fill_opacity=0.9)
                for i in range(4): pg.draw_rect(pm.Rect(bx+i*L/4,by-3,bx+(i+1)*L/4,by+3), color=(0,0,0), fill=(0,0,0) if i%2==0 else (1,1,1), width=0.6)
                pg.insert_text((bx+L+5,by+3), '%g ft'%ft, fontsize=8, fontname='helv'); pg.insert_text((bx,by-5), '0', fontsize=6, fontname='helv')
            x0=PW-M-180; mine=[d for d in devs if d['plan']==p['fn']]
            extra=[('Installed','%d of %d devices'%(sum(1 for d in mine if d.get('installed')),len(mine)))] if ab else []
            y=self.title_block(pg,x0,60,PW-M,self.base_rows(p,extra))+10
            here={}; allc={}
            for d in mine: here[d['type']]=here.get(d['type'],0)+1
            for d in devs:
                if any(d['plan']==q['fn'] for q in info): allc[d['type']]=allc.get(d['type'],0)+1
            self.html(pg,(x0,y,PW-M,y+16),'<div style="font-family:sans-serif;font-size:10px;font-weight:bold">Legend%s</div>'%(' (this floor / all floors)' if len(info)>1 else '')); y+=18
            if not here: self.html(pg,(x0,y,PW-M,y+14),'<div style="font-family:sans-serif;font-size:8.5px;color:#666">No devices on this floor.</div>'); y+=16
            for t,_,_ in DEVICE_TYPES:
                if t not in here or y>PH-80: continue
                pg.show_pdf_page(pm.Rect(x0,y,x0+14,y+14), self.icon(t), 0)
                self.html(pg,(x0+20,y+1,PW-M,y+15),'<table style="font-family:sans-serif;font-size:8.5px;width:100%%;border-collapse:collapse"><tr><td>%s</td><td style="text-align:right;font-weight:bold">%d%s</td></tr></table>'%(h(t),here[t],(' / %d'%allc[t]) if len(info)>1 else '')); y+=17
            if ab and mine and any(not d.get('installed') for d in mine) and y<PH-60:
                pg.show_pdf_page(pm.Rect(x0,y+2,x0+14,y+16), self.icon('Dome Cam',False), 0)
                self.html(pg,(x0+20,y+3,PW-M,y+30),'<div style="font-family:sans-serif;font-size:7.5px;color:#666">Outlined icons are planned but not marked installed.</div>'); y+=26
            if cones and any(t in CAM_DEF for t in here) and y<PH-50:
                self.html(pg,(x0,y+2,PW-M,y+30),'<div style="font-family:sans-serif;font-size:7.5px;color:#666">Shaded areas show approximate camera coverage%s.</div>'%(' in real feet' if p.get('ppf') else ''))
        if not info:
            pg=self.page(prefix); self.title_block(pg,self.M,70,self.M+260,self.base_rows(None))
            self.html(pg,(self.M,260,self.PW-self.M,300),'<div style="font-family:sans-serif;font-size:14px;color:#666">No floor plans on this job yet.</div>')
    def table_pages(self, title, cols, rows, per=22, empty='Nothing to show.', tail=''):
        chunks=[rows[i:i+per] for i in range(0,len(rows),per)] or [[]]
        for ci,ch in enumerate(chunks):
            pg=self.page(title+(' (continued)' if ci else ''))
            html='<table style="font-family:sans-serif;width:100%;border-collapse:collapse"><tr>'+''.join('<th style="text-align:%s;padding:4px 6px;border-bottom:1.5px solid #a49f2a;font-size:9px">%s</th>'%c for c in cols)+'</tr>'
            for rw in ch: html+='<tr>'+''.join('<td style="padding:3px 6px;border-bottom:0.5px solid #ddd;font-size:8.5px;text-align:%s">%s</td>'%(cols[i][0],v) for i,v in enumerate(rw))+'</tr>'
            if not ch: html+='<tr><td colspan="%d" style="padding:10px 6px;font-size:10px;color:#666">%s</td></tr>'%(len(cols),h(empty))
            if ci==len(chunks)-1 and tail: html+=tail
            self.html(pg,(self.M,62,self.PW-self.M,self.PH-30),html+'</table>')
        return pg
    def bom_pages(self):
        _,bom=bom_build(self.conn,self.row,True); pr=self.prices
        cols=[('right','Qty'),('left','Part #'),('left','Description'),('left','Floors')]+([('right','Unit price'),('right','Line total')] if pr else [])
        rows=[]
        for l in bom:
            rw=[h(mr_qty(l['qty'])),h(l['part'] or '-'),h(l['desc'] or l['type']),h(l['floors'] or 'Extra')]
            if pr: rw+=[money(l['sell']) if l.get('sell') is not None else '-',money(l['sell_total']) if l.get('sell') is not None else '-']
            rows.append(rw)
        tail='<tr><td colspan="5" style="padding:6px;text-align:right;font-size:9.5px;font-weight:bold">Materials total</td><td style="padding:6px;text-align:right;font-size:9.5px;font-weight:bold">%s</td></tr>'%money(sum(l['sell_total'] for l in bom)) if pr and bom else ''
        pg=self.table_pages('Bill of Materials',cols,rows,22,'No devices placed yet.',tail)
        tot=sum(l['qty'] for l in bom if l['src']=='plan')
        self.html(pg,(self.M,self.PH-48,self.PW-self.M,self.PH-26),'<div style="font-family:sans-serif;font-size:8px;color:#666">%d devices on %d floor plan%s.%s</div>'%(tot,len(self.info),'' if len(self.info)==1 else 's',' Prices do not include labor or tax.' if pr else ''))

def plan_pdf_bytes(conn, row, prices, cones, who):
    b=FoPdf(conn,row,prices,who); b.plan_pages(cones); b.bom_pages()
    return b.finish('Device locations and coverage are approximate.')

def asbuilt_pdf_bytes(conn, row, prices, who, cones=True):
    b=FoPdf(conn,row,prices,who); pm=b.pm; M,PW,PH=b.M,b.PW,b.PH; a=ab_load(row); devs=b.devs; names={p['fn']:p['name'] for p in b.info}
    mine=[d for d in devs if d['plan'] in names]; inst=sum(1 for d in mine if d.get('installed'))
    done=utc_day(row['completed_at'])
    pg=b.page('As-Built Record')
    y=b.title_block(pg,M,64,M+300,[('Job ID',row['id']),('Client',row['customer'] or '-'),('Site',row['address'] or '-'),('Job type',row['job_type'] or '-'),('Tech',nice_name(row['tech']) or '-'),('Finished',done or 'Not finished yet'),('Prepared',b.date+' by '+who)])
    summ='<div style="font-family:sans-serif;font-size:10px"><b>Devices:</b> %d placed, %d marked installed<br><b>Floor plans:</b> %d<br><b>Photos:</b> %d</div>'%(len(mine),inst,len(b.info),len(job_photos(conn,row['id'])))
    b.html(pg,(M+320,64,PW-M,140),summ)
    scope=(row['scope'] or '').strip(); notes=notes_log(conn,row).strip()
    punch=parse_ticks(row['punch_list'])
    html='<div style="font-family:sans-serif;font-size:9px;color:#111">'
    if scope: html+='<div style="font-size:10.5px;font-weight:bold;margin:2px 0">Scope of work</div><div style="white-space:pre-wrap">%s</div>'%h(scope[:1500])
    html+='<div style="font-size:10.5px;font-weight:bold;margin:8px 0 2px">Tech notes</div><div style="white-space:pre-wrap">%s</div>'%(h(notes[:3500]) if notes else '<span style="color:#777">No notes.</span>')
    if punch: html+='<div style="font-size:10.5px;font-weight:bold;margin:8px 0 2px">Punch list</div>'+''.join('<div>%s %s</div>'%('[x]' if t['done'] else '[ ]',h(t['text'])) for t in punch[:30])
    b.html(pg,(M,max(y,140)+12,PW-M,PH-30),html+'</div>')
    b.plan_pages(cones, ab=True, prefix='As-Built')
    cols=[('left','Label'),('left','Type'),('left','Floor'),('left','Part #'),('left','Cable label'),('left','Notes')]+([('left','IP'),('left','MAC')] if prices else [])+[('center','Installed')]
    rows=[]
    order=[t for t,_,_ in DEVICE_TYPES]
    for d in sorted(mine,key=lambda d:(order.index(d['type']),d.get('label') or '')):
        rw=[h(d.get('label') or '-'),h(d['type']),h(names.get(d['plan'],'')),h(d.get('part') or '-'),h(d.get('clabel') or '-'),h((d.get('note') or '')[:80] or '-')]
        if prices: rw+=[h(d.get('ip') or '-'),h(d.get('mac') or '-')]
        rows.append(rw+['Yes' if d.get('installed') else 'No'])
    b.table_pages('Device Schedule',cols,rows,24,'No devices on the plans.')
    ph=job_photos(conn,row['id']); lab={'before':'Before','during':'During','after':'After','survey':'Survey'}
    for i in range(0,len(ph),6):
        pg=b.page('Photos'+(' (continued)' if i else ''))
        for j,p in enumerate(ph[i:i+6]):
            cx=M+(j%3)*((PW-2*M)/3); cy=62+(j//3)*262; r=pm.Rect(cx+4,cy,cx+(PW-2*M)/3-4,cy+232)
            pg.draw_rect(r, color=(0.9,0.9,0.86), width=0.5)
            try: pdf_plan_image(pg, pm.Rect(r.x0+3,r.y0+3,r.x1-3,r.y1-3), os.path.join(app.config['UPLOAD_FOLDER'],secure_filename(p['filename'])), keep=True)
            except Exception: b.html(pg,(r.x0+8,r.y0+100,r.x1-8,r.y0+130),'<div style="font-family:sans-serif;font-size:9px;color:#777;text-align:center">Photo could not be shown</div>')
            b.html(pg,(r.x0,r.y1+3,r.x1,r.y1+18),'<div style="font-family:sans-serif;font-size:8.5px;color:#444">%s photo %s</div>'%(lab.get(p['photo_type'],p['photo_type']),h(mr_day(p['uploaded_at']))))
    b.bom_pages()
    pg=b.page('Client Sign-Off')
    sig=a.get('sig') or ''; src='asbuilt'
    if not sig and str(row['signature'] or '').startswith('data:image/png;base64,'): sig=row['signature']; src='completion'
    txt='<div style="font-family:sans-serif;font-size:10.5px;line-height:1.5">By signing, the client confirms the work and devices shown in this as-built record were reviewed.</div>'
    b.html(pg,(M,66,PW-M,110),txt)
    box=pm.Rect(M,120,M+360,260); pg.draw_rect(box, color=PDF_GOLD, width=1.2)
    if sig.startswith('data:image/png;base64,'):
        import base64
        try: pg.insert_image(pm.Rect(box.x0+8,box.y0+8,box.x1-8,box.y1-8), stream=base64.b64decode(sig.split(',',1)[1]), keep_proportion=True)
        except Exception: pass
        who_s=a.get('name') if src=='asbuilt' else (row['customer'] or '')
        when=a.get('signed_at') if src=='asbuilt' else utc_day(row['completed_at'])
        b.html(pg,(M,268,PW-M,330),'<div style="font-family:sans-serif;font-size:10px"><b>Signed by:</b> %s<br><b>Date:</b> %s%s</div>'%(h(who_s or '-'),h(when or '-'),'<br><span style="color:#666">Signature captured at job completion.</span>' if src=='completion' else ''))
    else:
        b.html(pg,(box.x0+10,box.y0+60,box.x1-10,box.y0+90),'<div style="font-family:sans-serif;font-size:11px;color:#888;text-align:center">Not signed yet</div>')
        b.html(pg,(M,268,PW-M,320),'<div style="font-family:sans-serif;font-size:10px">Name: ______________________________ &nbsp;&nbsp; Date: ______________</div>')
    return b.finish('As-built record. Device locations are approximate.')

def asbuilt_archive(conn, row, who):
    data=asbuilt_pdf_bytes(conn,row,False,who)
    fn=secure_filename('%s_blueprint_asbuilt_%s.pdf'%(row['id'],uuid.uuid4().hex[:8]))
    with open(os.path.join(app.config['UPLOAD_FOLDER'],fn),'wb') as f: f.write(data)
    name='As-built %s %s.pdf'%(row['id'],today_local().isoformat())
    conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at,orig_name) VALUES (?,?,?,?,?)',(row['id'],fn,'blueprint',datetime.utcnow().isoformat(),name))
    conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(row['id'],me(),'Saved the as-built PDF to the job files (%s)'%name,now_local_str()))
    conn.commit(); return fn, name

def pdf_response(data, fn):
    from flask import Response
    return Response(data, mimetype='application/pdf', headers={'Content-Disposition':'inline; filename="%s"'%fn,'Cache-Control':'no-store'})

@app.route('/job/<job_id>/plan.pdf')
def plan_pdf(job_id):
    conn=get_db(); row=page_job(conn, job_id)
    if not row: conn.close(); return redirect('/')
    prices=is_office() and request.args.get('copy')=='office'
    try: data=plan_pdf_bytes(conn,row,prices,request.args.get('cov','1')!='0',nice_name(me()) or me())
    finally: conn.close()
    return pdf_response(data,'%s-plan%s.pdf'%(secure_filename(row['id']) or 'job','-office' if prices else ''))

@app.route('/job/<job_id>/asbuilt.pdf')
def asbuilt_pdf(job_id):
    conn=get_db(); row=page_job(conn, job_id)
    if not row: conn.close(); return redirect('/')
    prices=is_office() and request.args.get('copy')=='office'
    try: data=asbuilt_pdf_bytes(conn,row,prices,nice_name(me()) or me(),request.args.get('cov','1')!='0')
    finally: conn.close()
    return pdf_response(data,'%s-asbuilt%s.pdf'%(secure_filename(row['id']) or 'job','-office' if prices else ''))

AB_CSS='''<style>.sigbox{border:2px dashed #a49f2a;border-radius:12px;background:#fff;touch-action:none;width:100%;height:200px;display:block}.abst{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:8px 0}
.abst div{border:2px solid #e5e5e5;border-radius:12px;padding:10px 4px;text-align:center;font-weight:600;font-size:.9rem}.abst b{display:block;font-size:1.4rem}.sigimg{max-width:100%;border:1px solid #ddd;border-radius:10px;background:#fff}</style>'''
@app.route('/job/<job_id>/asbuilt', methods=['GET','POST'])
def asbuilt_page(job_id):
    conn=get_db(); row=page_job(conn, job_id)
    if not row: conn.close(); return redirect('/')
    if row['job_type']=='Office Task': conn.close(); return redirect('/?job='+urllib.parse.quote(job_id))
    msg=''; a=ab_load(row); q=urllib.parse.quote(job_id)
    if request.method=='POST':
        act=request.form.get('action')
        if act=='sign':
            sig=request.form.get('sig') or ''; name=(request.form.get('name') or '').strip()[:100]
            if not sig.startswith('data:image/png;base64,') or len(sig)>400000: msg='Please have the client sign in the box first.'
            elif not name: msg='Type the client name first.'
            else:
                a={'sig':sig,'name':name,'signed_at':now_local_str(),'by':me()}
                conn.execute('UPDATE jobs SET asbuilt_json=? WHERE id=?',(json.dumps(a),job_id))
                conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,me(),'%s signed the as-built'%name,now_local_str())); conn.commit()
                row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
                fn,nm=asbuilt_archive(conn,row,nice_name(me()) or me()); msg='Signed and saved to the job files: %s'%nm
        elif act=='clear' and a.get('sig'):
            conn.execute('UPDATE jobs SET asbuilt_json=? WHERE id=?',(json.dumps({}),job_id)); conn.commit(); a={}; msg='Signature removed. You can sign again.'
            row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        elif act=='archive':
            fn,nm=asbuilt_archive(conn,row,nice_name(me()) or me()); msg='Saved to the job files: %s'%nm
    devs=[d for d in parse_devices(row['survey_devices']) if d['plan'] in set(plan_files(conn,job_id))]
    files=[b for b in blueprints(conn,job_id) if b['name'].startswith('As-built')]
    nph=len(job_photos(conn,job_id)); conn.close()
    body=AB_CSS+'<div class="muted">%s &middot; %s</div>'%(h(row['customer']),h(row['title']))
    body+='<div class="abst"><div>Devices<b>%d</b></div><div>Installed<b>%d</b></div><div>Photos<b>%d</b></div></div>'%(len(devs),sum(1 for d in devs if d.get('installed')),nph)
    body+='<a class="big-btn" target="_blank" href="/job/%s/asbuilt.pdf">Preview as-built PDF</a>'%h(q)
    if is_office(): body+='<a class="big-btn gray" target="_blank" href="/job/%s/asbuilt.pdf?copy=office">Office copy (prices, IP and MAC)</a>'%h(q)
    body+='<h2>Client sign-off</h2>'
    if a.get('sig'):
        body+='<div class="card"><img class="sigimg" src="%s" alt="client signature"><div><b>%s</b> signed on %s</div></div>'%(h(a['sig']),h(a.get('name')),h(a.get('signed_at')))
        body+='<form method="post" onsubmit="return confirm(&quot;Remove this signature?&quot;)"><input type="hidden" name="action" value="clear"><button class="big-btn gray">Sign again</button></form>'
    else:
        body+='''<form method="post" id="sigf" onsubmit="return sigSubmit()"><input type="hidden" name="action" value="sign"><input type="hidden" name="sig" id="sig">
<label>Client name</label><input name="name" maxlength="100" autocomplete="off" placeholder="Name of the person signing">
<label>Client signature</label><canvas class="sigbox" id="sigc" aria-label="Signature pad"></canvas>
<button type="button" class="big-btn gray" onclick="sigClear()">Clear</button><button class="big-btn">Sign and save as-built</button>
<div class="muted">Saves a PDF copy (no prices) to the job files. Nothing is sent to the client.</div></form>
<script>const C=document.getElementById('sigc'),X=C.getContext('2d');let drawn=false,down=false;
function sz(){const r=C.getBoundingClientRect(),d=window.devicePixelRatio||1;C.width=r.width*d;C.height=r.height*d;X.setTransform(d,0,0,d,0,0);X.lineWidth=2.6;X.lineCap='round';X.lineJoin='round';X.strokeStyle='#111';drawn=false}
sz();window.addEventListener('resize',()=>{if(!drawn)sz()});
function pt(e){const r=C.getBoundingClientRect();return [e.clientX-r.left,e.clientY-r.top]}
C.addEventListener('pointerdown',e=>{e.preventDefault();down=true;C.setPointerCapture(e.pointerId);const [x,y]=pt(e);X.beginPath();X.moveTo(x,y);X.lineTo(x+0.1,y+0.1);X.stroke();drawn=true});
C.addEventListener('pointermove',e=>{if(!down)return;e.preventDefault();const [x,y]=pt(e);X.lineTo(x,y);X.stroke()});
['pointerup','pointercancel'].forEach(n=>C.addEventListener(n,()=>{down=false}));
function sigClear(){X.clearRect(0,0,C.width,C.height);drawn=false}
function sigSubmit(){if(!drawn){alert('Please have the client sign in the box.');return false}const o=document.createElement('canvas');o.width=C.width;o.height=C.height;const ox=o.getContext('2d');ox.fillStyle='#fff';ox.fillRect(0,0,o.width,o.height);ox.drawImage(C,0,0);document.getElementById('sig').value=o.toDataURL('image/png');return true}</script>'''
    body+='<form method="post"><input type="hidden" name="action" value="archive"><button class="big-btn gray">Save as-built to job files now</button></form>'
    if files: body+='<h2>Saved as-builts</h2>'+''.join('<a class="big-btn gray" target="_blank" href="/uploads/%s">%s</a>'%(h(urllib.parse.quote(f['filename'])),h(f['name'])) for f in reversed(files))
    return ops_page('As-built', body, back='/?job='+q, msg=msg)

# ---------------- View-only share links (office creates and revokes; no prices; nothing is sent) ----------------
import secrets
SHARE_DAYS=(7,30,90)
def sl_init():
    conn=get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS share_links (token TEXT PRIMARY KEY, job_id TEXT, kind TEXT, created_by TEXT, created_at TEXT, expires_at TEXT, revoked_at TEXT, revoked_by TEXT, views INTEGER DEFAULT 0, last_view TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_share_job ON share_links(job_id)'); conn.commit(); conn.close()
sl_init()
def sl_state(r):
    if r['revoked_at']: return 'revoked'
    try:
        if datetime.utcnow()>=datetime.fromisoformat(r['expires_at']): return 'expired'
    except Exception: return 'expired'
    return 'active'
def sl_get(token):
    if not token or len(token)>80: return None, None, None
    conn=get_db(); r=conn.execute('SELECT * FROM share_links WHERE token=?',(token,)).fetchone()
    if not r or sl_state(r)!='active': conn.close(); return None, None, None
    row=conn.execute('SELECT * FROM jobs WHERE id=?',(r['job_id'],)).fetchone()
    if not row: conn.close(); return None, None, None
    return conn, r, row
def sl_when(iso):
    try:
        d=datetime.fromisoformat(iso)
        try:
            from zoneinfo import ZoneInfo
            d=d.replace(tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo('America/Chicago'))
        except Exception: d=d-timedelta(hours=6)
        return d.strftime('%b %-d, %Y %-I:%M %p CT')
    except Exception: return iso or ''
SL_KIND={'plan':'Floor plan','asbuilt':'As-built'}

@app.route('/job/<job_id>/share', methods=['GET','POST'])
def share_page(job_id):
    if not is_office(): return redirect('/')
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return redirect('/')
    q=urllib.parse.quote(job_id); msg=''; new=''
    if request.method=='POST':
        act=request.form.get('action')
        if act=='create':
            kind=request.form.get('kind') if request.form.get('kind') in SL_KIND else 'plan'
            try: days=int(request.form.get('days') or 30)
            except Exception: days=30
            days=days if days in SHARE_DAYS else 30
            tok=secrets.token_urlsafe(24); now=datetime.utcnow()
            conn.execute('INSERT INTO share_links (token,job_id,kind,created_by,created_at,expires_at) VALUES (?,?,?,?,?,?)',(tok,job_id,kind,me(),now.isoformat(timespec='seconds'),(now+timedelta(days=days)).isoformat(timespec='seconds')))
            conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,me(),'Made a view-only %s link (expires in %d days)'%(SL_KIND[kind].lower(),days),now_local_str()))
            conn.commit(); new=tok; msg='Link made. Copy it and send it yourself. Nothing was sent.'
        elif act=='revoke':
            tok=request.form.get('token') or ''
            n=conn.execute('UPDATE share_links SET revoked_at=?,revoked_by=? WHERE token=? AND job_id=? AND revoked_at IS NULL',(datetime.utcnow().isoformat(timespec='seconds'),me(),tok,job_id)).rowcount
            if n: conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,me(),'Turned off a view-only link',now_local_str()))
            conn.commit(); msg='Link turned off. It no longer opens.' if n else ''
    links=conn.execute('SELECT * FROM share_links WHERE job_id=? ORDER BY created_at DESC',(job_id,)).fetchall(); conn.close()
    base=request.host_url.rstrip('/')
    body='''<style>.sl{border:2px solid #e5e5e5;border-radius:12px;padding:12px;margin:10px 0}.sl.on{border-color:#a49f2a}.sl.new{box-shadow:0 0 0 3px #e9e7c4}.sl .u{font-family:monospace;font-size:.85rem;background:#f6f6f2;border-radius:8px;padding:10px;margin:8px 0;word-break:break-all}
.pill{display:inline-block;border-radius:999px;padding:3px 10px;font-weight:700;font-size:.85rem}.pill.active{background:#e8f5e9;color:#2e7d32}.pill.expired,.pill.revoked{background:#eee;color:#666}
.seg2{display:flex;gap:8px}.seg2 label{flex:1;margin:0}.seg2 input{display:none}.seg2 span{display:block;text-align:center;border:2px solid #ccc;border-radius:10px;padding:13px 4px;font-weight:700;min-height:48px}.seg2 input:checked+span{border-color:#a49f2a;background:#f7f6e6}</style>'''
    body+='<div class="muted">%s &middot; %s</div><div class="card">Anyone with the link can view it without logging in. It shows the plan and devices only. No prices, no rates, no IP or MAC. The app does not send it. You copy it and send it yourself.</div>'%(h(row['customer']),h(row['title']))
    body+='''<h2>Make a link</h2><form method="post"><input type="hidden" name="action" value="create"><label>What to share</label><div class="seg2"><label><input type="radio" name="kind" value="plan" checked><span>Floor plan</span></label><label><input type="radio" name="kind" value="asbuilt"><span>As-built</span></label></div>
<label>Link stops working after</label><div class="seg2">'''+''.join('<label><input type="radio" name="days" value="%d"%s><span>%d days</span></label>'%(d,' checked' if d==30 else '',d) for d in SHARE_DAYS)+'''</div><button class="big-btn">Make view-only link</button></form>'''
    body+='<h2>Links for this job</h2>'
    if not links: body+='<div class="muted">No links yet.</div>'
    for r in links:
        st=sl_state(r); url=base+'/s/'+r['token']
        body+='<div class="sl%s%s"><div><b>%s</b> <span class="pill %s">%s</span></div><div class="muted">Made %s by %s &middot; %s %s &middot; opened %d time%s</div>'%(' on' if st=='active' else '',' new' if r['token']==new else '',SL_KIND.get(r['kind'],r['kind']),st,{'active':'Active','expired':'Expired','revoked':'Turned off'}[st],
            h(sl_when(r['created_at'])),h(nice_name(r['created_by'])),'Expires' if st=='active' else ('Expired' if st=='expired' else 'Was set to expire'),h(sl_when(r['expires_at'])),r['views'] or 0,'' if (r['views'] or 0)==1 else 's')
        if st=='active':
            body+='<div class="u">%s</div><button type="button" class="big-btn" onclick="slCopy(this,&quot;%s&quot;)">Copy link</button><a class="big-btn gray" target="_blank" href="%s">Open it (what the client sees)</a>'%(h(url),h(url),h('/s/'+r['token']))
            body+='<form method="post" onsubmit="return confirm(&quot;Turn off this link? Anyone who has it will not be able to open it.&quot;)"><input type="hidden" name="action" value="revoke"><input type="hidden" name="token" value="%s"><button class="big-btn red">Turn off link</button></form>'%h(r['token'])
        body+='</div>'
    body+='''<script>function slCopy(b,u){const done=()=>{b.textContent='Copied';setTimeout(()=>b.textContent='Copy link',2000)};
if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(u).then(done,()=>prompt('Copy this link',u));else{const t=document.createElement('textarea');t.value=u;document.body.appendChild(t);t.select();try{document.execCommand('copy');done()}catch(e){prompt('Copy this link',u)}t.remove()}}</script>'''
    return ops_page('Share view-only link', body, back='/?job='+q, msg=msg)

SHARE_HTML='''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>True Sense Security: {{ kind }}</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,Arial,sans-serif;background:#fff;color:#111;max-width:900px;margin:auto;padding:14px;overflow-wrap:anywhere}
.top{display:flex;align-items:center;gap:12px;border-bottom:3px solid #a49f2a;padding-bottom:10px}.top img{height:42px}.top .t{font-weight:800;font-size:1.3rem}
h2{font-size:1.15rem;color:#a49f2a;border-bottom:2px solid #a49f2a;margin:22px 0 8px}.muted{color:#666;font-size:.92rem}.plan{position:relative;margin:8px 0;border:1px solid #ddd}.plan img{width:100%;height:auto;display:block}
.big-btn{display:block;width:100%;text-align:center;padding:16px;border:0;border-radius:10px;background:#a49f2a;color:#fff;font-size:1.1rem;font-weight:700;text-decoration:none;margin:12px 0}
.leg{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px}.leg div{display:flex;align-items:center;gap:8px;border:1px solid #eee;border-radius:10px;padding:6px 8px}.leg b{margin-left:auto}
.info{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:10px 0}.info span:nth-child(odd){color:#666}.sig{max-width:320px;width:100%;border:1px solid #ddd;border-radius:10px}</style></head><body>
<div class="top"><img src="/static/logo.png" alt="True Sense Security"><div><div class="t">{{ kind }}</div><div class="muted">{{ j.customer }}{% if j.title %} &middot; {{ j.title }}{% endif %}</div></div></div>
<div class="info"><span>Site</span><span>{{ j.address or '-' }}</span><span>Job</span><span>{{ j.id }}</span><span>Devices</span><span>{{ ndev }}{% if ab %} ({{ ninst }} installed){% endif %}</span><span>Link expires</span><span>{{ exp }}</span></div>
<a class="big-btn" href="{{ pdf }}">Download PDF</a>
{% for p in plans %}<h2>{{ p.name }}</h2><div class="plan"><img src="{{ p.src }}" alt="floor plan">{{ plan_svg(p.fn, devs, ab) }}</div>{% endfor %}
{% if not plans %}<div class="muted" style="margin:20px 0">No floor plans yet.</div>{% endif %}
{% if legend %}<h2>Legend</h2><div class="leg">{% for t,c,n in legend %}<div>{{ dev_icon(t,c,True,26) }}<span>{{ t }}</span><b>{{ n }}</b></div>{% endfor %}</div>{% if ab %}<div class="muted" style="margin-top:6px">Outlined symbols are planned but not marked installed. Shaded areas show approximate camera coverage.</div>{% endif %}{% endif %}
{% if ab and sig %}<h2>Client sign-off</h2><img class="sig" src="{{ sig.sig }}" alt="signature"><div><b>{{ sig.name }}</b> signed {{ sig.signed_at }}</div>{% endif %}
<p class="muted" style="margin-top:24px">View-only link from True Sense Security. Device locations are approximate.</p></body></html>'''

def sl_bump(conn, r):
    conn.execute('UPDATE share_links SET views=COALESCE(views,0)+1,last_view=? WHERE token=?',(datetime.utcnow().isoformat(timespec='seconds'),r['token'])); conn.commit()
def sl_headers(resp):
    resp.headers['Cache-Control']='private, no-store'; resp.headers['X-Robots-Tag']='noindex, nofollow'; resp.headers['Referrer-Policy']='no-referrer'; return resp
SL_GONE='<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Link not available</title></head><body style="font-family:Arial,sans-serif;max-width:560px;margin:40px auto;padding:14px;text-align:center"><img src="/static/logo.png" alt="True Sense Security" style="height:48px"><h2>This link is no longer available</h2><p style="color:#666">It may have expired or been turned off. Please contact True Sense Security for a new link.</p></body></html>'

@app.route('/s/<token>')
def share_view(token):
    conn,r,row=sl_get(token)
    if not r: return SL_GONE,410
    try:
        info=plan_info(conn,row['id']); devs=parse_devices(row['survey_devices']); fns={p['fn'] for p in info}
        mine=[d for d in devs if d['plan'] in fns]; ab=r['kind']=='asbuilt'
        colors={n:c for n,p,c in DEVICE_TYPES}; cnt={}
        for d in mine: cnt[d['type']]=cnt.get(d['type'],0)+1
        legend=[(t,colors[t],cnt[t]) for t,_,_ in DEVICE_TYPES if t in cnt]
        plans=[{'fn':p['fn'],'name':p['name'],'src':'/s/%s/f/%s'%(token,urllib.parse.quote(p['fn']))} for p in info]
        sig=ab_load(row) if ab else {}
        sl_bump(conn,r)
        html=render_template_string(SHARE_HTML, j=row, kind=SL_KIND.get(r['kind'],'Plan'), plans=plans, devs=mine, ab=ab, legend=legend, ndev=len(mine), ninst=sum(1 for d in mine if d.get('installed')),
                                    exp=sl_when(r['expires_at']), pdf='/s/%s/pdf'%token, sig=sig if sig.get('sig') else None)
    finally: conn.close()
    return sl_headers(app.make_response(html))

@app.route('/s/<token>/pdf')
def share_pdf(token):
    conn,r,row=sl_get(token)
    if not r: return SL_GONE,410
    try: data=asbuilt_pdf_bytes(conn,row,False,'True Sense Security') if r['kind']=='asbuilt' else plan_pdf_bytes(conn,row,False,True,'True Sense Security')
    finally: conn.close()
    return sl_headers(pdf_response(data,'%s-%s.pdf'%(secure_filename(row['id']) or 'job',r['kind'])))

@app.route('/s/<token>/f/<path:fn>')
def share_file(token, fn):
    conn,r,row=sl_get(token)
    if not r: return ('',410)
    fn=secure_filename(fn); ok=fn in set(plan_files(conn,row['id'])); conn.close()
    if not ok: return ('',404)
    return sl_headers(send_from_directory(app.config['UPLOAD_FOLDER'], fn))

# ---------------- Installed systems per customer and site (office only) ----------------
def sys_day(iso):
    d=utc_day(iso) if iso and 'T' in str(iso) else str(iso or '')[:10]
    try: return datetime.fromisoformat(d).strftime('%b %-d, %Y')
    except Exception: return d or '-'
def sys_sites(conn, client):
    jobs=[r for r in conn.execute("SELECT * FROM jobs WHERE LOWER(TRIM(customer))=? AND IFNULL(job_type,'')!='Office Task'",(ckey(client),)).fetchall()]
    eq=conn.execute('SELECT * FROM equipment WHERE LOWER(TRIM(client))=? ORDER BY device',(ckey(client),)).fetchall()
    sites={}
    def site(addr):
        k=ckey(addr) or '-'
        return sites.setdefault(k,{'addr':(addr or '').strip(),'jobs':[],'eq':[],'devs':[],'svc':None,'inst':None,'issues':[]})
    for r in jobs: site(r['address'])['jobs'].append(r)
    for e in eq:
        k=ckey(e['site'])
        if k not in sites and e['job_id']:
            jr=next((j for j in jobs if j['id']==e['job_id']),None)
            if jr: k=ckey(jr['address']) or '-'
        site(e['site'] if k not in sites else sites[k]['addr'] or e['site'])['eq'].append(e)
    colors={n:c for n,p,c in DEVICE_TYPES}
    for s in sites.values():
        for r in sorted(s['jobs'],key=lambda r:r['completed_at'] or r['created_at'] or ''):
            info=plan_info(conn,r['id']); names={p['fn']:p['name'] for p in info}
            inst=[d for d in parse_devices(r['survey_devices']) if d['plan'] in names and d.get('installed')]
            if inst and r['job_type']!='Survey': s['devs'].append({'job':r,'devs':inst,'names':names,'color':colors})
            done=r['status']=='completed'; svc=work_type(r)=='Service'
            if done and svc and (not s['svc'] or (r['completed_at'] or '')>(s['svc']['completed_at'] or '')): s['svc']=r
            if done and not svc and r['job_type']!='Survey' and (not s['inst'] or (r['completed_at'] or '')>(s['inst']['completed_at'] or '')): s['inst']=r
            if not done and svc: s['issues'].append(('Open service call',r,h(r['title'] or '')+(' &middot; '+h(r['scope'][:120]) if r['scope'] else '')))
            if r['help_flag'] and not done: s['issues'].append(('Tech needs help',r,h(r['help_note'] or '')))
            open_p=[t['text'] for t in parse_ticks(r['punch_list']) if not t['done']]
            if open_p and r['job_type']!='Survey': s['issues'].append(('%d open punch item%s'%(len(open_p),'' if len(open_p)==1 else 's'),r,'<br>'.join(h(x) for x in open_p[:5])))
    return sorted(sites.values(),key=lambda s:(s['addr']=='',s['addr'].lower()))

SYS_CSS='''<style>.sys{border:2px solid #e5e5e5;border-radius:14px;padding:12px;margin:12px 0}.sys h3{margin:0 0 4px;font-size:1.15rem}.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:8px 0}.kv span:nth-child(odd){color:#666}
.cnt{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.cnt span{display:inline-flex;align-items:center;gap:6px;border:1px solid #eee;border-radius:999px;padding:4px 10px 4px 4px;font-weight:700}
.iss{border-left:5px solid #c62828;background:#fff7f7;border-radius:8px;padding:10px;margin:6px 0}.iss a{font-weight:700}.ok{border-left:5px solid #2e7d32;background:#f3faf3;border-radius:8px;padding:10px;margin:6px 0}
table.dv{width:100%;border-collapse:collapse;font-size:.92rem}table.dv td,table.dv th{padding:7px 4px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}table.dv th{color:#666;font-weight:600}
details.sj{border:1px solid #e5e5e5;border-radius:12px;margin:8px 0;padding:0 10px}details.sj summary{min-height:48px;display:flex;align-items:center;font-weight:700;cursor:pointer}.lk{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.lk a{flex:1 1 140px;margin:0}</style>'''
@app.route('/systems')
def systems_page():
    if not is_office(): return redirect('/')
    client=(request.args.get('client') or '').strip(); only=ckey(request.args.get('site') or '')
    if not client: return redirect('/clients')
    conn=get_db(); sites=sys_sites(conn, client); conn.close()
    if only: sites=[s for s in sites if ckey(s['addr'])==only] or sites
    body=SYS_CSS+'<div class="muted">Devices marked installed on job plans, plus equipment records. No prices.</div>'
    if only: body+='<a class="big-btn gray" href="/systems?client=%s">Show all sites for %s</a>'%(h(urllib.parse.quote(client)),h(client))
    if not sites: body+='<div class="card">No jobs for this client yet.</div>'
    for s in sites:
        ndev=sum(len(x['devs']) for x in s['devs'])
        cnt={}
        for x in s['devs']:
            for d in x['devs']: cnt[d['type']]=cnt.get(d['type'],0)+1
        colors={n:c for n,p,c in DEVICE_TYPES}
        body+='<div class="sys"><h3>%s</h3>'%h(s['addr'] or 'No address on file')
        body+='<div class="kv"><span>Installed devices</span><span><b>%d</b>%s</span><span>Last install</span><span>%s</span><span>Last service</span><span>%s</span><span>Jobs here</span><span>%d</span></div>'%(ndev,
            (' + %d equipment record%s'%(len(s['eq']),'' if len(s['eq'])==1 else 's')) if s['eq'] else '',
            ('<a href="/?job=%s">%s</a> %s'%(h(urllib.parse.quote(s['inst']['id'])),h(s['inst']['id']),sys_day(s['inst']['completed_at']))) if s['inst'] else 'None yet',
            ('<a href="/?job=%s">%s</a> %s'%(h(urllib.parse.quote(s['svc']['id'])),h(s['svc']['id']),sys_day(s['svc']['completed_at']))) if s['svc'] else 'No service visits yet',len(s['jobs']))
        if cnt: body+='<div class="cnt">'+''.join('<span>%s%s %d</span>'%(dev_icon(t,colors[t],True,26),h(t),cnt[t]) for t,_,_ in DEVICE_TYPES if t in cnt)+'</div>'
        body+='<h2>Open troubles and issues</h2>'
        if s['issues']:
            for lab,r,txt in s['issues']:
                body+='<div class="iss"><div><b>%s</b> &middot; <a href="/?job=%s">%s</a></div><div class="muted">%s</div></div>'%(h(lab),h(urllib.parse.quote(r['id'])),h(r['id']),txt)
        else: body+='<div class="ok">Nothing open at this site.</div>'
        body+='<a class="big-btn gray" href="/new-job?%s">+ New service call here</a>'%h(urllib.parse.urlencode({'t':'Service','customer':client,'address':s['addr']}))
        body+='<h2>Installed by job</h2>'
        if not s['devs']: body+='<div class="muted">No devices marked installed on a plan yet.</div>'
        for x in reversed(s['devs']):
            r=x['job']; q=urllib.parse.quote(r['id'])
            body+='<details class="sj"><summary>%s &middot; %d device%s &middot; %s</summary>'%(h(r['id']),len(x['devs']),'' if len(x['devs'])==1 else 's',h(sys_day(r['completed_at']) if r['completed_at'] else 'In progress'))
            body+='<div class="muted">%s</div><div class="lk"><a class="big-btn" href="/?job=%s">Open job</a><a class="big-btn gray" href="/design/%s">Open plan</a><a class="big-btn gray" target="_blank" href="/job/%s/asbuilt.pdf">As-built PDF</a></div>'%(h(r['title'] or ''),h(q),h(q),h(q))
            body+='<table class="dv"><tr><th>Device</th><th>Type</th><th>Floor</th><th>Part #</th></tr>'+''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'%(h(d.get('label') or '-'),h(d['type']),h(x['names'].get(d['plan'],'')),h(d.get('part') or '-')) for d in x['devs'])+'</table></details>'
        if s['eq']:
            body+='<h2>Equipment records</h2><table class="dv"><tr><th>Device</th><th>Model</th><th>Installed</th><th>Warranty</th></tr>'+''.join('<tr><td><a href="/equipment/%d">%s</a></td><td>%s</td><td>%s</td><td>%s</td></tr>'%(e['id'],h(e['device'] or '-'),h(e['model'] or '-'),h(e['install_date'] or '-'),h(e['warranty_end'] or '-')) for e in s['eq'])+'</table>'
        tix_fn=globals().get('tickets_site_html')
        if callable(tix_fn):
            try:
                body+=tix_fn(customer=client, address=s['addr'])
            except Exception:
                app.logger.exception('ticket history on systems failed')
        body+='</div>'
    return ops_page('Installed systems: '+client, body, back='/clients?name='+urllib.parse.quote(client))

@app.route('/design/<job_id>')
def design(job_id):
    conn=get_db(); row=page_job(conn, job_id)
    if not row: conn.close(); return redirect('/')
    if row['job_type']=='Office Task': conn.close(); return redirect('/?job='+urllib.parse.quote(job_id))
    html=design_page(row, conn, DESIGN_JS_EDIT, DESIGN_CSS_EDIT); conn.close(); return html

def serve():
    port=int(os.environ.get('PORT',5000))
    try: from gunicorn.app.base import BaseApplication
    except ImportError: return app.run(host='0.0.0.0', port=port, threaded=True)
    class _G(BaseApplication):
        def load_config(self):
            for k,v in {'bind':'0.0.0.0:%d'%port,'workers':1,'worker_class':'gthread','threads':8,'timeout':120,'keepalive':5,'accesslog':'-'}.items(): self.cfg.set(k,v)
        def load(self): return app
    _G().run()

import fieldops_platform
fieldops_platform.register(sys.modules[__name__])
import fieldops_users
fieldops_users.register(sys.modules[__name__])
import fieldops_quote
fieldops_quote.register(sys.modules[__name__])
import fieldops_crm
fieldops_crm.register(sys.modules[__name__])
import fieldops_ops
fieldops_ops.register(sys.modules[__name__])
import fieldops_tickets
fieldops_tickets.register(sys.modules[__name__])
import fieldops_compliance
fieldops_compliance.register(sys.modules[__name__])
import fieldops_plans
fieldops_plans.register(sys.modules[__name__])
import fieldops_portal
fieldops_portal.register(sys.modules[__name__])
import fieldops_qbo_import
fieldops_qbo_import.register(sys.modules[__name__])
import fieldops_security
fieldops_security.register(sys.modules[__name__])
import fieldops_areas
fieldops_areas.register(sys.modules[__name__])

if __name__=='__main__':
    serve()
