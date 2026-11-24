"""Nightly off-site backup: consistent SQLite copy plus uploads, pulled by key.

The live app already writes a daily local snapshot on the Render disk (7 days).
This endpoint packages a consistent SQLite backup and the uploads folder so a
scheduled script can pull a copy off-site. Auth is a shared BACKUP_KEY, not a
login. The key is never logged.
"""
from __future__ import annotations

import hmac
import os
import sqlite3
import tarfile
import tempfile
from datetime import date
from functools import wraps
from io import BytesIO

from flask import abort, request, send_file

ROUTE = '/api/office/backup'
HEADER = 'X-Backup-Key'


def _configured_key():
    return (os.environ.get('BACKUP_KEY') or '').strip()


def key_matches(got, expected):
    got = (got or '').strip()
    expected = (expected or '').strip()
    if not got or not expected or len(got) != len(expected):
        return False
    return hmac.compare_digest(got.encode('utf-8'), expected.encode('utf-8'))


def require_backup_key(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        expected = _configured_key()
        if not expected:
            abort(503)
        got = (request.headers.get(HEADER) or '').strip()
        if not key_matches(got, expected):
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def sqlite_backup(db_path, dest_path):
    os.makedirs(os.path.dirname(dest_path) or '.', exist_ok=True)
    tmp = dest_path + '.tmp'
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        try:
            dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.Error:
            pass
    finally:
        dst.close()
        src.close()
    os.replace(tmp, dest_path)
    return dest_path


def pack_archive(db_path, upload_folder, dest_path=None):
    """Write a gzip tarball with fieldops.db and, if present, uploads/."""
    work = tempfile.mkdtemp(prefix='fo-bak-')
    try:
        db_out = os.path.join(work, 'fieldops.db')
        sqlite_backup(db_path, db_out)
        if dest_path:
            out = dest_path
            os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
        else:
            out = os.path.join(work, 'bundle.tgz')
        with tarfile.open(out, 'w:gz') as tar:
            tar.add(db_out, arcname='fieldops.db')
            if upload_folder and os.path.isdir(upload_folder):
                tar.add(upload_folder, arcname='uploads')
        if dest_path:
            return dest_path
        with open(out, 'rb') as f:
            return f.read()
    finally:
        for name in os.listdir(work):
            try:
                path = os.path.join(work, name)
                if os.path.isdir(path):
                    continue
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass


def restore_archive(archive_path, dest_dir):
    """Extract a backup tarball into dest_dir. Returns paths to db and uploads."""
    os.makedirs(dest_dir, exist_ok=True)
    with tarfile.open(archive_path, 'r:gz') as tar:
        tar.extractall(dest_dir, filter='data')
    db = os.path.join(dest_dir, 'fieldops.db')
    uploads = os.path.join(dest_dir, 'uploads')
    if not os.path.isfile(db):
        raise FileNotFoundError('Backup is missing fieldops.db')
    # sanity: open read-only and run a quick check
    conn = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
    try:
        conn.execute('PRAGMA integrity_check').fetchone()
        conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
    finally:
        conn.close()
    return db, uploads if os.path.isdir(uploads) else None


def register(fo):
    exempt = tuple(getattr(fo, 'CSRF_EXEMPT', ()) or ())
    if ROUTE not in exempt:
        fo.CSRF_EXEMPT = exempt + (ROUTE,)

    @fo.app.route(ROUTE, methods=['GET'])
    @require_backup_key
    def office_backup():
        raw = pack_archive(fo.DB_PATH, fo.app.config.get('UPLOAD_FOLDER') or '')
        day = date.today().isoformat()
        return send_file(
            BytesIO(raw),
            mimetype='application/gzip',
            as_attachment=True,
            download_name='fieldops-%s.tgz' % day,
        )
