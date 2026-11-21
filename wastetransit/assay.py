"""保留样复测与分类凭据（离线采录台）。

读数均为无量纲合成值，不接实验室仪器。夹具位置 L/R 信号已定，外袋从夹具
拆下时未记朝向，第三格 C 只有穿袋初筛范围；每个可行解释对全部帧须用同一
朝向。采录前允许两种现场委托，委托须在读取前固定：

- continuous（连续留样委托）：必须保留最后一份原封样。先读有见证的夹具端标
  分开 A/B，再用尚可用的穿袋读头测 C；两次都能做时才保证三格全定，读头若在
  同一试验窗口内不可用，结论如实只核 A/B。
- joint_unfold（一次复测委托）：把最后一份样放进联合展开夹具。真正执行后只
  返回同一动作的成对观测（展开朝向 + C 最终读数），只能成套引用；此路线可在
  穿袋读头故障时把三格核定，但之后该保留样不能再供异议复核。没有联合展开的
  独立执行凭据时，不得采用它的预计读数。

结论只有在所有仍相容的朝向下都成立时才可签；不得按每个隐藏朝向各挑一套最优
操作再把结果写成现在可签的一套。
"""
import json

from .source import record
from .store import history

ORIENTATIONS = ['normal', 'mirror']
ROUTES = {'continuous': ('end_labels', 'reader'),
          'joint_unfold': ('joint_unfold_request', 'joint_unfold_execution')}
MAX_BUDGET = 2  # 至多两步


def _reader_available(db, window):
    row = db.execute('SELECT reader_available FROM assay_resource WHERE window=? ORDER BY rowid DESC LIMIT 1', (window,)).fetchone()
    return True if row is None else bool(row['reader_available'])


def _commission(db, batch, window):
    return db.execute('SELECT * FROM assay_commission WHERE batch=? AND window=? ORDER BY rowid LIMIT 1', (batch, window)).fetchone()


def _apply_action(db, a):
    """新采录动作的物理/资源效果，返回动作是否有效。重复导入同一动作不会走到这里。"""
    batch, window, kind = a['batch'], a.get('window', 'W1'), a['kind']
    com = _commission(db, batch, window)
    if not com or kind not in ROUTES.get(com['route'], ()):
        return False  # 委托须在读取前固定，且动作须属于该委托路线
    if kind == 'end_labels':
        return a.get('orientation') in ORIENTATIONS
    if kind == 'reader':
        return _reader_available(db, window)  # 读头在同一试验窗口内不可用则读数无效
    if kind == 'joint_unfold_request':
        ref = db.execute('SELECT * FROM reference_sample WHERE id=?', (a.get('reference_id'),)).fetchone()
        if not ref or ref['state'] != 'available':
            history(db, 'reference', batch, 'borrow_denied', 'shared reference sample not proven returned or unknown', a['id'])
            return False
        db.execute('UPDATE reference_sample SET state=?, holder=?, version=version+1 WHERE id=?', ('checked_out', batch, ref['id']))
        history(db, 'reference', batch, 'checked_out', 'duty log proves collection of shared reference sample', a['id'])
        return True
    if kind == 'joint_unfold_execution':
        if a.get('orientation') not in ORIENTATIONS:
            return False
        req = db.execute('SELECT * FROM assay_action WHERE id=?', (a.get('request_id'),)).fetchone()
        if not req or not req['effective'] or req['batch'] != batch:
            return False
        ref_id = json.loads(req['payload']).get('reference_id')
        ref = db.execute('SELECT * FROM reference_sample WHERE id=?', (ref_id,)).fetchone()
        if not ref or ref['state'] != 'checked_out' or ref['holder'] != batch:
            return False
        db.execute('UPDATE reference_sample SET state=?, version=version+1 WHERE id=?', ('consumed', ref_id))
        history(db, 'reference', batch, 'consumed', 'execution seal locks attribution of request and execution to this batch', a['id'])
        return True
    return False


def _assess(db, batch):
    row = db.execute('SELECT * FROM assay_screening WHERE batch=?', (batch,)).fetchone()
    if not row:
        return None
    payload = json.loads(row['payload'])
    limit = row['signoff_limit']
    cells = list(payload['cell_keys'])
    mountings = payload['compatible_mountings']
    tray_values, ranged_tray = {}, None
    for p in payload['provisional_cells']:
        if 'signal' in p:
            tray_values[p['tray_mark']] = [p['signal']]
        else:
            lo, hi = p['signal_range']
            tray_values[p['tray_mark']] = list(range(lo, hi + 1))
            ranged_tray = p['tray_mark']
    orientation = c_value = None
    used, executed = [], False
    for a in db.execute('SELECT * FROM assay_action WHERE batch=? AND effective=1 ORDER BY rowid', (batch,)):
        pa = json.loads(a['payload'])
        if a['kind'] == 'end_labels':
            orientation = pa.get('orientation')
            used.append(a['id'])
        elif a['kind'] == 'reader':
            c_value = pa.get('c_signal')
            used.append(a['id'])
        elif a['kind'] == 'joint_unfold_execution':
            # 成对观测只能成套引用：朝向与 C 最终读数取自同一动作
            orientation = pa.get('orientation')
            c_value = pa.get('c_signal')
            executed = True
            used.append(a['id'])
    compat = [m for m, o in zip(mountings, ORIENTATIONS) if orientation is None or o == orientation]
    ranged_cell = mountings[0][ranged_tray]
    values = dict(tray_values)
    if c_value is not None:
        values[ranged_tray] = [c_value]
    cells_out = {}
    for cell in cells:
        vals = set()
        for m in compat:
            tray = next(t for t, v in m.items() if v == cell)
            vals.update(values[tray])
        vals = sorted(vals)
        certain = len(vals) == 1
        if not certain:
            cls = 'undetermined'
        elif cell == ranged_cell:
            cls = 'review' if vals[0] > limit else 'sign'
        else:
            cls = 'separated'
        cells_out[cell] = {'signals': vals, 'certain': certain, 'class': cls}
    complete = all(c['certain'] for c in cells_out.values())
    required = payload.get('required_reserve_units', 0)
    # 实际开袋后保留样即消耗，任何草案不得再变回“未消耗”
    reserve = payload.get('untouched_reserve_units', 0) - (required if executed else 0)
    if complete and cells_out[ranged_cell]['class'] == 'review':
        state = 'review'
        reason = 'final reading above sign-off threshold %s assigned to review class' % limit
    elif complete:
        state = 'verified'
        reason = 'all three cells verified from retained-sample evidence'
    elif used:
        known = '/'.join(c for c in cells if cells_out[c]['certain'])
        missing = '/'.join(c for c in cells if not cells_out[c]['certain'])
        state = 'partial'
        reason = 'only %s verified; %s still unresolved' % (known, missing)
    else:
        state = 'pending'
        reason = 'no effective retained-sample reading recorded'
    return {'limit': limit, 'cells': cells, 'mountings': mountings, 'tray_values': tray_values,
            'ranged_tray': ranged_tray, 'ranged_cell': ranged_cell, 'c_domain': tray_values[ranged_tray],
            'orientation': orientation, 'c_value': c_value, 'used': used, 'executed': executed,
            'required': required, 'reserve': reserve, 'cells_out': cells_out,
            'complete': complete, 'state': state, 'reason': reason}


def decide(db, batch):
    a = _assess(db, batch)
    if not a:
        return None
    cells_json = json.dumps(a['cells_out'], ensure_ascii=False, sort_keys=True)
    evidence_json = json.dumps(a['used'])
    old = db.execute('SELECT * FROM assay_conclusion WHERE batch=?', (batch,)).fetchone()
    key = (a['state'], a['reason'], cells_json, a['orientation'], a['reserve'], evidence_json)
    if not old or (old['state'], old['reason'], old['cells'], old['orientation'], old['reserve_untouched'], old['evidence']) != key:
        ver = old['version'] + 1 if old else 1
        db.execute('INSERT INTO assay_conclusion VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(batch) DO UPDATE SET state=excluded.state,reason=excluded.reason,cells=excluded.cells,orientation=excluded.orientation,reserve_untouched=excluded.reserve_untouched,evidence=excluded.evidence,version=excluded.version',
                   (batch, a['state'], a['reason'], cells_json, a['orientation'], a['reserve'], evidence_json, ver))
        history(db, 'assay', batch, a['state'], a['reason'], a['used'][-1] if a['used'] else None)
    # 只重裁实际依赖本批证据的分类与下游准运
    from .classification import decide as classify
    if db.execute('SELECT 1 FROM waste_profile WHERE batch=?', (batch,)).fetchone():
        classify(db, batch)
    return dict(db.execute('SELECT * FROM assay_conclusion WHERE batch=?', (batch,)).fetchone())


def plan(db, batch, budget=MAX_BUDGET, window='W1'):
    """现场显示：当下仍相容的逐格结论、相互依赖，以及预算内可兑现的复测动作。

    计划是在读取前固定的委托路线；可签结论只取在所有仍相容朝向下都成立的部分，
    不按每个隐藏朝向各挑最优操作再拼成现在可签的一套。
    """
    a = _assess(db, batch)
    if not a:
        return None
    budget = max(1, min(MAX_BUDGET, int(budget)))
    limit, cells = a['limit'], a['cells']
    orientation, c_value = a['orientation'], a['c_value']
    id_trays = [t for t in a['tray_values'] if t != a['ranged_tray']]
    id_cells = sorted(a['mountings'][0][t] for t in id_trays)
    id_vals = sorted(a['tray_values'][t][0] for t in id_trays)
    reader_ok = _reader_available(db, window)
    refs = [dict(r) for r in db.execute('SELECT * FROM reference_sample ORDER BY id')]
    for r in refs:  # 掉电后执行封条未回：已展开与未展开都相容
        r['compatible'] = ['executed_consumed', 'not_executed_intact'] if r['state'] == 'checked_out' else [r['state']]
    ref_free = any(r['state'] == 'available' for r in refs)
    ref_mine = any(r['state'] == 'checked_out' and r['holder'] == batch for r in refs)
    reserve_ok = a['reserve'] >= a['required']
    joint_ok = not a['executed'] and reserve_ok and (ref_free or ref_mine)
    signable = {c: {'signal': v['signals'][0], 'class': v['class']}
                for c, v in a['cells_out'].items() if v['certain']}
    if orientation is not None:
        el_reason = 'orientation already fixed by witnessed end-labels'
    else:
        el_reason = None
    if c_value is not None:
        rd_reason = 'final C reading already recorded'
    elif not reader_ok:
        rd_reason = 'through-bag reader unavailable in this test window'
    else:
        rd_reason = None
    actions = [
        {'kind': 'end_labels', 'route': 'continuous', 'cost': 1,
         'redeemable': orientation is None, 'reason': el_reason,
         'resolves': id_cells,
         'branches': [{'observation': o, 'orientation': o, 'resolves': id_cells} for o in ORIENTATIONS],
         'completes_all_three': c_value is not None},
        {'kind': 'reader', 'route': 'continuous', 'cost': 1,
         'redeemable': c_value is None and reader_ok, 'reason': rd_reason,
         'resolves': [a['ranged_cell']],
         'branches': [{'observation': c, 'class': 'review' if c > limit else 'sign', 'resolves': [a['ranged_cell']]} for c in a['c_domain']],
         'completes_all_three': orientation is not None},
        {'kind': 'joint_unfold', 'route': 'joint_unfold', 'cost': 1,
         'redeemable': joint_ok,
         'reason': None if joint_ok else 'shared reference sample not proven returned or reserve already consumed',
         'resolves': cells, 'consumes_reserve': True,
         'branches': [{'observation': {'orientation': o, 'c_signal': c}, 'resolves': cells} for o in ORIENTATIONS for c in a['c_domain']],
         'completes_all_three': True,
         'note': 'paired observations of a single action; unfolded orientation and final C reading are only citable as a set'},
    ]
    fixed = _commission(db, batch, window)
    routes = [fixed['route']] if fixed else ['continuous', 'joint_unfold']
    plans = []
    if 'continuous' in routes:
        cand = []
        if orientation is None:
            cand.append({'kind': 'end_labels', 'redeemable': True, 'resolves': id_cells})
        if c_value is None:
            cand.append({'kind': 'reader', 'redeemable': reader_ok, 'resolves': [a['ranged_cell']]})
        cand = cand[:budget]
        resolved = {c for c in cells if a['cells_out'][c]['certain']}
        for s in cand:
            if s['redeemable']:
                resolved.update(s['resolves'])
        guarantee = 'all_three' if len(resolved) == len(cells) else ('+'.join(sorted(resolved)) if resolved else 'none')
        branches = []
        if len(resolved) == len(cells) and any(s['kind'] == 'reader' and s['redeemable'] for s in cand):
            branches = [{'reading': {'c_signal': c}, 'outcome': 'review' if c > limit else 'verified',
                         'completes_all_three': True} for c in a['c_domain']]
        plans.append({'route': 'continuous', 'commission_fixed': bool(fixed),
                      'steps': [{'kind': s['kind'], 'redeemable': s['redeemable']} for s in cand],
                      'redeemable': bool(cand) and all(s['redeemable'] for s in cand),
                      'guarantee': guarantee, 'keeps_sealed_sample': True,
                      'reserve_after': a['reserve'],
                      'objection_review_after': a['reserve'] >= a['required'],
                      'branches': branches})
    if 'joint_unfold' in routes:
        branches = [{'reading': {'orientation': o, 'c_signal': c},
                     'outcome': 'review' if c > limit else 'verified',
                     'completes_all_three': True} for o in ORIENTATIONS for c in a['c_domain']] if joint_ok else []
        plans.append({'route': 'joint_unfold', 'commission_fixed': bool(fixed),
                      'steps': [{'kind': 'joint_unfold', 'redeemable': joint_ok}],
                      'redeemable': joint_ok,
                      'guarantee': 'all_three' if joint_ok else 'none',
                      'keeps_sealed_sample': False,
                      'reserve_after': a['reserve'] - a['required'] if joint_ok else a['reserve'],
                      'objection_review_after': False if joint_ok else a['reserve'] >= a['required'],
                      'branches': branches})
    return {'batch': batch, 'window': window, 'budget': budget,
            'orientation': orientation,
            'cells': a['cells_out'],
            'signable_now': signable,
            'invariants': ['one common orientation governs every frame; feasible interpretations never mix orientations',
                           '%s remain one-high(%s)/one-low(%s) under every compatible mounting' % ('/'.join(id_cells), id_vals[-1], id_vals[0])],
            'dependencies': ['%s are bound by one common mounting orientation' % '/'.join(id_cells),
                             '%s is independent within its screening range %s' % (a['ranged_cell'], a['c_domain'])],
            'reserve': {'required': a['required'], 'untouched': a['reserve'],
                        'objection_review': 'available' if a['reserve'] >= a['required'] else 'consumed'},
            'references': refs,
            'actions': actions,
            'plans': plans,
            'note': 'commission is fixed before reading; a conclusion is signable only when it holds under every still-compatible mounting, never by picking the best operation per hidden orientation'}


def import_data(db, data):
    touched = set()
    with db:
        for r in data.get('references', []):
            if record(db, 'reference-sample', r):
                db.execute('INSERT INTO reference_sample VALUES(?,?,?,?)', (r['id'], 'available', None, 1))
        for s in data.get('screenings', []):
            s = dict(s)
            s.setdefault('id', 'SCREEN-' + s['batch'])
            if record(db, 'assay-screening', s):
                db.execute('INSERT INTO assay_screening VALUES(?,?,?)', (s['batch'], s['limit'], json.dumps(s, ensure_ascii=False, sort_keys=True)))
            touched.add(s['batch'])
        for c in data.get('commissions', []):
            if record(db, 'assay-commission', c):
                db.execute('INSERT INTO assay_commission VALUES(?,?,?,?)', (c['id'], c['batch'], c.get('window', 'W1'), c['route']))
            touched.add(c['batch'])
        for r in data.get('resources', []):
            if record(db, 'assay-resource', r):
                db.execute('INSERT INTO assay_resource VALUES(?,?,?)', (r['id'], r.get('window', 'W1'), int(bool(r.get('reader_available', True)))))
        for a in data.get('actions', []):
            touched.add(a['batch'])
            if not record(db, 'assay-action', a):
                continue  # 重复导入同一动作不得重领
            effective = _apply_action(db, a)
            db.execute('INSERT INTO assay_action VALUES(?,?,?,?,?,?)', (a['id'], a['batch'], a.get('window', 'W1'), a['kind'], json.dumps(a, ensure_ascii=False, sort_keys=True), int(effective)))
        return [decide(db, b) for b in sorted(touched) if db.execute('SELECT 1 FROM assay_screening WHERE batch=?', (b,)).fetchone()]
