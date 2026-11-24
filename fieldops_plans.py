"""Recurring service plans: due visits become unscheduled tickets, plus QBO drafts.

Plans stay per site (monitoring, inspection, maintenance) with a frequency, a price
(office only), and a next visit date. Visits due within PLAN_LEAD_DAYS land in the
schedule tray as tickets. Recurring billing queues a QuickBooks invoice draft only.
Nothing is sent. RMR (monthly) is the sum of active plan price / every_months.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

KINDS = [('maintenance', 'Maintenance'), ('inspection', 'Inspection'), ('monitoring', 'Monitoring')]
KIND_SET = {k for k, _ in KINDS}


def _cols(conn, table):
    return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}


def _add_col(conn, table, coldef):
    name = coldef.split()[0]
    if name in _cols(conn, table):
        return
    conn.execute('ALTER TABLE %s ADD COLUMN %s' % (table, coldef))


def init_schema(conn):
    _add_col(conn, 'service_plans', 'company_id INTEGER')
    _add_col(conn, 'service_plans', 'site_id INTEGER')
    _add_col(conn, 'service_plans', 'kind TEXT')
    _add_col(conn, 'service_tickets', 'visit_date TEXT')
    _add_col(conn, 'qbo_drafts', 'plan_id INTEGER')
    _add_col(conn, 'qbo_drafts', 'visit_date TEXT')
    try:
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_tickets_plan_visit ON service_tickets(plan_id, visit_date) WHERE plan_id IS NOT NULL AND IFNULL(visit_date,\'\')!=\'\'')
    except Exception:
        conn.execute('CREATE INDEX IF NOT EXISTS ix_tickets_plan_visit ON service_tickets(plan_id, visit_date)')
    try:
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_qbo_drafts_plan_visit ON qbo_drafts(plan_id, visit_date) WHERE plan_id IS NOT NULL AND IFNULL(visit_date,\'\')!=\'\'')
    except Exception:
        conn.execute('CREATE INDEX IF NOT EXISTS ix_qbo_drafts_plan_visit ON qbo_drafts(plan_id, visit_date)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_plans_company ON service_plans(company_id)')


def rmr_monthly(conn):
    total = 0.0
    for p in conn.execute('SELECT price, every_months FROM service_plans WHERE active=1'):
        price = p['price'] or 0
        months = p['every_months'] or 0
        if price > 0 and months > 0:
            total += price / months
    return round(total, 2)


def _match_company_site(fo, conn, plan):
    cid = plan['company_id'] if 'company_id' in plan.keys() else None
    sid = plan['site_id'] if 'site_id' in plan.keys() else None
    try:
        import fieldops_platform as plat
    except Exception:
        plat = None
    if not cid and plan['client'] and plat:
        co = plat.upsert_company(conn, plan['client'], phone=plan['phone'] or '')
        cid = co['id'] if co else None
    if cid and not sid and plan['site'] and plat:
        site = plat.upsert_site(conn, cid, address=plan['site'])
        sid = site['id'] if site else None
    return cid, sid


def run_plans(fo, conn):
    """Create unscheduled tickets and invoice drafts for due visits. Drafts only."""
    import fieldops_tickets as tix
    lead = int(getattr(fo, 'PLAN_LEAD_DAYS', 14))
    horizon = (fo.today_local() + timedelta(days=lead)).isoformat()
    made = 0
    for p in conn.execute(
            "SELECT * FROM service_plans WHERE active=1 AND IFNULL(next_date,'')!='' AND next_date<=?",
            (horizon,)):
        try:
            nd = datetime.strptime(p['next_date'], '%Y-%m-%d').date()
        except ValueError:
            continue
        visit = nd.isoformat()
        cid, sid = _match_company_site(fo, conn, p)
        if cid and (not p['company_id'] or not p['site_id']):
            conn.execute(
                'UPDATE service_plans SET company_id=COALESCE(company_id,?), site_id=COALESCE(site_id,?) WHERE id=?',
                (cid, sid, p['id']))
        kind = (p['kind'] if 'kind' in p.keys() and p['kind'] in KIND_SET else 'maintenance')
        title = p['title'] or 'Service visit'
        existing = conn.execute(
            'SELECT id FROM service_tickets WHERE plan_id=? AND visit_date=?',
            (p['id'], visit)).fetchone()
        if not existing:
            issue = '%s visit on %s: %s.' % (kind.capitalize(), visit, title)
            row, err = tix.create_ticket(conn, {
                'company_id': cid, 'site_id': sid,
                'customer': p['client'] or '',
                'address': p['site'] or '',
                'phone': p['phone'] or '',
                'issue': issue,
                'priority': 'normal',
                'assigned_sub': (p['tech'] or '').strip() or None,
            }, created_by='Service Plan', source='plan')
            if err or not row:
                fo.app.logger.warning('plan ticket failed plan=%s: %s', p['id'], err)
            else:
                conn.execute(
                    'UPDATE service_tickets SET plan_id=?, visit_date=? WHERE id=?',
                    (p['id'], visit, row['id']))
                tix.add_event(conn, row['id'], 'plan', 'Opened from a %s plan for %s. Waiting in the tray.' % (kind, visit), 'Service Plan')
                made += 1
        price = p['price'] or 0
        if price and price > 0:
            ex = conn.execute(
                'SELECT id FROM qbo_drafts WHERE plan_id=? AND visit_date=?',
                (p['id'], visit)).fetchone()
            if not ex:
                jid = 'PLAN-%d-%s' % (p['id'], nd.strftime('%Y%m%d'))
                desc = '%s (%s) on %s' % (title, kind, visit)
                payload = {
                    'job_id': jid, 'plan_id': p['id'], 'visit_date': visit,
                    'doc_number': ('P%d-%s' % (p['id'], nd.strftime('%Y%m%d')))[:21],
                    'txn_date': visit,
                    'customer': {
                        'name': (p['client'] or '').strip(),
                        'phone': p['phone'] or '',
                        'address': p['site'] or '',
                    },
                    'line_items': [{
                        'description': desc[:4000], 'qty': 1,
                        'rate': round(price, 2), 'amount': round(price, 2),
                    }],
                    'total': round(price, 2),
                    'customer_memo': title,
                    'private_note': 'Field Ops service plan %d visit %s. Created as a draft. Do not send without Roland OK.' % (p['id'], visit),
                    'kind': 'final',
                }
                when = datetime.utcnow().isoformat()
                conn.execute(
                    '''INSERT INTO qbo_drafts (job_id,kind,amount,memo,status,payload,created_by,created_at,updated_at,plan_id,visit_date)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (jid, 'final', round(price, 2), (title)[:200], 'queued', json.dumps(payload),
                     'Service Plan', when, when, p['id'], visit))
        nxt = fo.add_months(nd, max(1, p['every_months'] or 12)).isoformat()
        conn.execute(
            'UPDATE service_plans SET next_date=?, last_job_id=? WHERE id=?',
            (nxt, 'PLAN-%d-%s' % (p['id'], nd.strftime('%Y%m%d')), p['id']))
    conn.commit()
    return made


def register(fo):
    def _run(conn):
        return run_plans(fo, conn)

    fo.run_plans = _run
    fo.plans_rmr = lambda conn: rmr_monthly(conn)

    orig = getattr(fo, 'ops_qbo_extra', None)
    # leave ops page as-is; LEFT JOIN is patched in fieldops_ops if we touch it

