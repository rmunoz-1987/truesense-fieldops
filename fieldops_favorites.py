"""Per-user favorites for jobs, plan devices, and quote catalog lines."""
from __future__ import annotations
from datetime import datetime

KINDS = ('job', 'device', 'catalog')
# API and UI use plural collection names (jobs, devices). Catalog stays catalog.
LIST_KEYS = {'job': 'jobs', 'device': 'devices', 'catalog': 'catalog'}


def user_key(fo):
    return fo.ckey(fo.me() or '')


def init_schema(conn):
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS user_favorites (
             user_key TEXT NOT NULL,
             kind TEXT NOT NULL,
             item_key TEXT NOT NULL,
             created_at TEXT,
             PRIMARY KEY (user_key, kind, item_key)
           )''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_user_favorites_user ON user_favorites(user_key, kind)')


def list_for(conn, key, kind=None):
    init_schema(conn)
    if kind:
        rows = conn.execute(
            'SELECT item_key FROM user_favorites WHERE user_key=? AND kind=? ORDER BY created_at, item_key',
            (key, kind)).fetchall()
        return [r['item_key'] for r in rows]
    rows = conn.execute(
        'SELECT kind, item_key FROM user_favorites WHERE user_key=? ORDER BY created_at, item_key',
        (key,)).fetchall()
    out = {LIST_KEYS[k]: [] for k in KINDS}
    for r in rows:
        bucket = LIST_KEYS.get(r['kind'])
        if bucket:
            out[bucket].append(r['item_key'])
    return out


def is_fav(conn, key, kind, item_key):
    init_schema(conn)
    return bool(conn.execute(
        'SELECT 1 FROM user_favorites WHERE user_key=? AND kind=? AND item_key=?',
        (key, kind, item_key)).fetchone())


def set_fav(conn, key, kind, item_key, on, now=''):
    if kind not in KINDS:
        return 'Unknown favorite type.'
    item_key = (item_key or '').strip()[:200]
    if not key or not item_key:
        return 'Missing favorite.'
    init_schema(conn)
    if on:
        conn.execute(
            'INSERT OR IGNORE INTO user_favorites (user_key, kind, item_key, created_at) VALUES (?,?,?,?)',
            (key, kind, item_key, now))
    else:
        conn.execute(
            'DELETE FROM user_favorites WHERE user_key=? AND kind=? AND item_key=?',
            (key, kind, item_key))
    return None


def register(fo):
    app = fo.app
    request = fo.request
    jsonify = fo.jsonify

    @app.route('/api/favorites')
    def favorites_get():
        if not fo.me():
            return jsonify({'error': 'login required'}), 401
        conn = fo.get_db()
        data = list_for(conn, user_key(fo))
        conn.close()
        return jsonify(data)

    @app.route('/api/favorites', methods=['POST'])
    def favorites_set():
        if not fo.me():
            return jsonify({'error': 'login required'}), 401
        payload = request.get_json(silent=True) or {}
        kind = (payload.get('kind') or '').strip()
        item_key = (payload.get('item_key') or payload.get('id') or '').strip()
        on = payload.get('on')
        if on is None:
            on = payload.get('favorite', True)
        on = bool(on)
        if kind == 'catalog' and not fo.may_estimate():
            return jsonify({'error': 'office only'}), 403
        if kind not in KINDS:
            return jsonify({'error': 'Unknown favorite type.'}), 400
        conn = fo.get_db()
        err = set_fav(conn, user_key(fo), kind, item_key, on, datetime.utcnow().isoformat())
        if err:
            conn.close()
            return jsonify({'error': err}), 400
        data = list_for(conn, user_key(fo))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'on': on, **data})
