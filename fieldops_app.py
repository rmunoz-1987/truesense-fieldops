from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect
import re, sqlite3, os, shutil, uuid, hmac, hashlib, json, threading, urllib.request, urllib.parse, math, time
from datetime import timedelta
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
    return n if n.lower() in USERS else ''
def is_office(): return me().lower() in OFFICE_NAMES
def can_see(job): return is_office() or ' '.join((job['tech'] or '').split()).lower() == me().lower()
def sub_logins(): return [nice_name(k) for k in sorted(USERS) if k not in OFFICE_NAMES]
def default_sub(): return (sub_logins() or [''])[0]

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

@app.before_request
def require_login():
    if request.path in ('/login', '/healthz', '/sw.js', '/manifest.webmanifest', '/offline', '/api/qbo/pending', '/api/qbo/result') or request.path.startswith('/static/') or request.path.startswith('/help-photo/') or request.path.startswith('/hub/') or request.path.startswith('/agreement/view/') or request.path in ('/api/tech-support/reply', '/api/qbo-monthly'): return
    if not USERS:
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
<script>if('serviceWorker' in navigator)window.addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}))</script>"""

MANIFEST = {'name':'True Sense Field Ops','short_name':'Field Ops','start_url':'/','scope':'/','display':'standalone','background_color':'#ffffff','theme_color':'#a49f2a',
    'icons':[{'src':'/static/icons/icon-192.png','sizes':'192x192','type':'image/png'},{'src':'/static/icons/icon-512.png','sizes':'512x512','type':'image/png'},
             {'src':'/static/icons/maskable-512.png','sizes':'512x512','type':'image/png','purpose':'maskable'}]}

SW_JS = r"""const V='fo-v3';const SHELL=['/offline','/static/icons/icon-192.png','/static/icons/icon-512.png'];
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(V).then(c=>c.addAll(SHELL)))});
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==V).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('message',e=>{if(e.data==='logout')e.waitUntil(caches.keys().then(ks=>Promise.all(ks.map(k=>caches.delete(k)))))});
self.addEventListener('fetch',e=>{const r=e.request,u=new URL(r.url);if(r.method!=='GET'||u.origin!==location.origin)return;
if(r.mode==='navigate'){if(u.pathname!=='/')return e.respondWith(fetch(r).catch(()=>caches.match('/offline')));
e.respondWith(fetch(r).then(res=>{if(res.ok&&!res.redirected){const c=res.clone();caches.open(V).then(x=>x.put('/',c))}return res}).catch(()=>caches.match('/').then(m=>m||caches.match('/offline'))));return}
if(u.pathname.startsWith('/static/')){e.respondWith(caches.match(r).then(m=>m||fetch(r).then(res=>{const c=res.clone();caches.open(V).then(x=>x.put(r,c));return res})));return}
if(u.pathname.startsWith('/api/jobs')||u.pathname.startsWith('/uploads/')){e.respondWith(fetch(r).then(res=>{if(res.ok){const c=res.clone();caches.open(V).then(x=>x.put(r,c))}return res}).catch(()=>caches.match(r).then(m=>m||new Response(JSON.stringify({error:'offline'}),{status:503,headers:{'Content-Type':'application/json'}}))))}});
self.addEventListener('push',e=>{let d={};try{d=e.data.json()}catch(x){d={title:'Field Ops',body:e.data?e.data.text():''}}
e.waitUntil(self.registration.showNotification(d.title||'Field Ops',{body:d.body||'',icon:'/static/icons/icon-192.png',badge:'/static/icons/icon-192.png',data:{url:d.url||'/'}}))});
self.addEventListener('notificationclick',e=>{e.notification.close();const url=(e.notification.data&&e.notification.data.url)||'/';
e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(ws=>{for(const w of ws){if('focus' in w){w.navigate(url);return w.focus()}}return clients.openWindow(url)}))});"""

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

@app.route('/offline')
def offline_page(): return OFFLINE_HTML

@app.after_request
def add_tap_css(resp):
    try:
        if resp.mimetype=='text/html' and not resp.direct_passthrough and resp.status_code<400 or resp.mimetype=='text/html' and resp.status_code==503:
            h=resp.get_data(as_text=True)
            if 'id="tap-css"' not in h:
                h=h.replace('</head>', TAP_CSS+PWA_HEAD+'</head>', 1) if '</head>' in h else TAP_CSS+h
                resp.set_data(h)
    except Exception: pass
    return resp

@app.route('/healthz')
def healthz(): return 'ok'

@app.route('/login', methods=['GET','POST'])
def login():
    if not USERS: return render_template_string(LOGIN_HTML, setup=True), 503
    error = None
    if request.method == 'POST':
        name = nice_name(request.form.get('name'))
        pw = request.form.get('password') or ''
        expected = USERS.get(name.lower())
        if expected and hmac.compare_digest(pw.encode(), expected.encode()):
            session.permanent = True; session['name'] = name
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
    for col in ['signature TEXT', 'materials TEXT', 'phone TEXT', 'checklist TEXT', 'scheduled_date TEXT', 'created_by TEXT', 'materials_list TEXT', 'private_notes TEXT', 'job_type TEXT', 'survey_list TEXT', 'quick_notes TEXT', 'punch_list TEXT', 'survey_form TEXT', 'survey_office TEXT', 'help_flag TEXT', 'help_note TEXT', 'help_at TEXT', 'help_by TEXT', 'help_photo TEXT', 'kelly_at TEXT', 'kelly_by TEXT', 'survey_devices TEXT', 'map_data TEXT', 'hub_approved_at TEXT', 'hub_approved_name TEXT', 'hub_created_at TEXT', 'hub_nonce TEXT', 'review_by TEXT', 'review_at TEXT', 'labor_rates TEXT', 'sale_amount REAL', 'plan_id INTEGER', 'estimate_json TEXT', 'pay_link TEXT', 'paid_at TEXT']:
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
  <div style="font-size:.85rem;color:#666;margin-top:4px">{{ name }} &middot; <a href="/logout" style="color:#666" onclick="if(!confirm('Log out?'))return false;try{navigator.serviceWorker.controller&&navigator.serviceWorker.controller.postMessage('logout');localStorage.removeItem('fo-q')}catch(e){}">Log out</a></div>
</div>
<div id="home"{% if office %} class="dash"{% endif %}>
{% if office %}<nav class="menu"><button class="big-btn" onclick="toggleNew()">+ New Job</button><a href="/clients" class="big-btn">Clients &amp; Surveys</a><a href="/schedule" class="big-btn gray">Schedule</a><a href="/calendar" class="big-btn gray">Calendar</a><a href="/to-order" class="big-btn gray">To Order</a><a href="/techs" class="big-btn gray">Subs</a><a href="/checklists" class="big-btn gray">Checklists</a><a href="/agreement" class="big-btn gray">Agreement</a><a href="/revenue" class="big-btn gray">Revenue</a><a href="/timelog" class="big-btn gray">Time Log</a><a href="/safety" class="big-btn gray">Safety</a><a href="/qbo" class="big-btn gray">QuickBooks</a><a href="/reports" class="big-btn gray">Reports</a><a href="/plans" class="big-btn gray">Service Plans</a><a href="/equipment" class="big-btn gray">Equipment</a><a href="/stock" class="big-btn gray">Stock{% if low_n %} <span style="background:#c62828;color:#fff;border-radius:8px;padding:0 6px">{{ low_n }} low</span>{% endif %}</a></nav><main>
<div class="tiles"><button class="tile" data-f="today" onclick="setF(this.dataset.f)"><b id="t-today">0</b>Today</button><button class="tile" data-f="progress" onclick="setF(this.dataset.f)"><b id="t-progress">0</b>In Progress</button><button class="tile" data-f="review" onclick="setF(this.dataset.f)"><b id="t-review">0</b>Needs Review</button><button class="tile" data-f="help" onclick="setF(this.dataset.f)"><b id="t-help">0</b>Needs Help</button><button class="tile" data-f="invoice" onclick="setF(this.dataset.f)"><b id="t-invoice">0</b>Ready to Invoice</button><button class="tile" data-f="follow" onclick="setF(this.dataset.f)"><b id="t-follow">0</b>Follow-ups Due</button><button class="tile" data-f="texts" onclick="setF(this.dataset.f)"><b id="t-texts">0</b>Texts to Review</button><button class="tile" id="tile-docs" onclick="location.href='/techs'"><b id="t-docs">0</b>Sub Docs Due</button><button class="tile" id="tile-sign" onclick="location.href='/agreement#pending'"><b id="t-sign">0</b>Needs Your Signature</button></div>{% endif %}
{% if not office and agr %}{% if agr.signed %}<a href="/agreement" style="display:block;text-align:center;color:#666;margin:6px 0">My signed Subcontractor Agreement (v{{ agr.version }})</a>{% else %}<a href="/agreement" class="big-btn" style="display:block;text-align:center;text-decoration:none;box-sizing:border-box;margin:8px 0">Please sign the Subcontractor Agreement (v{{ agr.version }})</a>{% endif %}{% endif %}
{% if not office %}<button id="push-btn" class="big-btn hidden" onclick="pushOn()">Turn on job alerts</button><div id="push-hint" class="hidden" style="text-align:center;color:#666;margin:6px 0">For job alerts on iPhone: tap Share, then Add to Home Screen, then open Field Ops from your home screen.</div>{% endif %}
<div id="search-box" class="row" style="align-items:center"><input type="search" id="q" placeholder="Search client, address, job, note, serial" onkeydown="if(event.key==='Enter')goSearch()"><button class="big-btn gray" style="flex:0 0 90px" onclick="goSearch()">Search</button></div>
{% if office %}<div id="office" class="job hidden"><div style="font-weight:600;margin-bottom:4px">Add New Job</div>
<div class="row" style="flex-wrap:wrap;gap:6px">{% for t in job_types %}<button class="big-btn gray qa" style="flex:1 1 45%" data-t="{{ t }}" onclick="quickType(this.dataset.t)">+ {{ t }}</button>{% endfor %}</div>
<datalist id="cl-list"></datalist>
<input type="text" id="n-id" placeholder="Job ID (e.g. TS-2026-0144)">
<input type="text" id="n-title" placeholder="Title" oninput="this.dataset.auto=''">
<input type="text" id="n-customer" placeholder="Customer" list="cl-list" onchange="pickClient()">
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
<div id="toast" class="hidden"></div>
<div id="detail" class="hidden">
<button class="big-btn gray" style="font-size:1.3rem;margin-bottom:8px" onclick="showList()">&larr; Back</button>
<div id="steps-box"><div class="stp-bar" id="stp-bar"></div><div class="stp-why" id="stp-why"></div><button class="big-btn" id="next-btn" onclick="nextStep()">Next Step</button><button class="big-btn gray" id="more-all" onclick="toggleGuided()">More</button></div>
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
<div id="sec-ab"><div class="job" id="ab-card"><div style="font-weight:800;font-size:1.4rem">As-Built</div><div id="ab-slot"></div><a id="eq-link" class="big-btn gray" href="/" onclick="this.href='/job/'+encodeURIComponent(currentJob.id)+'/equipment'">Equipment at this site</a>
<div class="ptitle" style="font-size:1.1rem;margin-top:14px">Blueprints</div><div id="bp-list"></div>
<div class="big-btn gray" onclick="document.getElementById('bp-file').click()">Upload Blueprint (PDF or image)</div>
<input type="file" id="bp-file" accept=".pdf,.png,.jpg,.jpeg,.webp,.heic,application/pdf,image/*" multiple style="display:none"></div></div>
<div id="sec-map"><button class="big-btn gray" onclick="mapOpen()">Site Map <span id="map-sum" style="font-weight:400;font-size:.9rem"></span></button></div>
<div id="sec-follow">{% if office %}<div class="job"><div style="font-weight:600">Follow-ups (office only)</div><div id="fu-list"></div>
<div style="font-size:.85rem;color:#666;margin-top:8px">Remind me on</div><input type="date" id="fu-date"><input type="text" id="fu-note" placeholder="What to follow up on (e.g. call about estimate)"><button class="big-btn" onclick="addFollow()">Add Follow-up</button></div>{% endif %}</div>
<div id="sec-hub">{% if office %}<div class="job"><div style="font-weight:600">Client Link (office only)</div><div class="muted" style="font-size:.9rem">Anyone with this link can see this job's summary, plan, photos and signature. Copy it and send it yourself.</div>
<input type="text" id="hub-url" readonly onclick="this.select()"><div id="hub-exp" style="font-weight:600"></div><div class="row"><button class="big-btn gray" onclick="copyHub()">Copy Link</button><button class="big-btn gray" onclick="newHub()">New Link</button></div><a class="big-btn gray" href="/" onclick="this.href='/job/'+encodeURIComponent(currentJob.id)+'/estimate'">Estimate: Good, Better, Best</a><div id="hub-appr" style="font-weight:700;font-size:1.1rem;color:#2e7d32"></div></div>{% endif %}</div>
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
document.getElementById('cl-list').innerHTML=QK.clients.map(c=>'<option value="'+esc(c.name)+'"></option>').join('');markQuick()}
function markQuick(){const t=document.getElementById('n-type').value;document.querySelectorAll('.qa').forEach(b=>{b.classList.toggle('gray',b.dataset.t!==t)})}
function quickType(t){const n=k=>document.getElementById('n-'+k);n('type').value=t;tplOpts();markQuick();autoTitle();n('customer').focus()}
function autoTitle(){const n=k=>document.getElementById('n-'+k);const t=n('title');if(!t.value||t.dataset.auto==='1'){const c=n('customer').value.trim();t.value=n('type').value+(c?' - '+c:'');t.dataset.auto='1'}}
function pickClient(){const n=k=>document.getElementById('n-'+k);const c=(QK&&QK.clients||[]).find(x=>x.name.toLowerCase()===n('customer').value.trim().toLowerCase());
if(c){if(!n('address').value)n('address').value=c.address;if(!n('phone').value)n('phone').value=c.phone}autoTitle()}
function goSearch(){const q=(document.getElementById('q').value||'').trim();if(q.length>1)location.href='/search?q='+encodeURIComponent(q);else{document.getElementById('q').value='';load()}}
function toggleNew(){tplOpts();const o=document.getElementById('office');o.classList.toggle('hidden');if(!o.classList.contains('hidden')){quickFill();o.scrollIntoView({behavior:'smooth'});document.getElementById('n-id').focus()}}
async function load(){const q=(document.getElementById('q').value||'').trim();
if(OFFICE&&!q)return loadDash();
const r=await fetch(q?'/api/jobs?q='+encodeURIComponent(q):'/api/jobs?status=assigned');const jobs=await r.json();
const groups={};jobs.forEach(j=>{const k=j.scheduled_date||'';(groups[k]=groups[k]||[]).push(j)});
const keys=Object.keys(groups).sort((a,b)=>a===''?1:b===''?-1:a.localeCompare(b));
document.getElementById('list').innerHTML=(keys.map(k=>`<div class="day">${dayLabel(k)}</div>`+groups[k].map(card).join('')).join('')||'<div class="job">'+(q?'No matching jobs':'No jobs')+'</div>')}
async function loadDash(){const [a,c,fu,tx,dx,sg]=await Promise.all([fetch('/api/jobs?status=assigned').then(r=>r.json()),fetch('/api/jobs?status=completed').then(r=>r.json()),fetch('/api/followups').then(r=>r.json()),fetch('/api/drafts').then(r=>r.json()),fetch('/api/sub-docs/alerts').then(r=>r.json()),fetch('/api/agreements/pending').then(r=>r.json())]);
document.getElementById('t-sign').textContent=sg.length;document.getElementById('tile-sign').classList.toggle('red',sg.length>0);
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
async function openJob(id){const r=await fetch('/api/jobs/'+encodeURIComponent(id));if(!r.ok){alert('Job not found');return}currentJob=await r.json();history.replaceState(null,'','/?job='+encodeURIComponent(currentJob.id));
guided=true;renderPhotos();renderBlueprints();renderHelp();renderClock();document.getElementById('support-note').value='';loadSupport();document.getElementById('help-form').classList.add('hidden');document.getElementById('help-note').value='';renderMats();renderTotals();fillSurvey();renderTicks('punch',currentJob.punch_list||[],savePunch,true);document.getElementById('punch-new').value='';document.getElementById('status').value=currentJob.status||'assigned';document.getElementById('hours').value='';
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
if(r.ok){const d=await r.json().catch(()=>({}));alert(currentJob.job_type==='Office Task'?'Task done':d.status==='review'?'Job finished. The office will review it.':'Job finished');showList()}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}};
async function addJob(){const v=k=>document.getElementById('n-'+k).value.trim();
const body={id:v('id'),title:v('title'),customer:v('customer'),address:v('address'),phone:v('phone'),tech:v('tech'),scope:v('scope'),scheduled_date:v('date'),checklist:v('checklist'),job_type:v('type')};
if(!body.id||!body.title){alert('Job ID and title are required');return}
const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(r.ok){try{localStorage.setItem('fo_last_tech',body.tech);localStorage.setItem('fo_last_type',body.job_type)}catch(e){}
['id','title','customer','address','phone','scope','date','checklist'].forEach(k=>document.getElementById('n-'+k).value='');document.getElementById('n-title').dataset.auto='';document.getElementById('office').classList.add('hidden');load()}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}}
load();
const qj=new URLSearchParams(location.search).get('job');if(qj)openJob(qj);
</script></body></html>'''

@app.route('/')
def index():
    techs=[(n,n) for n in sub_logins()]+[(nice_name(k),nice_name(k)+' (office)') for k in sorted(USERS) if k in OFFICE_NAMES]
    tpls=[]
    if is_office():
        conn=get_db(); tpls=[{'id':r['id'],'name':r['name'],'job_type':r['job_type'] or '','items':json.loads(r['items'] or '[]')} for r in conn.execute('SELECT * FROM checklist_templates ORDER BY name').fetchall()]; conn.close()
    agr=None; low_n=0
    if is_office():
        conn=get_db(); low_n=len(low_stock(conn)); conn.close()
    if not is_office():
        conn=get_db(); a=current_agreement(conn); agr={'version':a['version'],'signed':signed_current(conn, me())}; conn.close()
    return render_template_string(HTML, safety_items=SAFETY_ITEMS, def_tpl=DEFAULT_TEMPLATE, agr=agr, name=me(), office=is_office(), techs=techs, statuses=STATUSES, job_types=JOB_TYPES, obstacles=SURVEY_OBSTACLES, dev_types=DEVICE_TYPES, tpls=tpls, low_n=low_n)

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
    conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,phone,checklist,scheduled_date,created_by,job_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (jid,title,(d.get('customer') or '').strip(),(d.get('address') or '').strip(),(d.get('scope') or '').strip(),'assigned',tech,datetime.utcnow().isoformat(),(d.get('phone') or '').strip(),chk,sd,me(),jt))
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
    cl=[{'name':r['name'],'address':r['address'] or '','phone':r['phone'] or ''} for r in conn.execute('SELECT name,address,phone FROM clients ORDER BY name')]
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
    conn=get_db(); photos=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto','daily') ORDER BY id",(job_id,)).fetchall()]; bps=blueprints(conn, job_id); plans=plan_files(conn, job_id); srcs=survey_plan_sources(conn, row)
    extra={}
    if is_office():
        n,c=(row['hub_nonce'],row['hub_created_at']) if row['hub_nonce'] and row['hub_created_at'] else hub_new(conn, job_id)
        extra=hub_info(job_id,n,c); extra['labor']=job_labor(conn,row)
    extra['clock']=clock_state(conn, job_id); extra['steps']=job_steps(conn, row); extra['flow']=flow_state(conn, row)
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
    photos=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto','daily') ORDER BY id",(job_id,)).fetchall()]; conn.close()
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
    fns=[r['filename'] for r in conn.execute("SELECT filename FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto','daily') ORDER BY id",(job_id,)).fetchall()]
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
<a href="{{ back or '/' }}" class="big-btn gray">&larr; Back</a><h1>{{ title }}</h1>{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}{{ body }}</body></html>'''
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
            vals=((f.get('client') or '').strip()[:120],(f.get('site') or '').strip()[:200],(f.get('phone') or '').strip()[:40],(f.get('title') or 'Service visit').strip()[:120],em,nd,nice_name(f.get('tech') or '')[:60],max(0,fnum(f.get('price'))),chk,1 if f.get('active','1')=='1' else 0)
            if a=='add': conn.execute('INSERT INTO service_plans (client,site,phone,title,every_months,next_date,tech,price,checklist,active,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',vals+(now_local_str(),)); msg='Plan added'
            else: conn.execute('UPDATE service_plans SET client=?,site=?,phone=?,title=?,every_months=?,next_date=?,tech=?,price=?,checklist=?,active=? WHERE id=?',vals+(f.get('plan_id'),)); msg='Plan saved'
            conn.commit()
        elif a=='run': msg='Made %d visits'%run_plans(conn)
    plans=conn.execute('SELECT * FROM service_plans ORDER BY active DESC, next_date').fetchall()
    eid=request.args.get('edit'); ed=conn.execute('SELECT * FROM service_plans WHERE id=?',(eid,)).fetchone() if eid else None; conn.close()
    subs=sub_logins()
    def form(p):
        g=lambda k,d='': h((p[k] if p else d) or d)
        try: chk='\n'.join(json.loads(p['checklist'] or '[]')) if p else ''
        except Exception: chk=''
        return ('<form method="post"><input type="hidden" name="action" value="%s"><input type="hidden" name="plan_id" value="%s">'%('edit' if p else 'add',g('id'))+
            '<label>Client</label><input name="client" value="%s" required><label>Site address</label><input name="site" value="%s"><label>Client phone</label><input name="phone" value="%s">'%(g('client'),g('site'),g('phone'))+
            '<label>Visit name</label><input name="title" value="%s" placeholder="Camera checkup"><div class="row"><div><label>Every how many months</label><input name="every_months" type="number" min="1" max="60" value="%s"></div><div><label>Next visit</label><input name="next_date" type="date" value="%s" required></div></div>'%(g('title','Service visit'),g('every_months','12'),g('next_date'))+
            '<div class="row"><div><label>Sub</label><select name="tech">%s</select></div><div><label>Price per visit $</label><input name="price" type="number" step="0.01" min="0" value="%s"></div></div>'%(''.join('<option%s>%s</option>'%(' selected' if p and ckey(p['tech'])==ckey(n) else '',h(n)) for n in subs),('%g'%p['price']) if p and p['price'] else '')+
            '<label>Checklist, one per line (optional)</label><textarea name="checklist" rows="3">%s</textarea>'%h(chk)+
            '<label>Status</label><select name="active"><option value="1"%s>Active</option><option value="0"%s>Paused</option></select><button class="big-btn">%s</button></form>'%('' if p and not p['active'] else ' selected',' selected' if p and not p['active'] else '','Save Plan' if p else 'Add Plan'))
    body='<div class="muted">Visits are made automatically %d days before the due date and land on the sub list. Approving the visit makes an invoice draft with the plan price. Nothing is sent.</div>'%PLAN_LEAD_DAYS
    if ed: body+='<h2>Edit plan</h2>'+form(ed)+'<a class="big-btn gray" href="/plans">Cancel</a>'
    body+='<h2>Plans</h2>'+(''.join('<a class="card" href="/plans?edit=%d"><b>%s</b> %s<div>%s</div><div class="muted">%s</div><div class="muted">Every %d months &middot; next %s &middot; %s &middot; %s%s</div></a>'%(
        p['id'],h(p['title']),'<span class="okf">Active</span>' if p['active'] else '<span class="flag">Paused</span>',h(p['client']),h(p['site']),p['every_months'] or 12,h(nice_day(p['next_date'] or '')),h(p['tech'] or 'any sub'),money(p['price']),(' &middot; last visit '+h(p['last_job_id'])) if p['last_job_id'] else '') for p in plans) or '<div class="card">No plans yet.</div>')
    body+='<form method="post"><input type="hidden" name="action" value="run"><button class="big-btn gray">Make due visits now</button></form>'
    if not ed: body+='<h2>New plan</h2>'+form(None)
    return ops_page('Service Plans', body, msg=msg)

def job_parts_cost(conn, job_id):
    r=conn.execute('SELECT SUM(-qty*IFNULL(unit_cost,0)) c FROM stock_moves WHERE job_id=? AND qty<0',(job_id,)).fetchone()
    return round(r['c'] or 0,2)

@app.route('/reports')
def reports_page():
    if not is_office(): return redirect('/')
    t=today_local(); fr=(request.args.get('from') or '%d-01-01'%t.year)[:10]; to=(request.args.get('to') or '%d-12-31'%t.year)[:10]
    conn=get_db(); jobs=[]
    for r in conn.execute('SELECT * FROM jobs').fetchall():
        day=(r['scheduled_date'] or (r['created_at'] or '')[:10])
        if not (fr<=day<=to) or r['job_type']=='Office Task': continue
        lab=job_labor(conn, r); parts=job_parts_cost(conn, r['id']); sale=r['sale_amount'] or 0
        jobs.append({'id':r['id'],'title':r['title'] or '','client':r['customer'] or '','type':r['job_type'] or 'Install','tech':nice_name(r['tech']),'date':day,'status':r['status'],
                     'sale':sale,'labor':lab['total'],'parts':parts,'profit':round(sale-lab['total']-parts,2),'hours':round(sum(l['hours'] for l in lab['lines']),2),'lines':lab['lines'],'no_sale':not r['sale_amount']})
    conn.close()
    subs={}; types={}
    for j in jobs:
        for l in j['lines']:
            s_=subs.setdefault(l['sub'],{'name':l['sub'],'jobs':set(),'hours':0,'labor':0,'sale':0,'profit':0}); s_['jobs'].add(j['id']); s_['hours']+=l['hours']; s_['labor']+=l['cost']
        if j['tech']:
            s_=subs.setdefault(j['tech'],{'name':j['tech'],'jobs':set(),'hours':0,'labor':0,'sale':0,'profit':0}); s_['jobs'].add(j['id']); s_['sale']+=j['sale']; s_['profit']+=j['profit']
        ty=types.setdefault(j['type'],{'name':j['type'],'jobs':0,'hours':0,'sale':0,'labor':0,'parts':0,'profit':0})
        ty['jobs']+=1; ty['hours']+=j['hours']; ty['sale']+=j['sale']; ty['labor']+=j['labor']; ty['parts']+=j['parts']; ty['profit']+=j['profit']
    kind=request.args.get('csv')
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
    body+='<h2>By sub</h2><table><tr><th>Sub</th><th class="num">Jobs</th><th class="num">Hours</th><th class="num">Labor</th><th class="num">Profit</th></tr>'+''.join('<tr><td>%s</td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td></tr>'%(h(x['name']),len(x['jobs']),'%g'%round(x['hours'],2),money(x['labor']),money(x['profit'])) for x in sorted(subs.values(),key=lambda x:-x['hours']))+'</table><a class="big-btn gray" href="/reports?%s&csv=subs">Download sub CSV</a>'%q
    body+='<h2>By job type</h2><table><tr><th>Type</th><th class="num">Jobs</th><th class="num">Sales</th><th class="num">Profit</th></tr>'+''.join('<tr><td>%s</td><td class="num">%d</td><td class="num">%s</td><td class="num">%s</td></tr>'%(h(x['name']),x['jobs'],money(x['sale']),money(x['profit'])) for x in types.values())+'</table><a class="big-btn gray" href="/reports?%s&csv=types">Download job type CSV</a>'%q
    body+='<h2>By job</h2><table><tr><th>Job</th><th class="num">Sale</th><th class="num">Cost</th><th class="num">Profit</th></tr>'+''.join('<tr><td><a href="/?job=%s">%s</a><div class="muted">%s %s %s</div></td><td class="num">%s</td><td class="num">%s</td><td class="num"%s><b>%s</b></td></tr>'%(
        urllib.parse.quote(j['id']),h(j['title'] or j['id']),h(j['date']),h(j['client']),h(j['tech']),money(j['sale']),money(j['labor']+j['parts']),' style="color:#c62828"' if j['profit']<0 else '',money(j['profit'])) for j in sorted(jobs,key=lambda j:j['date'],reverse=True))+'</table><a class="big-btn gray" href="/reports?%s&csv=jobs">Download job CSV</a>'%q
    return ops_page('Reports', body)

def qbo_lines(conn, row):
    try: pkg=json.loads((conn.execute('SELECT package FROM invoice_drafts WHERE job_id=?',(row['id'],)).fetchone() or {'package':'{}'})['package'] or '{}')
    except Exception: pkg={}
    if pkg.get('line_items'): return [{'description':str(l.get('description'))[:4000],'qty':fnum(l.get('qty'),1),'rate':round(fnum(l.get('rate')),2),'amount':round(fnum(l.get('amount')),2)} for l in pkg['line_items']]
    e=load_estimate(row)
    if e.get('signed_at') and 0<=e.get('chosen',-1)<len(e['options']):
        o=e['options'][e['chosen']]
        return [{'description':'%s option: %s'%(o['tier'],o['name'])+(' - '+'; '.join(o['items']) if o['items'] else ''),'qty':1,'rate':o['price'],'amount':o['price']}]
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
        if r and x['status'] in ('created','paid'):
            out+='<form method="post"><input type="hidden" name="action" value="paylink"><input type="hidden" name="job" value="%s"><label>QuickBooks pay link (shows a Pay Now button on the client portal)</label><input name="pay_link" value="%s" placeholder="https://connect.intuit.com/..."><button class="big-btn gray">Save Pay Link</button></form>'%(h(x['job_id']),h(r['pay_link'] or ''))
        return out+'</div>'
    for st,title in (('queued','Waiting for the bot'),('error','Problems'),('created','In QuickBooks, not paid'),('paid','Paid')):
        xs=[x for x in q.values() if x['status']==st]
        if xs: body+='<h2>%s</h2>'%title+''.join(row_html(x) for x in sorted(xs,key=lambda x:x['updated_at'] or '',reverse=True))
    return ops_page('QuickBooks Export', body, msg=msg)

@app.route('/api/qbo/pending')
def qbo_pending():
    bad=sync_key_ok()
    if bad: return bad
    st=request.args.get('status','queued')
    if st not in ('queued','created'): return jsonify({'error':'status must be queued or created'}),400
    conn=get_db(); rows=conn.execute('SELECT * FROM qbo_queue WHERE status=? ORDER BY queued_at',(st,)).fetchall(); conn.close()
    return jsonify([dict(json.loads(r['payload'] or '{}'),status=r['status'],qbo_invoice_id=r['qbo_invoice_id'],queued_at=r['queued_at']) for r in rows])

@app.route('/api/qbo/result', methods=['POST'])
def qbo_result():
    bad=sync_key_ok()
    if bad: return bad
    d=request.get_json(silent=True) or {}; jid=str(d.get('job_id') or ''); st=d.get('status')
    if st not in ('created','paid','error'): return jsonify({'error':'status must be created, paid or error'}),400
    conn=get_db(); x=conn.execute('SELECT * FROM qbo_queue WHERE job_id=?',(jid,)).fetchone()
    if not x: conn.close(); return jsonify({'error':'job not in queue'}),404
    link=str(d.get('pay_link') or '').strip()[:500]
    if link and not link.startswith('https://'): conn.close(); return jsonify({'error':'pay_link must be https'}),400
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
    d['photos']=[dict(p) for p in conn.execute("SELECT filename,photo_type FROM photos WHERE job_id=? AND photo_type NOT IN ('blueprint','help','plan','devphoto','daily') ORDER BY id",(r['id'],)).fetchall()]
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
    n=conn.execute('UPDATE jobs SET scheduled_date=?,tech=? WHERE id=?',(sd or None,tech,job_id)).rowcount; conn.commit(); conn.close()
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
{% if j.job_type == 'Survey' %}<h2>Site Survey</h2><div class="card">{% if j.survey.want_desc %}<div><b>What you want:</b></div><div style="white-space:pre-wrap">{{ j.survey.want_desc }}</div>{% endif %}
{% if j.survey.conditions %}<div style="margin-top:8px"><b>Site conditions:</b></div><div style="white-space:pre-wrap">{{ j.survey.conditions }}</div>{% endif %}</div>{% endif %}
{% if j.plans %}<h2>{{ 'Plan and Devices' if j.job_type == 'Survey' else 'As-Built Plan' }}</h2>{% for fn in j.plans %}<div class="plan"><img src="{{ signed(fn) }}" alt="plan">{% for d in j.devices if d.plan == fn %}<span class="pin" style="left:{{ d.x }}%;top:{{ d.y }}%;background:{{ DEV_COLORS[d.type] }}">{{ d.label }}</span>{% endfor %}</div>{% endfor %}
{% if j.devices %}<div><b>Device count:</b> {% for t,n in dev_counts(j.devices).items() %}{{ t }} x{{ n }}{% if not loop.last %}, {% endif %}{% endfor %}</div>
<table><tr><th>Label</th><th>Device</th><th>Note</th>{% if j.job_type != 'Survey' %}<th>Status</th>{% endif %}</tr>{% for d in j.devices %}<tr><td><b>{{ d.label }}</b></td><td>{{ d.type }}</td><td>{{ d.note }}</td>{% if j.job_type != 'Survey' %}<td>{{ 'Installed' if d.installed else 'Planned' }}</td>{% endif %}</tr>{% endfor %}</table>{% endif %}{% endif %}
{% if j.photos %}<h2>Photos</h2><div class="ph">{% for p in j.photos %}<a href="{{ signed(p.filename) }}" target="_blank"><img src="{{ signed(p.filename) }}" alt="photo"></a>{% endfor %}</div>{% endif %}
{% if j.job_type not in ('Survey','Office Task') and j.status == 'completed' and j.signature and j.signature.startswith('data:image/') %}<h2>Completion Signature</h2><img src="{{ j.signature }}" alt="signature" style="width:260px;max-width:100%;border:1px solid #ddd">{% if j.completed_at %}<div class="muted">Completed {{ j.completed_at[:10] }}</div>{% endif %}{% endif %}
{% if est.options %}<h2>Your Options</h2>{% if est.note %}<div class="card" style="white-space:pre-wrap">{{ est.note }}</div>{% endif %}
{% if est.signed_at %}{% set c = est.options[est.chosen] %}<div class="ok">&#10003; {{ est.signed_name }} approved the {{ c.tier }} option ({{ c.name }}, {{ money(c.price) }}) on {{ est.signed_at }}</div>
{% if est.signature and est.signature.startswith('data:image/png;base64,') %}<img src="{{ est.signature }}" alt="signature" style="width:260px;max-width:100%;border:1px solid #ddd;margin-top:6px">{% endif %}
{% else %}<form method="post" action="/hub/{{ j.id|urlencode }}/estimate" id="est-form" onsubmit="return estSubmit()"><input type="hidden" name="t" value="{{ t }}"><input type="hidden" name="choice" id="est-choice"><input type="hidden" name="signature" id="est-sig">
{% for o in est.options %}<div class="card opt" data-i="{{ loop.index0 }}" onclick="estPick(this.dataset.i)" style="cursor:pointer;border-width:2px"><div style="font-size:.9rem;color:#a49f2a;font-weight:700">{{ o.tier|upper }}</div><div style="font-size:1.2rem;font-weight:800">{{ o.name }}</div>
{% if o['items'] %}<ul style="margin:6px 0 6px 18px;padding:0">{% for x in o['items'] %}<li>{{ x }}</li>{% endfor %}</ul>{% endif %}<div style="font-size:1.4rem;font-weight:800">{{ money(o.price) }}</div><button type="button" class="btn" style="background:#a49f2a;margin-top:6px">Pick {{ o.tier }}</button></div>{% endfor %}
<div id="est-sign" style="display:none"><h2>Sign to approve</h2><div id="est-picked" style="font-weight:700"></div><input name="name" placeholder="Your full name" required maxlength="100">
<div class="muted">Sign with your finger below</div><canvas id="est-pad" width="600" height="200" style="width:100%;height:160px;border:2px solid #ccc;border-radius:10px;background:#fff;touch-action:none"></canvas>
<button type="button" style="background:#eee;color:#111;border:0;border-radius:10px;padding:12px;width:100%;font-size:1rem" onclick="estClear()">Clear signature</button>
<label style="display:flex;gap:10px;align-items:center;margin:12px 0;font-size:1.05rem"><input type="checkbox" name="agree" value="1" required style="width:28px;height:28px;margin:0"> I approve this option and price.</label>
<button class="btn" type="submit">Approve and Sign</button></div></form>
<script>
var estDrawn=false;function estPick(i){document.getElementById("est-choice").value=i;var cs=document.querySelectorAll(".opt");for(var k=0;k<cs.length;k++){cs[k].style.borderColor=(cs[k].dataset.i==i)?"#2e7d32":"#e5e5e5";cs[k].style.background=(cs[k].dataset.i==i)?"#e8f5e9":"#fff"}
var s=document.getElementById("est-sign");s.style.display="block";document.getElementById("est-picked").textContent="You picked: "+cs[i].querySelector("div:nth-child(2)").textContent;s.scrollIntoView({behavior:"smooth"})}
(function(){var c=document.getElementById("est-pad"),x=c.getContext("2d"),on=false;x.lineWidth=4;x.lineCap="round";x.strokeStyle="#111";
function p(e){var r=c.getBoundingClientRect();return [(e.clientX-r.left)*c.width/r.width,(e.clientY-r.top)*c.height/r.height]}
c.addEventListener("pointerdown",function(e){on=true;var q=p(e);x.beginPath();x.moveTo(q[0],q[1]);c.setPointerCapture(e.pointerId)});
c.addEventListener("pointermove",function(e){if(!on)return;var q=p(e);x.lineTo(q[0],q[1]);x.stroke();estDrawn=true});
c.addEventListener("pointerup",function(){on=false})})();
function estClear(){var c=document.getElementById("est-pad");c.getContext("2d").clearRect(0,0,c.width,c.height);estDrawn=false}
function estSubmit(){if(document.getElementById("est-choice").value===""){alert("Pick an option first");return false}if(!estDrawn){alert("Please sign in the box");return false}
document.getElementById("est-sig").value=document.getElementById("est-pad").toDataURL("image/png");return true}
</script>{% endif %}{% endif %}
{% if j.job_type == 'Survey' and not est.options %}<h2>Estimate</h2>{% if j.hub_approved_at %}<div class="ok">&#10003; Estimate approved by {{ j.hub_approved_name or 'client' }} on {{ j.hub_approved_at }}</div>
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
    return render_template_string(HUB_HTML, j=j, est=est, money=money, t=request.values.get('t',''), signed=lambda fn: '/help-photo/'+urllib.parse.quote(fn)+'?t='+help_photo_token(fn))

@app.route('/hub/<job_id>/approve', methods=['POST'])
def hub_approve(job_id):
    conn,row=hub_job(job_id)
    if not row: return (HUB_EXPIRED_HTML,410)
    if row['job_type']=='Survey' and not row['hub_approved_at']:
        conn.execute('UPDATE jobs SET hub_approved_at=?,hub_approved_name=? WHERE id=?',(now_local_str(),(request.form.get('name') or '').strip()[:100],job_id)); conn.commit()
    conn.close()
    return redirect('/hub/'+urllib.parse.quote(job_id)+'?t='+urllib.parse.quote(request.values.get('t','')))

TIERS = ['Good','Better','Best']
def load_estimate(row):
    try: e=json.loads(row['estimate_json'] or '{}')
    except Exception: e={}
    e=e if isinstance(e,dict) else {}
    opts=[]
    for o in (e.get('options') or [])[:3]:
        if isinstance(o,dict) and str(o.get('name') or '').strip():
            opts.append({'tier':str(o.get('tier') or '')[:10],'name':str(o.get('name'))[:120],'items':[str(x)[:200] for x in (o.get('items') or [])][:30],'price':round(fnum(o.get('price')),2)})
    e['options']=opts
    return e

@app.route('/job/<job_id>/estimate', methods=['GET','POST'])
def estimate_edit(job_id):
    if not is_office(): return redirect('/')
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row: conn.close(); return redirect('/')
    e=load_estimate(row); msg=''
    if request.method=='POST':
        if e.get('signed_at'): msg='Already signed. Clear it first to change.'
        if request.form.get('action')=='clear':
            e={'options':e['options'],'note':e.get('note','')}; msg='Signature cleared. You can edit the options.'
        elif not e.get('signed_at'):
            opts=[]
            for i,t in enumerate(TIERS):
                n=(request.form.get('name%d'%i) or '').strip()
                if n: opts.append({'tier':t,'name':n[:120],'items':[x.strip() for x in (request.form.get('items%d'%i) or '').splitlines() if x.strip()][:30],'price':max(0,round(fnum(request.form.get('price%d'%i)),2))})
            e={'options':opts,'note':(request.form.get('note') or '').strip()[:1000]}; msg='Saved. Send the client their portal link when ready.'
        conn.execute('UPDATE jobs SET estimate_json=? WHERE id=?',(json.dumps(e),job_id)); conn.commit()
    conn.close()
    body='<div class="muted">%s &middot; %s</div>'%(h(row['customer']),h(row['title']))
    if e.get('signed_at'):
        ch=e['options'][e['chosen']] if 0<=e.get('chosen',-1)<len(e['options']) else {}
        body+='<div class="card"><span class="okf">Approved</span> <b>%s: %s</b> %s<div>Signed by %s on %s</div><img src="%s" alt="signature" style="width:260px;max-width:100%%;border:1px solid #ddd;margin-top:6px"></div>'%(h(ch.get('tier')),h(ch.get('name')),money(ch.get('price')),h(e.get('signed_name')),h(e.get('signed_at')),h(e.get('signature') if str(e.get('signature','')).startswith('data:image/png;base64,') else ''))
        body+='<form method="post" onsubmit="return confirm(\'Clear the client signature?\')"><input type="hidden" name="action" value="clear"><button class="big-btn gray">Clear signature to edit</button></form>'
    else:
        body+='<form method="post">'
        for i,t in enumerate(TIERS):
            o=next((x for x in e['options'] if x['tier']==t),{})
            body+='<h2>%s</h2><label>Option name</label><input name="name%d" value="%s" placeholder="%s"><label>What is included, one per line</label><textarea name="items%d" rows="4">%s</textarea><label>Price $</label><input name="price%d" type="number" step="0.01" min="0" value="%s">'%(
                t,i,h(o.get('name','')),{'Good':'4 cameras, 1 NVR','Better':'6 cameras, 1 NVR, app setup','Best':'8 cameras, AI NVR, 2 year service'}[t],i,h('\n'.join(o.get('items',[]))),i,('%g'%o['price']) if o.get('price') else '')
        body+='<h2>Note to client</h2><textarea name="note" rows="3" placeholder="Optional">%s</textarea><button class="big-btn">Save Estimate</button></form>'%h(e.get('note',''))
        body+='<div class="muted">Leave an option name blank to skip it. The client sees these on their portal link and can pick one and sign. Nothing is sent from here.</div>'
    return ops_page('Estimate: Good, Better, Best', body, back='/?job='+urllib.parse.quote(job_id), msg=msg)

@app.route('/hub/<job_id>/estimate', methods=['POST'])
def hub_estimate_sign(job_id):
    conn,row=hub_job(job_id)
    if not row: return (HUB_EXPIRED_HTML,410)
    e=load_estimate(row); f=request.form; sig=(f.get('signature') or '')
    try: ch=int(f.get('choice'))
    except (TypeError, ValueError): ch=-1
    name=(f.get('name') or '').strip()[:100]
    if not e.get('signed_at') and 0<=ch<len(e['options']) and name and sig.startswith('data:image/png;base64,') and len(sig)<300000 and f.get('agree')=='1':
        e.update({'chosen':ch,'signed_name':name,'signature':sig,'signed_at':now_local_str(),'ip':(request.headers.get('X-Forwarded-For') or request.remote_addr or '')[:60]})
        pick=e['options'][ch]
        conn.execute('UPDATE jobs SET estimate_json=?,hub_approved_at=?,hub_approved_name=?,sale_amount=? WHERE id=?',(json.dumps(e),now_local_str(),name,pick['price'] or None,job_id))
        conn.execute('INSERT INTO job_updates (job_id,author,note,created_at) VALUES (?,?,?,?)',(job_id,'Client portal','%s approved and signed the %s option: %s'%(name,pick['tier'],pick['name']),now_local_str()))
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
{% if done %}<div class="job"><div style="font-weight:700">Fully signed copies</div>{% if not smtp %}<div style="color:#b00020;font-weight:700;margin:4px 0">Email not set up. Add SMTP settings in Render to email copies.</div>{% endif %}
{% for d in done %}<div style="border-top:1px solid #eee;padding:8px 0"><b>{{ d.sub|title }}</b> v{{ d.version }} &middot; fully signed {{ d.co_signed_at }}<div class="muted">Email: {{ d.email_status or 'Not sent' }}{% if d.email_at %} ({{ d.email_at }}){% endif %}</div>
<div style="display:flex;gap:8px"><a class="btn" href="/agreement/copy/{{ d.id }}">Open Copy</a><form method="post" action="/agreement/email/{{ d.id }}" style="flex:1;margin:0" onsubmit="return confirm('Email the signed copy to the sub and to you?')"><button class="btn gold" type="submit">Email copy</button></form></div></div>{% endfor %}</div>{% endif %}
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
{% if not standalone %}<div class="noprint"><button class="btn gold" onclick="print()">Print</button><a class="btn" href="/agreement">Back</a></div>{% endif %}
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
    return render_template_string(AGR_COPY_HTML, s=s, segs=agr_parse(a['body'] if a else ''), vals=vals, standalone=False)

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
    for mo,inv,paid in rows: conn.execute('INSERT OR REPLACE INTO qbo_monthly (month,invoiced,paid,synced_at,source) VALUES (?,?,?,?,?)',(mo,inv,paid,now,source))
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
<h2>Revenue (office only)</h2><a class="btn" href="/">Back to Jobs</a>
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
    for t in ('photos','job_updates','invoice_drafts','followups','msg_drafts','job_hours'): conn.execute(f'DELETE FROM {t} WHERE job_id=?',(job_id,))
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

def serve():
    port=int(os.environ.get('PORT',5000))
    try: from gunicorn.app.base import BaseApplication
    except ImportError: return app.run(host='0.0.0.0', port=port, threaded=True)
    class _G(BaseApplication):
        def load_config(self):
            for k,v in {'bind':'0.0.0.0:%d'%port,'workers':1,'worker_class':'gthread','threads':8,'timeout':120,'keepalive':5,'accesslog':'-'}.items(): self.cfg.set(k,v)
        def load(self): return app
    _G().run()

if __name__=='__main__':
    serve()
