from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect
import sqlite3, os, uuid, hmac, hashlib, json
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

def job_dict(row):
    d = dict(row); d['checklist'] = parse_checklist(d.get('checklist')); return d

def me():
    n = (session.get('name') or '').strip()
    return n if n.lower() in USERS else ''
def is_office(): return me().lower() in OFFICE_NAMES
def can_see(job): return is_office() or (job['tech'] or '').strip().lower() == me().lower()

LOGIN_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>True Sense Field</title>
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
    if request.path in ('/login', '/healthz'): return
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
    for col in ['signature TEXT', 'materials TEXT', 'phone TEXT', 'checklist TEXT', 'scheduled_date TEXT', 'created_by TEXT']:
        try: conn.execute(f'ALTER TABLE jobs ADD COLUMN {col}')
        except: pass
    conn.execute('''CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, filename TEXT,
        photo_type TEXT, uploaded_at TEXT)''')
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
    conn.commit(); conn.close()

init_db()

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
.chk{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border);font-size:1rem}
.chk input{width:24px;height:24px;margin:0;accent-color:var(--accent)}
.day{font-weight:700;color:var(--accent);margin:16px 0 6px}
textarea,input{width:100%;padding:14px;border:1px solid var(--border);border-radius:10px;margin:8px 0;font-size:1rem}
canvas{touch-action:none;-webkit-user-select:none;user-select:none;border:2px dashed var(--border);border-radius:10px;background:#fafafa;width:100%;height:160px}
</style></head><body>
<div class="header">
  <img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px; margin:0 auto 4px; display:block;">
  <div class="sub">Field Ops</div>
  <div style="font-size:.85rem;color:#666;margin-top:4px">{{ name }} &middot; <a href="/logout" style="color:#666">Log out</a></div>
</div>
{% if office %}<a href="/clients" class="big-btn gray">Clients</a>{% endif %}
<div id="list"></div>
{% if office %}<div id="office" class="job"><div style="font-weight:600;margin-bottom:4px">Add New Job (Office)</div>
<input type="text" id="n-id" placeholder="Job ID (e.g. TS-2026-0144)">
<input type="text" id="n-title" placeholder="Title">
<input type="text" id="n-customer" placeholder="Customer">
<input type="text" id="n-address" placeholder="Address">
<input type="tel" id="n-phone" placeholder="Customer phone">
<input type="text" id="n-tech" placeholder="Tech name (e.g. Mike)">
<textarea id="n-scope" placeholder="Scope" rows="3"></textarea>
<div style="font-size:.85rem;color:#666">Scheduled date</div><input type="date" id="n-date">
<textarea id="n-checklist" placeholder="Checklist, one item per line (optional)" rows="3"></textarea>
<button class="big-btn" onclick="addJob()">Add Job</button></div>{% else %}<div id="office" class="hidden"></div>{% endif %}
<div id="detail" class="hidden">
<div class="job"><div class="job-id" id="d-id"></div><div id="d-title" style="font-weight:600;font-size:1.15rem"></div><div id="d-customer" style="color:#666"></div><div id="d-address" style="color:#666"></div><div id="d-scope" style="white-space:pre-wrap;margin-top:8px"></div><div id="d-created" style="color:#666;font-size:.85rem;margin-top:6px"></div></div>
<a id="directions" class="big-btn" target="_blank">Directions</a>
<a id="call" class="big-btn gray">Call Customer</a>
<a id="history" class="big-btn gray">Client History</a>
<div class="big-btn" onclick="document.getElementById('before').click()">Before Photos</div>
<input type="file" id="before" accept="image/*" multiple capture="environment" style="display:none">
<div id="before-preview" style="display:flex;gap:8px;flex-wrap:wrap"></div>
<div class="big-btn" onclick="document.getElementById('after').click()">After Photos</div>
<input type="file" id="after" accept="image/*" multiple capture="environment" style="display:none">
<div id="after-preview" style="display:flex;gap:8px;flex-wrap:wrap"></div>
<div class="job"><div style="font-weight:600;margin-bottom:6px">Checklist</div><div id="checklist"></div></div>
<div class="job"><div style="font-weight:600;margin-bottom:6px">Daily Updates</div><div id="updates"></div>
<textarea id="update-note" placeholder="Today's progress, issues, materials needed" rows="3"></textarea>
<button class="big-btn" onclick="saveUpdate()">Save Daily Update</button></div>
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
async function load(){const r=await fetch('/api/jobs?status=assigned');const jobs=await r.json();
const card=j=>`<div class="job"><div class="job-id">${j.id}</div><div style="font-weight:600">${j.title}</div><div style="color:#666">${j.customer}</div><div style="color:#666">${j.address}</div><button class="big-btn" onclick="openJob('${j.id}')">Open Job</button></div>`;
const groups={};jobs.forEach(j=>{const k=j.scheduled_date||'';(groups[k]=groups[k]||[]).push(j)});
const keys=Object.keys(groups).sort((a,b)=>a===''?1:b===''?-1:a.localeCompare(b));
document.getElementById('list').innerHTML=keys.map(k=>`<div class="day">${dayLabel(k)}</div>`+groups[k].map(card).join('')).join('')||'<div class="job">No jobs</div>'}
function ymd(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function dayLabel(k){if(!k)return 'No date';const t=new Date();const tm=new Date();tm.setDate(t.getDate()+1);
const d=new Date(k+'T12:00:00');const nice=d.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'});
if(k===ymd(t))return 'Today, '+nice;if(k===ymd(tm))return 'Tomorrow, '+nice;if(k<ymd(t))return 'Overdue, '+nice;return nice}
function renderChecklist(){document.getElementById('checklist').innerHTML=(currentJob.checklist||[]).map((c,i)=>`<label class="chk"><input type="checkbox" ${c.done?'checked':''} onchange="tick(${i},this.checked)"><span></span></label>`).join('');
[...document.querySelectorAll('#checklist .chk span')].forEach((el,i)=>el.textContent=currentJob.checklist[i].text)}
async function tick(i,v){currentJob.checklist[i].done=v;await fetch('/api/jobs/'+currentJob.id+'/checklist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({checklist:currentJob.checklist})})}
async function loadUpdates(){const r=await fetch('/api/jobs/'+currentJob.id+'/updates');const u=r.ok?await r.json():[];const el=document.getElementById('updates');el.innerHTML='';
u.forEach(x=>{const d=document.createElement('div');d.style.cssText='padding:8px 0;border-bottom:1px solid #e5e5e5';const m=document.createElement('div');m.style.cssText='font-size:.8rem;color:#666';m.textContent=x.created_at+' \u00b7 '+x.author;const t=document.createElement('div');t.style.whiteSpace='pre-wrap';t.textContent=x.note;d.append(m,t);el.appendChild(d)});
if(!u.length)el.innerHTML='<div style="color:#666">No updates yet</div>'}
async function saveUpdate(){const n=document.getElementById('update-note');if(!n.value.trim())return;
const r=await fetch('/api/jobs/'+currentJob.id+'/updates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:n.value})});
if(r.ok){n.value='';loadUpdates()}else alert('Error')}
async function openJob(id){const r=await fetch('/api/jobs/'+id);currentJob=await r.json();
document.getElementById('d-id').textContent=currentJob.id;document.getElementById('d-title').textContent=currentJob.title;
document.getElementById('d-customer').textContent=currentJob.customer;document.getElementById('d-address').textContent=currentJob.address;
document.getElementById('d-scope').textContent=currentJob.scope||'';document.getElementById('d-created').textContent=currentJob.created_by?'Created by '+currentJob.created_by:'';renderChecklist();loadUpdates();document.getElementById('update-note').value='';
const c=document.getElementById('call');const ph=(currentJob.phone||'').replace(/[^0-9+]/g,'');if(ph){c.href='tel:'+ph;c.textContent='Call Customer ('+currentJob.phone+')';c.style.display='block'}else{c.removeAttribute('href');c.style.display='none'}
document.getElementById('history').href='/clients?name='+encodeURIComponent(currentJob.customer||'');
document.getElementById('directions').href='https://maps.google.com/?q='+encodeURIComponent(currentJob.address);
document.getElementById('list').classList.add('hidden');document.getElementById('office').classList.add('hidden');document.getElementById('detail').classList.remove('hidden');clearSig()}
function showList(){document.getElementById('detail').classList.add('hidden');document.getElementById('list').classList.remove('hidden');document.getElementById('office').classList.remove('hidden');load()}
function preview(inp,el){document.getElementById(el).innerHTML='';[...inp.files].forEach(f=>{const i=document.createElement('img');i.src=URL.createObjectURL(f);i.style.width='70px';i.style.height='70px';i.style.objectFit='cover';i.style.borderRadius='8px';document.getElementById(el).appendChild(i)})}
document.getElementById('before').onchange=e=>preview(e.target,'before-preview');
document.getElementById('after').onchange=e=>preview(e.target,'after-preview');
document.getElementById('finish').onclick=async()=>{const fd=new FormData();
fd.append('notes',document.getElementById('notes').value);fd.append('materials',document.getElementById('materials').value);
fd.append('hours',document.getElementById('hours').value||0);fd.append('miles',document.getElementById('miles').value||0);
fd.append('checklist',JSON.stringify(currentJob.checklist||[]));fd.append('signature',canvas.toDataURL());
[...document.getElementById('before').files].forEach(f=>fd.append('before',f));
[...document.getElementById('after').files].forEach(f=>fd.append('after',f));
const r=await fetch('/api/jobs/'+currentJob.id+'/complete',{method:'POST',body:fd});
if(r.ok){alert('Job finished');showList()}else alert('Error')};
async function addJob(){const v=k=>document.getElementById('n-'+k).value.trim();
const body={id:v('id'),title:v('title'),customer:v('customer'),address:v('address'),phone:v('phone'),tech:v('tech'),scope:v('scope'),scheduled_date:v('date'),checklist:v('checklist')};
if(!body.id||!body.title){alert('Job ID and title are required');return}
const r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(r.ok){['id','title','customer','address','phone','tech','scope','date','checklist'].forEach(k=>document.getElementById('n-'+k).value='');load()}else{const e=await r.json().catch(()=>({}));alert(e.error||'Error')}}
load();
</script></body></html>'''

@app.route('/')
def index(): return render_template_string(HTML, name=me(), office=is_office())

@app.route('/api/jobs')
def list_jobs():
    status=request.args.get('status','assigned')
    conn=get_db()
    if status=='completed': rows=conn.execute("SELECT * FROM jobs WHERE status='completed' ORDER BY completed_at DESC").fetchall()
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
    conn.execute('INSERT INTO jobs (id,title,customer,address,scope,status,tech,created_at,phone,checklist,scheduled_date,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (jid,title,(d.get('customer') or '').strip(),(d.get('address') or '').strip(),(d.get('scope') or '').strip(),'assigned',(d.get('tech') or 'Mike').strip(),datetime.utcnow().isoformat(),(d.get('phone') or '').strip(),chk,sd,me()))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'id':jid}),201

@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    conn=get_db(); row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone(); conn.close()
    if not row: return ('',404)
    if not can_see(row): return jsonify({'error':'not your job'}),403
    return jsonify(job_dict(row))

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
    conn.execute('UPDATE jobs SET status=?,notes=?,materials=?,signature=?,total_minutes=total_minutes+?,total_miles=total_miles+?,completed_at=? WHERE id=?',
                 ('completed',notes,materials,sig,minutes,miles,now,job_id))
    for key,ptype in [('before','before'),('after','after')]:
        for f in request.files.getlist(key):
            if f and f.filename:
                fn=f"{job_id}_{ptype}_{uuid.uuid4().hex[:8]}.jpg"
                f.save(os.path.join(app.config['UPLOAD_FOLDER'],fn))
                conn.execute('INSERT INTO photos (job_id,filename,photo_type,uploaded_at) VALUES (?,?,?,?)',(job_id,fn,ptype,now))
    conn.commit()
    j=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    photos=[{'filename':p['filename'],'type':p['photo_type']} for p in conn.execute('SELECT filename,photo_type FROM photos WHERE job_id=?',(job_id,)).fetchall()]
    pkg={'job_id':job_id,'customer':j['customer'],'phone':j['phone'],'address':j['address'],'title':j['title'],'scope':j['scope'],
         'tech':j['tech'],'materials':j['materials'] or '','hours':round((j['total_minutes'] or 0)/60,2),'miles':j['total_miles'] or 0,
         'notes':j['notes'] or '','photos':photos,'completed_at':j['completed_at'],'status':'ready_for_review (not sent)'}
    conn.execute('INSERT OR REPLACE INTO invoice_drafts (job_id,package,status,created_at) VALUES (?,?,?,?)',(job_id,json.dumps(pkg),'ready_for_review',now))
    conn.commit(); conn.close()
    app.logger.warning('INVOICE_DRAFT_READY %s', json.dumps(pkg))
    return jsonify({'ok':True,'invoice_draft':pkg})

CLIENTS_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Clients</title>
<style>body{font-family:-apple-system,sans-serif;background:#fcfcfc;color:#111;max-width:480px;margin:auto;padding:12px}
.job{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:16px;margin-bottom:12px}
.job-id{color:#a49f2a;font-weight:700;font-size:.8rem}a{color:#111}.muted{color:#666}
.btn{display:block;background:#f0f0f0;color:#111;padding:14px;border-radius:10px;text-align:center;text-decoration:none;font-weight:600;margin:8px 0}
.client{display:block;text-decoration:none}h2{color:#a49f2a;margin:8px 0 12px}img{width:80px;height:80px;object-fit:cover;border-radius:8px;margin:4px 4px 0 0}
.sec{font-weight:600;margin-top:10px;font-size:.9rem}.folder{font-weight:700;color:#a49f2a;margin:16px 0 6px}summary{cursor:pointer}</style></head><body>
<img src="https://images.squarespace-cdn.com/content/v1/67d276f4d07a612e1c237f44/17e784b4-e371-44a7-a3b5-2c26669c7fc0/Logo.png?format=500w" alt="True Sense Security" style="height:48px;width:auto;object-fit:contain;margin:0 auto 4px;display:block;border-radius:0">
{% macro jobcard(j, open) %}<details class="job"{% if open %} open{% endif %}><summary><span class="job-id">{{ j.id }}</span> <b>{{ j.title }}</b><div class="muted" style="font-size:.85rem">Tech: {{ j.tech or '-' }}{% if j.scheduled_date %} &middot; {{ j.scheduled_date }}{% endif %}{% if j.completed_at %} &middot; Done {{ j.completed_at[:10] }}{% endif %}{% if j.created_by %} &middot; Created by {{ j.created_by }}{% endif %}</div></summary>
{% if j.scope %}<div class="sec">Scope</div><div style="white-space:pre-wrap">{{ j.scope }}</div>{% endif %}
<div class="sec">Daily Updates ({{ j.updates|length }})</div>{% for u in j.updates %}<div style="padding:4px 0;border-bottom:1px solid #eee"><div class="muted" style="font-size:.8rem">{{ u.created_at }} &middot; {{ u.author }}</div><div style="white-space:pre-wrap">{{ u.note }}</div></div>{% else %}<div class="muted">None</div>{% endfor %}
<div class="sec">Notes</div><div style="white-space:pre-wrap">{{ j.notes or '' }}</div>{% if not j.notes %}<div class="muted">None</div>{% endif %}
{% if j.materials %}<div class="sec">Materials</div><div>{{ j.materials }}</div>{% endif %}
<div class="sec">Checklist ({{ j.checklist|selectattr('done')|list|length }}/{{ j.checklist|length }})</div>{% for c in j.checklist %}<div>{{ '&#9745;'|safe if c.done else '&#9744;'|safe }} {{ c.text }}</div>{% endfor %}
<div class="sec">Photos ({{ j.photos|length }})</div>{% for p in j.photos %}<a href="/uploads/{{ p.filename }}" target="_blank"><img src="/uploads/{{ p.filename }}" alt="{{ p.photo_type }}"></a>{% else %}<div class="muted">None</div>{% endfor %}
</details>{% endmacro %}
{% if client is none %}<h2>Clients</h2><a class="btn" href="/">Back to Jobs</a>
{% for c in clients %}<a class="job client" href="/clients?name={{ c.customer|urlencode }}"><div style="font-weight:600">&#128193; {{ c.customer }}</div>
<div class="muted">{{ c.address or '' }}</div><div class="muted">{{ c.jobs }} job{{ '' if c.jobs == 1 else 's' }}, {{ c.active }} active</div></a>
{% else %}<div class="job">No clients yet</div>{% endfor %}
{% else %}<h2>&#128193; {{ client }}</h2>{% if office %}<a class="btn" href="/clients">Back to Clients</a>{% else %}<a class="btn" href="/">Back to Jobs</a><div class="muted" style="margin-bottom:8px">Read-only client history</div>{% endif %}
{% set active = jobs|rejectattr('status','equalto','completed')|list %}{% set done = jobs|selectattr('status','equalto','completed')|list %}
<div class="job">{% if info.address %}<div>{{ info.address }}</div>{% endif %}{% if info.phone %}<div><a href="tel:{{ info.phone }}">{{ info.phone }}</a></div>{% endif %}
<div class="muted" style="margin-top:4px">{{ active|length }} active &middot; {{ done|length }} completed</div></div>
<div class="folder">Active Jobs</div>{% for j in active %}{{ jobcard(j, true) }}{% else %}<div class="muted" style="margin-bottom:12px">None</div>{% endfor %}
<div class="folder">Completed Jobs</div>{% for j in done %}{{ jobcard(j, false) }}{% else %}<div class="muted">None</div>{% endfor %}
{% endif %}
</body></html>'''

def tech_has_client(conn, customer):
    return any(can_see(r) for r in conn.execute('SELECT tech FROM jobs WHERE customer=?',(customer,)).fetchall())

@app.route('/clients')
def clients_page():
    conn=get_db(); name=request.args.get('name')
    if not is_office() and (name is None or not tech_has_client(conn, name)): conn.close(); return redirect('/')
    if name is None:
        rows=conn.execute("SELECT customer, MAX(address) AS address, COUNT(*) AS jobs, SUM(status!='completed') AS active FROM jobs WHERE IFNULL(customer,'')!='' GROUP BY customer ORDER BY LOWER(customer)").fetchall()
        conn.close(); return render_template_string(CLIENTS_HTML, client=None, clients=rows)
    rows=conn.execute("SELECT * FROM jobs WHERE customer=? ORDER BY (status='completed'), COALESCE(scheduled_date, created_at) DESC",(name,)).fetchall()
    jobs=[]
    for r in rows:
        d=job_dict(r); d['updates']=[dict(u) for u in conn.execute('SELECT author,note,created_at FROM job_updates WHERE job_id=? ORDER BY id DESC',(r['id'],)).fetchall()]; d['photos']=[dict(p) for p in conn.execute('SELECT filename,photo_type FROM photos WHERE job_id=? ORDER BY id',(r['id'],)).fetchall()]; jobs.append(d)
    conn.close()
    info={'address':next((j['address'] for j in jobs if j['address']),''),'phone':next((j['phone'] for j in jobs if j['phone']),'')}
    return render_template_string(CLIENTS_HTML, client=name, jobs=jobs, info=info, office=is_office())

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
