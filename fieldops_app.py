from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect, g
import sqlite3, os, uuid, hashlib, hmac, time
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename

app = Flask(__name__)

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
        signature TEXT, materials TEXT)''')
    for col in ['signature TEXT', 'materials TEXT']:
        try: conn.execute(f'ALTER TABLE jobs ADD COLUMN {col}')
        except: pass
    conn.execute('''CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, filename TEXT,
        photo_type TEXT, uploaded_at TEXT)''')
    count = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    if count == 0:
        now = datetime.utcnow().isoformat()
        conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at) VALUES (?,?,?,?,?,?,?,?)',
                     ('TS-2026-0142','Access Control - Main Entrance','ABC Warehouse','1234 Industrial Blvd, Houston, TX','Install 2 Maglocks, 1 Aiphone, 1 Card Reader.','assigned','Mike',now))
        conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at) VALUES (?,?,?,?,?,?,?,?)',
                     ('TS-2026-0143','Camera Service Call','XYZ Retail','567 Commerce St, Houston, TX','Troubleshoot offline NVR.','assigned','Mike',now))
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
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()

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

@app.route('/api/me')
def me(): return jsonify({'name': g.user['name'], 'role': g.user['role']})

HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>True Sense Field</title>
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
textarea,input{width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem}
canvas#sig{display:block;border:2px dashed var(--border);border-radius:10px;background:#fafafa;width:100%;height:160px;box-sizing:border-box;touch-action:none;-ms-touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;-webkit-tap-highlight-color:transparent;overscroll-behavior:contain}
</style></head><body>
<div class="header">
  <img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px; margin:0 auto 4px; display:block;">
  <div class="sub">Field Ops</div>
  <div id="who" style="font-size:.85rem;color:#666;margin-top:4px"></div>
</div>
<div id="list"></div>
<div id="office" class="job"><div style="font-weight:600;margin-bottom:4px">Add New Job (Office)</div>
<input type="text" id="n-id" placeholder="Job ID (e.g. TS-2026-0144)">
<input type="text" id="n-title" placeholder="Title">
<input type="text" id="n-customer" placeholder="Customer">
<input type="text" id="n-address" placeholder="Address">
<textarea id="n-scope" placeholder="Scope" rows="3"></textarea>
<button class="big-btn" onclick="addJob()">Add Job</button></div>
<div id="detail" class="hidden">
<div class="job"><div class="job-id" id="d-id"></div><div id="d-title" style="font-weight:600;font-size:1.15rem"></div><div id="d-customer" style="color:#666"></div><div id="d-address" style="color:#666"></div><div id="d-scope" style="white-space:pre-wrap;margin-top:8px"></div></div>
<a id="directions" class="big-btn" target="_blank">Directions</a>
<a id="call" class="big-btn gray">Call Customer</a>
<div class="big-btn" onclick="document.getElementById('before').click()">Before Photos</div>
<input type="file" id="before" accept="image/*" multiple capture="environment" style="display:none">
<div id="before-preview" style="display:flex;gap:8px;flex-wrap:wrap"></div>
<div class="big-btn" onclick="document.getElementById('after').click()">After Photos</div>
<input type="file" id="after" accept="image/*" multiple capture="environment" style="display:none">
<div id="after-preview" style="display:flex;gap:8px;flex-wrap:wrap"></div>
<textarea id="notes" placeholder="Notes" rows="3"></textarea>
<input type="text" id="materials" placeholder="Materials used">
<div>Customer Signature</div><canvas id="sig"></canvas>
<button class="big-btn gray" onclick="clearSig()" style="padding:12px">Clear Signature</button>
<input type="number" id="hours" placeholder="Hours" step="0.25">
<input type="number" id="miles" placeholder="Miles" step="0.1">
<button class="big-btn green" id="finish">Finish Job</button>
<button class="big-btn gray" onclick="showList()">Back</button>
</div>
<script>
let currentJob=null;
// ---- Signature pad: touch on the canvas only draws, never scrolls ----
const canvas=document.getElementById('sig'); const ctx=canvas.getContext('2d');
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
async function load(){const r=await fetch('/api/jobs?status=assigned');const jobs=await r.json();
document.getElementById('list').innerHTML=jobs.map(j=>`<div class="job"><div class="job-id">${j.id}</div><div style="font-weight:600">${j.title}</div><div style="color:#666">${j.customer}</div><div style="color:#666">${j.address}</div><button class="big-btn" onclick="openJob('${j.id}')">Open Job</button></div>`).join('')||'<div class="job">No jobs</div>'}
async function openJob(id){const r=await fetch('/api/jobs/'+id);currentJob=await r.json();
document.getElementById('d-id').textContent=currentJob.id;document.getElementById('d-title').textContent=currentJob.title;
document.getElementById('d-customer').textContent=currentJob.customer;document.getElementById('d-address').textContent=currentJob.address;
document.getElementById('d-scope').textContent=currentJob.scope||'';
document.getElementById('directions').href='https://maps.google.com/?q='+encodeURIComponent(currentJob.address);
document.getElementById('list').classList.add('hidden');document.getElementById('office').classList.add('hidden');document.getElementById('detail').classList.remove('hidden');clearSig()}
function showList(){document.getElementById('detail').classList.add('hidden');document.getElementById('list').classList.remove('hidden');document.getElementById('office').classList.remove('hidden');load()}
function preview(inp,el){document.getElementById(el).innerHTML='';[...inp.files].forEach(f=>{const i=document.createElement('img');i.src=URL.createObjectURL(f);i.style.width='70px';i.style.height='70px';i.style.objectFit='cover';i.style.borderRadius='8px';document.getElementById(el).appendChild(i)})}
document.getElementById('before').onchange=e=>preview(e.target,'before-preview');
document.getElementById('after').onchange=e=>preview(e.target,'after-preview');
document.getElementById('finish').onclick=async()=>{const fd=new FormData();
fd.append('notes',document.getElementById('notes').value);fd.append('materials',document.getElementById('materials').value);
fd.append('hours',document.getElementById('hours').value||0);fd.append('miles',document.getElementById('miles').value||0);
if(sigDrawn)fd.append('signature',canvas.toDataURL('image/png'));
[...document.getElementById('before').files].forEach(f=>fd.append('before',f));
[...document.getElementById('after').files].forEach(f=>fd.append('after',f));
const r=await fetch('/api/jobs/'+currentJob.id+'/complete',{method:'POST',body:fd});
if(r.ok){alert('Job finished');showList()}else alert('Error')};
async function addJob(){const v=k=>document.getElementById('n-'+k).value.trim();
const body={id:v('id'),title:v('title'),customer:v('customer'),address:v('address'),scope:v('scope')};
if(!body.id||!body.title){alert('Job ID and title are required');return}
const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(r.ok){['id','title','customer','address','scope'].forEach(k=>document.getElementById('n-'+k).value='');load()}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}}
fetch('/api/me').then(r=>r.json()).then(u=>{document.getElementById('who').innerHTML='Signed in as '+u.name+' &middot; <a href="/logout" style="color:#a49f2a">Log out</a>';if(u.role!=='office')document.getElementById('office').classList.add('hidden')});
load();
</script></body></html>'''

@app.route('/')
def index(): return render_template_string(HTML)

@app.route('/api/jobs')
def list_jobs():
    status=request.args.get('status','assigned')
    conn=get_db(); rows=conn.execute('SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC',(status,)).fetchall(); conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/jobs', methods=['POST'])
def create_job():
    if not is_office(): return jsonify({'error':'office only'}),403
    d=request.get_json(silent=True) or request.form
    jid=(d.get('id') or '').strip(); title=(d.get('title') or '').strip()
    if not jid or not title: return jsonify({'error':'Job ID and title are required'}),400
    conn=get_db()
    if conn.execute('SELECT 1 FROM jobs WHERE id=?',(jid,)).fetchone():
        conn.close(); return jsonify({'error':'That Job ID already exists'}),409
    conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at) VALUES (?,?,?,?,?,?,?,?)',
                 (jid,title,(d.get('customer') or '').strip(),(d.get('address') or '').strip(),(d.get('scope') or '').strip(),'assigned',(d.get('tech') or 'Mike').strip(),datetime.utcnow().isoformat()))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'id':jid}),201

@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone(); conn.close()
    return jsonify(dict(row)) if row else ('',404)

@app.route('/api/jobs/<job_id>/complete', methods=['POST'])
def complete(job_id):
    notes=request.form.get('notes',''); materials=request.form.get('materials','')
    hours=float(request.form.get('hours') or 0); miles=float(request.form.get('miles') or 0)
    sig=request.form.get('signature','')
    if not sig.startswith('data:image/png;base64,') or len(sig)>2_000_000: sig=None
    minutes=int(hours*60)
    conn=get_db(); now=datetime.utcnow().isoformat()
    conn.execute('UPDATE jobs SET status=?,notes=?,materials=?,signature=COALESCE(?,signature),total_minutes=total_minutes+?,total_miles=total_miles+?,completed_at=? WHERE id=?',
                 ('completed',notes,materials,sig,minutes,miles,now,job_id))
    for key,ptype in [('before','before'),('after','after')]:
        for f in request.files.getlist(key):
            if f and f.filename:
                fn=f"{job_id}_{ptype}_{uuid.uuid4().hex[:8]}.jpg"
                f.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
                conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at) VALUES (?,?,?,?)',(job_id,fn,ptype,now))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',5000)))
