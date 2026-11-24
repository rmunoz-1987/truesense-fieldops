"""Office Users page and database logins for Field Ops.

Env NAME_PASSWORD logins keep working and are listed read-only.
Database users can be added, reset, or deactivated by office staff.
Passwords are hashed with werkzeug and never stored in the clear.
Login lockout stays in fieldops_app. This module does not send email or texts.
"""
from __future__ import annotations

import re
import time

from werkzeug.security import check_password_hash, generate_password_hash

_CACHE = {'at': 0, 'users': {}}
CACHE_SECS = 2
ROLES = (('field', 'Field'), ('office', 'Office'))


def nkey(name):
    return ' '.join((name or '').split()).lower()


def nice(name):
    return ' '.join((name or '').split()).title()


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS app_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL DEFAULT 'field',
        password_hash TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT,
        updated_at TEXT,
        created_by TEXT
    )''')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_app_users_key ON app_users(name_key)')


def password_ok(pw):
    pw = pw or ''
    if len(pw) < 10:
        return False, 'Use at least 10 characters.'
    has_letter = any(c.isalpha() for c in pw)
    has_other = any(not c.isalpha() for c in pw)
    if not (has_letter and has_other):
        return False, 'Mix letters with numbers or symbols.'
    return True, None


def _bust():
    _CACHE['at'] = 0
    _CACHE['users'] = {}


def _load(conn=None):
    now = time.time()
    if _CACHE['users'] and now - _CACHE['at'] < CACHE_SECS:
        return _CACHE['users']
    rows = {}
    close = False
    if conn is None:
        import fieldops_app as fo
        conn = fo.get_db()
        close = True
    try:
        for r in conn.execute('SELECT * FROM app_users'):
            rows[r['name_key']] = {
                'id': r['id'], 'name': r['name'], 'name_key': r['name_key'],
                'role': r['role'], 'password_hash': r['password_hash'],
                'active': int(r['active'] or 0), 'created_at': r['created_at'],
                'updated_at': r['updated_at'], 'created_by': r['created_by'] or '',
            }
    except Exception:
        rows = {}
    finally:
        if close:
            conn.close()
    _CACHE['users'] = rows
    _CACHE['at'] = now
    return rows


def has_any():
    return bool(_load())


def active_user(name_key):
    u = _load().get(nkey(name_key))
    if u and u['active']:
        return u
    return None


def office_keys():
    return {k for k, u in _load().items() if u['active'] and u['role'] == 'office'}


def active_field_users():
    return [u for u in _load().values() if u['active'] and u['role'] == 'field']


def check_login(name, password):
    u = active_user(name)
    if not u or not u.get('password_hash'):
        return None, False
    try:
        ok = check_password_hash(u['password_hash'], password or '')
    except Exception:
        ok = False
    return (u if ok else None), ok


def env_entries(fo):
    """Read-only view of Render NAME_PASSWORD logins."""
    office = set(fo.OFFICE_NAMES)
    out = []
    for k in sorted(fo.USERS):
        out.append({
            'name': fo.nice_name(k),
            'name_key': k,
            'role': 'office' if k in office else 'field',
            'source': 'Render',
            'active': True,
        })
    return out


def add_user(conn, name, role, password, created_by, env_keys=None):
    name = nice(name)
    key = nkey(name)
    if not key:
        return None, 'Name is required.'
    if role not in ('field', 'office'):
        return None, 'Role must be Field or Office.'
    ok, err = password_ok(password)
    if not ok:
        return None, err
    if env_keys and key in env_keys:
        return None, 'That name already has a Render login. It stays as-is.'
    if conn.execute('SELECT 1 FROM app_users WHERE name_key=?', (key,)).fetchone():
        return None, 'A user with that name already exists.'
    when = _now()
    conn.execute(
        'INSERT INTO app_users (name,name_key,role,password_hash,active,created_at,updated_at,created_by) VALUES (?,?,?,?,1,?,?,?)',
        (name, key, role, generate_password_hash(password), when, when, (created_by or '')[:80]))
    _bust()
    return conn.execute('SELECT * FROM app_users WHERE name_key=?', (key,)).fetchone(), None


def set_password(conn, user_id, password):
    ok, err = password_ok(password)
    if not ok:
        return err
    row = conn.execute('SELECT id FROM app_users WHERE id=?', (user_id,)).fetchone()
    if not row:
        return 'User not found.'
    conn.execute(
        'UPDATE app_users SET password_hash=?, updated_at=? WHERE id=?',
        (generate_password_hash(password), _now(), user_id))
    _bust()
    return None


def set_active(conn, user_id, active, actor_key=None):
    row = conn.execute('SELECT * FROM app_users WHERE id=?', (user_id,)).fetchone()
    if not row:
        return 'User not found.'
    if actor_key and nkey(row['name']) == nkey(actor_key) and not active:
        return 'You cannot deactivate your own login.'
    conn.execute(
        'UPDATE app_users SET active=?, updated_at=? WHERE id=?',
        (1 if active else 0, _now(), user_id))
    _bust()
    return None


def set_role(conn, user_id, role, actor_key=None):
    if role not in ('field', 'office'):
        return 'Role must be Field or Office.'
    row = conn.execute('SELECT * FROM app_users WHERE id=?', (user_id,)).fetchone()
    if not row:
        return 'User not found.'
    if actor_key and nkey(row['name']) == nkey(actor_key) and role != 'office' and row['role'] == 'office':
        return 'You cannot remove your own office access.'
    conn.execute('UPDATE app_users SET role=?, updated_at=? WHERE id=?', (role, _now(), user_id))
    _bust()
    return None


def _now():
    from datetime import datetime
    return datetime.utcnow().isoformat()


def register(fo):
    from flask import request, redirect

    app = fo.app
    h = fo.h

    def office_only():
        if not fo.me():
            return redirect('/login')
        if not fo.is_office():
            return redirect('/')
        return None

    def users_page_body(msg=''):
        conn = fo.get_db()
        db_rows = list(conn.execute('SELECT * FROM app_users ORDER BY active DESC, name COLLATE NOCASE'))
        conn.close()
        env = env_entries(fo)
        body = ''
        if msg:
            body += '<div class="plat-msg">%s</div>' % h(msg)
        body += '<p class="muted">Render logins stay in place and cannot be edited here. Database users work alongside them. Login lockout still applies.</p>'
        body += '<div class="plat-panel"><h2>Render logins</h2>'
        if env:
            body += '<ul class="plat-list">'
            for e in env:
                body += (
                    '<li><b>%s</b> <span class="plat-pill">%s</span>'
                    '<div class="muted">Set in Render. Password is not shown. Role comes from OFFICE_NAMES.</div></li>'
                    % (h(e['name']), 'Office' if e['role'] == 'office' else 'Field'))
            body += '</ul>'
        else:
            body += '<div class="muted">No Render NAME_PASSWORD logins are set.</div>'
        body += '</div>'
        body += '<div class="plat-panel"><h2>Database users</h2>'
        if db_rows:
            body += '<ul class="plat-list">'
            for r in db_rows:
                st = 'Active' if r['active'] else 'Deactivated'
                body += '<li><b>%s</b> <span class="plat-pill">%s</span> <span class="muted">%s</span>' % (
                    h(r['name']), 'Office' if r['role'] == 'office' else 'Field', st)
                if r['active']:
                    body += (
                        '<div class="plat-mini-actions">'
                        '<a href="/users/%d/password">Set password</a>'
                        '<form method="post" action="/users/%d/deactivate" style="display:inline" onsubmit="return confirm(\'Deactivate %s? They will not be able to log in.\')">'
                        '<button type="submit" class="plat-link-btn">Deactivate</button></form></div>'
                    ) % (r['id'], r['id'], h(r['name']))
                else:
                    body += (
                        '<div class="plat-mini-actions">'
                        '<form method="post" action="/users/%d/activate" style="display:inline">'
                        '<button type="submit" class="plat-link-btn">Reactivate</button></form>'
                        '<a href="/users/%d/password">Set password</a></div>'
                    ) % (r['id'], r['id'])
                body += '</li>'
            body += '</ul>'
        else:
            body += '<div class="muted">No database users yet. Add one below.</div>'
        body += (
            '<form class="plat-form" method="post" action="/users" style="margin-top:16px">'
            '<h2 style="margin-top:0">Add user</h2>'
            '<label>Name</label><input name="name" required maxlength="80" autocomplete="off">'
            '<div class="plat-row"><div><label>Role</label><select name="role">'
            '<option value="field">Field</option><option value="office">Office</option>'
            '</select></div></div>'
            '<label>Password</label><input name="password" type="password" required autocomplete="new-password">'
            '<div class="muted">At least 10 characters, mix letters with numbers or symbols.</div>'
            '<button class="plat-btn plat-btn-gold" type="submit">Add user</button></form>'
        )
        body += '</div>'
        return body

    @app.route('/users', methods=['GET', 'POST'])
    def users_page():
        gate = office_only()
        if gate:
            return gate
        msg = ''
        if request.method == 'POST':
            conn = fo.get_db()
            _, err = add_user(
                conn,
                request.form.get('name'),
                request.form.get('role') or 'field',
                request.form.get('password') or '',
                fo.me(),
                env_keys=set(fo.USERS),
            )
            if err:
                msg = err
                conn.close()
            else:
                conn.commit(); conn.close()
                msg = 'User added.'
        body = users_page_body(msg)
        return fo.ops_page('Users',
            '<div class="plat-head"><div class="plat-head-text"><div class="plat-kicker">Office</div>'
            '<h1 class="plat-title">Users<span class="plat-dot">.</span></h1></div></div>' + body,
            back='/more')

    @app.route('/users/<int:uid>/password', methods=['GET', 'POST'])
    def users_password(uid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        row = conn.execute('SELECT * FROM app_users WHERE id=?', (uid,)).fetchone()
        if not row:
            conn.close()
            return redirect('/users')
        msg = ''
        if request.method == 'POST':
            err = set_password(conn, uid, request.form.get('password') or '')
            if err:
                msg = err
            else:
                conn.commit(); conn.close()
                return redirect('/users')
        conn.close()
        body = ''
        if msg:
            body += '<div class="plat-msg">%s</div>' % h(msg)
        body += (
            '<div class="plat-panel"><p>Set a new password for <b>%s</b>. The current password is never shown.</p>'
            '<form class="plat-form" method="post">'
            '<label>New password</label><input name="password" type="password" required autocomplete="new-password">'
            '<div class="muted">At least 10 characters, mix letters with numbers or symbols.</div>'
            '<button class="plat-btn plat-btn-gold" type="submit">Save password</button></form></div>'
        ) % h(row['name'])
        return fo.ops_page('Set password',
            '<div class="plat-head"><div class="plat-head-text"><h1 class="plat-title">Set password<span class="plat-dot">.</span></h1></div></div>' + body,
            back='/users')

    @app.route('/users/<int:uid>/deactivate', methods=['POST'])
    def users_deactivate(uid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        err = set_active(conn, uid, False, actor_key=fo.me())
        if not err:
            conn.commit()
        conn.close()
        return redirect('/users')

    @app.route('/users/<int:uid>/activate', methods=['POST'])
    def users_activate(uid):
        gate = office_only()
        if gate:
            return gate
        conn = fo.get_db()
        set_active(conn, uid, True, actor_key=fo.me())
        conn.commit(); conn.close()
        return redirect('/users')

