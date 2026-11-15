"""Pocket Agent 后端：在手机上按项目开对话，派给本机的 Claude Code 或 Codex 干活，实时看它在做什么。

由 server.py 挂在 /api/console/* 下，所有接口都要口令。
每条对话 = 一个项目目录 + 一个 AI + 一个续接的会话 id。每发一句话就在后台跑一次
`claude -p --resume` 或 `codex exec resume`，输出按行解析成「正在干什么」，跑完把回复写回对话。
不常驻进程：跑完就退，服务重启也不会丢上下文（上下文在 CLI 自己的会话里）。
"""
import calendar, json, os, re, shutil, signal, subprocess, threading, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
MSGS = os.path.join(DATA, 'msgs')
FILES = os.path.join(DATA, 'files')
THREADS = os.path.join(DATA, 'threads.json')
METER = os.path.join(DATA, 'meter.json')
QUOTA = os.path.join(DATA, 'quota.json')
# 改名、置顶、文件夹都记在这台（汇总）上，按手机上看到的完整 id 存：MacBook 的对话也一样能整理，不用改那边的代码
META = os.path.join(DATA, 'meta.json')
for d in (MSGS, FILES): os.makedirs(d, exist_ok=True)

HOME = os.path.expanduser('~')
# 所有因人而异的东西都在 config.json 里（见 config.example.json），代码里不写死任何人的路径和模型
CFG = {}
try:
    with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as _f: CFG = json.load(_f)
except FileNotFoundError: pass
CLAUDE = CFG.get('claude_bin') or shutil.which('claude') or 'claude'
CODEX = os.path.expanduser(CFG.get('codex_bin') or shutil.which('codex') or 'codex')
# Codex 不传模型就走 ~/.codex/config.toml 的默认，可能是最贵的那档；想省额度就在配置里写死
CODEX_EFFORT = ['-c', 'model_reasoning_effort=' + CFG['codex_effort']] if CFG.get('codex_effort') else []
CODEX_ARGS = (['-m', CFG['codex_model']] if CFG.get('codex_model') else []) + CODEX_EFFORT
# 项目只能从这几个根目录下选：手机传来的路径不能随便指到系统目录去
ROOTS = [os.path.realpath(os.path.expanduser(r)) for r in (CFG.get('roots') or ['~/projects'])]

LOCK = threading.Lock()
LIVE = {}    # tid -> {'proc', 'lines': [...], 'started', 'queue': [(text, files, msg_id)]}

# Claude 可选的模型和强度；空串 = 不传参数，走本机 ~/.claude/settings.json 的默认
MODELS = ('', 'opus', 'sonnet', 'haiku', 'fable')
EFFORTS = ('', 'low', 'medium', 'high', 'xhigh', 'max')
# Codex 可换的模型；空串 = CODEX_ARGS 里的默认模型。强度不让在手机上选，统一用配置里的 codex_effort，免得手滑开太高把额度烧光
XMODELS = tuple([''] + list(CFG.get('codex_models') or []))

SYS = ('你现在是被用户从手机控制台远程派活的，用户不在电脑前，看不到终端。'
       '调用 Bash 时 description 写中文，用户在手机上实时看这些描述。'
       '回复用中文、简短，先说结论；做完要说清楚改了什么、结果在哪（给手机能打开的地址，不要给 localhost）。'
       '没法验证的直说。不要部署到服务器、不要 git push、不要删除用户文件，除非用户在这条消息里明确要求。'
       # 手机上打字慢：要用户拍板的地方让 AI 出成按钮，前端认 ```choose 代码块渲染成可点的选项
       '需要用户做选择时（选方案、确认方向、回答你的提问），不要让用户打字，用下面的格式写，手机上会变成可点的按钮：\n'
       '```choose\n? 问题（一句话）\n- 选项一\n- 选项二（推荐）\n```\n'
       '每个问题单独一个块，2–5 个选项，选项要短；可以多选的把第一行写成 ```choose 多选；用户总能另外打字补充，不用写「其他」。'
       '用户点完会发回「问题 → 选项」的文字。')
# 想让 AI 用别的语言回复、或者加自己的规矩：config.json 里写 system_prompt 整段替换（```choose 按钮的格式说明建议保留）
SYS = CFG.get('system_prompt') or SYS


def _load(fp, default):
    try:
        with open(fp, encoding='utf-8') as f: return json.load(f)
    except Exception: return default

def _save(fp, obj):
    # 先写 .tmp 再 rename：写到一半断电也不会留下半个 JSON
    tmp = fp + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, fp)

def now(): return time.strftime('%Y-%m-%d %H:%M:%S')
def mfile(tid): return os.path.join(MSGS, tid + '.json')


def projects():
    out = []
    for r in ROOTS:
        try: names = sorted(os.listdir(r))
        except OSError: continue
        for n in names:
            p = os.path.join(r, n)
            if not n.startswith('.') and os.path.isdir(p): out.append(p)
    # 最近动过的项目排前面，手机上下拉框不用翻到底
    return sorted(out, key=lambda p: -os.path.getmtime(p))

def safe_dir(p):
    p = os.path.realpath(os.path.expanduser(str(p or '')))
    return p if any(p == r or p.startswith(r + os.sep) for r in map(os.path.realpath, ROOTS)) and os.path.isdir(p) else None


# 服务重启时，上次没跑完的进程已经和我们断开，状态改掉，免得手机上一直转圈。
# 只能由 server.py 启动时调：以前写在模块顶层，任何人 import console 做个测试都会把正在跑的对话误标成出错
def reset_stale():
    with LOCK:
        _ts = _load(THREADS, [])
        for _t in _ts:
            if _t.get('status') == 'running': _t['status'] = 'error'; _t['note'] = '服务重启，这一轮被中断了'
        _save(THREADS, _ts)


def meter():
    return {'local': _load(METER, {})}

# ---------- 订阅额度（5 小时窗口 / 本周） ----------
# 都是两家自己报的百分比，不是估算：Claude 每次跑都会吐一条 rate_limit_event，控制台跑的时候顺手存下；
# Codex 把额度写在会话记录里，读最近动过的那几个文件就有（桌面版、命令行用的也算进去）
def save_claude_quota(info):
    w = info.get('unifiedWindows') or {}
    q = {k: {'pct': round((w[k].get('utilization') or 0) * 100), 'reset': w[k].get('resetsAt')} for k in ('five_hour', 'seven_day') if k in w}
    if not q: return
    with LOCK:
        a = _load(QUOTA, {}); a['claude'] = dict(q, ts=time.time()); _save(QUOTA, a)

_cq = [0, None]
_MON = {m: i for i, m in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
def _limit_until(l):
    # 额度用光时 Codex 不再写百分比，只在报错里说「try again at Nov 4th, 2026 2:44 AM」（本地时间），从这里抠出解封时间
    m = re.search(r'try again at (\w{3}) (\d+)\w*, (\d{4}) (\d+):(\d+) ([AP]M)', l)
    if not m or m.group(1) not in _MON: return None
    h = int(m.group(4)) % 12 + (12 if m.group(6) == 'PM' else 0)
    return time.mktime((int(m.group(3)), _MON[m.group(1)], int(m.group(2)), h, int(m.group(5)), 0, 0, 0, -1))

def _iso_ts(x):
    import datetime
    try: return datetime.datetime.fromisoformat(x.replace('Z', '+00:00')).timestamp()
    except Exception: return time.time()

def codex_quota():
    if time.time() - _cq[0] < 30: return _cq[1]
    root, r, lim = os.path.join(os.path.expanduser('~'), '.codex', 'sessions'), None, None
    fs = []
    for d, _, ns in os.walk(root):
        fs += [os.path.join(d, n) for n in ns if n.endswith('.jsonl')]
    # 看最近动过的 40 个会话，按每条记录自己的时间取最新的一条带百分比的。不能按文件修改时间：老对话续聊时文件是新的，
    # 里面最后一条百分比却可能是好几天前的。只认带 used_percent 的：新版还会写 limit_id=premium 的空记录，
    # 额度用光后更是只有空记录＋报错（11-03 两次因此显示错）
    ts_of = lambda l: (re.search(r'"timestamp":"([^"]+)"', l) or [None, ''])[1]
    good, hit = ('', None), ('', None)
    for fp in sorted(fs, key=os.path.getmtime, reverse=True)[:40]:
        for l in _tail_lines(fp, 200000):
            if '"rate_limits"' in l and '"used_percent"' in l:
                if ts_of(l) > good[0]: good = (ts_of(l), l)
            elif 'usage_limit_exceeded' in l and ts_of(l) > hit[0]: hit = (ts_of(l), l)
    if good[1]:
        try:
            d = json.loads(good[1]); rl = ((d.get('payload') or {}).get('rate_limits') or {})
            r = {k2: {'pct': round(rl[k]['used_percent']), 'reset': rl[k]['resets_at']} for k, k2 in (('primary', 'five_hour'), ('secondary', 'seven_day')) if rl.get(k)}
            r = dict(r, ts=_iso_ts(good[0])) if r else None
        except Exception: r = None
    # 报错比最新的百分比还晚 = 现在正卡着
    if hit[1] and hit[0] > good[0]: lim = (_limit_until(hit[1]), _iso_ts(hit[0]))
    if lim and lim[0] and lim[0] > time.time():
        # 解封时间和哪个窗口的重置时间对得上，就是哪个用光了；都对不上按长短猜：5 小时以内的是 5 小时窗口
        r, until = dict(r or {}), lim[0]
        near = lambda k: abs(((r.get(k) or {}).get('reset') or 0) - until) < 600
        k = 'five_hour' if near('five_hour') else 'seven_day' if near('seven_day') else 'five_hour' if until - time.time() < 5.2 * 3600 else 'seven_day'
        r[k] = {'pct': 100, 'reset': until}; r['ts'] = lim[1]
    _cq[:] = [time.time(), r]
    return r

def quota():
    return {'claude': _load(QUOTA, {}).get('claude'), 'codex': codex_quota()}

def quota_refresh(who='claude'):
    if who == 'codex':
        # Codex 的额度只写在会话记录里，没跑过就不会更新；让它回一个字写一条新的（配置里可指定一个便宜模型）
        cheap = ['-m', CFG['codex_cheap_model'], '-c', 'model_reasoning_effort=low'] if CFG.get('codex_cheap_model') else []
        try: subprocess.run([CODEX, 'exec', *cheap, '--skip-git-repo-check',
                             '-s', 'read-only', '只回复数字1'], cwd='/tmp', stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
        except Exception as e: return {'error': '没刷新成：' + str(e)[:100]}
        _cq[0] = 0
        return {'ok': True, 'quota': quota()}
    # 手动刷新：用最便宜的 Haiku 回一个字，只为拿到它带回来的额度信息，花的额度可以忽略
    try:
        p = subprocess.run([CLAUDE, '-p', '回复1', '--model', 'haiku', '--output-format', 'stream-json', '--verbose'],
                           cwd='/tmp', stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
        for l in p.stdout.splitlines():
            if '"rate_limit_event"' in l: save_claude_quota(json.loads(l).get('rate_limit_info') or {})
    except Exception as e: return {'error': '没刷新成：' + str(e)[:100]}
    _cq[0] = 0
    return {'ok': True, 'quota': quota()}

def add_usage(agent, cost=0.0, tin=0, tout=0):
    with LOCK:
        m = _load(METER, {})
        day = time.strftime('%Y-%m-%d')
        for k in ('total', day):
            a = m.setdefault(agent, {}).setdefault(k, {'runs': 0, 'cost': 0.0, 'in': 0, 'out': 0})
            a['runs'] += 1; a['cost'] = round(a['cost'] + (cost or 0), 4); a['in'] += tin or 0; a['out'] += tout or 0
        _save(METER, m)


def local_state():
    with LOCK: ts = _load(THREADS, [])
    for t in ts:
        lv = LIVE.get(t['id'])
        if lv: t['live'] = lv['lines'][-1] if lv['lines'] else '启动中…'; t['elapsed'] = int(time.time() - lv['started'])
        t['last'] = _last_line(t['id'])
    # 手机上能选的 Codex 模型来自配置，前端不写死；codex_default 只用来在按钮上显示「默认」是哪个
    return {'threads': ts, 'projects': projects(), 'roots': ROOTS, 'meter': meter(), 'quota': quota(), 'active': active(),
            'codex_models': list(XMODELS[1:]), 'codex_default': CFG.get('codex_model') or ''}

_lastc = {}
def _last_line(tid):
    # 列表卡片上那行摘要：最后一句话的开头，一眼看出这条停在哪。按文件修改时间缓存，列表 2.5 秒刷一次不用每次都读
    fp = mfile(tid)
    try: mt = os.path.getmtime(fp)
    except OSError: return ''
    c = _lastc.get(tid)
    if c and c[0] == mt: return c[1]
    msgs = [m for m in _load(fp, []) if m.get('role') in ('ai', 'user') and not m.get('withdrawn')]
    s = ''
    if msgs:
        m = msgs[-1]
        s = re.sub(r'```[\s\S]*?```', ' ', (m.get('text') or '').split(REF_MARK)[0])
        # 引用回复的那段原话不算这句话的内容，摘要里只留自己说的
        s = re.sub(r'^〔回复这段〕[\s\S]*?〔/回复〕\s*', '', s)
        # 表格行压成一行全是表头单元格，读不出意思，整行丢掉
        s = re.sub(r'(?m)^\s*\|.*$', ' ', s)
        s = re.sub(r'[#>*`|_\-]+', ' ', s)
        s = re.sub(r'\s+', ' ', s).strip()[:90]
        if m['role'] == 'user': s = '你：' + s
    _lastc[tid] = (mt, s)
    return s

def local_thread(tid, after=0):
    with LOCK:
        t = next((x for x in _load(THREADS, []) if x['id'] == tid), None)
        msgs = _load(mfile(tid), [])
    if not t: return {'error': '没有这个对话'}
    lv = LIVE.get(tid)
    return {'thread': t, 'msgs': [m for m in msgs if m['id'] > after and not m.get('withdrawn')],
            'live': lv['lines'][-40:] if lv else [], 'elapsed': int(time.time() - lv['started']) if lv else 0,
            'queued': [q[2] for q in lv['queue']] if lv else []}


def _upd(tid, **kv):
    with LOCK:
        ts = _load(THREADS, [])
        for t in ts:
            if t['id'] == tid: t.update(kv); t['updated'] = now()
        _save(THREADS, ts)

def _add_msg_nolock(tid, role, text, files=(), **extra):
    msgs = _load(mfile(tid), [])
    m = {'id': (msgs[-1]['id'] if msgs else 0) + 1, 'role': role, 'text': text, 'files': list(files), 'ts': now(), **extra}
    msgs.append(m); _save(mfile(tid), msgs)
    return m

def _add_msg(tid, role, text, files=(), **extra):
    with LOCK: return _add_msg_nolock(tid, role, text, files, **extra)

def _opts(b, agent='claude'):
    m, e = str(b.get('model') or ''), str(b.get('effort') or '')
    # net：这个对话里 Codex 能不能联网（含局域网，比如连路由器）。默认关，只给手机上点了「开」的对话放开
    if agent == 'codex': return {'model': m if m in XMODELS else '', 'effort': '', 'net': bool(b.get('net'))}
    return {'model': m if m in MODELS else '', 'effort': e if e in EFFORTS else ''}

def _start(t, text, files):
    _upd(t['id'], status='running', note='')
    threading.Thread(target=_run, args=(t, text, files), daemon=True).start()


def local_act(name, b):
    if name == 'history':
        return {'items': history(force=bool(b.get('force')))}
    if name == 'import':
        return import_session(str(b.get('agent') or ''), str(b.get('session') or ''))
    if name == 'create':
        cwd = safe_dir(b.get('cwd'))
        if not cwd: return {'error': '项目目录不对'}
        agent = 'codex' if b.get('agent') == 'codex' else 'claude'
        t = {'id': uuid.uuid4().hex[:10], 'title': str(b.get('title') or os.path.basename(cwd))[:40], 'agent': agent,
             'cwd': cwd, 'session': None, 'status': 'idle', 'created': now(), 'updated': now(), **_opts(b, agent)}
        with LOCK:
            ts = _load(THREADS, []); ts.insert(0, t); _save(THREADS, ts)
        return {'ok': True, 'thread': t}
    tid = str(b.get('id', ''))
    with LOCK: t = next((x for x in _load(THREADS, []) if x['id'] == tid), None)
    if not t: return {'error': '没有这个对话'}
    if name == 'send':
        text = str(b.get('text', ''))[:8000].strip()
        files = [f for f in (b.get('files') or []) if isinstance(f, str) and re.fullmatch(r'data/files/[\w.\-]+', f)][:9]
        if not text and not files: return {'error': '空消息'}
        # 还在干活时不拒收，先排队：这一轮结束（做完或被停下）后把排队的话合成一句接着发。
        # 判断和入队放在同一把锁里，免得正好赶上上一轮收尾，消息被漏掉
        with LOCK:
            m = _add_msg_nolock(tid, 'user', text, files)
            lv = LIVE.get(tid)
            if lv:
                lv['queue'].append((text, files, m['id']))
                # 「插入」= 排进队再把这一轮停掉：停下后收尾会立刻接着处理队里的话，不用等它做完
                if b.get('now') and lv['proc']:
                    lv['cut'] = True
                    try: os.killpg(lv['proc'].pid, signal.SIGTERM)
                    except Exception: pass
                return {'ok': True, 'queued': True}
            LIVE[tid] = {'proc': None, 'lines': [], 'started': time.time(), 'queue': []}
        _start(t, text, files)
        return {'ok': True}
    if name == 'unqueue':
        # 撤回还没轮到的话：只在队里时能撤；已经发给 AI 的撤不回。消息不真删，打 withdrawn 标记——
        # 消息 id 是「最后一条 +1」，删掉末尾那条会让下一条复用 id，手机端按 id 增量拉取就漏掉了
        mid = int(b.get('mid') or 0)
        with LOCK:
            lv = LIVE.get(tid)
            hit = next((x for x in (lv['queue'] if lv else []) if x[2] == mid), None)
            if not hit: return {'error': '已经发给它了，撤不回'}
            lv['queue'].remove(hit)
            msgs = _load(mfile(tid), [])
            for m in msgs:
                if m['id'] == mid: m['withdrawn'] = True
            _save(mfile(tid), msgs)
        return {'ok': True, 'text': hit[0], 'files': hit[1]}
    if name == 'side':
        # 顺便问一句：--fork-session 带着全部上下文另开一个分支来答，主线的会话不变，
        # 所以它正在干活时也能问，答完它也不知道被问过。答案存成 side 消息，只给手机看
        q = str(b.get('text', ''))[:4000].strip()
        if not q: return {'error': '空消息'}
        if t['agent'] != 'claude' or not t.get('session'): return {'error': '只有聊过至少一句的 Claude 对话能顺便问'}
        threading.Thread(target=_side, args=(t, q), daemon=True).start()
        return {'ok': True}
    if name == 'set':
        # 改模型/强度：对 Claude 从下一句起生效，--resume 换模型也能接上原来的上下文
        _upd(tid, **_opts(b, t['agent'])); return {'ok': True}
    if name == 'stop':
        lv = LIVE.get(tid)
        if lv and lv['proc']:
            # 整个进程组一起杀：Claude 调起的 Codex、跑着的脚本都要停
            try: os.killpg(lv['proc'].pid, signal.SIGTERM)
            except Exception: pass
        return {'ok': True}
    if name == 'mark':
        st = b.get('status')
        if st not in ('done', 'idle', 'review'): return {'error': 'bad status'}
        _upd(tid, status=st); return {'ok': True}
    if name == 'delete':
        if tid in LIVE: return {'error': '还在跑，先停'}
        with LOCK:
            _save(THREADS, [x for x in _load(THREADS, []) if x['id'] != tid])
        return {'ok': True}
    return {'error': 'unknown'}


# ---------- 接上电脑上已有的对话 ----------
# 在电脑终端/桌面版里开过的 Claude、Codex 会话，记录都在本机文件里；挂成控制台对话后，
# 发话走的还是 --resume / exec resume，上下文原样接上。只读这些记录，不改它们。
CLAUDE_PROJ = os.path.join(HOME, '.claude', 'projects')
CODEX_SESS = os.path.join(HOME, '.codex', 'sessions')
CODEX_INDEX = os.path.join(HOME, '.codex', 'session_index.jsonl')
_hist = [0.0, []]
_meta = {}   # fp -> (mtime, size, 条目)

# 每次都把几百个会话文件的尾巴读一遍太费：文件没变就直接用上次解析出来的标题、目录
def _cached(fp):
    c = _meta.get(fp)
    try: st = os.stat(fp)
    except OSError: return {}
    if c and c[0] == st.st_mtime and c[1] == st.st_size: return c[2]
    return None

def _keep(fp, item):
    st = os.stat(fp); _meta[fp] = (st.st_mtime, st.st_size, item)
    return item

def _jl(lines):
    for l in lines:
        try: yield json.loads(l)
        except Exception: pass

def _tail_lines(fp, n):
    # 会话文件动辄几十 MB，只读尾巴；第一行多半是被截断的半行，丢掉
    with open(fp, 'rb') as f:
        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - n))
        ls = f.read().decode('utf-8', 'ignore').splitlines()
    return ls[1:] if size > n else ls

def _head_lines(fp, k):
    out = []
    with open(fp, encoding='utf-8', errors='ignore') as f:
        for l in f:
            out.append(l)
            if len(out) >= k: break
    return out

def _say(text):
    # 系统注入的提醒、命令回显这类不是人说的话，开头都是 < 或固定前缀
    s = (text or '').strip()
    if not s or s.startswith('<') or s.startswith('# AGENTS.md') or 'Caveat: The messages below' in s: return ''
    return s

def _claude_text(d):
    m = d.get('message') or {}
    c = m.get('content')
    if isinstance(c, str): return _say(c)
    return _say('\n'.join(x.get('text', '') for x in c or [] if isinstance(x, dict) and x.get('type') == 'text'))

def _claude_items():
    out = []
    for dirn in os.listdir(CLAUDE_PROJ) if os.path.isdir(CLAUDE_PROJ) else []:
        # 工作台自动派的活、/tmp 下的试验，不是要接着干的对话
        if 'studio-data-runs' in dirn or dirn.startswith('-private-'): continue
        dp = os.path.join(CLAUDE_PROJ, dirn)
        for fn in os.listdir(dp) if os.path.isdir(dp) else []:
            fp = os.path.join(dp, fn)
            if not fn.endswith('.jsonl') or os.path.getsize(fp) < 5000: continue
            hit = _cached(fp)
            if hit is not None:
                if hit: out.append(hit)
                continue
            title = cwd = first = entry = None
            for d in reversed(list(_jl(_tail_lines(fp, 400000)))):
                if d.get('type') == 'custom-title' and d.get('customTitle'): title = d['customTitle']; break
                if d.get('type') == 'ai-title' and not title: title = d.get('aiTitle')
            # 用开头的 cwd：会话文件按启动目录存，--resume 必须在那个目录下跑才找得到
            for d in _jl(_head_lines(fp, 80)):
                cwd = cwd or d.get('cwd'); entry = entry or d.get('entrypoint')
                if not first and d.get('type') == 'user' and not d.get('isMeta'): first = _claude_text(d)
            if not cwd or not (title or first): _keep(fp, {}); continue
            out.append(_keep(fp, {'agent': 'claude', 'session': fn[:-6], 'title': _short(title or first, 60), 'cwd': cwd,
                        'mtime': os.path.getmtime(fp), 'size': os.path.getsize(fp), 'fp': fp,
                        # 不是在终端里手动开的（claude -p 被脚本/工作台/别的 AI 调起），默认收起来
                        'auto': entry != 'cli'}))
    return out

def _codex_items():
    # 只列桌面版里起过名字的对话；codex exec 跑出来的多是别的流程派的子任务，列出来只会刷屏
    names = {d.get('id'): d.get('thread_name') for d in _jl(open(CODEX_INDEX, encoding='utf-8'))} if os.path.isfile(CODEX_INDEX) else {}
    out = []
    for root, _, fns in os.walk(CODEX_SESS):
        for fn in fns:
            sid = fn[-42:-6]
            if not fn.endswith('.jsonl') or not names.get(sid): continue
            fp = os.path.join(root, fn)
            hit = _cached(fp)
            if hit is not None:
                if hit: out.append(dict(hit, title=_short(names[sid], 60)))
                continue
            meta = next(_jl(_head_lines(fp, 1)), {}).get('payload') or {}
            # 桌面版每做一步有风险的操作，都会另起一个「Guardian review」子会话审批，也带名字；这类子会话不是人开的对话
            if not meta.get('cwd') or meta.get('parent_thread_id') or (isinstance(meta.get('source'), dict) and meta['source'].get('subagent')):
                _keep(fp, {}); continue
            out.append(_keep(fp, {'agent': 'codex', 'session': sid, 'title': _short(names[sid], 60), 'cwd': meta['cwd'],
                        'mtime': os.path.getmtime(fp), 'size': os.path.getsize(fp), 'fp': fp, 'auto': False}))
    return out

def history(force=False):
    if force or time.time() - _hist[0] > 60:
        _hist[:] = [time.time(), sorted(_claude_items() + _codex_items(), key=lambda x: -x['mtime'])]
    with LOCK: bound = {t.get('session'): t['id'] for t in _load(THREADS, []) if t.get('session')}
    out = []
    for x in _hist[1]:
        y = {k: v for k, v in x.items() if k != 'fp'}
        y['updated'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(x['mtime']))
        y['tid'] = bound.get(x['session'])
        y['gone'] = not os.path.isdir(x['cwd'])
        # 刚动过的多半还开在电脑上；两边同时往一个会话里写，后写的会把上下文搅乱
        y['busy'] = time.time() - x['mtime'] < 600
        out.append(y)
    return out

def _recent(x, k=6):
    # 接过来时带上最后几句，手机上一看就知道之前聊到哪了
    msgs = []
    lines = _tail_lines(x['fp'], 3000000)
    if x['agent'] == 'claude':
        for d in _jl(lines):
            if d.get('isSidechain') or d.get('isMeta'): continue
            role = {'user': 'user', 'assistant': 'ai'}.get(d.get('type'))
            s = _claude_text(d) if role else ''
            if not s: continue
            # 一条回复会拆成好几行记录，连着的同一角色并成一条
            if msgs and msgs[-1][0] == role: msgs[-1][1] += '\n\n' + s
            else: msgs.append([role, s])
    else:
        for d in _jl(lines):
            p = d.get('payload') or {}
            if d.get('type') != 'response_item' or p.get('type') != 'message' or p.get('role') not in ('user', 'assistant'): continue
            s = _say('\n'.join(c.get('text', '') for c in p.get('content') or [] if isinstance(c, dict)))
            if not s: continue
            role = 'user' if p['role'] == 'user' else 'ai'
            if msgs and msgs[-1][0] == role: msgs[-1][1] += '\n\n' + s
            else: msgs.append([role, s])
    return [(r, s if len(s) < 3000 else s[:3000] + '\n…（太长，后面省略）') for r, s in msgs[-k:]]

def import_session(agent, sid):
    _hist[0] = 0  # 强制重扫，防止拿着一分钟前的旧列表
    x = next((y for y in history() if y['agent'] == agent and y['session'] == sid), None)
    if not x: return {'error': '电脑上没找到这个对话'}
    if x['tid']: return {'ok': True, 'thread': {'id': x['tid']}}
    if x['gone']: return {'error': '它原来的项目目录不在了：' + x['cwd']}
    fp = next(y['fp'] for y in _hist[1] if y['session'] == sid)
    t = {'id': uuid.uuid4().hex[:10], 'title': x['title'][:40], 'agent': agent, 'cwd': x['cwd'], 'session': sid,
         'status': 'idle', 'created': now(), 'updated': now(), 'model': '', 'effort': '', 'imported': True}
    head = ('（这是从电脑上接过来的%s对话，最后动过：%s。下面是最后几句，直接发话就能接着干，它记得前面所有内容。）'
            % ('Claude' if agent == 'claude' else 'Codex', x['updated']))
    with LOCK:
        ts = _load(THREADS, [])
        if any(y.get('session') == sid for y in ts): return {'error': '刚刚已经接过了，刷新一下'}
        ts.insert(0, t); _save(THREADS, ts)
        _add_msg_nolock(t['id'], 'ai', head)
        for r, s in _recent({**x, 'fp': fp}): _add_msg_nolock(t['id'], r, s)
    return {'ok': True, 'thread': t}


def _short(s, n=120):
    s = ' '.join(str(s).split())
    return s if len(s) <= n else s[:n] + '…'

def _tool_line(name, inp):
    # 把工具调用翻成人话，手机上一眼能看懂它在干嘛
    inp = inp or {}
    fp = lambda: os.path.basename(str(inp.get('file_path') or inp.get('path') or ''))
    if name == 'Bash': return '▶ ' + _short(inp.get('description') or inp.get('command'))
    if name == 'Read': return '📖 读 ' + fp()
    if name in ('Edit', 'MultiEdit'): return '✏️ 改 ' + fp()
    if name == 'Write': return '📝 写 ' + fp()
    if name in ('Grep', 'Glob'): return '🔍 搜 ' + _short(inp.get('pattern'), 60)
    if name in ('WebSearch', 'WebFetch'): return '🌐 ' + _short(inp.get('query') or inp.get('url'), 80)
    if name == 'Agent': return '🤖 派子任务 ' + _short(inp.get('description'), 60)
    return '🔧 ' + name


def _run(t, text, files):
    tid, lv = t['id'], LIVE[t['id']]
    prompt = text + ''.join(f'\n[附件] {os.path.join(HERE, f)}' for f in files)
    env = dict(os.environ)
    if t['agent'] == 'claude':
        cmd = [CLAUDE, '-p', prompt, '--output-format', 'stream-json', '--verbose',
               '--permission-mode', 'auto', '--append-system-prompt', SYS]
        if t.get('model'): cmd += ['--model', t['model']]
        if t.get('effort'): cmd += ['--effort', t['effort']]
        if t.get('session'): cmd += ['--resume', t['session']]
    else:
        imgs = [os.path.join(HERE, f) for f in files if re.search(r'\.(png|jpe?g|webp|gif)$', f, re.I)]
        xa = ['-m', t['model'], *CODEX_EFFORT] if t.get('model') in XMODELS[1:] else CODEX_ARGS
        base = ['--json', *xa, '--skip-git-repo-check', '-c', 'sandbox_mode="workspace-write"']
        # workspace-write 的 Seatbelt 默认拒绝所有出站连接（报 Operation not permitted）。只放开网络，写文件的限制照旧；
        # Codex 的域名白名单走 HTTP/SOCKS 代理、还默认挡内网 IP，ssh 这种直连 TCP 用不上，所以按对话整体开关
        if t.get('net'): base += ['-c', 'sandbox_workspace_write.network_access=true']
        for i in imgs: base += ['-i', i]
        p2 = SYS + '\n\n用户：' + prompt if not t.get('session') else prompt
        cmd = [CODEX, 'exec', 'resume', t['session'], *base, p2] if t.get('session') else [CODEX, 'exec', *base, '-C', t['cwd'], p2]
    reply, sess, err = [], t.get('session'), ''
    try:
        p = subprocess.Popen(cmd, cwd=t['cwd'], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        lv['proc'] = p
        for line in p.stdout:
            try: d = json.loads(line)
            except Exception: continue
            if t['agent'] == 'claude': sess, r = _claude_evt(d, lv, sess)
            else: sess, r = _codex_evt(d, lv, sess)
            if r is not None: reply.append(r)
        p.wait()
        if p.returncode not in (0, None) and not reply:
            err = '被停止了' if p.returncode < 0 or p.returncode == 143 else f'出错退出（{p.returncode}）：' + '\n'.join(lv['lines'][-3:])
    except Exception as e:
        err = '启动失败：' + str(e)
    finally:
        out = '\n\n'.join(x for x in reply if x).strip() or err or ('✓ 上下文压缩好了，聊过的要点它还记得，接着说就行' if text.strip() == '/compact' else '（没有文字回复）')
        if text.strip() == '/context' and reply: out = _ctx_brief(out)
        files_out = _find_files(out, t['cwd'])
        _add_msg(tid, 'ai', out, files_out)
        # 用户自己点的「停下」不算出错
        _upd(tid, session=sess, status='error' if err and not reply and err != '被停止了' else 'review', note=err)
        with LOCK:
            q, cut = lv['queue'], lv.pop('cut', False)
            if q: lv.update(proc=None, lines=[], started=time.time(), queue=[])
            else: LIVE.pop(tid, None)
        if q:
            # 重新读一遍对话：要拿到刚存下的会话 id，以及中途改过的模型/强度
            with LOCK: t2 = next((x for x in _load(THREADS, []) if x['id'] == tid), None)
            if not t2:
                with LOCK: LIVE.pop(tid, None)
                return
            head = ('（我刚把你上一轮打断了，先处理下面这句，处理完再看上一轮要不要接着做）\n' if cut
                    else '（你上一轮干活时，我又补充了下面的话）\n' if len(q) > 1 or reply else '')
            _start(t2, head + '\n\n'.join(x[0] for x in q if x[0]), [f for x in q for f in x[1]][:9])


def _ctx_brief(s):
    # /context 原文会把每个技能、MCP 工具都列一遍，手机上要翻好几屏；只留总量和分类那张表
    m = re.search(r'\*\*Tokens:\*\*\s*([\d.]+k?)\s*/\s*([\d.]+[km]?)\s*\((\d+)%\)', s)
    if not m: return s
    pc = int(m.group(3))
    head = f'📊 上下文用了 **{pc}%**（{m.group(1)} / {m.group(2)}）' + ('，还很宽裕' if pc < 50 else '，快满了，可以点「压缩上下文」' if pc >= 80 else '')
    tb = re.search(r'### Estimated usage by category\n+((?:\|.*\n?)+)', s)
    return head + ('\n\n' + tb.group(1).strip() if tb else '')


def _side(t, q):
    cmd = [CLAUDE, '-p', q, '--resume', t['session'], '--fork-session', '--output-format', 'json',
           '--permission-mode', 'plan', '--append-system-prompt', '这是用户顺便插的一个问题，简短回答，不要动手改任何东西。']
    if t.get('model'): cmd += ['--model', t['model']]
    try:
        p = subprocess.run(cmd, cwd=t['cwd'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
        d = json.loads(p.stdout or '{}')
        ans = (d.get('result') or '').strip() or '（没答出来）'
    except Exception as e:
        ans = '没问成：' + str(e)[:200]
    # 问题记在同一条里，手机上问答排在一起
    _add_msg(t['id'], 'side', ans, q=q)


def _claude_evt(d, lv, sess):
    ty = d.get('type')
    if ty == 'system' and d.get('session_id'): sess = d['session_id']
    if ty == 'rate_limit_event': save_claude_quota(d.get('rate_limit_info') or {})
    if ty == 'assistant':
        for c in d['message'].get('content', []):
            if c.get('type') == 'tool_use': lv['lines'].append(_tool_line(c.get('name'), c.get('input')))
            elif c.get('type') == 'text' and c.get('text', '').strip(): lv['lines'].append('💬 ' + _short(c['text']))
    if ty == 'result':
        sess = d.get('session_id') or sess
        u = d.get('usage') or {}
        add_usage('claude', d.get('total_cost_usd') or 0, (u.get('input_tokens') or 0) + (u.get('cache_read_input_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0), u.get('output_tokens') or 0)
        for x in d.get('permission_denials') or []:
            lv['lines'].append('⛔ 被拦下：' + _tool_line(x.get('tool_name'), x.get('tool_input')))
        r = d.get('result') or ''
        if d.get('permission_denials'):
            r += '\n\n> 有 %d 个操作被自动拦下（风险较高），要做的话在消息里明确说一声。' % len(d['permission_denials'])
        return sess, r
    return sess, None

def _codex_evt(d, lv, sess):
    ty = d.get('type')
    if ty == 'thread.started': sess = d.get('thread_id') or sess
    it = d.get('item') or {}
    if ty == 'item.started' and it.get('type') == 'command_execution':
        lv['lines'].append('▶ ' + _short(re.sub(r"^/bin/\w+ -lc '?|'$", '', it.get('command', ''))))
    if ty == 'item.completed':
        if it.get('type') == 'agent_message':
            lv['lines'].append('💬 ' + _short(it.get('text', '')))
            return sess, it.get('text', '')
        if it.get('type') == 'file_change':
            lv['lines'].append('✏️ 改 ' + ', '.join(os.path.basename(c.get('path', '')) for c in it.get('changes', []))[:120])
    if ty == 'turn.completed':
        u = d.get('usage') or {}
        add_usage('codex', 0, u.get('input_tokens') or 0, u.get('output_tokens') or 0)
    return sess, None


def _find_files(text, cwd):
    # 回复里提到的本机图片/视频，在项目根目录下的就作为附件显示出来（server.py 的 /f/ 只放行这些）
    out = []
    for m in re.findall(r'(/[^\s`\'"()<>，。]+\.(?:png|jpe?g|webp|gif|mp4|mov))', text, re.I):
        p = os.path.realpath(m)
        if any(p.startswith(r + os.sep) for r in ROOTS) and os.path.isfile(p):
            rel = 'f' + p
            if rel not in out: out.append(rel)
    return out[:6]


# ---------- 电脑上正在跑的（终端/桌面版里开的，不是控制台派的）----------
# 只看会话文件最近有没有被写：Claude、Codex 干活时每一步都往里追加，几十秒不动就是停下等人了
RUNNING, RECENT = 90, 1800
_act = [0.0, []]

_la = {}
def _last_act(x):
    # 文件没变就用上次的结果：手机每几秒拉一次，别每次都把尾巴重读一遍
    try: k = (os.path.getmtime(x['fp']), os.path.getsize(x['fp']))
    except OSError: return ''
    if _la.get(x['fp'], (None,))[0] != k: _la[x['fp']] = (k, _last_act0(x))
    return _la[x['fp']][1]

def _last_act0(x):
    for d in reversed(list(_jl(_tail_lines(x['fp'], 300000)))):
        if x['agent'] == 'claude':
            if d.get('isSidechain'): continue
            c = (d.get('message') or {}).get('content')
            if d.get('type') == 'assistant' and isinstance(c, list):
                for it in reversed(c):
                    if it.get('type') == 'tool_use': return _tool_line(it.get('name'), it.get('input'))
                    if it.get('type') == 'text' and it.get('text', '').strip(): return '💬 ' + _short(it['text'], 100)
            elif d.get('type') == 'user' and _claude_text(d): return '🙋 ' + _short(_claude_text(d), 80)
        else:
            p = d.get('payload') or {}
            if p.get('type') == 'function_call':
                try: a = json.loads(p.get('arguments') or '{}')
                except Exception: a = {}
                cmd = a.get('cmd') or a.get('command')
                return '▶ ' + _short(' '.join(cmd) if isinstance(cmd, list) else cmd or p.get('name'), 100)
            if p.get('type') == 'message' and p.get('role') in ('assistant', 'user'):
                s = _say('\n'.join(c.get('text', '') for c in p.get('content') or [] if isinstance(c, dict)))
                if s: return ('💬 ' if p['role'] == 'assistant' else '🙋 ') + _short(s, 100)
    return ''

_lt = {}
def _last_ts(x):
    # 「在跑」看会话里最后一条记录的时间，不看文件修改时间：有东西会只碰文件不写内容
    # （workbench-v2 那个会话 10-25 就停了，文件时间却一直在变，手机上就一直显示在跑）
    try: k = (os.path.getmtime(x['fp']), os.path.getsize(x['fp']))
    except OSError: return 0
    if _lt.get(x['fp'], (None,))[0] != k:
        ts = 0
        for d in reversed(list(_jl(_tail_lines(x['fp'], 300000)))):
            v = d.get('timestamp')
            if isinstance(v, str) and len(v) >= 19:
                try: ts = calendar.timegm(time.strptime(v[:19], '%Y-%m-%dT%H:%M:%S')); break
                except ValueError: pass
        _lt[x['fp']] = (k, ts or k[0])
    return _lt[x['fp']][1]

def active():
    # 手机每两三秒拉一次 state：列表一分钟才重扫一次，这里只 stat 文件看有没有在变，便宜
    if time.time() - _act[0] < 2: return _act[1]
    if time.time() - _hist[0] > 60: _hist[:] = [time.time(), sorted(_claude_items() + _codex_items(), key=lambda x: -x['mtime'])]
    with LOCK: mine = {t.get('session') for t in _load(THREADS, []) if t.get('session') and t['id'] in LIVE}
    out = []
    for x in _hist[1]:
        if x['auto'] or x['session'] in mine: continue
        # 先用文件时间粗筛（便宜），近期动过的再读最后一条记录的真实时间
        try:
            if time.time() - os.path.getmtime(x['fp']) > RECENT: continue
        except OSError: continue
        mt = _last_ts(x)
        if time.time() - mt > RECENT: continue
        out.append({'agent': x['agent'], 'session': x['session'], 'title': x['title'], 'cwd': x['cwd'],
                    'updated': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mt)),
                    'running': time.time() - mt < RUNNING, 'live': _last_act(x)})
        if len(out) >= 8: break   # 按最近排的，列太多会把控制台自己的对话挤到下面去
    _act[:] = [time.time(), sorted(out, key=lambda y: (y['running'], y['updated']), reverse=True)]
    return _act[1]

def peek(agent, sid):
    # 只看不接：读最后十来句给手机看，不建对话、不调模型
    x = next((y for y in _hist[1] if y['agent'] == agent and y['session'] == sid), None)
    if not x:
        _hist[0] = 0; history()
        x = next((y for y in _hist[1] if y['agent'] == agent and y['session'] == sid), None)
    if not x: return {'error': '电脑上没找到这个对话'}
    mt = _last_ts(x)
    return {'title': x['title'], 'cwd': x['cwd'], 'agent': agent, 'session': sid, 'live': _last_act(x),
            'running': time.time() - mt < RUNNING, 'updated': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mt)),
            'msgs': [{'role': r, 'text': s} for r, s in _recent(x, 12)]}


# ---------- 多台电脑合在一起 ----------
# 本机的对话 id 原样；另一台的加上「机器:」前缀，手机端照常拿 id 发请求，这里按前缀转过去
LOCAL_NAME = CFG.get('machine_name') or os.uname().nodename.split('.')[0]


_askc = {}
def _asks(tid):
    # 最后一条 AI 回复里有选择题 = 在等用户拍板，通知里要说「等你选」而不是「做完了」
    fp = mfile(tid)
    try: mt = os.path.getmtime(fp)
    except OSError: return False
    c = _askc.get(tid)
    if c and c[0] == mt: return c[1]
    ms = [m for m in _load(fp, []) if m.get('role') == 'ai']
    v = bool(ms) and '```choose' in (ms[-1].get('text') or '')
    _askc[tid] = (mt, v); return v

def brief():
    day = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - 86400))   # 一天没动的不可能刚做完，不发
    # 手机 App 在后台盯进度用：只要标题和状态，比整份 state 小得多，走流量时每 15 秒拉一次也不心疼
    return {'threads': [{'id': t['id'], 'title': t.get('title', ''),
                         'status': t.get('status'), 'last': (t.get('last') or '')[:60], 'mname': t.get('mname', ''),
                         'ask': not t.get('machine') and t.get('status') == 'review' and _asks(t['id'])} for t in state()['threads'] if t.get('status') == 'running' or t.get('updated', '') >= day]}

def state():
    s = local_state()
    for t in s['threads']: t['machine'], t['mname'] = '', LOCAL_NAME
    # 必须复制一份：active() 返回的是 2 秒缓存里的那个列表，原地 += 会把另一台的会话塞进本机缓存，
    # 下一次再被当成本机的标一遍、又追加一遍，列表里就出现一条没标电脑的重复项
    s['active'] = [dict(a, machine='', mname=LOCAL_NAME) for a in s['active']]
    s['threads'].sort(key=lambda t: t.get('updated', ''), reverse=True)
    meta = _load(META, {})
    for t in s['threads']: _apply_meta(t, meta)
    for t in s['threads']:
        # 待办只算真要用户动手的：AI 在问、让选，或者出错；单纯「做完了」不算，免得越堆越多
        t['ask'] = not t.get('machine') and t.get('status') == 'review' and _asks(t['id'])
    s['folders'] = sorted({x['folder'] for x in meta.values() if x.get('folder')})
    s['machines'] = [{'id': '', 'name': LOCAL_NAME, 'online': True}]
    return s

def _apply_meta(t, meta):
    x = meta.get(t['id']) or {}
    if x.get('title'): t['title'] = x['title']
    t['pin'], t['folder'] = x.get('pin') or 0, x.get('folder') or ''
    return t

def tidy():
    # 「全部收起」：做完了、AI 没在问的，从「待你看」挪到「其他」；在问的和出错的留着
    with LOCK:
        ts = _load(THREADS, []); n = 0
        for t in ts:
            if t.get('status') == 'review' and not _asks(t['id']): t['status'] = 'idle'; n += 1
        _save(THREADS, ts)
    return {'ok': True, 'n': n}

def set_meta(b):
    tid = str(b.get('id') or '')
    if not tid: return {'error': '没有这个对话'}
    with LOCK:
        meta = _load(META, {}); x = meta.setdefault(tid, {})
        if 'title' in b: x['title'] = str(b['title'] or '').strip()[:40]
        # 置顶存的是置顶的时间，越晚置顶的排越上面
        if 'pin' in b: x['pin'] = time.time() if b['pin'] else 0
        if 'folder' in b: x['folder'] = str(b['folder'] or '').strip()[:16]
        meta[tid] = {k: v for k, v in x.items() if v}
        if not meta[tid]: meta.pop(tid)
        _save(META, meta)
    return {'ok': True}

def thread(tid, after=0):
    r = local_thread(tid, after)
    if r.get('thread'): _apply_meta(r['thread'], _load(META, {}))
    return r

# ---------- 搜历史 ----------
def _snip(text, q, n=36):
    # 命中位置前后各截一段，手机上一行半能看出上下文
    i = text.lower().find(q)
    if i < 0: return ''
    a, b = max(0, i - n), min(len(text), i + len(q) + n * 2)
    s = re.sub(r'\s+', ' ', text[a:b]).strip()
    return ('…' if a else '') + s + ('…' if b < len(text) else '')

def _hit_msgs(msgs, q):
    # 一条对话里命中好几句，只给最近的那句（最可能是要找的），再报个总数
    hits = [m for m in msgs if q in (m.get('text') or '').lower() and not m.get('withdrawn')]
    if not hits: return None
    m = hits[-1]
    return {'mid': m.get('id'), 'hit': _snip(m['text'], q), 'role': m.get('role'), 'n': len(hits)}

def search(q):
    q = (q or '').strip().lower()
    if len(q) < 1: return {'threads': [], 'pc': []}
    threads = []
    with LOCK: ts = _load(THREADS, [])
    meta = _load(META, {})
    for t in ts:
        t = _apply_meta(dict(t), meta)
        h = _hit_msgs(_load(mfile(t['id']), []), q)
        if h or q in (t.get('title') or '').lower():
            threads.append(dict(h or {'hit': '', 'n': 0}, id=t['id'], title=t.get('title'), agent=t.get('agent'), cwd=t.get('cwd'),
                                updated=t.get('updated'), machine='', mname=LOCAL_NAME))
    threads.sort(key=lambda x: x.get('updated') or '', reverse=True)
    # 电脑上开过、还没接进派活的：Claude 搜内容（会话文件 ~500M，grep 一遍零点几秒），Codex 文件近 10G 只搜标题
    mine = {t.get('session') for t in ts if t.get('session')}
    if time.time() - _hist[0] > 60: history()
    # 程序自动派的会话（工作台、总结脚本调的 claude -p）全是重复的模板话，搜出来只会刷屏
    items = {x['session']: x for x in _hist[1] if x['session'] not in mine and not x['auto']}
    pc = []
    try:
        r = subprocess.run(['grep', '-rliF', '--include=*.jsonl', q, CLAUDE_PROJ], capture_output=True, text=True, timeout=8)
        fps = sorted((f for f in r.stdout.splitlines() if os.path.basename(f)[:-6] in items), key=os.path.getmtime, reverse=True)[:25]
    except Exception: fps = []
    for fp in fps:
        x = items[os.path.basename(fp)[:-6]]; hit = ''
        # 只认人说的话和 AI 的正文，工具输出里的命中多半是代码和日志，不是要找的
        for d in reversed(list(_jl(_tail_lines(fp, 6000000)))):
            if d.get('type') not in ('user', 'assistant') or d.get('isMeta') or d.get('isSidechain'): continue
            hit = _snip(_claude_text(d), q)
            if hit: break
        if hit or q in x['title'].lower():
            pc.append({'agent': 'claude', 'session': x['session'], 'title': x['title'], 'cwd': x['cwd'], 'hit': hit, 'auto': x['auto'],
                       'updated': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(x['mtime']))})
    seen = {p['session'] for p in pc}
    for x in _hist[1]:
        if x['auto'] or x['session'] in mine or x['session'] in seen or q not in x['title'].lower(): continue
        pc.append({'agent': x['agent'], 'session': x['session'], 'title': x['title'], 'cwd': x['cwd'], 'hit': '', 'auto': x['auto'],
                   'updated': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(x['mtime']))})
    pc.sort(key=lambda p: p['updated'], reverse=True)
    return {'threads': threads[:40], 'pc': pc[:30]}

# ---------- 引用另一个对话 ----------
# 发消息时带上 refs：把被引用那条的记录写成文件、消息里只放路径，让它自己去读（省得一次塞满上下文）
REFS = os.path.join(DATA, 'refs')
REF_MARK = '\n\n〔引用对话〕'
def _ref_src(rid):
    with LOCK: t = next((x for x in _load(THREADS, []) if x['id'] == rid), None)
    msgs = _load(mfile(rid), []) if t else []
    if not t: return None
    t = _apply_meta(dict(t, id=rid), _load(META, {}))
    msgs = [x for x in msgs if x.get('role') in ('user', 'ai') and not x.get('withdrawn') and (x.get('text') or '').strip()]
    return t, msgs

def _ref_line(x):
    # 被引用那条里如果也引用过别的，那段说明不再往下传，免得层层套娃
    return ('你' if x['role'] == 'user' else 'AI') + '：' + (x.get('text') or '').split(REF_MARK)[0].strip()

def ref_block(refs):
    parts, refs = [], [str(r) for r in refs][:5]
    for rid in refs:
        got = _ref_src(rid)
        if not got: continue
        t, msgs = got
        who = 'Claude' if t.get('agent') == 'claude' else 'Codex'
        head = f"《{t.get('title')}》（{who} · {os.path.basename(t.get('cwd') or '')}）"
        os.makedirs(REFS, exist_ok=True)
        fp = os.path.join(REFS, re.sub(r'[^\w.-]', '_', rid) + '.md')
        with open(fp + '.tmp', 'w', encoding='utf-8') as f:
            f.write(f"# {t.get('title')}\n\n{who} · 项目目录 {t.get('cwd')} · 共 {len(msgs)} 句\n\n")
            f.write('\n\n'.join(_ref_line(x) for x in msgs))
        os.replace(fp + '.tmp', fp)
        parts.append(f"- {head}：完整记录在 {fp}（共 {len(msgs)} 句）")
    if not parts: return ''
    tail = '\n请先读这些记录再接着干：先看文件末尾最近的 40 句左右，需要更早的再往上翻。'
    return REF_MARK + '\n' + '\n'.join(parts) + tail

def act(name, b):
    if name == 'send' and b.get('refs'):
        b = dict(b, text=str(b.get('text') or '') + ref_block(b['refs']))
    if name == 'search': return search(str(b.get('q') or ''))
    if name == 'brief': return brief()
    if name == 'quota_refresh': return quota_refresh(str(b.get('who') or 'claude'))
    if name == 'meta': return set_meta(b)
    if name == 'tidy': return tidy()
    if name == 'delete':
        # 只是从手机列表里拿掉；电脑上的会话记录不动，要回来可以「接上电脑上的对话」再接
        r = local_act('delete', b)
        if r.get('ok'):
            with LOCK:
                meta = _load(META, {})
                if meta.pop(str(b.get('id')), None) is not None: _save(META, meta)
        return r
    if name == 'history':
        return {'items': [dict(x, machine='', mname=LOCAL_NAME) for x in local_act(name, b)['items']]}
    if name == 'peek': return peek(str(b.get('agent') or ''), str(b.get('session') or ''))
    return local_act(name, b)
