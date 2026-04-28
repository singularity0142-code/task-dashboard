#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
奇点 Agent — 后台常驻脚本
功能：
  1. 监听 boss_tasks 新任务 → 私信对应成员
  2. 监听成员回复奇点 → 汇总更新 Supabase + 通知老板
  3. 智能路由：知道哪个任务分配给谁
"""

import time, json, urllib.request, urllib.parse, urllib.error, re, os, math
from datetime import datetime

# ── 配置 ──────────────────────────────────────────────
OPENAI_KEY = os.getenv('OPENAI_API_KEY') or 'sk-openai-api-key-fill-me'
OPENAI_MODEL = os.getenv('OPENAI_MODEL') or 'gpt-4.1-mini'
QIDIAN_BOT = '8770441016:AAGP-D6f4htaiKDB4I2xX9A3fffu09LueuA'
BOSS_CHAT   = '8666564306'   # 秦文浩（WENHAO QIN）

SURL = 'https://nkkbvhtmlooverkhyqsa.supabase.co'
SKEY = ''.join(['eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9',
                '.eyJpc3MiOiJzdXBhYmFzZ',
                'SIsInJlZiI6Im5ra2J2aH',
                'RtbG9vdmVya2h5cXNhIiwi',
                'cm9sZSI6ImFub24iLCJpYX',
                'QiOjE3NzU2OTM1NTksImV4',
                'cCI6MjA5MTI2OTU1OX0.7o',
                'RpImggZHpvusrS6kJKWN6c',
                'v12hYG4NqxUFCIZDCIs'])

# 成员 Telegram Chat ID
MEMBERS = {
    '王玟':   '7885643514',   # 测试：用老板账号模拟
    '陈冬梅': '8527399930',
    '吴晓磊': '8759710763',
    '张娜娜': '8482000777',
    '张慧':   None,            # 待绑定
    '韩笑':   None,            # 待绑定
}

# 名字模糊匹配（语音识别可能有偏差）
NAME_ALIASES = {
    '王玟':   ['王玟', '王文', '王雯', '小玟', 'wangwen', 'wang wen'],
    '陈冬梅': ['陈冬梅', '冬梅', 'chendongmei', 'chen dongmei'],
    '吴晓磊': ['吴晓磊', '晓磊', 'wuxiaolei', 'wu xiaolei', '吴晓'],
    '张娜娜': ['张娜娜', '娜娜', 'zhangnana', 'zhang nana'],
    '张慧':   ['张慧', '张会', 'zhanghui', 'zhang hui'],
    '韩笑':   ['韩笑', '韩啸', 'hanxiao', 'han xiao', '汉笑', '含笑', '韩晓', '憨笑', '寒笑', 'HanXiao', 'Hanxiao'],
}

POLL_INTERVAL = 3      # 秒：轮询 Telegram 消息间隔
TASK_CHECK_INTERVAL = 10  # 秒：检查新任务间隔

# 项目名映射（用于从消息中识别项目）
PROJECT_ALIASES = {
    '理享招生项目': ['理享招生', '理享', '招生项目', '招生', '理享前程'],
    'w':            ['w项目', 'W项目', 'w', 'W'],
}

# 等待老板确认项目的挂起记录 {id: {reporter, text, records, chat_id, ts}}
PENDING_CONFIRMATIONS = {}

FINANCE_KEYWORDS = ['收入', '营收', '入账', '回款', '报销', '差旅', '支出', '花了', '费用', '成本', '收了', '收款', '学费', '缴费', '交了', '付了', '付款']
PROGRESS_KEYWORDS = ['招了', '招到', '招生', '完成了', '已招', '共招', '今天招', '本周招', '招聘了', '已经招', '招到了', '招募', '已录', '入职了']

# ── Telegram 工具 ──────────────────────────────────────
def tg_send(chat_id, text, parse_mode='Markdown'):
    url = f'https://api.telegram.org/bot{QIDIAN_BOT}/sendMessage'
    body = json.dumps({'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'[TG发送失败] {e}')
        return None

def tg_get_updates(offset=0):
    url = f'https://api.telegram.org/bot{QIDIAN_BOT}/getUpdates?offset={offset}&limit=20&timeout=2'
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read()).get('result', [])
    except Exception as e:
        if '409' not in str(e):
            print(f'[TG轮询失败] {e}')
        return []

# ── Supabase 工具 ──────────────────────────────────────
def sb_request(method, path, body=None):
    url = f'{SURL}/rest/v1/{path}'
    headers = {
        'apikey': SKEY,
        'Authorization': f'Bearer {SKEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation'
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode()
            return json.loads(text) if text else []
    except urllib.error.HTTPError as e:
        print(f'[SB错误] {method} {path}: {e.code} {e.read().decode()[:100]}')
        return None
    except Exception as e:
        print(f'[SB异常] {e}')
        return None

def get_pending_tasks():
    """获取待发派的新任务"""
    return sb_request('GET', 'boss_tasks?status=eq.sent&qidian_notified=eq.false&order=id.desc') or []

def get_all_active_tasks():
    """获取所有进行中的任务，用于上下文匹配"""
    return sb_request('GET', 'boss_tasks?status=in.(sent,in-progress)&order=id.desc') or []

def update_task(task_id, updates):
    return sb_request('PATCH', f'boss_tasks?id=eq.{task_id}', updates)

def insert_finance_record(project, type_, category, amount, note, reporter):
    record = {
        'id': int(time.time() * 1000),
        'project': project,
        'type': type_,        # 'income' | 'expense'
        'category': category,
        'amount': amount,
        'note': note,
        'reporter': reporter,
    }
    result = sb_request('POST', 'finance_records', record)
    print(f'[💰 财务录入] {reporter} | {type_} ¥{amount} | {category} | 项目:{project}')
    return result

def call_openai(system_prompt, user_content, max_tokens=300):
    """调用 OpenAI Chat Completions API，返回纯文本内容。"""
    url = 'https://api.openai.com/v1/chat/completions'
    body = json.dumps({
        'model': OPENAI_MODEL,
        'max_tokens': max_tokens,
        'temperature': 0,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_content},
        ]
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        'Authorization': f'Bearer {OPENAI_KEY}',
        'content-type': 'application/json',
    }, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=8) as r:   # 超时从15s缩短到8s
            d = json.loads(r.read())
            return d['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f'[OpenAI调用失败] {e}')
        return None


def parse_member_message(text, reporter='', known_project=None):
    """一次 AI 调用同时提取财务记录 + 进度数量，避免两次串行调用"""
    proj_list = ', '.join(list(PROJECT_ALIASES.keys()) + ['其他'])
    default_proj_hint = f'（如果消息中没有明确项目名，优先填"{known_project}"）' if known_project else ''
    system = f"""你是财务+进度信息提取助手。从员工消息中同时提取：
1. 所有收入/支出（finance）
2. 招生/完成数量进度（progress）

可识别项目：{proj_list}{default_proj_hint}
输出纯JSON（不要任何说明）：
{{"finance":[{{"type":"income|expense","category":"简短分类","amount":数字,"project":"项目名","note":"原文摘要"}}],"progress":{{"is_progress":true|false,"count":数字或0}}}}

finance rules:
- amount: 纯阿拉伯数字，中文金额换算：1w=10000,1万=10000,一万=10000,1k=1000,一千=1000,1千=1000
- type: income=收入/入账/营收/学费/回款，expense=支出/报销/差旅/费用/花了/付了
- project: 识别不出且无默认提示时填"其他"

progress rules:
- is_progress: 消息是否包含招生/完成数量汇报
- count: 本次新增数量（不是累计），0表示没有进度信息

如果没有财务信息，finance=[]；如果没有进度信息，progress={{"is_progress":false,"count":0}}"""
    raw = call_openai(system, text, max_tokens=400)
    if not raw:
        return [], None
    try:
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            return [], None
        result = json.loads(m.group(0))
        # 处理财务记录
        fin_records = result.get('finance') or []
        for r in fin_records:
            r['reporter'] = reporter
            r['amount'] = normalize_amount(r.get('amount', 0))
        # 统一已知项目替换"其他"
        specific_projs = [r['project'] for r in fin_records if r.get('project') and r['project'] != '其他']
        if specific_projs:
            best = specific_projs[0]
            for r in fin_records:
                if r.get('project') == '其他':
                    r['project'] = best
        # 处理进度
        prog = result.get('progress') or {}
        progress_report = None
        if prog.get('is_progress') and prog.get('count', 0) > 0:
            progress_report = {'count': int(prog['count'])}
        return fin_records, progress_report
    except Exception as e:
        print(f'[消息解析失败] {e} | raw={raw[:100]}')
        return [], None

def normalize_amount(val):
    """将中文/简写金额统一转换为 float，支持：1w/1W/1万/一万=10000, 1k/1K/1千/一千=1000, 1.5万=15000 等"""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    s = re.sub(r'[￥¥,，\s元块圆]', '', s)
    if not s:
        return 0.0
    # 尝试直接转数字
    try:
        return float(s)
    except:
        pass
    # 中文数字字符 → 阿拉伯数字（处理"一万"→"1万"、"两千"→"2千"等）
    cn_map = {'零':'0','一':'1','二':'2','两':'2','三':'3','四':'4','五':'5',
              '六':'6','七':'7','八':'8','九':'9'}
    for ch, num in cn_map.items():
        s = s.replace(ch, num)
    # 带单位匹配
    for pat, mult in [
        (r'^([\d.]+)[wW万]$',       10000),
        (r'^([\d.]+)[kK千]$',       1000),
        (r'^([\d.]+)百$',           100),
        (r'^([\d.]+)亿$',           100000000),
        (r'^([\d.]+)[wW万]([\d.]+)千?$', None),  # 1万5千 → 特殊处理
    ]:
        if mult is None:
            m = re.match(r'^([\d.]+)[wW万]([\d.]+)千?$', s)
            if m:
                return float(m.group(1)) * 10000 + float(m.group(2)) * 1000
        else:
            m = re.match(pat, s)
            if m:
                return float(m.group(1)) * mult
    # 形如 "1万5" → 15000
    m = re.match(r'^([\d.]+)[wW万]([\d]+)$', s)
    if m:
        return float(m.group(1)) * 10000 + float(m.group(2)) * 1000
    # 再尝试直接转（中文替换后可能是纯数字）
    try:
        return float(s)
    except:
        return 0.0


def parse_finance_from_text(text, reporter=''):
    """用 AI 从成员消息中提取财务信息，返回 list of records"""
    proj_list = ', '.join(list(PROJECT_ALIASES.keys()) + ['其他'])
    system = f"""你是财务信息提取助手。从用户的消息中提取所有收入和支出信息。
可识别的项目：{proj_list}
输出纯JSON数组，每条记录格式：
{{"type":"income|expense","category":"简短分类名","amount":数字,"project":"项目名","note":"原文摘要"}}
- type: income=收入/入账/营收/回款，expense=支出/报销/差旅/费用/成本
- category: 简短描述，如"招生收入"、"差旅费"、"场地费"等
- amount: 必须是纯阿拉伯数字（无单位符号）。中文/简写金额请换算：1w=10000, 1万=10000, 一万=10000, 1k=1000, 一千=1000, 1千=1000, 1.5万=15000，以此类推
- project: 从消息中识别，识别不出填"其他"
- note: 原始消息前60字
如果消息中没有任何财务信息，输出空数组 []
只输出JSON，不要任何解释。"""

    raw = call_openai(system, text)
    if not raw:
        return []
    try:
        m = re.search(r'\[[\s\S]*\]', raw)
        if not m:
            return []
        records = json.loads(m.group(0))
        for r in records:
            r['reporter'] = reporter
            # 用 normalize_amount 兜底，防止模型返回非纯数字
            r['amount'] = normalize_amount(r.get('amount', 0))
        # 如果有明确项目，把"其他"记录也统一改为该项目
        specific_projects = [r['project'] for r in records if r.get('project') and r['project'] != '其他']
        if specific_projects:
            best_project = specific_projects[0]
            for r in records:
                if r.get('project') == '其他':
                    r['project'] = best_project
        return records
    except Exception as e:
        print(f'[财务解析失败] {e} | raw={raw[:100]}')
        return []

def parse_boss_command(text):
    """用 AI 解析老板的任务指令，返回任务信息或 None"""
    members = '、'.join(list(NAME_ALIASES.keys()))
    projects = '、'.join(list(PROJECT_ALIASES.keys()) + ['其他'])
    system = f"""你是任务解析助手。从老板的指令中提取任务信息。
可识别成员：{members}
可识别项目：{projects}
输出纯JSON格式：
{{"assignee":"成员名","title":"任务标题","target":数字或null,"project":"项目名","deadline":"YYYY-MM-DD或null","priority":"high|mid|low"}}
- target: 如果有明确数量目标（如招生100人、拜访50家客户），填数字，否则null
- 如果消息不是任务指令（闲聊/问候/查询），输出 {{"type":"non_task"}}
只输出JSON，不要解释。"""
    raw = call_openai(system, text)
    if not raw:
        return None
    try:
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            return None
        result = json.loads(m.group(0))
        if result.get('type') == 'non_task':
            return None
        if not result.get('assignee') or not result.get('title'):
            return None
        return result
    except Exception as e:
        print(f'[老板指令解析失败] {e} | raw={raw[:100]}')
        return None

def parse_progress_report(text):
    """从成员消息中提取招生/完成数量，返回 {'count': N} 或 None"""
    has_progress = any(kw in text for kw in PROGRESS_KEYWORDS)
    if not has_progress:
        return None
    system = """判断消息是否包含完成/招生的数量报告（纯进度汇报，不要提取金额）。
如果包含，输出 {"is_progress":true,"count":数字}
如果不包含，输出 {"is_progress":false}
只输出JSON。"""
    raw = call_openai(system, text, max_tokens=80)
    if not raw:
        return None
    try:
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            return None
        result = json.loads(m.group(0))
        if result.get('is_progress') and result.get('count'):
            return {'count': int(result['count'])}
        return None
    except:
        return None

def get_task_target_progress(task):
    """从 description 中提取 [target:N] 和 [progress:N]"""
    desc = task.get('description') or ''
    t_match = re.search(r'\[target:(\d+)\]', desc)
    p_match = re.search(r'\[progress:(\d+)\]', desc)
    target = int(t_match.group(1)) if t_match else None
    progress = int(p_match.group(1)) if p_match else 0
    return target, progress

def update_task_progress_count(task_id, additional, current_desc):
    """累加 [progress:N]，返回新的累计值"""
    p_match = re.search(r'\[progress:(\d+)\]', current_desc)
    current = int(p_match.group(1)) if p_match else 0
    new_val = current + additional
    if p_match:
        new_desc = re.sub(r'\[progress:\d+\]', f'[progress:{new_val}]', current_desc)
    else:
        new_desc = f'[progress:{new_val}]\n' + current_desc
    update_task(task_id, {'description': new_desc})
    return new_val

# 辅导师 → 团队长映射（汇报进度时同步更新团队长任务）
RECRUIT_TEAM_MAP = {
    '吴晓磊': '韩笑',
}

def sync_progress_for_member(reporter_name, count):
    """任意成员汇报进度时：1)更新成员自己的 boss_task 进度；2)如有团队长映射，同步到团队长"""
    if count <= 0:
        return

    leader = RECRUIT_TEAM_MAP.get(reporter_name)
    leader_target = 0

    # ── 1. 如有团队长映射，同步到团队长 boss_task ──
    if leader:
        leader_tasks = sb_request('GET', f'boss_tasks?assignee=eq.{urllib.parse.quote(leader)}&status=in.(sent,in-progress)&order=id.desc') or []
        for task in leader_tasks:
            desc = task.get('description') or ''
            title = task.get('title') or ''
            if '[target:' in desc:
                new_val = update_task_progress_count(task.get('id'), count, desc)
                tM = re.search(r'\[target:(\d+)\]', desc)
                leader_target = int(tM.group(1)) if tM else 0
                pct = round(new_val / leader_target * 100, 1) if leader_target else 0
                print(f'[📊 团队进度同步] {reporter_name}→{leader} 累计:{new_val}/{leader_target}（{pct}%）')
                break
            # 兜底：从标题/描述里提取数字，自动写入 [target:N][progress:count]
            num_match = re.search(r'(\d+)', title + ' ' + desc)
            if num_match:
                inferred_target = int(num_match.group(1))
                if 1 < inferred_target < 100000:
                    new_desc = f'[target:{inferred_target}]\n[progress:{count}]\n' + desc
                    update_task(task.get('id'), {'description': new_desc})
                    leader_target = inferred_target
                    pct = round(count / leader_target * 100, 1)
                    print(f'[📊 团队进度补录] {leader} 从标题推断target={inferred_target}，progress={count}（{pct}%）')
                    break

    # ── 2. 更新成员自己的 boss_task（所有成员通用）──
    reporter_tasks = sb_request('GET', f'boss_tasks?assignee=eq.{urllib.parse.quote(reporter_name)}&status=in.(sent,in-progress)&order=id.desc') or []
    reporter_task = None
    for t in reporter_tasks:
        if '[target:' in (t.get('description') or ''):
            reporter_task = t
            break

    if reporter_task:
        new_val = update_task_progress_count(reporter_task['id'], count, reporter_task.get('description') or '')
        tM = re.search(r'\[target:(\d+)\]', reporter_task.get('description') or '')
        target = int(tM.group(1)) if tM else 0
        pct = round(new_val / target * 100, 1) if target else 0
        print(f'[📊 成员进度] {reporter_name} 累计:{new_val}/{target}（{pct}%）PT={math.floor(new_val/target*10) if target else 0}')
    elif leader_target > 0:
        # 成员无目标任务，用团队长目标自动创建
        task_desc = f'[target:{leader_target}]\n[progress:{count}]\n目标：招生{leader_target}人（跟随{leader}团队任务）'
        bt = {
            'id': int(time.time() * 1000),
            'title': f'招生{leader_target}人',
            'assignee': reporter_name,
            'priority': 'mid',
            'description': task_desc,
            'status': 'in-progress',
            'qidian_notified': True,
        }
        sb_request('POST', 'boss_tasks', bt)
        pt = math.floor(count / leader_target * 10)
        print(f'[📊 成员任务创建] {reporter_name} 自动创建 target={leader_target} progress={count} PT={pt}')

def handle_boss_message(text):
    """处理老板通过 Telegram 发来的指令（任务分配等）"""
    # ── 先检查是否是对项目确认请求的回复 ──
    if PENDING_CONFIRMATIONS:
        proj = match_project_name(text)
        if proj:
            now = time.time()
            processed = []
            for conf_id, conf in list(PENDING_CONFIRMATIONS.items()):
                if now - conf['ts'] < 600:  # 10分钟内的挂起确认
                    for rec in conf['records']:
                        insert_finance_record(proj, rec['type'], rec['category'], rec['amount'], rec['note'], rec['reporter'])
                    tg_send(BOSS_CHAT, f"✅ 已将 *{conf['reporter']}* 的记录录入「{proj}」（{len(conf['records'])}条）")
                    tg_send(conf['chat_id'], f"✅ 秦总已确认，记录已录入「{proj}」")
                    processed.append(conf_id)
            for cid in processed:
                del PENDING_CONFIRMATIONS[cid]
            if processed:
                print(f'[✅ 确认完成] 已将 {len(processed)} 条挂起记录录入「{proj}」')
                return
        # 清理超时挂起（>10分钟）
        expired = [cid for cid, c in PENDING_CONFIRMATIONS.items() if time.time() - c['ts'] > 600]
        for cid in expired:
            del PENDING_CONFIRMATIONS[cid]

    cmd = parse_boss_command(text)
    if not cmd:
        # 不是任务指令，简单确认
        print(f'[老板消息] 非任务指令：{text[:40]}')
        return
    assignee = cmd.get('assignee', '')
    title    = cmd.get('title', '')
    target   = cmd.get('target')  # 可能为 None
    project  = cmd.get('project', '理享招生项目')
    deadline = cmd.get('deadline')
    priority = cmd.get('priority', 'mid')

    # target 写入 description
    desc_parts = []
    if target:
        desc_parts.append(f'[target:{target}]')
        desc_parts.append(f'[progress:0]')
        desc_parts.append(f'目标：{title}，需完成 {target}')
    task_desc = '\n'.join(desc_parts)

    # 插入 boss_tasks
    task_id = int(time.time() * 1000)
    bt = {
        'id': task_id,
        'title': title,
        'assignee': assignee,
        'deadline': deadline,
        'priority': priority,
        'description': task_desc,
        'status': 'sent',
        'qidian_notified': False,
    }
    result = sb_request('POST', 'boss_tasks', bt)
    if result:
        target_str = f'，目标 {target} 人' if target else ''
        tg_send(BOSS_CHAT,
            f"✅ *任务已创建*\n\n"
            f"已给 *{assignee}* 安排：「{title}」{target_str}\n"
            f"项目：{project}\n"
            f"奇点将自动通知并跟进进展 🙏"
        )
        print(f'[老板指令] 已创建任务 → {assignee}：{title}（target={target}）')
    else:
        tg_send(BOSS_CHAT, f"⚠️ 任务创建失败，请检查系统日志")

def add_reply_to_task(task_id, reply_text, replier):
    """把成员回复追加到任务记录"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    note = f'[{now}] {replier}回复：{reply_text}'
    # 先取当前 description
    tasks = sb_request('GET', f'boss_tasks?id=eq.{task_id}')
    if tasks:
        old_desc = tasks[0].get('description') or ''
        new_desc = (old_desc + '\n' + note).strip()
        update_task(task_id, {'description': new_desc, 'status': 'in-progress'})

def ask_project_confirmation(reporter, text, records, chat_id):
    """当财务/进度项目无法确认时，发给老板确认"""
    proj_list = '、'.join(list(PROJECT_ALIASES.keys()))
    conf_id = int(time.time() * 1000)
    PENDING_CONFIRMATIONS[conf_id] = {
        'reporter': reporter,
        'text': text,
        'records': records,
        'chat_id': chat_id,
        'ts': time.time(),
    }
    boss_msg = (
        f"❓ *需要确认项目归属*\n\n"
        f"*{reporter}* 发来消息：\n_{text[:120]}_\n\n"
        f"已识别到财务/进度信息，但无法判断属于哪个项目。\n"
        f"可选项目：{proj_list}\n\n"
        f"请直接回复项目名称，奇点将完成录入 👆"
    )
    tg_send(BOSS_CHAT, boss_msg)
    print(f'[❓ 等待确认] #{conf_id} {reporter} 消息需要秦总确认项目')
    return conf_id

def match_project_name(text):
    """从文本里匹配已知项目名，返回规范化名或 None"""
    text = text.strip()
    for canonical, aliases in PROJECT_ALIASES.items():
        if text == canonical or text in aliases or canonical in text:
            return canonical
    return None

# ── 名字匹配 ──────────────────────────────────────────
def find_member_chat_id(assignee_name):
    """根据任务负责人名字找到 Telegram Chat ID"""
    if not assignee_name:
        return None
    for canonical, aliases in NAME_ALIASES.items():
        if any(a in assignee_name for a in aliases):
            return MEMBERS.get(canonical)
    return None

def find_member_name_by_chat(chat_id):
    """根据 Chat ID 反查成员名字"""
    for name, cid in MEMBERS.items():
        if cid and str(cid) == str(chat_id):
            return name
    return None

def match_task_by_tg_msg_id(tg_msg_id, active_tasks):
    """通过 Telegram message_id 精准匹配任务（description 首行存有 [tg:xxx]）"""
    if not tg_msg_id:
        return None
    pattern = f'[tg:{tg_msg_id}]'
    for task in active_tasks:
        if pattern in (task.get('description') or ''):
            return task
    return None

def match_task_by_reply(reply_text, active_tasks, replier_name):
    """兜底：按 assignee 匹配最新任务"""
    if not active_tasks:
        return None
    # 优先匹配分配给该成员的最新任务（active_tasks 已按 id.desc 排序）
    for task in active_tasks:
        if replier_name and replier_name in (task.get('assignee') or ''):
            return task
    return None  # 不再默认返回第一条，避免错误归档

# ── 核心：处理新任务 ──────────────────────────────────
def handle_new_task(task):
    title    = task.get('title', '（未命名）')
    assignee = task.get('assignee', '')
    deadline = task.get('deadline', '')
    priority = task.get('priority', 'mid')
    desc     = task.get('description', '')
    task_id  = task.get('id')

    pri_label = {'high': '🔴 高', 'mid': '🟡 中', 'low': '🟢 低'}.get(priority, '🟡 中')

    chat_id = find_member_chat_id(assignee)

    if chat_id:
        # 私信成员
        dl_str = ''
        if deadline:
            try:
                dl_str = f"\n📅 截止时间：{datetime.strptime(deadline, '%Y-%m-%d').strftime('%Y年%m月%d日')}"
            except:
                dl_str = f"\n📅 截止时间：{deadline}"

        msg = (
            f"📋 *奇点任务通知*\n\n"
            f"秦总给你安排了一项任务：\n\n"
            f"*{title}*{dl_str}\n"
            f"优先级：{pri_label}\n"
            f"{('说明：' + desc) if desc else ''}\n\n"
            f"请直接回复此消息告知进展或完成情况，奇点会转达给秦总 🙏"
        )
        result = tg_send(chat_id, msg)
        if result and result.get('ok'):
            tg_msg_id = result.get('result', {}).get('message_id')
            # 把 tg_message_id 存入 description 首行，供回复精准匹配
            new_desc = (f'[tg:{tg_msg_id}]\n' + desc).strip() if tg_msg_id else desc
            update_task(task_id, {'qidian_notified': True, 'status': 'in-progress', 'description': new_desc})
            print(f'[✅ 已通知] {assignee} | 任务：{title} | tg_msg_id={tg_msg_id}')

            # 同时告知老板已发派
            boss_msg = (
                f"📤 *奇点已通知*\n\n"
                f"已私信 *{assignee}*：\n"
                f"「{title}」\n"
                f"等待对方回复进展..."
            )
            tg_send(BOSS_CHAT, boss_msg)
        else:
            print(f'[❌ 通知失败] {assignee} | {result}')
    else:
        # 成员未配置 Chat ID，仅告知老板，不提绑定引导
        update_task(task_id, {'qidian_notified': True})
        msg = (
            f"⚠️ *奇点提示*\n\n"
            f"任务「{title}」已记录\n"
            f"负责人 *{assignee}* 暂未配置 Telegram，待秦总补充账号后奇点自动跟进"
        )
        tg_send(BOSS_CHAT, msg)
        print(f'[⚠️ 未配置] {assignee} 无 Chat ID')

# ── 核心：处理成员回复 ────────────────────────────────
def handle_member_reply(chat_id, text, from_name, reply_to_msg_id=None):
    member_name = find_member_name_by_chat(chat_id)

    # 如果是老板自己，尝试解析为任务指令
    is_boss = str(chat_id) == str(BOSS_CHAT)
    if is_boss and not member_name:
        handle_boss_message(text)
        return

    active_tasks = get_all_active_tasks()

    # 测试模式下，任何来自老板账号的消息都当王玟的回复处理
    if is_boss:
        member_name = '王玟（测试）'

    # 优先用 reply_to_message_id 精准匹配（最可靠）
    task = match_task_by_tg_msg_id(reply_to_msg_id, active_tasks)
    if task:
        print(f'[精准匹配] reply_to={reply_to_msg_id} → 任务：{task.get("title")}')
    else:
        task = match_task_by_reply(text, active_tasks, member_name)

    # ── 财务 + 进度一次性解析（单次 AI 调用）──
    has_content = any(kw in text for kw in FINANCE_KEYWORDS + PROGRESS_KEYWORDS)
    fin_records = []
    progress_report = None
    if has_content:
        reporter_name = member_name or from_name
        # 从员工当前任务推断默认项目，减少问老板确认的频率
        known_project = None
        if task:
            known_project = task.get('project') or match_project_name(task.get('title') or '')
        if not known_project:
            bt_list = sb_request('GET', f'boss_tasks?assignee=eq.{urllib.parse.quote(reporter_name)}&status=in.(sent,in-progress)&order=id.desc&limit=3') or []
            for bt in bt_list:
                p = bt.get('project') or match_project_name(bt.get('title') or '')
                if p:
                    known_project = p
                    break
        fin_records, progress_report = parse_member_message(text, reporter=reporter_name, known_project=known_project)
        # 录入已识别项目的财务记录
        unknown_proj_records = []
        for fr in fin_records:
            raw_proj = fr.get('project') or '其他'
            normalized = match_project_name(raw_proj) or (known_project if raw_proj == '其他' and known_project else None)
            if normalized:
                fr['project'] = normalized
                insert_finance_record(normalized, fr['type'], fr['category'], fr['amount'], fr['note'], fr['reporter'])
            else:
                unknown_proj_records.append(fr)
        if unknown_proj_records:
            ask_project_confirmation(reporter_name, text, unknown_proj_records, chat_id)
            fin_records = [f for f in fin_records if f not in unknown_proj_records]

    progress_summary = ''
    if progress_report:
        cnt = progress_report['count']
        reporter = member_name or from_name

        sync_progress_for_member(reporter, cnt)

        # 尝试从 task 获取累计进度用于汇报；task 不存在时直接查 boss_task
        cum_prog, cum_target = 0, 0
        if task:
            task_id_prog = task.get('id')
            cur_desc = task.get('description') or ''
            t2, op = get_task_target_progress(task)
            if t2:
                cum_prog = update_task_progress_count(task_id_prog, cnt, cur_desc)
                cum_target = t2
        if not cum_target:
            # 从 boss_tasks 里查当前累计
            bt_list = sb_request('GET', f'boss_tasks?assignee=eq.{urllib.parse.quote(reporter)}&status=in.(sent,in-progress)&order=id.desc') or []
            for bt in bt_list:
                tM = re.search(r'\[target:(\d+)\]', bt.get('description') or '')
                pM = re.search(r'\[progress:(\d+)\]', bt.get('description') or '')
                if tM:
                    cum_target = int(tM.group(1))
                    cum_prog   = int(pM.group(1)) if pM else cnt
                    break

        if cum_target:
            pct = round(cum_prog / cum_target * 100, 1)
            pt  = math.floor(cum_prog / cum_target * 10)
            progress_summary = (
                f'\n\n📊 *招生进度更新*\n'
                f'本次 +{cnt} 人\n'
                f'累计：{cum_prog}/{cum_target} 人（{pct}%）\n'
                f'PT值：{pt}'
            )
            print(f'[📊 进度更新] {reporter}：累计{cum_prog}/{cum_target}（{pct}%）PT={pt}')
        else:
            progress_summary = f'\n\n📊 *招生进度*\n本次 +{cnt} 人（台账已记录）'
            print(f'[📊 进度更新] {reporter}：+{cnt}人（无目标任务，仅台账）')

    if task:
        task_id = task.get('id')
        title   = task.get('title', '（未知任务）')
        add_reply_to_task(task_id, text, member_name)

        # 汇总给老板
        fin_summary = ''
        if fin_records:
            lines = []
            for fr in fin_records:
                sign = '+' if fr['type'] == 'income' else '-'
                lines.append(f"{fr['category']}：{sign}¥{fr['amount']:.0f}（已录入{fr['project']}）")
            fin_summary = '\n\n💰 *财务已自动录入*\n' + '\n'.join(lines)

        boss_msg = (
            f"📩 *{member_name} 回复*\n\n"
            f"任务：「{title}」\n\n"
            f"回复内容：\n_{text}_"
            f"{fin_summary}"
            f"{progress_summary}"
        )
        tg_send(BOSS_CHAT, boss_msg)
        print(f'[📩 已转达] {member_name} → 老板 | 任务：{title}')

        ack = "✅ 收到！已转达给秦总。"
        if fin_records:
            ack += f"\n💰 财务数据已自动录入（{len(fin_records)}条）"
        if progress_summary:
            ack += f"\n{progress_summary}"
        tg_send(chat_id, ack)
    else:
        # 没有匹配任务 —— 找或创建该成员的通用收件箱任务，确保消息写入 boss_tasks
        reporter = member_name or from_name
        inbox_tasks = sb_request('GET', f'boss_tasks?assignee=eq.{urllib.parse.quote(reporter)}&order=id.desc&limit=5') or []
        inbox_task = inbox_tasks[0] if inbox_tasks else None
        if not inbox_task:
            # 自动创建收件箱任务
            inbox_task = {
                'id': int(time.time() * 1000),
                'title': f'{reporter} 日常汇报',
                'assignee': reporter,
                'priority': 'low',
                'description': f'[收件箱] {reporter} 的消息汇总',
                'status': 'in-progress',
                'qidian_notified': True,
            }
            sb_request('POST', 'boss_tasks', inbox_task)
            print(f'[📥 创建收件箱] {reporter} → boss_task #{inbox_task["id"]}')
        # 写入回复记录（供 Avatar 展示）
        add_reply_to_task(inbox_task['id'], text, reporter)

        fin_summary = ''
        if fin_records:
            lines = []
            for fr in fin_records:
                sign = '+' if fr['type'] == 'income' else '-'
                lines.append(f"{fr['category']}：{sign}¥{fr['amount']:.0f}（已录入{fr['project']}）")
            fin_summary = '\n\n💰 *财务已自动录入*\n' + '\n'.join(lines)

        boss_msg = (
            f"📩 *{reporter} 消息*\n\n_{text}_"
            f"{fin_summary}"
            f"{progress_summary}"
        )
        tg_send(BOSS_CHAT, boss_msg)
        print(f'[📩 已转达] {reporter} → 老板（收件箱任务 #{inbox_task["id"]}）')
        ack = "✅ 收到，已转达给秦总。"
        if fin_records:
            ack += f"\n💰 财务数据已自动录入（{len(fin_records)}条）"
        if progress_summary:
            ack += f"\n{progress_summary}"
        tg_send(chat_id, ack)

# ── 主循环 ────────────────────────────────────────────
def main():
    print('=' * 50)
    print('🤖 奇点 Agent 启动')
    print(f'   Bot: @QAssistant701_bot')
    print(f'   老板 Chat ID: {BOSS_CHAT}')
    print(f'   已绑定成员: {[k for k,v in MEMBERS.items() if v]}')
    print('=' * 50)

    # 启动通知老板（不阻塞主循环）
    try:
        tg_send(BOSS_CHAT,
            "🟢 *奇点已上线*\n\n"
            "现在开始监听任务指令。\n"
            "老板在看板语音下达任务后，奇点会自动通知对应负责人并持续跟进汇报 💪"
        )
    except Exception as e:
        print(f'[启动通知失败] {e}')

    tg_offset = 0
    last_task_check = 0

    while True:
        now = time.time()

        # ── 检查新任务 ──
        if now - last_task_check >= TASK_CHECK_INTERVAL:
            last_task_check = now
            try:
                pending = get_pending_tasks()
                for task in pending:
                    handle_new_task(task)
            except Exception as e:
                print(f'[任务检查异常] {e}')

        # ── 监听成员回复 ──
        try:
            updates = tg_get_updates(tg_offset)
            for u in updates:
                tg_offset = u['update_id'] + 1
                msg = u.get('message', {})
                if not msg:
                    continue
                chat_id  = msg.get('chat', {}).get('id')
                text     = msg.get('text', '').strip()
                frm      = msg.get('from', {})
                from_name = f"{frm.get('first_name','')} {frm.get('last_name','')}".strip()

                if not text or not chat_id:
                    continue

                # 忽略 /start 命令，不主动引导绑定
                if text.startswith('/start'):
                    continue

                reply_to_msg_id = msg.get('reply_to_message', {}).get('message_id')
                print(f'[收到消息] chat_id={chat_id} from={from_name} reply_to={reply_to_msg_id} text={text[:40]}')
                try:
                    handle_member_reply(chat_id, text, from_name, reply_to_msg_id)
                except Exception as e:
                    print(f'[消息处理异常] {e} | chat={chat_id} text={text[:40]}')

        except Exception as e:
            print(f'[消息监听异常] {e}')

        time.sleep(POLL_INTERVAL)

if __name__ == '__main__':
    while True:
        try:
            main()
        except Exception as e:
            print(f'[💀 主进程崩溃] {e}，5秒后自动重启...')
            time.sleep(5)
