"""
AI Execution Companion OS v2 — Telegram Bot (Single File)
==========================================================
极简主流程：/today → 自动锁定A → 开始2分钟 → 完成/升级/卡住 → 证据
管理入口：/manage → goal/phases/tasks
"""

import os
import re
import json
import sqlite3
import logging
from datetime import datetime, date
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


###############################################################################
# ██████  DATABASE LAYER (SQLite)
###############################################################################

DB_PATH = os.environ.get("ECOS_DB_PATH", "ecos.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        timezone TEXT DEFAULT 'Asia/Shanghai',
        default_step_minutes INTEGER DEFAULT 8,
        tone TEXT DEFAULT 'firm_kind',
        low_energy_mode INTEGER DEFAULT 0,
        weekly_summary_enabled INTEGER DEFAULT 0,
        streak_days INTEGER DEFAULT 0,
        last_progress_date TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS goals (
        goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        deadline_date TEXT,
        track TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS phases (
        phase_id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        is_active INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS task_items (
        task_id INTEGER PRIMARY KEY AUTOINCREMENT,
        phase_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        type TEXT DEFAULT 'misc',
        status TEXT DEFAULT 'not_started',
        tags TEXT DEFAULT '[]',
        difficulty_self_rating INTEGER,
        source TEXT DEFAULT 'manual',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS mainlines (
        mainline_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        goal_id INTEGER,
        phase_id INTEGER,
        date TEXT NOT NULL,
        title TEXT NOT NULL,
        source TEXT DEFAULT 'manual',
        locked INTEGER DEFAULT 1,
        task_id_ref INTEGER,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS steps (
        step_id INTEGER PRIMARY KEY AUTOINCREMENT,
        mainline_id INTEGER NOT NULL,
        kind TEXT DEFAULT 'micro',
        duration_min INTEGER DEFAULT 2,
        instruction TEXT NOT NULL,
        acceptance_criteria TEXT NOT NULL,
        difficulty INTEGER DEFAULT 1,
        status TEXT DEFAULT 'ready',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS deferred_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        deferred_step_id INTEGER NOT NULL,
        mainline_id INTEGER,
        reason TEXT DEFAULT 'exit',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS stuck_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        step_id INTEGER NOT NULL,
        stuck_type TEXT NOT NULL,
        emotion_label TEXT,
        user_note TEXT,
        timestamp TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        counter_evidence TEXT NOT NULL,
        tags TEXT DEFAULT '["small_win"]',
        timestamp TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS if_then_plans (
        plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        if_trigger TEXT NOT NULL,
        then_action TEXT NOT NULL,
        reward TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS import_drafts (
        import_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        phase_id INTEGER NOT NULL,
        source TEXT DEFAULT 'paste',
        raw_text TEXT,
        parsed_items TEXT DEFAULT '[]',
        state TEXT DEFAULT 'draft',
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()


# ── User CRUD ──

def ensure_user(uid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        conn.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return dict(row)


def get_user(uid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_user(uid, **kw):
    conn = get_conn()
    s = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE users SET {s} WHERE user_id=?", list(kw.values()) + [uid])
    conn.commit()
    conn.close()


def update_streak(uid):
    u = get_user(uid)
    today = date.today().isoformat()
    if u["last_progress_date"] != today:
        n = u["streak_days"] + 1
        update_user(uid, streak_days=n, last_progress_date=today)
        return n
    return u["streak_days"]


# ── Goal ──

def create_goal(uid, title, deadline=None, track=None):
    conn = get_conn()
    cur = conn.execute("INSERT INTO goals (user_id,title,deadline_date,track) VALUES (?,?,?,?)",
                       (uid, title, deadline, track))
    gid = cur.lastrowid
    conn.commit()
    conn.close()
    return gid


def get_active_goal(uid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM goals WHERE user_id=? AND is_active=1 ORDER BY created_at DESC LIMIT 1",
                       (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Phase ──

def create_phase(goal_id, title, is_active=1):
    conn = get_conn()
    if is_active:
        conn.execute("UPDATE phases SET is_active=0 WHERE goal_id=?", (goal_id,))
    cur = conn.execute("INSERT INTO phases (goal_id,title,is_active) VALUES (?,?,?)",
                       (goal_id, title, is_active))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def list_phases(goal_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM phases WHERE goal_id=? ORDER BY created_at", (goal_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_phase(goal_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM phases WHERE goal_id=? AND is_active=1", (goal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_active_phase(goal_id, phase_id):
    conn = get_conn()
    conn.execute("UPDATE phases SET is_active=0 WHERE goal_id=?", (goal_id,))
    conn.execute("UPDATE phases SET is_active=1 WHERE phase_id=?", (phase_id,))
    conn.commit()
    conn.close()


# ── TaskItem ──

def create_task(phase_id, title, type_="misc", status="not_started", tags=None, difficulty=None, source="manual"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO task_items (phase_id,title,type,status,tags,difficulty_self_rating,source) VALUES (?,?,?,?,?,?,?)",
        (phase_id, title, type_, status, json.dumps(tags or []), difficulty, source))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def list_tasks(phase_id, status_filter=None):
    conn = get_conn()
    q = "SELECT * FROM task_items WHERE phase_id=?"
    p = [phase_id]
    if status_filter:
        q += " AND status=?"
        p.append(status_filter)
    q += " ORDER BY created_at"
    rows = conn.execute(q, p).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_task(task_id, **kw):
    kw["updated_at"] = datetime.now().isoformat()
    conn = get_conn()
    s = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE task_items SET {s} WHERE task_id=?", list(kw.values()) + [task_id])
    conn.commit()
    conn.close()


def delete_task(task_id):
    conn = get_conn()
    conn.execute("DELETE FROM task_items WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()


# ── Mainline ──

def create_mainline(uid, title, source="manual", goal_id=None, phase_id=None, task_id_ref=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO mainlines (user_id,goal_id,phase_id,date,title,source,task_id_ref) VALUES (?,?,?,?,?,?,?)",
        (uid, goal_id, phase_id, date.today().isoformat(), title, source, task_id_ref))
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return mid


def get_today_mainline(uid):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM mainlines WHERE user_id=? AND date=? ORDER BY created_at DESC LIMIT 1",
        (uid, date.today().isoformat())).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Step ──

def create_step(mainline_id, kind, dur, instruction, criteria, difficulty=1):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO steps (mainline_id,kind,duration_min,instruction,acceptance_criteria,difficulty) VALUES (?,?,?,?,?,?)",
        (mainline_id, kind, dur, instruction, criteria, difficulty))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def get_step(sid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM steps WHERE step_id=?", (sid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_step(sid, **kw):
    conn = get_conn()
    s = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE steps SET {s} WHERE step_id=?", list(kw.values()) + [sid])
    conn.commit()
    conn.close()


def get_active_step(mainline_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM steps WHERE mainline_id=? AND status IN ('ready','executing') ORDER BY created_at DESC LIMIT 1",
        (mainline_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Deferred ──

def create_deferred(uid, step_id, mainline_id, reason="exit"):
    conn = get_conn()
    conn.execute("INSERT INTO deferred_links (user_id,deferred_step_id,mainline_id,reason) VALUES (?,?,?,?)",
                 (uid, step_id, mainline_id, reason))
    conn.commit()
    conn.close()


def get_deferred(uid):
    conn = get_conn()
    row = conn.execute(
        "SELECT dl.*, s.instruction, s.acceptance_criteria, s.duration_min, s.mainline_id, m.title as mainline_title "
        "FROM deferred_links dl "
        "JOIN steps s ON dl.deferred_step_id=s.step_id "
        "JOIN mainlines m ON dl.mainline_id=m.mainline_id "
        "WHERE dl.user_id=? ORDER BY dl.created_at DESC LIMIT 1",
        (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def clear_deferred(uid):
    conn = get_conn()
    conn.execute("DELETE FROM deferred_links WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()


# ── StuckEvent ──

def create_stuck_event(step_id, stuck_type, emotion_label=None):
    conn = get_conn()
    conn.execute("INSERT INTO stuck_events (step_id,stuck_type,emotion_label) VALUES (?,?,?)",
                 (step_id, stuck_type, emotion_label))
    conn.commit()
    conn.close()


# ── Evidence ──

def create_evidence(uid, text, tags=None):
    conn = get_conn()
    cur = conn.execute("INSERT INTO evidence (user_id,counter_evidence,tags) VALUES (?,?,?)",
                       (uid, text, json.dumps(tags or ["small_win"])))
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    return eid


def list_evidence(uid, limit=10):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM evidence WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
                        (uid, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── IfThen ──

def save_if_then(uid, if_trigger, then_action, reward=None):
    conn = get_conn()
    conn.execute("INSERT INTO if_then_plans (user_id,date,if_trigger,then_action,reward) VALUES (?,?,?,?,?)",
                 (uid, date.today().isoformat(), if_trigger, then_action, reward))
    conn.commit()
    conn.close()


# ── ImportDraft ──

def create_import_draft(uid, phase_id, raw_text, parsed_items, source="paste"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO import_drafts (user_id,phase_id,source,raw_text,parsed_items) VALUES (?,?,?,?,?)",
        (uid, phase_id, source, raw_text, json.dumps(parsed_items, ensure_ascii=False)))
    iid = cur.lastrowid
    conn.commit()
    conn.close()
    return iid


def get_import_draft(iid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM import_drafts WHERE import_id=?", (iid,)).fetchone()
    conn.close()
    if row:
        d = dict(row)
        d["parsed_items"] = json.loads(d["parsed_items"])
        return d
    return None


def confirm_import(iid):
    draft = get_import_draft(iid)
    if not draft:
        return
    for item in draft["parsed_items"]:
        create_task(draft["phase_id"], item.get("title", ""), item.get("type", "misc"),
                    item.get("status", "not_started"), item.get("tags", []),
                    item.get("difficulty_self_rating"), draft["source"])
    conn = get_conn()
    conn.execute("UPDATE import_drafts SET state='confirmed' WHERE import_id=?", (iid,))
    conn.commit()
    conn.close()


def discard_import(iid):
    conn = get_conn()
    conn.execute("UPDATE import_drafts SET state='discarded' WHERE import_id=?", (iid,))
    conn.commit()
    conn.close()


###############################################################################
# ██████  LLM LAYER (OpenAI)
###############################################################################

_openai_client = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            return None
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=key)
        except ImportError:
            logger.warning("openai package not installed")
            return None
    return _openai_client


def _call_llm(system, user_msg, retries=1):
    c = _get_openai()
    if not c:
        return None
    for attempt in range(retries + 1):
        try:
            extra = "\n\n⚠️ 你必须只输出JSON，不要输出其他文字。" if attempt > 0 else ""
            resp = c.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": system + extra},
                    {"role": "user", "content": user_msg}
                ],
                temperature=0.7, max_tokens=800,
                response_format={"type": "json_object"})
            return json.loads(resp.choices[0].message.content.strip())
        except Exception as e:
            logger.warning(f"LLM error (attempt {attempt+1}): {e}")
    return None


SYS_BASE = """你是"执行陪伴系统"的AI引擎。你只输出JSON。
语气：坚定、温和、短句。不说教。用"我们现在只做…""下一步是…"。中文输出。"""


def llm_micro_step(mainline_title, task_title=None):
    system = SYS_BASE + '\n输出格式：{"type":"micro_step","micro_step":{"duration_min":2,"instruction":"...","acceptance_criteria":"..."}}\ninstruction必须2分钟内可完成的具体动作。'
    user = f"今日主线：{mainline_title}"
    if task_title:
        user += f"\n任务：{task_title}"
    user += "\n生成一个2分钟起步动作。"
    r = _call_llm(system, user)
    if r and "micro_step" in r:
        return r
    return {"type": "micro_step", "micro_step": {
        "duration_min": 2,
        "instruction": f"打开「{mainline_title[:20]}」相关材料，找到你要开始的位置。",
        "acceptance_criteria": "材料已打开在屏幕上"}}


def llm_upgrade_step(mainline_title, micro_instruction=None):
    system = SYS_BASE + '\n输出格式：{"type":"next_step","step":{"duration_min":8,"instruction":"...","acceptance_criteria":"...","difficulty":1}}\n这是完成2分钟起步后的8分钟升级动作。'
    user = f"今日主线：{mainline_title}"
    if micro_instruction:
        user += f"\n刚完成：{micro_instruction}"
    user += "\n生成8分钟升级动作。"
    r = _call_llm(system, user)
    if r and "step" in r:
        return r
    return {"type": "next_step", "step": {
        "duration_min": 8,
        "instruction": f"继续推进「{mainline_title[:20]}」——完成下一个小节或练习。",
        "acceptance_criteria": "能用1句话说出完成了什么", "difficulty": 1}}


def llm_if_then(mainline_title):
    system = SYS_BASE + '\n输出格式：{"type":"if_then_plan","plan":{"if_trigger":"如果…","then_action":"那么…","reward":"完成后…"}}'
    r = _call_llm(system, f"今日主线：{mainline_title}\n生成if-then实施意图。")
    if r and "plan" in r:
        return r
    return {"type": "if_then_plan", "plan": {
        "if_trigger": "如果我开始犹豫或想刷手机",
        "then_action": "我先做2分钟起步动作", "reward": "完成后休息3分钟"}}


def llm_intervention(stuck_type, emotion=None, mainline=None, step_instr=None, evidence_list=None):
    ev_section = ""
    if stuck_type == "SELF_LIMITING" and evidence_list:
        ev_section = f"\nSELF_LIMITING必须包含evidence_quotes数组：\n{json.dumps(evidence_list, ensure_ascii=False)}"
    system = SYS_BASE + f"""
输出格式：{{"type":"intervention","stuck_type":"{stuck_type}","emotion_label":"...","body_reset":"30秒身体动作","intervention_text":"30-90秒干预(<150字)","restart_step":{{"duration_min":2,"instruction":"...","acceptance_criteria":"..."}},"push_line":"一句话","evidence_quotes":null}}{ev_section}"""
    user = f"卡点：{stuck_type}"
    if emotion: user += f"\n情绪：{emotion}"
    if mainline: user += f"\n主线：{mainline}"
    r = _call_llm(system, user + "\n生成干预。")
    if r and "intervention_text" in r:
        if stuck_type == "SELF_LIMITING" and not r.get("evidence_quotes") and evidence_list:
            r["evidence_quotes"] = evidence_list[:3]
        return r
    # Fallback
    FB = {
        "PERFECTIONISM": ("双手握拳3秒，松开。", "完美是陷阱。我们只做一个烂版本——比空白好一万倍。",
            {"duration_min": 2, "instruction": "写下关于任务你知道的3个词。不准修改。", "acceptance_criteria": "3个词出现在屏幕上"}, "烂版本 > 空白 →"),
        "GOAL_TOO_BIG": ("站起来伸展双臂5秒。", "你不需要看到终点，只看下一步。",
            {"duration_min": 2, "instruction": "打开你需要的页面或文件。只是打开。", "acceptance_criteria": "页面已打开"}, "打开了就是开始 →"),
        "OVERTHINKING": ("鼻子吸气→再吸→嘴巴长呼气，做两轮。", "大脑在转圈不是在前进。开始了才会想清楚。",
            {"duration_min": 2, "instruction": "不做选择——直接做第一个动作。", "acceptance_criteria": "动手了"}, "动了就对了 →"),
        "EMOTIONAL_FRICTION": ("双脚踩实地面，感受脚底压力10秒。", "给情绪取个名字。情绪不需要消失，带着它做2分钟。",
            {"duration_min": 2, "instruction": "带着情绪，写下今天任务的标题。", "acceptance_criteria": "写下了标题"}, "我们已经在动了 →"),
        "REWARD_MISMATCH": ("手机翻面朝下推远。", "先做2分钟再刷——带着完成感刷，完全不一样。",
            {"duration_min": 2, "instruction": "手机远离，打开任务材料。", "acceptance_criteria": "手机远离+材料打开"}, "2分钟后你自由了 →"),
        "SELF_LIMITING": ("双手按压桌面5秒，松开。", "「我不行」是想法不是事实。只需要试2分钟。",
            {"duration_min": 2, "instruction": "写下：「我不确定我行，但我可以试2分钟。」", "acceptance_criteria": "写下了这句话"}, "试了就是证据 →"),
    }
    fb = FB.get(stuck_type, FB["OVERTHINKING"])
    return {"type": "intervention", "stuck_type": stuck_type, "emotion_label": emotion or "",
            "body_reset": fb[0], "intervention_text": fb[1], "restart_step": fb[2], "push_line": fb[3],
            "evidence_quotes": evidence_list[:3] if stuck_type == "SELF_LIMITING" and evidence_list else None}


###############################################################################
# ██████  CORE ENGINE (Rules Layer)
###############################################################################

def choose_candidates(uid, phase_id=None, low_energy=False):
    if not phase_id:
        goal = get_active_goal(uid)
        if goal:
            phase = get_active_phase(goal["goal_id"])
            if phase:
                phase_id = phase["phase_id"]
    if not phase_id:
        return {
            "A": {"title": "建立任务池 — 写出5个待办任务标题", "reason": "还没有任务", "task_id": None},
            "B": {"title": "建立任务池 — 写出3个关键任务标题", "reason": "轻量版", "task_id": None}}

    ip = list_tasks(phase_id, "in_progress")
    ns = list_tasks(phase_id, "not_started")
    all_t = ip + ns
    if not all_t:
        return {
            "A": {"title": "建立任务池 — 写出5个待办任务标题", "reason": "任务池为空", "task_id": None},
            "B": {"title": "建立任务池 — 写出3个关键任务标题", "reason": "轻量版", "task_id": None}}

    primary = ip[0] if ip else ns[0]
    secondary = next((t for t in all_t if t["task_id"] != primary["task_id"]), None)
    a = {"title": f"推进「{primary['title']}」",
         "reason": "继续进行中" if primary["status"] == "in_progress" else "优先启动",
         "task_id": primary["task_id"]}
    if secondary:
        b = {"title": f"轻量推进「{secondary['title']}」", "reason": "低能量也能推进",
             "task_id": secondary["task_id"]}
    else:
        b = {"title": f"「{primary['title']}」— 只做最小起步", "reason": "缩小版",
             "task_id": primary["task_id"]}
    return {"A": a, "B": b}


BIG_GOAL_RE = [r"\d+天", r"\d+个月", r"学位", r"毕业", r"全部", r"所有", r"完成整个",
               r"master", r"degree", r"finish all", r"月内", r"半年", r"一年"]

def is_big_goal(text):
    t = text.lower()
    return any(re.search(p, t) for p in BIG_GOAL_RE)


def parse_import_text(raw):
    items = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line: continue
        parts = re.split(r"\s*[-—]\s*", line)
        title = parts[0].strip()
        if not title: continue
        status, tags, type_ = "not_started", [], "misc"
        for part in parts[1:]:
            p = part.strip().lower()
            if p in ("not_started", "in_progress", "completed", "dropped"):
                status = p
            elif p.startswith("tags:"):
                tags = [t.strip() for t in p.replace("tags:", "").split(",") if t.strip()]
            elif p.startswith("type:"):
                type_ = p.replace("type:", "").strip()
        items.append({"title": title, "type": type_, "status": status, "tags": tags, "difficulty_self_rating": None})
    return items


###############################################################################
# ██████  TELEGRAM BOT HANDLERS
###############################################################################

def bkb(buttons):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in buttons])


async def _send(msg, text, markup):
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ── /start ──

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "🎯 <b>Execution Companion</b>\n\n今天只做一件事，一步一步走。",
        parse_mode="HTML",
        reply_markup=bkb([[("▶️ 开始今天", "cmd_today")], [("⚙️ 管理", "cmd_manage")]]))


# ── /today ──

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    uid = update.effective_user.id
    ensure_user(uid)
    if update.callback_query:
        await update.callback_query.answer()

    # 1. Check deferred
    deferred = get_deferred(uid)
    if deferred:
        ctx.user_data["step_id"] = deferred["deferred_step_id"]
        ctx.user_data["ml_id"] = deferred["mainline_id"]
        update_step(deferred["deferred_step_id"], status="ready")
        clear_deferred(uid)
        await _send(msg,
            f"📌 <b>继续昨天的推进</b>\n\n🔹 {deferred['mainline_title']}\n\n{deferred['instruction']}\n\n✅ {deferred['acceptance_criteria']}",
            bkb([[("▶️ 开始 2 分钟", "timer_micro")], [("🔄 换一个新任务", "today_fresh")]]))
        return

    # 2. Check existing mainline today
    existing = get_today_mainline(uid)
    if existing:
        step = get_active_step(existing["mainline_id"])
        if step:
            ctx.user_data["step_id"] = step["step_id"]
            ctx.user_data["ml_id"] = existing["mainline_id"]
            await _send(msg,
                f"📌 <b>{existing['title']}</b>\n\n{step['instruction']}\n\n✅ {step['acceptance_criteria']}",
                bkb([[("▶️ 开始 {0} 分钟".format(step['duration_min']),
                       "timer_micro" if step['kind'] == 'micro' else "timer_upgrade")]]))
            return

    # 3. Generate new
    await _gen_today(msg, ctx, uid)


async def _gen_today(msg, ctx, uid):
    goal = get_active_goal(uid)
    phase_id = None
    goal_id = None
    if goal:
        goal_id = goal["goal_id"]
        phase = get_active_phase(goal_id)
        if phase:
            phase_id = phase["phase_id"]

    user = get_user(uid)
    low = bool(user.get("low_energy_mode", 0))
    cands = choose_candidates(uid, phase_id, low)
    ctx.user_data["cands"] = cands
    chosen = cands["B"] if low else cands["A"]
    ctx.user_data["chosen"] = chosen

    ml_id = create_mainline(uid, chosen["title"], "auto_from_phase" if phase_id else "manual",
                            goal_id, phase_id, chosen.get("task_id"))
    ctx.user_data["ml_id"] = ml_id

    if chosen.get("task_id"):
        update_task(chosen["task_id"], status="in_progress")

    micro = llm_micro_step(chosen["title"])
    ms = micro["micro_step"]
    sid = create_step(ml_id, "micro", ms["duration_min"], ms["instruction"], ms["acceptance_criteria"])
    ctx.user_data["step_id"] = sid

    # If-then (quiet save)
    it = llm_if_then(chosen["title"])
    if it and "plan" in it:
        p = it["plan"]
        save_if_then(uid, p.get("if_trigger", ""), p.get("then_action", ""), p.get("reward"))

    btns = [[("▶️ 开始 2 分钟", "timer_micro")]]
    if not low:
        btns.append([("🔄 换一个", "switch_B")])
    btns.append([("🔋 低能量模式", "low_energy")])

    await _send(msg,
        f"📌 <b>今日主线</b>：{chosen['title']}\n\n🔹 <b>2 分钟起步</b>\n\n{ms['instruction']}\n\n✅ {ms['acceptance_criteria']}",
        bkb(btns))


# ── Callback Router ──

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    uid = update.effective_user.id
    ensure_user(uid)

    if d == "cmd_today": return await cmd_today(update, ctx)
    if d == "cmd_manage": return await _manage(q.message, uid)

    if d == "today_fresh":
        clear_deferred(uid)
        return await _gen_today(q.message, ctx, uid)

    # Switch to B
    if d == "switch_B":
        cands = ctx.user_data.get("cands", {})
        b = cands.get("B")
        if not b: return
        ml_id = ctx.user_data.get("ml_id")
        if ml_id:
            conn = get_conn()
            conn.execute("UPDATE mainlines SET title=? WHERE mainline_id=?", (b["title"], ml_id))
            conn.commit()
            conn.close()
        micro = llm_micro_step(b["title"])
        ms = micro["micro_step"]
        sid = create_step(ml_id, "micro", ms["duration_min"], ms["instruction"], ms["acceptance_criteria"])
        ctx.user_data["step_id"] = sid
        ctx.user_data["chosen"] = b
        await q.edit_message_text(
            f"📌 <b>已切换</b>：{b['title']}\n\n🔹 <b>2 分钟起步</b>\n\n{ms['instruction']}\n\n✅ {ms['acceptance_criteria']}",
            parse_mode="HTML", reply_markup=bkb([[("▶️ 开始 2 分钟", "timer_micro")]]))
        return

    if d == "low_energy":
        update_user(uid, low_energy_mode=1)
        return await _gen_today(q.message, ctx, uid)

    # Timer micro
    if d == "timer_micro":
        sid = ctx.user_data.get("step_id")
        if sid: update_step(sid, status="executing")
        await q.edit_message_text(
            "⏱ <b>2 分钟开始！</b>\n\n做完点「完成」，卡住点「卡住」。",
            parse_mode="HTML", reply_markup=bkb([
                [("✅ 完成了", "done_micro")],
                [("🧱 卡住了", "stuck"), ("↩️ 缩小", "shrink")],
                [("🚪 退出（明天继续）", "exit")]]))
        return

    # Timer upgrade
    if d == "timer_upgrade":
        sid = ctx.user_data.get("step_id")
        if sid: update_step(sid, status="executing")
        await q.edit_message_text(
            "⏱ <b>8 分钟继续！</b>\n\n保持这个势头。",
            parse_mode="HTML", reply_markup=bkb([
                [("✅ 完成了", "done_upgrade")],
                [("🧱 卡住了", "stuck"), ("↩️ 缩小", "shrink")],
                [("🚪 退出（明天继续）", "exit")]]))
        return

    # Done micro → offer upgrade
    if d == "done_micro":
        sid = ctx.user_data.get("step_id")
        if sid: update_step(sid, status="done")
        ml_id = ctx.user_data.get("ml_id")
        conn = get_conn()
        row = conn.execute("SELECT title FROM mainlines WHERE mainline_id=?", (ml_id,)).fetchone()
        conn.close()
        title = row["title"] if row else "任务"
        step = get_step(sid) if sid else None
        upgrade = llm_upgrade_step(title, step["instruction"] if step else None)
        us = upgrade["step"]
        new_sid = create_step(ml_id, "upgrade", us["duration_min"], us["instruction"], us["acceptance_criteria"], us.get("difficulty", 1))
        ctx.user_data["step_id"] = new_sid
        await q.edit_message_text(
            f"✅ <b>2 分钟完成！</b>\n\n🔥 继续 8 分钟吗？\n\n{us['instruction']}\n\n✅ {us['acceptance_criteria']}\n\n<i>结束也算赢。</i>",
            parse_mode="HTML", reply_markup=bkb([
                [("🔥 继续 8 分钟", "timer_upgrade")],
                [("🌙 结束（也算赢）", "review_start")]]))
        return

    # Done upgrade
    if d == "done_upgrade":
        sid = ctx.user_data.get("step_id")
        if sid: update_step(sid, status="done")
        return await _review(q, ctx, uid)

    if d == "review_start":
        return await _review(q, ctx, uid)

    # Stuck → emotion
    if d == "stuck":
        await q.edit_message_text("先给情绪取个名字：", reply_markup=bkb([
            [("😤 烦躁", "emo_烦躁"), ("😰 焦虑", "emo_焦虑")],
            [("😩 疲惫", "emo_疲惫"), ("😶 麻木", "emo_麻木")],
            [("😔 沮丧", "emo_沮丧"), ("🤷 说不清", "emo_说不清")]]))
        return

    if d.startswith("emo_"):
        ctx.user_data["emo"] = d[4:]
        await q.edit_message_text(
            f"情绪：<b>{d[4:]}</b>\n\n什么卡住了你？", parse_mode="HTML",
            reply_markup=bkb([
                [("✨ 完美主义", "st_PERFECTIONISM")],
                [("🏔 目标太大", "st_GOAL_TOO_BIG")],
                [("🌀 想太多", "st_OVERTHINKING")],
                [("😶‍🌫️ 情绪内耗", "st_EMOTIONAL_FRICTION")],
                [("📱 想刷手机", "st_REWARD_MISMATCH")],
                [("🔒 觉得不行", "st_SELF_LIMITING")]]))
        return

    if d.startswith("st_"):
        st = d[3:]
        emo = ctx.user_data.get("emo", "")
        sid = ctx.user_data.get("step_id")
        ml_id = ctx.user_data.get("ml_id")
        step = get_step(sid) if sid else None
        conn = get_conn()
        row = conn.execute("SELECT title FROM mainlines WHERE mainline_id=?", (ml_id,)).fetchone() if ml_id else None
        conn.close()
        ml_title = row["title"] if row else None

        ev_list = None
        if st == "SELF_LIMITING":
            evs = list_evidence(uid, 5)
            ev_list = [e["counter_evidence"] for e in evs] if evs else None
        if sid:
            create_stuck_event(sid, st, emo)

        iv = llm_intervention(st, emo, ml_title, step["instruction"] if step else None, ev_list)
        lines = [f"💬 <b>{iv.get('intervention_text', '')}</b>\n",
                 f"🫁 <i>{iv.get('body_reset', '')}</i>\n"]
        if iv.get("evidence_quotes"):
            lines.append("📋 <b>你的证据：</b>")
            for eq in iv["evidence_quotes"]:
                lines.append(f"  · {eq[:60]}")
            lines.append("")
        rs = iv.get("restart_step", {})
        lines += [f"🔸 <b>起步动作</b>（{rs.get('duration_min',2)} 分钟）\n",
                  rs.get("instruction", ""), f"\n✅ {rs.get('acceptance_criteria', '')}",
                  f"\n💬 <i>{iv.get('push_line', '→')}</i>"]
        if ml_id:
            new_sid = create_step(ml_id, "micro", rs.get("duration_min", 2),
                                  rs.get("instruction", "做一个最小动作"), rs.get("acceptance_criteria", "动了就行"))
            ctx.user_data["step_id"] = new_sid
        await q.edit_message_text("\n".join(lines), parse_mode="HTML",
            reply_markup=bkb([[("▶️ 开始 2 分钟", "timer_micro")]]))
        return

    # Shrink
    if d == "shrink":
        sid = ctx.user_data.get("step_id")
        ml_id = ctx.user_data.get("ml_id")
        step = get_step(sid) if sid else None
        instr = step["instruction"] if step else "做一个最小动作"
        first = instr.split("，")[0] if "，" in instr else instr.split("。")[0]
        if ml_id:
            new_sid = create_step(ml_id, "micro", 2, f"只做一件事：{first}。做完就算赢。", "完成了这一个动作")
            ctx.user_data["step_id"] = new_sid
        await q.edit_message_text(
            f"↩️ <b>缩小到 2 分钟</b>\n\n只做：{first}\n\n✅ 做完就算赢",
            parse_mode="HTML", reply_markup=bkb([[("▶️ 开始 2 分钟", "timer_micro")]]))
        return

    # Exit (defer)
    if d == "exit":
        sid = ctx.user_data.get("step_id")
        ml_id = ctx.user_data.get("ml_id")
        if sid and ml_id:
            update_step(sid, status="deferred")
            create_deferred(uid, sid, ml_id, "exit")
        await q.edit_message_text(
            "🌙 <b>没关系，明天继续。</b>\n\n下次 /today 会自动接上。退出不是失败，是暂停。",
            parse_mode="HTML", reply_markup=bkb([[("🎯 主菜单", "cmd_start_fresh")]]))
        return

    if d == "cmd_start_fresh":
        await q.edit_message_text("🎯 随时发 /today 继续。", parse_mode="HTML")
        return

    # Session end
    if d == "session_end":
        u = get_user(uid)
        evs = list_evidence(uid, 5)
        lines = ["🌙 <b>今天的推进完成了</b>\n", f"🔥 连续推进 <b>{u['streak_days']}</b> 天"]
        if evs:
            lines += ["\n📋 <b>证据库</b>"]
            for e in evs:
                lines.append(f"  · {e['counter_evidence'][:50]}")
        lines.append("\n每一步都是证据。明天见。")
        await q.edit_message_text("\n".join(lines), parse_mode="HTML",
            reply_markup=bkb([[("🎯 新 Session", "cmd_today")]]))
        return

    # Review tags
    if d.startswith("rtag_"):
        ctx.user_data["rtag"] = d[5:]
        return await _finish_review(q, ctx, uid)
    if d == "rtag_skip":
        return await _finish_review(q, ctx, uid)

    # ── Manage callbacks ──
    if d == "m_goal": return await _goal_menu(q, uid)
    if d == "m_phases": return await _phases_menu(q, uid)
    if d == "m_tasks": return await _tasks_menu(q, ctx, uid)

    if d == "goal_create":
        ctx.user_data["awaiting"] = "goal_title"
        await q.edit_message_text("📝 发送你的目标：\n<i>例如：180天拿到CS学位</i>", parse_mode="HTML")
        return

    if d == "phase_create":
        ctx.user_data["awaiting"] = "phase_title"
        await q.edit_message_text("📝 发送阶段名称：\n<i>例如：Sophia先修阶段</i>", parse_mode="HTML")
        return

    if d.startswith("pa_"):
        pid = int(d[3:])
        goal = get_active_goal(uid)
        if goal: set_active_phase(goal["goal_id"], pid)
        await q.edit_message_text("✅ 阶段已激活。", parse_mode="HTML",
            reply_markup=bkb([[("← 返回", "cmd_manage")]]))
        return

    if d == "t_add":
        ctx.user_data["awaiting"] = "task_title"
        await q.edit_message_text("📝 发送任务标题：", parse_mode="HTML")
        return

    if d == "t_import":
        ctx.user_data["awaiting"] = "import_paste"
        await q.edit_message_text(
            "📋 <b>粘贴任务清单</b>（一行一个）\n\n格式：<code>任务名 - 状态 - tags:标签</code>\n\n状态和标签可选。",
            parse_mode="HTML")
        return

    if d.startswith("ic_"):
        iid = int(d[3:])
        confirm_import(iid)
        dr = get_import_draft(iid)
        n = len(dr["parsed_items"]) if dr else 0
        await q.edit_message_text(f"✅ 已导入 {n} 个任务！\n\n发 /today 开始推进。", parse_mode="HTML")
        return

    if d.startswith("id_"):
        discard_import(int(d[3:]))
        await q.edit_message_text("🗑 已丢弃。", parse_mode="HTML")
        return

    if d.startswith("tt_"):
        tid = int(d[3:])
        conn = get_conn()
        row = conn.execute("SELECT status FROM task_items WHERE task_id=?", (tid,)).fetchone()
        conn.close()
        if row:
            cycle = {"not_started": "in_progress", "in_progress": "completed", "completed": "not_started", "dropped": "not_started"}
            update_task(tid, status=cycle.get(row["status"], "not_started"))
        return await _tasks_menu(q, ctx, uid)

    if d.startswith("td_"):
        delete_task(int(d[3:]))
        return await _tasks_menu(q, ctx, uid)

    if d == "t_back": return await _manage(q.message, uid, edit=True)


# ── Review ──

async def _review(q, ctx, uid):
    await q.edit_message_text(
        "✅ <b>推进了一步！</b>\n\n今天卡在哪了？（可选）", parse_mode="HTML",
        reply_markup=bkb([
            [("✨ 完美主义", "rtag_PERFECTIONISM"), ("🌀 想太多", "rtag_OVERTHINKING")],
            [("📱 想刷手机", "rtag_REWARD_MISMATCH"), ("🔒 觉得不行", "rtag_SELF_LIMITING")],
            [("跳过", "rtag_skip")]]))


async def _finish_review(q, ctx, uid):
    ml_id = ctx.user_data.get("ml_id")
    conn = get_conn()
    row = conn.execute("SELECT title FROM mainlines WHERE mainline_id=?", (ml_id,)).fetchone() if ml_id else None
    conn.close()
    title = row["title"] if row else "任务"
    sid = ctx.user_data.get("step_id")
    step = get_step(sid) if sid else None
    ev_text = f"完成了：{title}"
    if step: ev_text += f" → {step['instruction'][:40]}…"
    tags = ["small_win"]
    tag = ctx.user_data.get("rtag")
    if tag: tags.append(tag)
    create_evidence(uid, ev_text, tags)
    streak = update_streak(uid)
    all_ev = list_evidence(uid, 100)
    await q.edit_message_text(
        f"📋 <b>证据已记录</b>\n\n「{ev_text}」\n\n🔥 连续推进 <b>{streak}</b> 天\n📋 证据库共 <b>{len(all_ev)}</b> 条\n\n每一步都是证据。",
        parse_mode="HTML", reply_markup=bkb([
            [("🎯 继续下一步", "cmd_today")],
            [("🌙 今天结束", "session_end")]]))


# ── Manage ──

async def cmd_manage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    msg = update.message or update.callback_query.message
    if update.callback_query: await update.callback_query.answer()
    await _manage(msg, uid)


async def _manage(msg, uid, edit=False):
    goal = get_active_goal(uid)
    lines = ["⚙️ <b>管理中心</b>\n"]
    if goal:
        lines.append(f"🧭 目标：{goal['title']}")
        phase = get_active_phase(goal["goal_id"])
        if phase:
            lines.append(f"📂 阶段：{phase['title']}")
            tasks = list_tasks(phase["phase_id"])
            c = sum(1 for t in tasks if t["status"] == "completed")
            lines.append(f"📋 任务：{c}/{len(tasks)}")
    else:
        lines.append("还没有目标。")
    u = get_user(uid)
    lines.append(f"\n🔥 连续：{u['streak_days']} 天")
    mk = bkb([[("🧭 目标", "m_goal"), ("📂 阶段", "m_phases")],
              [("📋 任务", "m_tasks")],
              [("▶️ /today", "cmd_today")]])
    text = "\n".join(lines)
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=mk)
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=mk)


async def _goal_menu(q, uid):
    goal = get_active_goal(uid)
    if goal:
        text = f"🧭 <b>当前目标</b>：{goal['title']}"
    else:
        text = "🧭 还没有目标。"
    await q.edit_message_text(text, parse_mode="HTML",
        reply_markup=bkb([[("➕ 创建目标", "goal_create")], [("← 返回", "cmd_manage")]]))


async def _phases_menu(q, uid):
    goal = get_active_goal(uid)
    if not goal:
        await q.edit_message_text("先创建目标。", reply_markup=bkb([[("🧭 创建", "goal_create")]]))
        return
    phases = list_phases(goal["goal_id"])
    if not phases:
        await q.edit_message_text("📂 还没有阶段。", parse_mode="HTML",
            reply_markup=bkb([[("➕ 创建阶段", "phase_create")], [("← 返回", "cmd_manage")]]))
        return
    lines = ["📂 <b>阶段</b>\n"]
    btns = []
    for p in phases:
        icon = "🟢" if p["is_active"] else "⚪"
        lines.append(f"{icon} {p['title']}")
        if not p["is_active"]:
            btns.append([(f"激活「{p['title'][:12]}」", f"pa_{p['phase_id']}")])
    btns += [[("➕ 新阶段", "phase_create")], [("← 返回", "cmd_manage")]]
    await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=bkb(btns))


async def _tasks_menu(q, ctx, uid):
    goal = get_active_goal(uid)
    phase = get_active_phase(goal["goal_id"]) if goal else None
    msg = q.message if hasattr(q, 'message') else q
    if not phase:
        try: await msg.edit_text("先创建目标和阶段。", reply_markup=bkb([[("🧭 创建", "goal_create")]]))
        except: await msg.reply_text("先创建目标和阶段。", reply_markup=bkb([[("🧭 创建", "goal_create")]]))
        return
    tasks = list_tasks(phase["phase_id"])
    icons = {"not_started": "⬜", "in_progress": "🟡", "completed": "✅", "dropped": "🗑"}
    lines = [f"📋 <b>任务</b>（{phase['title']}）\n", "点击切换状态\n"]
    btns = []
    for t in tasks[:20]:
        btns.append([(f"{icons.get(t['status'],'⬜')} {t['title'][:28]}", f"tt_{t['task_id']}")])
    btns += [[("➕ 添加", "t_add"), ("📋 批量导入", "t_import")], [("← 返回", "t_back")]]
    if not tasks: lines.append("还没有任务。")
    try: await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=bkb(btns))
    except: await msg.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=bkb(btns))


# ── Text handler ──

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    aw = ctx.user_data.get("awaiting")
    ensure_user(uid)

    if aw == "goal_title":
        ctx.user_data["awaiting"] = None
        gid = create_goal(uid, text)
        pid = create_phase(gid, "默认阶段", 1)
        await update.message.reply_text(
            f"✅ 目标：<b>{text}</b>\n已创建「默认阶段」。",
            parse_mode="HTML", reply_markup=bkb([
                [("📋 添加任务", "m_tasks")], [("▶️ /today", "cmd_today")]]))
        return

    if aw == "phase_title":
        ctx.user_data["awaiting"] = None
        goal = get_active_goal(uid)
        if goal:
            create_phase(goal["goal_id"], text, 1)
            await update.message.reply_text(f"✅ 阶段「{text}」已激活。", parse_mode="HTML",
                reply_markup=bkb([[("📋 任务", "m_tasks"), ("← 返回", "cmd_manage")]]))
        return

    if aw == "task_title":
        ctx.user_data["awaiting"] = None
        goal = get_active_goal(uid)
        phase = get_active_phase(goal["goal_id"]) if goal else None
        if phase:
            create_task(phase["phase_id"], text)
            await update.message.reply_text(f"✅ 已添加：{text}", parse_mode="HTML",
                reply_markup=bkb([[("➕ 继续添加", "t_add"), ("📋 查看", "m_tasks")]]))
        return

    if aw == "import_paste":
        ctx.user_data["awaiting"] = None
        goal = get_active_goal(uid)
        phase = get_active_phase(goal["goal_id"]) if goal else None
        if not phase:
            await update.message.reply_text("先创建目标和阶段。")
            return
        parsed = parse_import_text(text)
        if not parsed:
            await update.message.reply_text("没有解析到任务，请检查格式。")
            return
        iid = create_import_draft(uid, phase["phase_id"], text, parsed)
        icons = {"not_started": "⬜", "in_progress": "🟡", "completed": "✅"}
        lines = [f"📋 <b>导入预览</b>（{len(parsed)} 个）\n"]
        for i, it in enumerate(parsed[:20]):
            tags = ", ".join(it.get("tags", []))
            ts = f" [{tags}]" if tags else ""
            lines.append(f"{i+1}. {icons.get(it['status'],'⬜')} {it['title']}{ts}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML",
            reply_markup=bkb([[("✅ 确认导入", f"ic_{iid}")], [("🗑 丢弃", f"id_{iid}")]]))
        return

    # Default: quick manual mainline
    if is_big_goal(text):
        await update.message.reply_text(
            f"⚡ 「{text[:20]}…」太大了。\n建议 /manage 创建目标+任务，然后 /today 自动拆步。",
            parse_mode="HTML", reply_markup=bkb([[("⚙️ 管理", "cmd_manage")], [("▶️ 直接开始", "cmd_today")]]))
        return

    ml_id = create_mainline(uid, text)
    micro = llm_micro_step(text)
    ms = micro["micro_step"]
    sid = create_step(ml_id, "micro", ms["duration_min"], ms["instruction"], ms["acceptance_criteria"])
    ctx.user_data["ml_id"] = ml_id
    ctx.user_data["step_id"] = sid
    await update.message.reply_text(
        f"🔒 <b>已锁定</b>：{text}\n\n🔹 <b>2 分钟起步</b>\n\n{ms['instruction']}\n\n✅ {ms['acceptance_criteria']}",
        parse_mode="HTML", reply_markup=bkb([[("▶️ 开始 2 分钟", "timer_micro")]]))


# ── Extra commands ──

async def cmd_evidence(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    evs = list_evidence(uid, 10)
    if not evs:
        await update.message.reply_text("📋 证据库空的。发 /today 完成第一步。")
        return
    u = get_user(uid)
    lines = [f"📋 <b>证据库</b>（{len(evs)}）\n"]
    for e in evs: lines.append(f"  · {e['counter_evidence'][:60]}")
    lines.append(f"\n🔥 连续 <b>{u['streak_days']}</b> 天")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    goal = get_active_goal(uid)
    lines = ["📊 <b>状态</b>\n"]
    if goal:
        lines.append(f"🧭 {goal['title']}")
        phase = get_active_phase(goal["goal_id"])
        if phase:
            tasks = list_tasks(phase["phase_id"])
            c = sum(1 for t in tasks if t["status"] == "completed")
            lines.append(f"📂 {phase['title']}  📋 {c}/{len(tasks)}")
    lines.append(f"🔥 连续：{u['streak_days']} 天")
    df = get_deferred(uid)
    if df: lines.append("⏸ 有未完成步骤")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


###############################################################################
# ██████  MAIN
###############################################################################

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: 设置 TELEGRAM_BOT_TOKEN 环境变量")
        return

    init_db()
    logger.info("DB initialized")

    if os.environ.get("OPENAI_API_KEY", "").strip():
        logger.info("OpenAI enabled")
    else:
        logger.warning("OPENAI_API_KEY not set → fallback mode")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("manage", cmd_manage))
    app.add_handler(CommandHandler("evidence", cmd_evidence))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🚀 Execution Companion Bot v2 is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
