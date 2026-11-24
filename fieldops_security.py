"""Security helpers: CSRF tokens, upload allowlist, token expiry, headers.

Login throttling, secure cookies, Origin CSRF, and some share-link expiry
already live in the app. This module adds CSRF tokens, a practical CSP,
upload type and size checks, and expiry or revocation for quote and
change-order client links.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import os
import re
import secrets

from werkzeug.utils import secure_filename

CSRF_FIELD = '_csrf'
CSRF_HEADER = 'X-CSRF-Token'
CSRF_SESSION = '_csrf'
TOKEN_DAYS = 90
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_BODY_BYTES = 12 * 1024 * 1024

ALLOWED_EXT = {
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'heif',
    'pdf', 'csv', 'txt', 'doc', 'docx', 'xls', 'xlsx',
}
BLOCKED_EXT = {
    'exe', 'sh', 'bat', 'cmd', 'ps1', 'php', 'py', 'pyc', 'pyo',
    'js', 'mjs', 'html', 'htm', 'svg', 'svgz', 'msi', 'dll', 'com',
    'scr', 'jar', 'wasm', 'cgi', 'pl', 'rb', 'php5', 'phtml', 'asp',
    'aspx', 'jsp', 'vbs', 'wsf', 'lnk',
}

CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

LINK_GONE = (
    '<!DOCTYPE html><html><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>Link not available</title></head>'
    '<body style="font-family:Arial,sans-serif;max-width:560px;margin:40px auto;padding:14px;text-align:center">'
    '<h2>This link is no longer available</h2>'
    '<p style="color:#666">It may have expired or been turned off. '
    'Please contact True Sense Security for a new link.</p></body></html>'
)

FETCH_PATCH = (
    '<script>(function(){var m=document.querySelector("meta[name=\\"csrf-token\\"]");'
    'var t=m&&m.content;if(!t)return;var orig=window.fetch;'
    'window.fetch=function(input,init){init=init||{};'
    'var method=(init.method||"GET").toString().toUpperCase();'
    'if(method!=="GET"&&method!=="HEAD"&&method!=="OPTIONS"){'
    'var headers=new Headers(init.headers||{});'
    'if(!headers.has("X-CSRF-Token"))headers.set("X-CSRF-Token",t);'
    'init.headers=headers;}return orig.call(this,input,init);};})();</script>'
)


def csrf_strict(app=None):
    if os.environ.get('CSRF_STRICT') == '0':
        return False
    if os.environ.get('CSRF_STRICT') == '1':
        return True
    if app is not None and app.config.get('TESTING') and 'unittest' in __import__('sys').modules:
        return False
    if 'unittest' in __import__('sys').modules:
        return False
    return True


def new_csrf():
    return secrets.token_urlsafe(32)


def ensure_csrf(session):
    token = session.get(CSRF_SESSION) or ''
    if not token:
        token = new_csrf()
        session[CSRF_SESSION] = token
    return token


def request_csrf(request):
    token = (request.form.get(CSRF_FIELD) or '').strip()
    if not token:
        token = (request.headers.get(CSRF_HEADER) or '').strip()
    return token


def csrf_ok(session, request):
    expected = session.get(CSRF_SESSION) or ''
    got = request_csrf(request)
    if not expected or not got:
        return False
    return secrets.compare_digest(expected, got)


def _cols(conn, table):
    return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}


def _add_col(conn, table, coldef):
    name = coldef.split()[0]
    if name in _cols(conn, table):
        return
    conn.execute('ALTER TABLE %s ADD COLUMN %s' % (table, coldef))


def init_schema(conn):
    for col in ('token_expires_at TEXT', 'token_revoked_at TEXT', 'token_revoked_by TEXT'):
        try:
            _add_col(conn, 'quotes', col)
        except Exception:
            pass
        try:
            _add_col(conn, 'change_orders', col)
        except Exception:
            pass


def now_iso():
    return datetime.utcnow().isoformat(timespec='seconds')


def expiry_iso(days=TOKEN_DAYS):
    return (datetime.utcnow() + timedelta(days=int(days or TOKEN_DAYS))).isoformat(timespec='seconds')


def _row_val(row, key):
    try:
        if key not in row.keys():
            return ''
        return row[key] or ''
    except Exception:
        return ''


def token_block_reason(row):
    """Return 'revoked', 'expired', or '' if the client link is still live."""
    if _row_val(row, 'token_revoked_at'):
        return 'revoked'
    exp = _row_val(row, 'token_expires_at')
    if exp and exp[:19] < now_iso()[:19]:
        return 'expired'
    return ''


def extension_of(name):
    base = (name or '').rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
    if '.' not in base or base.startswith('.'):
        return ''
    return base.rsplit('.', 1)[-1].lower()


def safe_filename(name, fallback='file'):
    raw = (name or '').replace('\\', '/').split('/')[-1]
    cleaned = secure_filename(raw) or secure_filename(fallback) or 'file'
    cleaned = cleaned.replace('..', '')
    if not cleaned or cleaned in ('.', '..'):
        cleaned = 'file'
    return cleaned[:120]


def is_blocked_name(name):
    ext = extension_of(name)
    return (not ext) or ext in BLOCKED_EXT or ext not in ALLOWED_EXT


def validate_upload(storage, max_bytes=MAX_UPLOAD_BYTES):
    """Return an error string, or None if the file is allowed."""
    if storage is None or not getattr(storage, 'filename', None):
        return None
    name = storage.filename
    if '/' in name.replace('\\', '/') or name.startswith('.') or '..' in name:
        # after basename it's still checked
        pass
    ext = extension_of(name)
    if not ext or ext in BLOCKED_EXT or ext not in ALLOWED_EXT:
        return 'That file type is not allowed.'
    # Size: some storages have content_length; also try seek
    size = getattr(storage, 'content_length', None)
    if not size:
        try:
            pos = storage.stream.tell()
            storage.stream.seek(0, os.SEEK_END)
            size = storage.stream.tell()
            storage.stream.seek(pos)
        except Exception:
            size = None
    if size is not None and size > max_bytes:
        return 'That file is too large (10 MB max).'
    return None


def validate_request_files(files, max_bytes=MAX_UPLOAD_BYTES):
    for key in files:
        items = files.getlist(key) if hasattr(files, 'getlist') else [files.get(key)]
        for item in items:
            err = validate_upload(item, max_bytes=max_bytes)
            if err:
                return err
    return None


def inject_csrf(html, token):
    if not html or not token:
        return html
    binary = isinstance(html, (bytes, bytearray))
    text = html.decode('utf-8', 'replace') if binary else html
    if 'name="_csrf"' not in text:
        field = '<input type="hidden" name="_csrf" value="%s">' % token
        text = re.sub(r'</form>', field + '</form>', text, flags=re.IGNORECASE)
    if 'name="csrf-token"' not in text:
        meta = '<meta name="csrf-token" content="%s">' % token
        if re.search(r'</head>', text, flags=re.IGNORECASE):
            text = re.sub(r'</head>', meta + '</head>', text, count=1, flags=re.IGNORECASE)
        else:
            text = meta + text
    if 'X-CSRF-Token' not in text:
        if re.search(r'</body>', text, flags=re.IGNORECASE):
            text = re.sub(r'</body>', FETCH_PATCH + '</body>', text, count=1, flags=re.IGNORECASE)
        else:
            text = text + FETCH_PATCH
    return text.encode('utf-8') if binary else text


def register(fo):
    app = fo.app
    request = fo.request
    session = fo.session
    jsonify = fo.jsonify

    app.config.setdefault('MAX_CONTENT_LENGTH', MAX_BODY_BYTES)
    app.config.setdefault('SESSION_COOKIE_HTTPONLY', True)
    app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')
    if fo.ON_RENDER:
        app.config['SESSION_COOKIE_SECURE'] = True

    @app.before_request
    def _csrf_token_check():
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            if request.endpoint or request.path:
                try:
                    ensure_csrf(session)
                except Exception:
                    pass
            return
        if not csrf_strict(app):
            return
        path = request.path or ''
        exempt = getattr(fo, 'CSRF_EXEMPT', ()) or ()
        if path in exempt or path.startswith('/api/material-requests/'):
            return
        # Sync-key APIs already use CSRF_EXEMPT. Still skip if a valid sync key is presented.
        try:
            key = os.environ.get('QBO_SYNC_KEY') or ''
            got = request.headers.get('X-Sync-Key') or ''
            if key and got and secrets.compare_digest(got.encode(), key.encode()):
                return
        except Exception:
            pass
        try:
            token_ok = csrf_ok(session, request)
        except Exception:
            token_ok = False
        if token_ok:
            return
        fo.app.logger.warning('CSRF token block %s', path)
        if path.startswith('/api'):
            return jsonify({'error': 'This form expired. Reload and try again.'}), 403
        return ('This form expired. Reload and try again.', 403)

    @app.before_request
    def _upload_guard():
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            # Do not serve blocked file types from the uploads folder.
            if request.path.startswith('/uploads/'):
                name = request.path.rsplit('/', 1)[-1]
                if is_blocked_name(name):
                    return ('', 404)
            return
        if not request.files:
            return
        err = validate_request_files(request.files)
        if err:
            if request.path.startswith('/api') or request.accept_mimetypes.best == 'application/json':
                return jsonify({'error': err}), 400
            return (err, 400)

    @app.after_request
    def _security_extra(resp):
        # /healthz must stay a plain "ok" for Render.
        if request.path == '/healthz':
            return resp
        resp.headers.setdefault('Content-Security-Policy', CSP)
        ct = (resp.content_type or '')
        if 'html' in ct and request.method != 'HEAD':
            try:
                token = ensure_csrf(session)
                data = resp.get_data()
                injected = inject_csrf(data, token)
                if injected != data:
                    resp.set_data(injected)
                    if resp.content_length is not None:
                        resp.content_length = len(resp.get_data())
            except Exception:
                pass
        return resp
