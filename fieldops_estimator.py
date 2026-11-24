"""Estimator role: trimmed workspace and server-side route gates.

Estimators can work Sales, Quotes, Customers, Floor Plans, surveys, and the
catalog (read plus favorites). Money, billing, QBO, Users, Settings, and
other office tools return 403. Quotes they write are never sent to clients.
"""
from __future__ import annotations

from flask import request, jsonify, render_template_string

# Paths and prefixes an estimator may use. Everything else is 403.
ALLOW_EXACT = {
    '/', '/more', '/me', '/search', '/quote', '/catalog', '/customers',
    '/sales', '/clients', '/new-job', '/tasks', '/opportunities',
}

ALLOW_PREFIXES = (
    '/static/', '/uploads/', '/quotes', '/quote', '/customers/', '/contacts/',
    '/sites/', '/systems', '/clients', '/sales', '/opportunities', '/tasks',
    '/activities', '/floor-plans', '/design/', '/me', '/search',
    '/api/me/theme', '/api/favorites', '/api/followups', '/api/today',
    '/api/quick', '/api/opportunities/', '/api/push/',
)

# Job APIs used for surveys and the plan designer. Money and convert-to-job stay blocked.
JOB_API_OK = {
    'survey', 'photos', 'plans', 'devices', 'map', 'areas', 'blueprints',
    'site', 'updates', 'quick-notes', 'checklist', 'complete', 'status', 'followups',
}

SEND_ACTIONS = ('link', 'revoke_link')

BLOCKED_FOR_TESTS = (
    '/revenue', '/reports', '/money', '/qbo', '/timelog', '/compliance',
    '/compliance/1099', '/techs', '/users', '/settings', '/to-order',
    '/orders', '/plans', '/costing', '/timesheets', '/jobs', '/schedule',
    '/history', '/calendar', '/checklists', '/safety', '/equipment',
    '/stock', '/tickets', '/change-orders',
)

FORBIDDEN_MSG = 'This is an office area. It is not part of the estimator workspace.'

FORBIDDEN_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Office only</title>
<style>body{font-family:-apple-system,Arial,sans-serif;max-width:520px;margin:48px auto;padding:20px;color:#111;background:#f7f6ef}
h1{font-size:1.6rem;margin:0 0 12px}p{line-height:1.45}a{color:#6b6a16;font-weight:700}</style></head>
<body><h1>Office only</h1><p>This page is for the office. It is not part of the estimator workspace.</p>
<p><a href="/">Back to Today</a></p></body></html>'''


def _job_api_allowed(path, method):
    rest = path[len('/api/jobs/'):]
    if not rest:
        return False
    if '/' not in rest:
        # GET one job, or POST /api/jobs already handled as collection.
        return method == 'GET'
    first = rest.split('/', 1)[1].split('/', 1)[0]
    if method == 'GET':
        return first in JOB_API_OK or first in ('steps', 'followups')
    if method in ('POST', 'DELETE'):
        return first in JOB_API_OK
    return False


def estimator_allowed(path, method='GET', action=None):
    """Return True if an estimator may hit this path."""
    method = (method or 'GET').upper()
    path = path or '/'
    low = path.lower()
    if '/portal' in low or '/invoice' in low or path.endswith('/sale') or '/sale' in path.split('/')[-1:]:
        if path.startswith('/api/jobs/') and path.rstrip('/').endswith('/sale'):
            return False
        if '/portal' in low or '/invoice' in low:
            return False
    if path in ('/login', '/logout', '/healthz', '/sw.js', '/outbox.js',
                '/manifest.webmanifest', '/offline') or path.startswith('/static/'):
        return True
    if path == '/catalog' or path.startswith('/catalog/'):
        return method in ('GET', 'HEAD', 'OPTIONS')
    if method == 'POST' and path.startswith('/quotes/'):
        if (action or '') in SEND_ACTIONS:
            return False
    if path in ALLOW_EXACT:
        return True
    if path == '/api/jobs':
        return True
    if path.startswith('/api/jobs/'):
        return _job_api_allowed(path, method)
    for p in ALLOW_PREFIXES:
        if path == p.rstrip('/') or path.startswith(p):
            return True
    return False


def forbidden_response():
    if path_wants_json():
        return jsonify({'error': FORBIDDEN_MSG}), 403
    return FORBIDDEN_HTML, 403, {'Content-Type': 'text/html; charset=utf-8'}


def path_wants_json():
    if (request.path or '').startswith('/api'):
        return True
    best = request.accept_mimetypes.best
    return best == 'application/json' and 'text/html' not in request.accept_mimetypes


def register(fo):
    app = fo.app

    @app.before_request
    def estimator_guard():
        if not fo.is_estimator():
            return
        action = None
        try:
            if request.form:
                action = request.form.get('action')
        except Exception:
            action = None
        if estimator_allowed(request.path, request.method, action):
            return
        return forbidden_response()
