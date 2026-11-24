"""Plan areas: sidebar, count into quote, notes and photos, Start walk and Present.

Builds on the existing True Sense plan designer. Does not copy any Consolidator
assets. Areas are the job's floor plans, with a count badge, notes, and photos.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import struct
import uuid
import zlib
from datetime import datetime
from pathlib import Path

import fieldops_quote as quote

AREA_PHOTO_TYPE = 'area'
MARKUP = quote.DEFAULT_MARKUP
SOURCE = 'plan-count'


def _cols(conn, table):
    return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}


def _add_col(conn, table, coldef):
    name = coldef.split()[0]
    if name in _cols(conn, table):
        return
    conn.execute('ALTER TABLE %s ADD COLUMN %s' % (table, coldef))


def init_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS plan_meta ("
        "filename TEXT PRIMARY KEY, job_id TEXT, name TEXT, scale TEXT, src TEXT, created_at TEXT,"
        "sort_order INTEGER, notes TEXT)"
    )
    _add_col(conn, 'plan_meta', 'sort_order INTEGER')
    _add_col(conn, 'plan_meta', 'notes TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS plan_area_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        plan_fn TEXT NOT NULL,
        filename TEXT NOT NULL,
        created_at TEXT,
        created_by TEXT
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_area_photos_plan ON plan_area_photos(job_id, plan_fn)')
    conn.execute('''CREATE TABLE IF NOT EXISTS device_catalog_map (
        device_type TEXT PRIMARY KEY,
        catalog_id INTEGER NOT NULL
    )''')


def now_iso():
    return datetime.utcnow().isoformat(timespec='seconds')


def blank_png(w=1200, h=800, color=(252, 252, 248)):
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    raw = b''.join(b'\x00' + (bytes(color) * w) for _ in range(h))
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(raw, 9))
        + chunk(b'IEND', b'')
    )


def next_sort(conn, job_id):
    row = conn.execute(
        'SELECT MAX(sort_order) FROM plan_meta WHERE job_id=?', (job_id,)).fetchone()
    top = row[0] if row and row[0] is not None else -1
    return int(top) + 1


def ordered_files(conn, job_id, files):
    if not files:
        return files
    try:
        meta = {}
        for r in conn.execute('SELECT filename, sort_order FROM plan_meta WHERE job_id=?', (job_id,)):
            if r['sort_order'] is not None:
                meta[r['filename']] = r['sort_order']
    except Exception:
        return files
    if not meta:
        return files
    return sorted(files, key=lambda fn: (meta[fn] if fn in meta else 10 ** 9, files.index(fn)))


def counts_for(devs, fn=None):
    out = {}
    for d in devs or []:
        if fn and d.get('plan') != fn:
            continue
        typ = d.get('type') or ''
        if typ:
            out[typ] = out.get(typ, 0) + 1
    return out


def area_photos(conn, job_id, plan_fn=None):
    if plan_fn:
        rows = conn.execute(
            'SELECT * FROM plan_area_photos WHERE job_id=? AND plan_fn=? ORDER BY id',
            (job_id, plan_fn)).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM plan_area_photos WHERE job_id=? ORDER BY id', (job_id,)).fetchall()
    return [dict(r) for r in rows]


def notes_of(conn, job_id):
    out = {}
    try:
        for r in conn.execute('SELECT filename, notes FROM plan_meta WHERE job_id=?', (job_id,)):
            out[r['filename']] = r['notes'] or ''
    except Exception:
        pass
    return out


def catalog_map(conn):
    out = {}
    try:
        for r in conn.execute(
            '''SELECT m.device_type, m.catalog_id, c.name, c.sku, c.cost, c.sell, c.unit
               FROM device_catalog_map m
               LEFT JOIN catalog_items c ON c.id=m.catalog_id'''
        ):
            out[r['device_type']] = {
                'catalog_id': r['catalog_id'],
                'name': r['name'] or r['device_type'],
                'sku': r['sku'] or '',
                'cost': r['cost'] or 0,
                'sell': r['sell'] or 0,
                'unit': r['unit'] or '',
            }
    except Exception:
        pass
    return out


def match_catalog(conn, typ):
    typ = (typ or '').strip()
    if not typ:
        return None
    mapped = conn.execute(
        'SELECT catalog_id FROM device_catalog_map WHERE device_type=?', (typ,)).fetchone()
    if mapped:
        row = conn.execute('SELECT * FROM catalog_items WHERE id=?', (mapped['catalog_id'],)).fetchone()
        if row:
            return row
    return conn.execute(
        'SELECT * FROM catalog_items WHERE active=1 AND LOWER(TRIM(name))=?',
        (typ.lower(),)).fetchone()


def set_symbol_map(conn, typ, catalog_id):
    typ = (typ or '').strip()
    if not typ:
        return 'Device type is required.'
    if catalog_id:
        cat = conn.execute('SELECT id FROM catalog_items WHERE id=?', (int(catalog_id),)).fetchone()
        if not cat:
            return 'Catalog item not found.'
        conn.execute(
            'INSERT OR REPLACE INTO device_catalog_map (device_type, catalog_id) VALUES (?, ?)',
            (typ, int(catalog_id)))
    else:
        conn.execute('DELETE FROM device_catalog_map WHERE device_type=?', (typ,))
    return None


def areas_payload(conn, row, fo=None, present=False, walk=False, plan_base=''):
    job_id = row['id']
    devs = []
    try:
        devs = fo.parse_devices(row['survey_devices']) if fo else []
    except Exception:
        devs = []
    photos = area_photos(conn, job_id)
    by_fn = {}
    for p in photos:
        by_fn.setdefault(p['plan_fn'], []).append({
            'id': p['id'],
            'filename': p['filename'],
            'url': (plan_base + p['filename']) if plan_base else ('/uploads/' + p['filename']),
        })
    quote_id = None
    try:
        q = conn.execute(
            "SELECT id FROM quotes WHERE job_id=? AND status!='accepted' ORDER BY id DESC",
            (job_id,)).fetchone()
        quote_id = q['id'] if q else None
    except Exception:
        quote_id = None
    items = []
    try:
        for r in conn.execute('SELECT id, name FROM catalog_items WHERE active=1 ORDER BY name COLLATE NOCASE'):
            items.append({'id': r['id'], 'name': r['name']})
    except Exception:
        items = []
    return {
        'area_notes': notes_of(conn, job_id),
        'area_photos': by_fn,
        'area_counts': {fn: sum(1 for d in devs if d.get('plan') == fn)
                        for fn in {d.get('plan') for d in devs if d.get('plan')}},
        'catalog_map': catalog_map(conn),
        'catalog_items': items,
        'quote_id': quote_id,
        'present': bool(present),
        'walk': bool(walk),
        'plan_base': plan_base or '',
    }


def add_blank_area(conn, job_id, name, fo, created_by=''):
    name = ' '.join((name or '').split())[:60] or fo.pm_next_name(conn, job_id)
    fn = '%s_plan_%s.png' % (fo.secure_filename(job_id) or 'job', uuid.uuid4().hex[:8])
    path = os.path.join(fo.app.config['UPLOAD_FOLDER'], fn)
    with open(path, 'wb') as f:
        f.write(blank_png())
    now = now_iso()
    conn.execute(
        "INSERT INTO photos (job_id,filename,photo_type,uploaded_at,orig_name) VALUES (?,?,?,?,?)",
        (job_id, fn, 'plan', now, name))
    fo.pm_add(conn, job_id, fn, name, 'blank')
    try:
        conn.execute('UPDATE plan_meta SET sort_order=? WHERE filename=?', (next_sort(conn, job_id), fn))
    except Exception:
        pass
    return fn


def duplicate_area(conn, job_id, src_fn, fo, created_by=''):
    files = fo.plan_files(conn, job_id)
    if src_fn not in files:
        return None, 'Area not found.'
    ext = os.path.splitext(src_fn)[1] or '.png'
    new_fn = '%s_plan_%s%s' % (fo.secure_filename(job_id) or 'job', uuid.uuid4().hex[:8], ext)
    src_path = os.path.join(fo.app.config['UPLOAD_FOLDER'], fo.secure_filename(src_fn))
    dst_path = os.path.join(fo.app.config['UPLOAD_FOLDER'], new_fn)
    if os.path.exists(src_path):
        shutil.copyfile(src_path, dst_path)
    else:
        with open(dst_path, 'wb') as f:
            f.write(blank_png())
    now = now_iso()
    meta = conn.execute('SELECT * FROM plan_meta WHERE filename=?', (src_fn,)).fetchone()
    name = ((meta['name'] if meta else 'Area') + ' copy')[:60]
    conn.execute(
        "INSERT INTO photos (job_id,filename,photo_type,uploaded_at,orig_name) VALUES (?,?,?,?,?)",
        (job_id, new_fn, 'plan', now, name))
    fo.pm_copy(conn, src_fn, new_fn, job_id)
    conn.execute('UPDATE plan_meta SET name=?, sort_order=? WHERE filename=?',
                 (name, next_sort(conn, job_id), new_fn))
    try:
        notes = (meta['notes'] or '') if meta and 'notes' in meta.keys() else ''
        if notes:
            conn.execute('UPDATE plan_meta SET notes=? WHERE filename=?', (notes, new_fn))
    except Exception:
        pass
    # devices
    row = conn.execute('SELECT survey_devices FROM jobs WHERE id=?', (job_id,)).fetchone()
    devs = fo.parse_devices(row['survey_devices'] if row else '[]')
    added = []
    for d in list(devs):
        if d.get('plan') != src_fn:
            continue
        nd = dict(d)
        nd['id'] = 'd' + uuid.uuid4().hex[:10]
        nd['plan'] = new_fn
        if d.get('photo'):
            oldp = fo.secure_filename(d['photo'])
            srcp = os.path.join(fo.app.config['UPLOAD_FOLDER'], oldp)
            if os.path.exists(srcp):
                newp = '%s_devphoto_%s.jpg' % (fo.secure_filename(job_id) or 'job', uuid.uuid4().hex[:8])
                shutil.copyfile(srcp, os.path.join(fo.app.config['UPLOAD_FOLDER'], newp))
                nd['photo'] = newp
        added.append(nd)
        devs.append(nd)
    conn.execute('UPDATE jobs SET survey_devices=? WHERE id=?', (json.dumps(devs), job_id))
    for p in area_photos(conn, job_id, src_fn):
        oldp = fo.secure_filename(p['filename'])
        srcp = os.path.join(fo.app.config['UPLOAD_FOLDER'], oldp)
        ext = os.path.splitext(oldp)[1] or '.jpg'
        newp = '%s_area_%s%s' % (fo.secure_filename(job_id) or 'job', uuid.uuid4().hex[:8], ext)
        if os.path.exists(srcp):
            shutil.copyfile(srcp, os.path.join(fo.app.config['UPLOAD_FOLDER'], newp))
        conn.execute(
            'INSERT INTO plan_area_photos (job_id,plan_fn,filename,created_at,created_by) VALUES (?,?,?,?,?)',
            (job_id, new_fn, newp, now, created_by))
    return new_fn, None


def reorder_areas(conn, job_id, order, fo):
    files = fo.plan_files(conn, job_id)
    order = [fn for fn in (order or []) if fn in files]
    for fn in files:
        if fn not in order:
            order.append(fn)
    for i, fn in enumerate(order):
        conn.execute('UPDATE plan_meta SET sort_order=? WHERE filename=?', (i, fn))
        if not conn.execute('SELECT 1 FROM plan_meta WHERE filename=?', (fn,)).fetchone():
            info = {x['fn']: x for x in fo.plan_info(conn, job_id)}
            fo.pm_add(conn, job_id, fn, (info.get(fn) or {}).get('name') or 'Area')
            conn.execute('UPDATE plan_meta SET sort_order=? WHERE filename=?', (i, fn))
    return order


def save_notes(conn, job_id, fn, notes, fo):
    files = fo.plan_files(conn, job_id)
    if fn not in files:
        return 'Area not found.'
    if not conn.execute('SELECT 1 FROM plan_meta WHERE filename=?', (fn,)).fetchone():
        info = {x['fn']: x for x in fo.plan_info(conn, job_id)}
        fo.pm_add(conn, job_id, fn, (info.get(fn) or {}).get('name') or 'Area')
    conn.execute('UPDATE plan_meta SET notes=? WHERE filename=?', ((notes or '')[:4000], fn))
    return None


def add_area_photo(conn, job_id, fn, storage, fo, created_by=''):
    files = fo.plan_files(conn, job_id)
    if fn not in files:
        return None, 'Area not found.'
    if not storage or not storage.filename:
        return None, 'Choose a photo.'
    ext = os.path.splitext(storage.filename)[1].lower() or '.jpg'
    if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.heic', '.heif'):
        return None, 'Photo must be an image.'
    if ext == '.jpeg':
        ext = '.jpg'
    name = '%s_area_%s%s' % (fo.secure_filename(job_id) or 'job', uuid.uuid4().hex[:8], ext)
    storage.save(os.path.join(fo.app.config['UPLOAD_FOLDER'], name))
    conn.execute(
        'INSERT INTO plan_area_photos (job_id,plan_fn,filename,created_at,created_by) VALUES (?,?,?,?,?)',
        (job_id, fn, name, now_iso(), created_by))
    pid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    return {'id': pid, 'filename': name, 'url': '/uploads/' + name}, None


def delete_area_photo(conn, job_id, photo_id, fo):
    row = conn.execute(
        'SELECT * FROM plan_area_photos WHERE id=? AND job_id=?', (photo_id, job_id)).fetchone()
    if not row:
        return False
    conn.execute('DELETE FROM plan_area_photos WHERE id=?', (photo_id,))
    try:
        os.remove(os.path.join(fo.app.config['UPLOAD_FOLDER'], fo.secure_filename(row['filename'])))
    except OSError:
        pass
    return True


def cleanup_area(conn, job_id, fn, fo):
    for p in area_photos(conn, job_id, fn):
        delete_area_photo(conn, job_id, p['id'], fo)


def count_into_quote(conn, job, scope, fn, who, fo):
    """Turn device counts into quote lines at cost + 25%. Re-run updates quantities."""
    devs = fo.parse_devices(job['survey_devices'])
    scope = 'area' if scope == 'area' else 'all'
    if scope == 'area':
        if not fn:
            return None, 'Pick an area.'
        devs = [d for d in devs if d.get('plan') == fn]
        key = fn
    else:
        key = 'all'
    counts = counts_for(devs)
    if not counts:
        return None, 'No devices to count on this area.'
    existing = conn.execute(
        "SELECT * FROM quotes WHERE job_id=? AND status!='accepted' ORDER BY id DESC",
        (job['id'],)).fetchone()
    old_lines = quote.lines_of(existing) if existing else []
    kept = [ln for ln in old_lines
            if not (ln.get('source') == SOURCE and (ln.get('plan_key') or '') == key)]
    new_lines = []
    unmapped = []
    for typ, qty in counts.items():
        cat = match_catalog(conn, typ)
        if cat:
            cost = float(cat['cost'] or 0)
            sell = round(cost * MARKUP, 2) if cost > 0 else float(cat['sell'] or 0)
            ln = quote.freeze_line(
                cat['name'], qty, cost, sell,
                catalog_id=cat['id'], unit=cat['unit'] or '')
        else:
            unmapped.append(typ)
            ln = quote.freeze_line(typ, qty, 0, 0)
        ln['source'] = SOURCE
        ln['plan_key'] = key
        new_lines.append(ln)
    lines = kept + new_lines
    title = (job['title'] or job['id'] or 'Plan count').strip()[:200]
    info = {x['fn']: x for x in fo.plan_info(conn, job['id'])}
    if scope == 'area' and fn in info:
        note_bit = 'Counted from %s.' % info[fn]['name']
    else:
        note_bit = 'Counted from all areas.'
    data = {
        'title': (existing['title'] if existing else title),
        'company_id': existing['company_id'] if existing else job['company_id'],
        'site_id': existing['site_id'] if existing else job['site_id'],
        'job_id': job['id'],
        'opportunity_id': existing['opportunity_id'] if existing else None,
        'notes': (existing['notes'] if existing else '') or note_bit,
        'clarifications': (existing['clarifications'] if existing and 'clarifications' in existing.keys() else ''),
        'lines': lines,
    }
    row, err = quote.save_quote(
        conn, data, quote_id=existing['id'] if existing else None, created_by=who or 'Roland')
    if err:
        return None, err
    # Preserve source tags (save_quote re-freezes known fields only unless patched).
    frozen = quote.lines_of(row)
    tagged = []
    incoming = list(lines)
    for ln in frozen:
        match = None
        for i, src in enumerate(incoming):
            if (src.get('name') == ln.get('name') and src.get('qty') == ln.get('qty')
                    and src.get('source') == SOURCE):
                match = incoming.pop(i)
                break
        if match:
            ln = dict(ln)
            ln['source'] = SOURCE
            ln['plan_key'] = match.get('plan_key') or key
        tagged.append(ln)
    conn.execute('UPDATE quotes SET lines_json=? WHERE id=?', (json.dumps(tagged), row['id']))
    row = conn.execute('SELECT * FROM quotes WHERE id=?', (row['id'],)).fetchone()
    return {
        'quote_id': row['id'],
        'total': row['total'],
        'lines': quote.lines_of(row),
        'unmapped': unmapped,
        'scope': scope,
    }, None


def add_asbuilt_area_pages(b, conn, row, fo):
    """Append notes and photos, grouped by area, to an as-built PDF."""
    info = b.info if hasattr(b, 'info') else fo.plan_info(conn, row['id'])
    notes = notes_of(conn, row['id'])
    if not info:
        return
    any_content = any(notes.get(p['fn']) or area_photos(conn, row['id'], p['fn']) for p in info)
    if not any_content:
        return
    M, PW, PH = b.M, b.PW, b.PH
    for p in info:
        fn = p['fn']
        text = (notes.get(fn) or '').strip()
        photos = area_photos(conn, row['id'], fn)
        if not text and not photos:
            continue
        pg = b.page('Area: %s' % (p.get('name') or 'Area'))
        html = '<div style="font-family:sans-serif;color:#111">'
        html += '<div style="font-size:13px;font-weight:bold;margin:0 0 6px">%s</div>' % fo.h(p.get('name') or 'Area')
        html += '<div style="font-size:10.5px;font-weight:bold;margin:8px 0 2px">Area notes</div>'
        if text:
            html += '<div style="font-size:10px;white-space:pre-wrap">%s</div>' % fo.h(text[:4000])
        else:
            html += '<div style="font-size:10px;color:#777">No notes.</div>'
        b.html(pg, (M, 64, PW - M, 200), html + '</div>')
        if not photos:
            continue
        for i in range(0, len(photos), 4):
            if i:
                pg = b.page('Area photos: %s (continued)' % (p.get('name') or 'Area'))
                y0 = 64
            else:
                y0 = 220
            for j, ph in enumerate(photos[i:i + 4]):
                cols = 2
                cw = (PW - 2 * M) / cols
                cx = M + (j % cols) * cw
                cy = y0 + (j // cols) * 230
                r = b.pm.Rect(cx + 4, cy, cx + cw - 4, cy + 210)
                pg.draw_rect(r, color=(0.9, 0.9, 0.86), width=0.5)
                path = os.path.join(fo.app.config['UPLOAD_FOLDER'], fo.secure_filename(ph['filename']))
                try:
                    fo.pdf_plan_image(pg, b.pm.Rect(r.x0 + 3, r.y0 + 3, r.x1 - 3, r.y1 - 18), path, keep=True)
                except Exception:
                    b.html(pg, (r.x0 + 8, r.y0 + 80, r.x1 - 8, r.y0 + 110),
                           '<div style="font-family:sans-serif;font-size:9px;color:#777;text-align:center">Photo could not be shown</div>')
                b.html(pg, (r.x0, r.y1 - 16, r.x1, r.y1),
                       '<div style="font-family:sans-serif;font-size:8px;color:#444;text-align:center">%s</div>'
                       % fo.h((ph.get('created_at') or '')[:16].replace('T', ' ')))


def _asset(name):
    return Path(__file__).resolve().parent.joinpath(name).read_text(encoding='utf-8')


def register(fo):
    try:
        conn = fo.get_db()
        init_schema(conn)
        conn.commit(); conn.close()
    except Exception:
        pass
    app = fo.app
    request = fo.request
    jsonify = fo.jsonify
    redirect = fo.redirect

    orig_plan_files = fo.plan_files

    def plan_files(conn, job_id):
        return ordered_files(conn, job_id, orig_plan_files(conn, job_id))

    fo.plan_files = plan_files

    orig_extra = getattr(fo, 'design_data_extra', None)

    def design_data_extra(conn, row):
        data = orig_extra(conn, row) if callable(orig_extra) else {}
        present = (request.args.get('present') == '1') or bool(getattr(fo, '_present_once', False))
        walk = request.args.get('walk') == '1'
        data.update(areas_payload(conn, row, fo=fo, present=present, walk=walk,
                                  plan_base=getattr(fo, '_plan_base', '') or ''))
        return data

    fo.design_data_extra = design_data_extra

    orig_page = fo.design_page

    def design_page(row, conn, js_extra='', body_extra=''):
        css = '<style>%s</style>' % _asset('fieldops_areas.css')
        js = '\n' + _asset('fieldops_areas.js')
        present = request.args.get('present') == '1' or bool(getattr(fo, '_present_once', False))
        walk = request.args.get('walk') == '1'
        try:
            html = orig_page(row, conn, js_extra + js, css + (body_extra or ''))
        finally:
            fo._present_once = False
            fo._plan_base = ''
        if '<script src="/outbox.js"' not in html:
            html = html.replace('</head>', '<script src="/outbox.js" defer></script></head>', 1)
        cls = []
        if present:
            cls.append('pd-present')
        if walk:
            cls.append('pd-walk')
        if cls:
            html = html.replace('<body>', '<body class="%s">' % ' '.join(cls), 1)
        return html

    fo.design_page = design_page

    orig_delete = getattr(fo, 'delete_plan', None)
    # Route already bound; wrap by decorating a before hook on that endpoint is hard.
    # Cleanup is also done from a after-request? We'll hook SQL via a wrapper if we can replace the view.
    if orig_delete:
        def delete_plan(job_id):
            fn = ''
            try:
                fn = fo.secure_filename(str((request.get_json(silent=True) or {}).get('filename') or ''))
            except Exception:
                fn = ''
            resp = orig_delete(job_id)
            if fn:
                try:
                    conn = fo.get_db()
                    cleanup_area(conn, job_id, fn, fo)
                    conn.commit(); conn.close()
                except Exception:
                    pass
            return resp
        app.view_functions['delete_plan'] = delete_plan

    orig_asbuilt = fo.asbuilt_pdf_bytes

    def asbuilt_pdf_bytes(conn, row, prices, who, cones=True):
        # Rebuild with area pages by calling the original pieces if needed.
        # We patch by running original then... can't insert. Instead, temporarily wrap FoPdf.finish.
        orig_finish = None
        added = {'ok': False}

        def hook_init():
            return None

        # Monkeypatch FoPdf.plan_pages to append area pages after plan pages when ab=True
        FoPdf = fo.FoPdf
        orig_pages = FoPdf.plan_pages

        def plan_pages(self, cones, ab=False, prefix='Floor Plan'):
            orig_pages(self, cones, ab=ab, prefix=prefix)
            if ab:
                try:
                    add_asbuilt_area_pages(self, conn, row, fo)
                except Exception:
                    fo.app.logger.exception('area as-built pages')

        FoPdf.plan_pages = plan_pages
        try:
            return orig_asbuilt(conn, row, prices, who, cones)
        finally:
            FoPdf.plan_pages = orig_pages

    fo.asbuilt_pdf_bytes = asbuilt_pdf_bytes

    def _edit_job(job_id):
        conn = fo.get_db()
        row, err = fo.survey_edit_row(conn, job_id)
        return conn, row, err

    def _payload(conn, row, extra=None):
        data = {'ok': True, 'plan_info': fo.plan_info(conn, row['id']),
                'devices': fo.parse_devices(row['survey_devices'])}
        data.update(areas_payload(conn, row, fo=fo))
        if extra:
            data.update(extra)
        return jsonify(data)

    @app.route('/api/jobs/<job_id>/areas', methods=['POST'])
    def areas_add(job_id):
        conn, row, err = _edit_job(job_id)
        if err:
            conn.close(); return err
        name = (request.form.get('name') or (request.get_json(silent=True) or {}).get('name') or '')
        fn = add_blank_area(conn, job_id, name, fo, fo.me())
        conn.commit()
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        resp = _payload(conn, row, {'filename': fn})
        conn.close()
        return resp

    @app.route('/api/jobs/<job_id>/areas/reorder', methods=['POST'])
    def areas_reorder(job_id):
        conn, row, err = _edit_job(job_id)
        if err:
            conn.close(); return err
        order = (request.get_json(silent=True) or {}).get('order') or []
        reorder_areas(conn, job_id, order, fo)
        conn.commit()
        resp = _payload(conn, row)
        conn.close()
        return resp

    @app.route('/api/jobs/<job_id>/areas/duplicate', methods=['POST'])
    def areas_duplicate(job_id):
        conn, row, err = _edit_job(job_id)
        if err:
            conn.close(); return err
        fn = fo.secure_filename(str((request.get_json(silent=True) or {}).get('filename') or ''))
        new_fn, err2 = duplicate_area(conn, job_id, fn, fo, fo.me())
        if err2:
            conn.close(); return jsonify({'error': err2}), 400
        conn.commit()
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        resp = _payload(conn, row, {'filename': new_fn})
        conn.close()
        return resp

    @app.route('/api/jobs/<job_id>/areas/notes', methods=['POST'])
    def areas_notes(job_id):
        conn, row, err = _edit_job(job_id)
        if err:
            conn.close(); return err
        d = request.get_json(silent=True) or {}
        fn = fo.secure_filename(str(d.get('filename') or ''))
        err2 = save_notes(conn, job_id, fn, d.get('notes') or '', fo)
        if err2:
            conn.close(); return jsonify({'error': err2}), 400
        conn.commit()
        resp = _payload(conn, row)
        conn.close()
        return resp

    @app.route('/api/jobs/<job_id>/areas/photos', methods=['POST'])
    def areas_photos(job_id):
        conn, row, err = _edit_job(job_id)
        if err:
            conn.close(); return err
        fn = fo.secure_filename(str(request.form.get('filename') or ''))
        photo, err2 = add_area_photo(conn, job_id, fn, request.files.get('photo') or request.files.get('file'), fo, fo.me())
        if err2:
            conn.close(); return jsonify({'error': err2}), 400
        conn.commit()
        resp = _payload(conn, row, {'photo': photo})
        conn.close()
        return resp

    @app.route('/api/jobs/<job_id>/areas/photos/<int:pid>/delete', methods=['POST'])
    def areas_photo_delete(job_id, pid):
        conn, row, err = _edit_job(job_id)
        if err:
            conn.close(); return err
        if not delete_area_photo(conn, job_id, pid, fo):
            conn.close(); return jsonify({'error': 'Photo not found.'}), 404
        conn.commit()
        resp = _payload(conn, row)
        conn.close()
        return resp

    @app.route('/api/jobs/<job_id>/areas/count-into-quote', methods=['POST'])
    def areas_count_quote(job_id):
        if not fo.is_office():
            return jsonify({'error': 'Office only.'}), 403
        conn, row, err = _edit_job(job_id)
        if err:
            conn.close(); return err
        d = request.get_json(silent=True) or {}
        result, err2 = count_into_quote(
            conn, row, d.get('scope') or 'area',
            fo.secure_filename(str(d.get('filename') or '')), fo.me(), fo)
        if err2:
            conn.close(); return jsonify({'error': err2}), 400
        conn.commit()
        resp = _payload(conn, row, result)
        conn.close()
        return resp

    @app.route('/api/jobs/<job_id>/areas/map-symbol', methods=['POST'])
    def areas_map_symbol(job_id):
        if not fo.is_office():
            return jsonify({'error': 'Office only.'}), 403
        conn, row, err = _edit_job(job_id)
        if err:
            conn.close(); return err
        d = request.get_json(silent=True) or {}
        cid = d.get('catalog_id')
        try:
            cid = int(cid) if cid not in (None, '', 0, '0') else 0
        except (TypeError, ValueError):
            conn.close(); return jsonify({'error': 'Bad catalog item.'}), 400
        err2 = set_symbol_map(conn, d.get('type') or d.get('device_type'), cid)
        if err2:
            conn.close(); return jsonify({'error': err2}), 400
        conn.commit()
        resp = _payload(conn, row)
        conn.close()
        return resp

    @app.route('/s/<token>/present')
    def share_present(token):
        conn, r, row = fo.sl_get(token)
        if not r:
            return fo.SL_GONE, 410
        try:
            fo.sl_bump(conn, r)
            fo._present_once = True
            fo._plan_base = '/s/%s/f/' % token
            html = fo.design_page(row, conn, fo.DESIGN_JS_EDIT, fo.DESIGN_CSS_EDIT)
        finally:
            fo._present_once = False
            fo._plan_base = ''
            conn.close()
        return fo.sl_headers(fo.app.make_response(html))

    @app.route('/opportunities/<int:oid>/design')
    def opportunity_design(oid):
        if not fo.is_office():
            return redirect('/')
        conn = fo.get_db()
        opp = conn.execute('SELECT * FROM opportunities WHERE id=?', (oid,)).fetchone()
        if not opp:
            conn.close(); return redirect('/opportunities')
        jid = opp['job_id'] or ''
        if not jid:
            import fieldops_quote as qmod
            jid = qmod._next_job_id(conn)
            company = conn.execute('SELECT * FROM companies WHERE id=?', (opp['company_id'],)).fetchone() if opp['company_id'] else None
            site = conn.execute('SELECT * FROM sites WHERE id=?', (opp['site_id'],)).fetchone() if opp['site_id'] else None
            when = now_iso()
            customer = company['name'] if company else (opp['title'] or 'Opportunity')
            address = (site['address'] if site else '') or (company['billing_address'] if company else '') or ''
            conn.execute(
                '''INSERT INTO jobs (id,title,customer,address,phone,scope,status,tech,created_at,scheduled_date,created_by,job_type,company_id,site_id,contact_id,sale_amount)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (jid, opp['title'] or 'Survey', customer, address, (company['phone'] if company else '') or '',
                 opp['title'] or '', 'assigned', fo.default_sub() or '', when, None, fo.me() or '', 'Survey',
                 opp['company_id'], opp['site_id'], opp['contact_id'], 0))
            try:
                import fieldops_platform as plat
                plat.link_job(conn, conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone(),
                              company_id=opp['company_id'])
            except Exception:
                pass
            conn.execute('UPDATE opportunities SET job_id=?, updated_at=? WHERE id=? AND IFNULL(job_id,"")=""',
                         (jid, when, oid))
            conn.commit()
        conn.close()
        return redirect('/design/' + jid)
