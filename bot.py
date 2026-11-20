import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, time, date, timedelta
import calendar
import pytz
import re
from dotenv import load_dotenv
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import database as db
import currency as fx

# 加载环境变量
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Berlin")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# 保存用户当前正在输入的修改状态
user_states = {}

def is_owner(chat_id: int) -> bool:
    """验证用户权限。若未配置 OWNER_CHAT_ID 则默认所有私聊用户可用"""
    if not OWNER_CHAT_ID:
        return True
    try:
        return int(chat_id) == int(OWNER_CHAT_ID)
    except (ValueError, TypeError):
        return str(chat_id).strip() == str(OWNER_CHAT_ID).strip()

def get_today() -> date:
    """获取当前配置时区（例如 Europe/Berlin 柏林时间）的实际日期"""
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).date()

def calculate_workdays(start_date: date, end_date: date, work_dates: set = None, **kwargs) -> int:
    """计算从 start_date 到 end_date 之间用户主动设定的 Freelancer 兼职工作天数"""
    if start_date > end_date or not work_dates:
        return 0
    workdays = 0
    curr = start_date
    while curr <= end_date:
        if curr.strftime("%Y-%m-%d") in work_dates:
            workdays += 1
        curr += timedelta(days=1)
    return workdays

def parse_date_safely(d_str: str) -> date:
    """安全解析各种日期格式并返回 date 对象（支持 YYYY-MM-DD, 以及省略年份的 MM-DD / MM/DD 智能年份推断）"""
    try:
        parts = d_str.strip().replace('/', '-').split('-')
        today = get_today()
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        elif len(parts) == 2:
            m, d = int(parts[0]), int(parts[1])
            y = today.year if (m > today.month or (m == today.month and d >= today.day)) else today.year + 1
            return date(y, m, d)
        return date.max
    except Exception:
        return date.max

def check_overload_alert(chat_id: int) -> str | None:
    """超载防护弹窗检测（已停用）"""
    return None

def get_fixed_salary_payments(start_range: date, end_range: date, fixed_salary: float, fixed_start_str: str, fixed_day: int = 1) -> float:
    """计算在 [start_range, end_range] 日期区间内，且在 fixed_start_str 生效日期之后的固定工资总到账金额"""
    if fixed_salary <= 0 or not fixed_start_str or start_range > end_range:
        return 0.0
    start_eff = parse_date_safely(fixed_start_str)
    if start_eff == date.max:
        return 0.0

    total_sal = 0.0
    y = start_range.year
    m = start_range.month
    end_y = end_range.year
    end_m = end_range.month

    while (y < end_y) or (y == end_y and m <= end_m):
        _, max_day = calendar.monthrange(y, m)
        pay_day = min(fixed_day, max_day)
        p_date = date(y, m, pay_day)
        if start_range <= p_date <= end_range and p_date >= start_eff:
            total_sal += fixed_salary

        if m == 12:
            y += 1
            m = 1
        else:
            m += 1

    return total_sal

def parse_fixed_salary_input(text: str, rates: dict):
    """智能解析用户输入的固定月薪、起薪月份与发薪日，例如：'从12月开始我的固定收入是2600欧'"""
    t = text.strip()
    if re.search(r"^(关闭|取消|清空|删除)\s*(固定收入|固定工资|固定工作|月薪)?", t) or re.search(r"^(固定收入|固定工资|固定工作|月薪)?\s*(关闭|取消|0|清空)$", t):
        return 0.0, "", 1, "closed"

    fixed_day = 1
    m_day = re.search(r"每月\s*(\d{1,2})\s*[日号]", t)
    if m_day:
        fixed_day = max(1, min(28, int(m_day.group(1))))
        t = t[:m_day.start()] + " " + t[m_day.end():]

    fixed_start = ""
    # 匹配 YYYY-MM-DD 或 YYYY-MM
    m_ymd = re.search(r"(20\d{2})[-/](\d{1,2})(?:[-/](\d{1,2}))?", t)
    if m_ymd:
        y = int(m_ymd.group(1))
        m = int(m_ymd.group(2))
        d = int(m_ymd.group(3)) if m_ymd.group(3) else fixed_day
        fixed_start = f"{y:04d}-{m:02d}-{d:02d}"
        fixed_day = d
        t = t[:m_ymd.start()] + " " + t[m_ymd.end():]
    else:
        # 匹配 MM-DD 或 MM/DD
        m_mmdd = re.search(r"\b(\d{1,2})[-/](\d{1,2})\b", t)
        if m_mmdd and 1 <= int(m_mmdd.group(1)) <= 12 and 1 <= int(m_mmdd.group(2)) <= 31:
            m = int(m_mmdd.group(1))
            d = int(m_mmdd.group(2))
            today = get_today()
            y = today.year if (m > today.month or (m == today.month and d >= today.day)) else today.year + 1
            fixed_start = f"{y:04d}-{m:02d}-{d:02d}"
            fixed_day = d
            t = t[:m_mmdd.start()] + " " + t[m_mmdd.end():]
        else:
            m_month = re.search(r"(?:从|起)?\s*(\d{1,2})\s*月(?:\s*(?:份|开始|起))?", t)
            if m_month and 1 <= int(m_month.group(1)) <= 12:
                m = int(m_month.group(1))
                today = get_today()
                y = today.year if m >= today.month else today.year + 1
                fixed_start = f"{y:04d}-{m:02d}-{fixed_day:02d}"
                t = t[:m_month.start()] + " " + t[m_month.end():]

    t_clean = re.sub(r"(固定收入|固定工资|固定工作|月薪|收入|工资|是|为|从|开始|起|我的|工作)", " ", t).strip()
    m_amt = re.search(r"([€$¥]?\s*\d+(?:\.\d+)?\s*(?:欧|eur|euro|元|rmb|cny|块|刀|usd|\$|€|¥)?)", t_clean, re.I)
    if not m_amt:
        return None
    amt_str = m_amt.group(1).strip()
    val_eur, _ = fx.parse_currency_input(amt_str, rates)
    
    if not fixed_start:
        today = get_today()
        fixed_start = f"{today.year:04d}-{today.month:02d}-{fixed_day:02d}"

    return val_eur, fixed_start, fixed_day, "ok"

def get_fixed_expense_payments(start_range: date, end_range: date, expenses: list) -> float:
    """计算在 [start_range, end_range] 日期区间内所有生效中固定支出的总扣款金额"""
    if not expenses or start_range > end_range:
        return 0.0
    total_exp = 0.0
    for exp in expenses:
        amt = exp.get("amount", 0.0) or 0.0
        if amt <= 0:
            continue
        pay_day = exp.get("pay_day", 1) or 1
        start_str = exp.get("start_date", "") or ""
        end_str = exp.get("end_date", "") or ""
        start_eff = parse_date_safely(start_str) if start_str else date.min
        if start_eff == date.max:
            start_eff = date.min
        end_eff = parse_date_safely(end_str) if end_str else date.max

        y, m = start_range.year, start_range.month
        end_y, end_m = end_range.year, end_range.month
        while (y < end_y) or (y == end_y and m <= end_m):
            _, max_day = calendar.monthrange(y, m)
            p_date = date(y, m, min(pay_day, max_day))
            if start_range <= p_date <= end_range and start_eff <= p_date <= end_eff:
                total_exp += amt
            if m == 12:
                y += 1
                m = 1
            else:
                m += 1
    return total_exp

def parse_fixed_expense_input(text: str, rates: dict):
    """智能解析用户输入的固定每月支出、扣款日与到期月份，例如：'房租 1000欧 每月1号' 或 '学费 650欧 30号 到2027-02结束'"""
    t = text.strip()
    t = re.sub(r"^(?:添加|新增|设置)?(?:固定支出|固定开销|每月支出)\s*", "", t).strip()
    today = get_today()

    # 1. 提取截止期限 (e.g. 到2027-03结束, 至2027-02, 2027-03到期, 2027-03-15, 到3月结束, 到明年3月)
    end_date = ""
    m_end_ymd = re.search(r"(?:到|至|截止到?|期限)?\s*(20\d{2})[-/](\d{1,2})(?:[-/](\d{1,2}))?\s*(?:结束|到期|截止|为止)?", t)
    if m_end_ymd and 1 <= int(m_end_ymd.group(2)) <= 12:
        ey = int(m_end_ymd.group(1))
        em = int(m_end_ymd.group(2))
        _, max_ed = calendar.monthrange(ey, em)
        if m_end_ymd.group(3):
            ed = min(max_ed, max(1, int(m_end_ymd.group(3))))
        else:
            ed = max_ed
        end_date = f"{ey:04d}-{em:02d}-{ed:02d}"
        t = t[:m_end_ymd.start()] + " " + t[m_end_ymd.end():]
    else:
        m_end_m = re.search(r"(?:到|至|截止到?)\s*(?:明年)?\s*(\d{1,2})\s*月(?:\s*(?:份|底|结束|到期|截止|为止))?", t)
        if m_end_m and 1 <= int(m_end_m.group(1)) <= 12:
            em = int(m_end_m.group(1))
            ey = today.year if em >= today.month else today.year + 1
            _, ed = calendar.monthrange(ey, em)
            end_date = f"{ey:04d}-{em:02d}-{ed:02d}"
            t = t[:m_end_m.start()] + " " + t[m_end_m.end():]

    # 2. 提取每月扣款日 (e.g. 每月6号, 每月30日, 6号, 30日)
    pay_day = 1
    m_day = re.search(r"(?:每月)?\s*(\d{1,2})\s*[日号](?:扣款|到账|交)?", t)
    if m_day and 1 <= int(m_day.group(1)) <= 31:
        pay_day = int(m_day.group(1))
        t = t[:m_day.start()] + " " + t[m_day.end():]

    # 3. 提取金额 (e.g. 1000欧, 600, €650, 5000元)
    m_amt = re.search(r"([€$¥]?\s*\d+(?:\.\d+)?\s*(?:欧|eur|euro|元|rmb|cny|块|刀|usd|\$|€|¥)?)", t, re.I)
    if not m_amt:
        return None
    amt_str = m_amt.group(1).strip()
    try:
        val_eur, _ = fx.parse_currency_input(amt_str, rates)
    except Exception:
        return None
    if val_eur <= 0:
        return None
    t = t[:m_amt.start()] + " " + t[m_amt.end():]

    # 4. 剩余部分作为名称 title
    title = re.sub(r"(每月|长期|无期限|到|至|结束|到期)", " ", t).strip()
    if not title:
        title = "固定支出"

    start_date = f"{today.year:04d}-{today.month:02d}-01"
    return title, val_eur, pay_day, start_date, end_date


def build_calendar_keyboard(chat_id: int, year: int, month: int):
    """生成带有状态切换的 Telegram 交互式日历组件（主动点选哪一天，哪一天算 Freelancer 工作日）"""
    keyboard = []
    
    # 顶部年月与切换月份按钮
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)

    keyboard.append([
        InlineKeyboardButton("◀️", callback_data=f"cal_nav_{prev_y}_{prev_m}"),
        InlineKeyboardButton(f"📅 {year}年 {month}月", callback_data="cal_ignore"),
        InlineKeyboardButton("▶️", callback_data=f"cal_nav_{next_y}_{next_m}")
    ])

    # 星期表头
    week_headers = ["一", "二", "三", "四", "五", "六", "日"]
    keyboard.append([InlineKeyboardButton(w, callback_data="cal_ignore") for w in week_headers])

    # 获取当前用户选中的 Freelancer 兼职开工日
    work_days = db.get_work_days(chat_id)
    work_date_set = {wd["work_date"] for wd in work_days}
    today_str = get_today().strftime("%Y-%m-%d")

    # 当月日历矩阵
    month_cal = calendar.monthcalendar(year, month)
    for week in month_cal:
        row = []
        for day_num in week:
            if day_num == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_ignore"))
            else:
                d_str = f"{year:04d}-{month:02d}-{day_num:02d}"
                is_work = d_str in work_date_set

                # 显示图标：
                # 💼 = 选中的兼职工作日
                # 📍 = 今天（未选兼职时）
                # 正常数字 = 无兼职安排（默认全职上班/休息）
                if is_work:
                    btn_text = f"💼{day_num}"
                elif d_str == today_str:
                    btn_text = f"📍{day_num}"
                else:
                    btn_text = f"{day_num}"

                row.append(InlineKeyboardButton(btn_text, callback_data=f"cal_toggle_{d_str}_{year}_{month}"))
        keyboard.append(row)

    # 底部图例与返回按钮
    keyboard.append([
        InlineKeyboardButton("💼 = 兼职工作日 | 数字 = 无兼职 (点击切换)", callback_data="cal_ignore")
    ])
    keyboard.append([
        InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")
    ])

    return InlineKeyboardMarkup(keyboard)

def build_datepicker_keyboard(year: int, month: int, prefix: str = "mspick", extra_id: int = 0, return_cb: str = "refresh", return_text: str = "🔙 取消并返回看板"):
    """通用的交互式日历日期选择器组件"""
    keyboard = []
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)

    keyboard.append([
        InlineKeyboardButton("◀️", callback_data=f"{prefix}_nav_{prev_y}_{prev_m}_{extra_id}"),
        InlineKeyboardButton(f"📅 {year}年 {month}月", callback_data="cal_ignore"),
        InlineKeyboardButton("▶️", callback_data=f"{prefix}_nav_{next_y}_{next_m}_{extra_id}")
    ])

    week_headers = ["一", "二", "三", "四", "五", "六", "日"]
    keyboard.append([InlineKeyboardButton(w, callback_data="cal_ignore") for w in week_headers])

    today = get_today()
    month_cal = calendar.monthcalendar(year, month)
    for week in month_cal:
        row = []
        for day_num in week:
            if day_num == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_ignore"))
            else:
                d_str = f"{year:04d}-{month:02d}-{day_num:02d}"
                d_obj = date(year, month, day_num)
                if d_obj == today:
                    btn_text = f"📍{day_num}"
                else:
                    btn_text = f"{day_num}"
                row.append(InlineKeyboardButton(btn_text, callback_data=f"{prefix}_date_{d_str}_{extra_id}"))
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(return_text, callback_data=return_cb)
    ])
    return InlineKeyboardMarkup(keyboard)

def build_milestone_list_panel(chat_id: int, page: int = 0, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """构建【阶段节点管理】次面板文本与键盘，操作后停留在当前页，由用户自行选择是否返回主看板"""
    milestones = db.get_milestones(chat_id, status="pending")
    prefix = f"{notice}\n\n" if notice else ""
    if not milestones:
        text = f"{prefix}📋 **【阶段节点管理】**\n\n当前没有进行中的阶段节点！"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ 添加阶段节点", callback_data="add_milestone")],
            [InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")]
        ])
        return text, kb

    milestones = sorted(milestones, key=lambda x: parse_date_safely(x['target_date']))
    page_size = 5
    total_pages = (len(milestones) + page_size - 1) // page_size
    page = max(0, min(page, total_pages - 1))
    paged_ms = milestones[page * page_size : (page + 1) * page_size]

    keyboard = []
    for m in paged_ms:
        money_val = m['target_money']
        money_repr = f"€{money_val:.0f}" if money_val == int(money_val) else f"€{money_val:.2f}"
        keyboard.append([
            InlineKeyboardButton(f"✅ 完成: {m['title']}", callback_data=f"done_m_{m['id']}_{page}")
        ])
        keyboard.append([
            InlineKeyboardButton(f"💰 金额: {money_repr}", callback_data=f"editmoney_m_{m['id']}_{page}"),
            InlineKeyboardButton(f"📅 截止: {m['target_date']}", callback_data=f"editdate_m_{m['id']}_{page}"),
            InlineKeyboardButton("🗑️ 删除", callback_data=f"del_m_{m['id']}_{page}")
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ 上一页", callback_data=f"list_m_page_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages} 页", callback_data="cal_ignore"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("下一页 ▶️", callback_data=f"list_m_page_{page + 1}"))

    keyboard.append(nav_row)
    keyboard.append([
        InlineKeyboardButton("➕ 添加新节点", callback_data="add_milestone"),
        InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")
    ])

    text = (
        f"{prefix}📋 **阶段节点管理 (共 {len(milestones)} 项 · 第 {page + 1}/{total_pages} 页)**：\n"
        "点击即可标记完成、修改目标金额、修改截止日期或删除（操作后将留在此面板）："
    )
    return text, InlineKeyboardMarkup(keyboard)

def build_fixed_expenses_panel(chat_id: int, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """构建【固定每月支出管理】次面板文本与键盘，操作后停留在当前面板"""
    expenses = db.get_fixed_expenses(chat_id)
    rates = fx.get_exchange_rates()
    prefix = f"{notice}\n\n" if notice else ""
    keyboard = [
        [InlineKeyboardButton("➕ 添加一项固定支出", callback_data="add_fixed_expense")]
    ]
    if not expenses:
        msg_text = (
            f"{prefix}📉 **【固定每月支出管理】**\n\n"
            "当前暂未设置任何固定每月支出（如房租、生活费、学费、分期还款等）。\n\n"
            "💡 设置后，系统会自动在每个月的对应日期扣减该支出（支持设置**到期结束月份**，到期后自动停止），无需再每个月手动新建重复的阶段节点！"
        )
    else:
        total_m = sum(e["amount"] for e in expenses)
        msg_text = (
            f"{prefix}📉 **【固定每月支出管理】** (共 {len(expenses)} 项 · 合计 `{fx.format_dual_currency(total_m, rates)} /月`)\n\n"
            "当前生效中的固定支出如下（点击下方按钮可继续删除或新增）：\n\n"
        )
        for idx, e in enumerate(expenses, 1):
            e_dual = fx.format_dual_currency(e["amount"], rates)
            end_desc = f"至 `{e['end_date'][:7]}` 结束" if e.get("end_date") else "长期有效"
            msg_text += f"{idx}. **{e['title']}**: `{e_dual}/月` (每月 `{e['pay_day']}` 日 | {end_desc})\n"
            keyboard.append([
                InlineKeyboardButton(f"🗑️ 删除: {e['title']} (€{e['amount']:.0f}/月)", callback_data=f"del_fexp_{e['id']}")
            ])

    keyboard.append([InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")])
    return msg_text, InlineKeyboardMarkup(keyboard)

def format_overview(chat_id: int, show_all_milestones: bool = False, show_all_intervals: bool = False) -> str:
    profile = db.get_profile(chat_id)
    if not profile:
        return "👋 欢迎使用财务与目标追踪机器人！\n目前尚未配置任何数据，请点击下方按钮初始化数据。"
    
    rates = fx.get_exchange_rates()
    cny_rate = rates["CNY"]
    usd_rate = rates["USD"]
    
    balance = profile["current_balance"]
    rate = profile["hourly_rate"]
    fixed_salary = profile.get("fixed_salary", 0.0) or 0.0
    fixed_start = profile.get("fixed_salary_start", "") or ""
    fixed_day = profile.get("fixed_salary_day", 1) or 1

    fixed_salary_desc = "未设置 (暂仅靠兼职工时)"
    if fixed_salary > 0:
        fixed_salary_dual = fx.format_dual_currency(fixed_salary, rates)
        start_desc = f"从 {fixed_start[:7]} 起 " if fixed_start else ""
        fixed_salary_desc = f"{start_desc}`{fixed_salary_dual} /月` (每月 {fixed_day} 日到账)"

    today = get_today()
    fixed_expenses = db.get_fixed_expenses(chat_id)
    active_expenses = []
    for exp in fixed_expenses:
        end_str = exp.get("end_date", "") or ""
        if end_str:
            end_dt = parse_date_safely(end_str)
            if end_dt < today:
                continue
        active_expenses.append(exp)

    if active_expenses:
        total_monthly_exp = sum(e["amount"] for e in active_expenses)
        exp_dual = fx.format_dual_currency(total_monthly_exp, rates)
        fixed_exp_desc = f"共 {len(active_expenses)} 项 (合计 `{exp_dual} /月`)\n"
        for e in active_expenses:
            e_amt_str = f"€{e['amount']:.0f}" if e["amount"] == int(e["amount"]) else f"€{e['amount']:.2f}"
            end_label = f"至 {e['end_date'][:7]} 结束" if e.get("end_date") else "长期"
            fixed_exp_desc += f"  ◦ **{e['title']}**: `{e_amt_str}/月` (每月{e['pay_day']}日 | {end_label})\n"
    else:
        fixed_exp_desc = "未设置 (点击【📉 固定支出管理】添加)\n"

    text = (
        "📊 **【财务与目标多币种看板】**\n"
        f"💱 **实时汇率**: `1 EUR ≈ {cny_rate:.4f} CNY` | `1 EUR ≈ {usd_rate:.4f} USD`\n"
        "─────────────────\n"
        f"💰 **当前资金**: `{fx.format_dual_currency(balance, rates)}`\n"
        f"⏱️ **Freelancer时薪**: `{fx.format_hourly_rate(rate, rates)} /小时`\n"
        f"💼 **固定工作月薪**: {fixed_salary_desc}\n"
        f"📉 **固定每月支出**: {fixed_exp_desc}"
    )
    
    # 获取阶段节点
    milestones = db.get_milestones(chat_id, status="pending")
    if not milestones:
        text += "\n📅 **【进行中的阶段节点】**:\n暂无一次性阶段节点，点击【➕ 添加阶段节点】新建。\n"
    else:
        # 按截止日期由近及远排序（使用 parse_date_safely 防止字符串排序问题）
        milestones = sorted(milestones, key=lambda x: parse_date_safely(x['target_date']))

        if not show_all_milestones and len(milestones) > 3:
            show_ms = milestones[:3]
            text += f"\n📅 **【进行中的阶段节点】** (近期 3 项 · 共 {len(milestones)} 项):\n"
            for idx, m in enumerate(show_ms, 1):
                money_dual = fx.format_dual_currency(m['target_money'], rates)
                text += f"{idx}. 📌 **{m['title']}** (`{m['target_date']}`): `{money_dual}` (`{m['target_hours']:.1f}h`)\n"
        else:
            header_note = f" (共 {len(milestones)} 项)" if len(milestones) > 3 else ""
            text += f"\n📅 **【进行中的阶段节点】**{header_note}:\n"
            for idx, m in enumerate(milestones, 1):
                money_dual = fx.format_dual_currency(m['target_money'], rates)
                text += f"{idx}. 📌 **{m['title']}** (`{m['target_date']}`): `{money_dual}` (`{m['target_hours']:.1f}h`)\n"

    if milestones or active_expenses:
        user_work_days = db.get_work_days(chat_id)
        work_date_set = {wd["work_date"] for wd in user_work_days}

        # 确定推算时间终点：覆盖到最远阶段节点所在月份的月底（若无阶段节点，默认覆盖到下个月底）
        max_ms_dt = today + timedelta(days=35)
        for m in milestones:
            d_obj = parse_date_safely(m['target_date'])
            if d_obj != date.max and d_obj > max_ms_dt:
                max_ms_dt = d_obj
        _, last_day_max = calendar.monthrange(max_ms_dt.year, max_ms_dt.month)
        horizon_dt = date(max_ms_dt.year, max_ms_dt.month, last_day_max)

        # 按日期聚合所有【一次性阶段节点】与【固定每月支出】，让固定支出也直接进入细分账目时间轴
        date_groups = {}
        for m in milestones:
            d_obj = parse_date_safely(m['target_date'])
            d_str = d_obj.strftime("%Y-%m-%d")
            if d_str not in date_groups:
                date_groups[d_str] = {"date": d_str, "dt": d_obj, "titles": [], "total_money": 0.0}
            date_groups[d_str]["titles"].append(m['title'])
            date_groups[d_str]["total_money"] += m['target_money']

        for exp in fixed_expenses:
            amt = exp.get("amount", 0.0) or 0.0
            if amt <= 0:
                continue
            pay_day = exp.get("pay_day", 1) or 1
            start_str = exp.get("start_date", "") or ""
            end_str = exp.get("end_date", "") or ""
            start_eff = parse_date_safely(start_str) if start_str else date.min
            if start_eff == date.max:
                start_eff = date.min
            end_eff = parse_date_safely(end_str) if end_str else date.max

            y, m_num = today.year, today.month
            end_y, end_m = horizon_dt.year, horizon_dt.month
            while (y < end_y) or (y == end_y and m_num <= end_m):
                _, max_day = calendar.monthrange(y, m_num)
                p_date = date(y, m_num, min(pay_day, max_day))
                if today <= p_date <= horizon_dt and start_eff <= p_date <= end_eff:
                    d_str = p_date.strftime("%Y-%m-%d")
                    if d_str not in date_groups:
                        date_groups[d_str] = {"date": d_str, "dt": p_date, "titles": [], "total_money": 0.0}
                    date_groups[d_str]["titles"].append(f"{exp['title']}[固定]")
                    date_groups[d_str]["total_money"] += amt
                if m_num == 12:
                    y += 1
                    m_num = 1
                else:
                    m_num += 1

        sorted_dates = sorted(date_groups.keys(), key=lambda d: parse_date_safely(d))
        overdue_money = sum(date_groups[d]["total_money"] for d in sorted_dates if parse_date_safely(d) < today)
        remaining_dates = [d for d in sorted_dates if parse_date_safely(d) >= today]

        # 核心算法：向后前瞻的多阶段动态平滑排班（Forward-Looking Piecewise Bottleneck Smoothing）
        # 自动将后续紧迫节点的工时需求向前序可用兼职日平摊预存（最高拉满至 10.0h/天），避免前松后紧
        phase_start_dt = today
        phase_start_cash = max(0.0, balance - overdue_money)
        initial_uncovered_overdue = max(0.0, overdue_money - balance)
        date_pace_map = {}
        first_phase = True

        while remaining_dates:
            best_pace = 0.0
            best_idx = 0
            cum_m = initial_uncovered_overdue if first_phase else 0.0
            start_calc = phase_start_dt if phase_start_dt == today else (phase_start_dt + timedelta(days=1))

            for idx, d_str in enumerate(remaining_dates):
                grp = date_groups[d_str]
                cum_m += grp["total_money"]
                cand_fixed_m = get_fixed_salary_payments(start_calc, grp["dt"], fixed_salary, fixed_start, fixed_day)
                gap_m = max(0.0, cum_m - (phase_start_cash + cand_fixed_m))
                gap_h = (gap_m / rate) if rate > 0 else 0.0
                w = calculate_workdays(start_calc, grp["dt"], work_date_set)
                if gap_h <= 0:
                    req = 0.0
                elif w > 0:
                    req = gap_h / w
                else:
                    req = 999.0

                if req > 10.0:
                    # 达到 10h/天 上限：将此前所有可用兼职日拉满至 10h/天 提前预存，并在该节点截断
                    best_pace = req
                    best_idx = idx
                    break

                if req >= best_pace or idx == 0:
                    best_pace = req
                    best_idx = idx

            bottleneck_d = remaining_dates[best_idx]
            phase_dates = remaining_dates[:best_idx + 1]
            phase_w = calculate_workdays(start_calc, date_groups[bottleneck_d]["dt"], work_date_set)
            phase_fixed_m = get_fixed_salary_payments(start_calc, date_groups[bottleneck_d]["dt"], fixed_salary, fixed_start, fixed_day)
            total_due = sum(date_groups[d]["total_money"] for d in phase_dates)
            if first_phase:
                total_due += initial_uncovered_overdue
                first_phase = False

            eff_pace = min(best_pace, 10.0) if best_pace > 0 else 0.0
            total_earned = phase_w * eff_pace * rate + phase_fixed_m

            for d_str in phase_dates:
                date_pace_map[d_str] = best_pace

            phase_start_cash = max(0.0, phase_start_cash + total_earned - total_due)
            phase_start_dt = date_groups[bottleneck_d]["dt"]
            remaining_dates = remaining_dates[best_idx + 1:]

        text += "─────────────────\n"

        interval_lines = []
        intervals = []
        cur_cash = max(0.0, balance - overdue_money)
        carry_overdue = max(0.0, overdue_money - balance)
        prev_d = today

        for d_str in sorted_dates:
            grp = date_groups[d_str]
            d_dt = grp["dt"]
            if d_dt < today:
                continue
            start_calc = (prev_d + timedelta(days=1)) if prev_d != today else prev_d
            int_workdays = calculate_workdays(start_calc, d_dt, work_date_set)
            int_fixed_m = get_fixed_salary_payments(start_calc, d_dt, fixed_salary, fixed_start, fixed_day)

            due_money = grp["total_money"] + carry_overdue
            carry_overdue = 0.0

            raw_pace = date_pace_map.get(d_str, 0.0)
            eff_pace = min(raw_pace, 10.0) if raw_pace > 0 else 0.0
            work_h = int_workdays * eff_pace
            earned_m = work_h * rate

            avail_before_work = cur_cash + int_fixed_m
            avail_after_work = avail_before_work + earned_m
            surplus = avail_after_work - due_money
            shortfall_m = max(0.0, -surplus)
            shortfall_h = (shortfall_m / rate) if rate > 0 else 0.0
            cur_cash = max(0.0, surplus)

            grp_names = " + ".join(grp["titles"])
            prev_name = "今天" if prev_d == today else prev_d.strftime("%m-%d")
            curr_name = d_dt.strftime("%m-%d")
            fixed_note = f" (固定工资+€{int_fixed_m:.0f})" if int_fixed_m > 0 else ""
            surplus_note = f"，预存结余 `€{cur_cash:.2f}`" if cur_cash > 0.01 else ""

            if shortfall_m > 0.01:
                status = "over"
                if work_h > 0.05:
                    interval_lines.append(
                        f"• ⚠️天数不足 `[{prev_name} ➔ {curr_name}]` ({int_workdays}兼职日 | {grp_names}): "
                        f"需 `€{due_money:.2f}` | 差 `€{shortfall_m:.2f}` | 已排 `{work_h:.1f}h`，天数不够，还需 `{shortfall_h:.1f}h`{fixed_note}"
                    )
                else:
                    interval_lines.append(
                        f"• ⚠️天数不足 `[{prev_name} ➔ {curr_name}]` ({int_workdays}兼职日 | {grp_names}): "
                        f"需 `€{due_money:.2f}` | 差 `€{shortfall_m:.2f}` | 天数不够，还需 `{shortfall_h:.1f}h`{fixed_note}"
                    )
            elif work_h <= 0.05:
                status = "zero"
                interval_lines.append(
                    f"• 🟢全额覆盖 `[{prev_name} ➔ {curr_name}]` ({int_workdays}兼职日 | {grp_names}): "
                    f"需 `€{due_money:.2f}` | 差 `€0.00` | 需工作 `0.0h`{fixed_note}{surplus_note}"
                )
            else:
                status = "ok"
                tag = "🔥冲刺期" if eff_pace > 6.0 else "🟢平稳期"
                interval_lines.append(
                    f"• {tag} `[{prev_name} ➔ {curr_name}]` ({int_workdays}兼职日 | {grp_names}): "
                    f"需 `€{due_money:.2f}` | 差 `€0.00` | 需工作 `{work_h:.1f}h` (`{eff_pace:.1f}h/天`){fixed_note}{surplus_note}"
                )

            intervals.append({
                "start_dt": prev_d,
                "end_dt": d_dt,
                "workdays": int_workdays,
                "titles": list(grp["titles"]),
                "due_money": due_money,
                "work_h": work_h,
                "shortfall_m": shortfall_m,
                "shortfall_h": shortfall_h,
                "fixed_income": int_fixed_m,
                "end_cash": cur_cash,
                "status": status
            })
            prev_d = d_dt

        merged_phases = []
        for inv in intervals:
            if merged_phases and (
                merged_phases[-1]["status"] == inv["status"]
                or (merged_phases[-1]["status"] == "ok" and inv["status"] == "zero" and inv["fixed_income"] == 0)
            ):
                prev = merged_phases[-1]
                prev["end_dt"] = inv["end_dt"]
                prev["workdays"] += inv["workdays"]
                prev["titles"].extend(inv["titles"])
                prev["due_money"] += inv["due_money"]
                prev["work_h"] += inv["work_h"]
                prev["shortfall_m"] += inv["shortfall_m"]
                prev["shortfall_h"] += inv["shortfall_h"]
                prev["fixed_income"] += inv["fixed_income"]
                prev["end_cash"] = inv["end_cash"]
            else:
                merged_phases.append(dict(inv))

        if not show_all_intervals:
            text += "🧭 **【各阶段Freelancer工时规划】**:\n"
            for idx, p in enumerate(merged_phases, 1):
                s_name = "今天" if p["start_dt"] == today else p["start_dt"].strftime("%m-%d")
                e_name = p["end_dt"].strftime("%m-%d")
                short_titles = " + ".join(p["titles"][:2])
                if len(p["titles"]) > 2:
                    short_titles += f" 等 {len(p['titles'])} 项"
                fixed_note = f" (固定工资+€{p['fixed_income']:.0f})" if p.get("fixed_income", 0.0) > 0 else ""
                surplus_note = f"，预存结余 `€{p['end_cash']:.2f}`" if p.get("end_cash", 0.0) > 0.01 else ""

                if p["status"] == "zero":
                    tag = f"🟢阶段{idx}·全额覆盖"
                    text += (
                        f"• **{tag}** `[{s_name} ➔ {e_name}]` ({p['workdays']}兼职日 | {short_titles}): "
                        f"需 `€{p['due_money']:.2f}` | 差 `€0.00` | 需工作 `0.0h`{fixed_note}{surplus_note}\n"
                    )
                elif p["status"] == "ok":
                    daily_h = (p["work_h"] / p["workdays"]) if p["workdays"] > 0 else 0.0
                    tag = f"🔥阶段{idx}·冲刺" if daily_h > 6.0 else f"🟢阶段{idx}·平稳"
                    text += (
                        f"• **{tag}** `[{s_name} ➔ {e_name}]` ({p['workdays']}兼职日 | {short_titles}): "
                        f"需 `€{p['due_money']:.2f}` | 差 `€0.00` | 需工作 `{p['work_h']:.1f}h` (`{daily_h:.1f}h/天`){fixed_note}{surplus_note}\n"
                    )
                else:
                    tag = f"⚠️阶段{idx}·天数不足"
                    if p["work_h"] > 0.05:
                        text += (
                            f"• **{tag}** `[{s_name} ➔ {e_name}]` ({p['workdays']}兼职日 | {short_titles}): "
                            f"需 `€{p['due_money']:.2f}` | 差 `€{p['shortfall_m']:.2f}` | 已排 `{p['work_h']:.1f}h`，天数不够，还需 `{p['shortfall_h']:.1f}h`{fixed_note}\n"
                        )
                    else:
                        text += (
                            f"• **{tag}** `[{s_name} ➔ {e_name}]` ({p['workdays']}兼职日 | {short_titles}): "
                            f"需 `€{p['due_money']:.2f}` | 差 `€{p['shortfall_m']:.2f}` | 天数不够，还需 `{p['shortfall_h']:.1f}h`{fixed_note}\n"
                        )
        else:
            text += "🔍 **【各节点逐项区间细分审计】**:\n"
            text += "\n".join(interval_lines) + "\n"

    text += "─────────────────\n请选择你要进行的操作："
    return text

def get_main_keyboard(chat_id: int, show_all_milestones: bool = False, show_all_intervals: bool = False):
    milestones = db.get_milestones(chat_id, status="pending")
    has_more_ms = len(milestones) > 3

    m_val = 1 if show_all_milestones else 0
    i_val = 1 if show_all_intervals else 0

    keyboard = [
        [
            InlineKeyboardButton("💰 更新今天的总资金", callback_data="set_balance"),
            InlineKeyboardButton("➕ 记录一笔新收入", callback_data="add_balance"),
        ],
        [
            InlineKeyboardButton("⏱️ Freelancer时薪", callback_data="set_rate"),
            InlineKeyboardButton("💼 固定工作月薪", callback_data="set_fixed_salary"),
        ],
        [
            InlineKeyboardButton("📉 固定支出管理", callback_data="manage_fixed_expenses"),
            InlineKeyboardButton("📅 安排兼职工作日历", callback_data="manage_off_days"),
        ],
        [
            InlineKeyboardButton("➕ 添加阶段节点", callback_data="add_milestone"),
            InlineKeyboardButton("📋 管理/完成节点", callback_data="list_milestones"),
        ],
    ]

    toggle_row = []
    if has_more_ms:
        if show_all_milestones:
            toggle_row.append(InlineKeyboardButton("🔼 收起远期节点", callback_data=f"tg_v_0_{i_val}"))
        else:
            toggle_row.append(InlineKeyboardButton(f"🔍 展开待办节点({len(milestones)})", callback_data=f"tg_v_1_{i_val}"))

    if show_all_intervals:
        toggle_row.append(InlineKeyboardButton("🔼 收起细分账目", callback_data=f"tg_v_{m_val}_0"))
    else:
        toggle_row.append(InlineKeyboardButton("🔍 查看细分账目", callback_data=f"tg_v_{m_val}_1"))

    if len(toggle_row) == 2:
        keyboard.append(toggle_row)
        keyboard.append([InlineKeyboardButton("🔄 刷新看板与汇率", callback_data="refresh")])
    elif len(toggle_row) == 1:
        toggle_row.append(InlineKeyboardButton("🔄 刷新看板与汇率", callback_data="refresh"))
        keyboard.append(toggle_row)
    else:
        keyboard.append([InlineKeyboardButton("🔄 刷新看板与汇率", callback_data="refresh")])

    return InlineKeyboardMarkup(keyboard)

def get_reminder_keyboard(chat_id: int):
    """每日 9:00 专用提醒快捷键盘，首要突出更新总资金"""
    keyboard = [
        [
            InlineKeyboardButton("📝 立即更新今天的总资金", callback_data="set_balance"),
        ],
        [
            InlineKeyboardButton("👀 查看完整详细看板", callback_data="refresh"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not db.get_profile(chat_id):
        db.update_profile(chat_id, current_balance=0.0, target_amount=0.0, hourly_rate=0.0)
    
    text = format_overview(chat_id)
    await update.message.reply_text(
        text,
        reply_markup=get_main_keyboard(chat_id),
        parse_mode="Markdown"
    )

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """一键备份数据库命令 /backup"""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id):
        await update.message.reply_text("⛔ 抱歉，仅机器人所有者可执行备份操作。")
        return
    db_path = db.DB_FILE
    if os.path.exists(db_path):
        try:
            with open(db_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="budget_data.db",
                    caption="📦 **【数据库备份文件】**\n\n云端上线后，直接在 Telegram 中将此文件发送给机器人，即可一键恢复所有预算与阶段目标！"
                )
        except Exception as e:
            logger.error(f"Failed to send backup document: {e}")
            await update.message.reply_text(f"❌ 备份文件读取失败: {e}")
    else:
        await update.message.reply_text("⚠️ 数据库文件暂不存在。")

async def seed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """一键生成 Render 环境变量 SEED_DATA 命令 /seed 或 /export"""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id):
        await update.message.reply_text("⛔ 抱歉，仅机器人所有者可导出数据配置。")
        return
    try:
        import export_seed
        seed_str = export_seed.generate_seed()
        if not seed_str:
            await update.message.reply_text("⚠️ 数据库为空或暂无数据，无法生成 SEED_DATA。")
            return
        
        reply_msg = (
            "🌱 **【Render 云端永久固化配置】**\n\n"
            "为防止 Render 免费版休眠重启或重新部署时数据丢失，请将下方代码块完整复制，粘贴到 Render 仪表板的 **Environment Variables** 中的 `SEED_DATA`：\n\n"
            f"```text\n{seed_str}\n```\n\n"
            "💡 **提示**：复制上方长文本后填入 Render 环境变量即可，重新部署时云端将自动以最新账目开机！"
        )
        if len(reply_msg) > 4000:
            import io
            buf = io.BytesIO(seed_str.encode("utf-8"))
            buf.name = "SEED_DATA.txt"
            await update.message.reply_document(
                document=buf,
                caption="🌱 **【Render 云端 SEED_DATA 配置文件】**\n由于数据较长，已导出为文本文件。打开并复制文件全部内容填入 Render 即可！"
            )
        else:
            await update.message.reply_text(reply_msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to generate seed data: {e}")
        await update.message.reply_text(f"❌ 生成 SEED_DATA 失败: {e}")

async def daily_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """每日定时触发的提醒任务：弹窗直接引导用户更新今天的最新总资产"""
    target_chat_ids = []
    if OWNER_CHAT_ID:
        try:
            target_chat_ids.append(int(OWNER_CHAT_ID))
        except ValueError:
            pass
    
    import sqlite3
    conn = sqlite3.connect(db.DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM user_profile")
    rows = c.fetchall()
    conn.close()
    
    for r in rows:
        if r[0] not in target_chat_ids:
            target_chat_ids.append(r[0])
            
    for cid in target_chat_ids:
        try:
            profile = db.get_profile(cid)
            rates = fx.get_exchange_rates()
            cur_bal = profile["current_balance"] if profile else 0.0
            cur_str = fx.format_dual_currency(cur_bal, rates)

            reminder_text = (
                "⏰ **【每日 9:00 财务打卡】**\n\n"
                "新的一天开始了！请核对并更新您今天的最新总资金：\n"
                f"💰 **昨日总资金**: `{cur_str}`\n\n"
                "👇 点击下方按钮，直接输入今天银行卡/账户的最新数额："
            )
            await context.bot.send_message(
                chat_id=cid,
                text=reminder_text,
                reply_markup=get_reminder_keyboard(cid),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"发送定时提醒给 {cid} 失败: {e}")

async def safe_edit_message(query, text: str, reply_markup=None):
    """安全编辑消息：防范 Markdown 渲染错误与 Message is not modified 异常"""
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
    except (BadRequest, telegram.error.BadRequest) as e:
        err_msg = str(e)
        if "Message is not modified" in err_msg:
            return
        logger.warning(f"Markdown edit failed ({e}), falling back to plain text")
        clean_text = text.replace('*', '').replace('`', '')
        try:
            await query.edit_message_text(text=clean_text, reply_markup=reply_markup)
        except Exception as e2:
            logger.error(f"Failed to edit message in fallback: {e2}")
    except Exception as e:
        logger.error(f"Unexpected error in safe_edit_message: {e}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = query.data

    cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消并返回", callback_data="refresh")]])

    if data == "refresh":
        user_states.pop(chat_id, None)
        try:
            await query.answer()
        except Exception:
            pass

        text = format_overview(chat_id)
        await safe_edit_message(
            query,
            text=text,
            reply_markup=get_main_keyboard(chat_id)
        )
    elif data.startswith("tg_v_"):
        try:
            await query.answer()
        except Exception:
            pass
        parts = data.split("_")
        show_m = (parts[2] == "1")
        show_i = (parts[3] == "1")
        text = format_overview(chat_id, show_all_milestones=show_m, show_all_intervals=show_i)
        await safe_edit_message(query, text=text, reply_markup=get_main_keyboard(chat_id, show_all_milestones=show_m, show_all_intervals=show_i))

    elif data == "toggle_intervals_on":
        try:
            await query.answer()
        except Exception:
            pass
        text = format_overview(chat_id, show_all_milestones=False, show_all_intervals=True)
        await safe_edit_message(query, text=text, reply_markup=get_main_keyboard(chat_id, show_all_milestones=False, show_all_intervals=True))

    elif data == "toggle_intervals_off":
        try:
            await query.answer()
        except Exception:
            pass
        text = format_overview(chat_id, show_all_milestones=False, show_all_intervals=False)
        await safe_edit_message(query, text=text, reply_markup=get_main_keyboard(chat_id, show_all_milestones=False, show_all_intervals=False))

    elif data == "add_balance":
        user_states[chat_id] = {"action": "add_balance"}
        await safe_edit_message(
            query,
            "➕ 请直接回复消息输入 **本次增加的资金**：\n\n"
            "💡 例如赚到了一笔钱，输入后会自动累加到当前资金：\n"
            "- 输入人民币：`2000元`、`2000rmb`、`¥2000`\n"
            "- 输入欧元：`300`、`300欧`、`300eur`、`€300`",
            reply_markup=cancel_btn
        )
    elif data == "set_balance":
        user_states[chat_id] = {"action": "set_balance"}
        await safe_edit_message(
            query,
            "💰 请直接回复消息输入 **今天你的最新总资产数额**：\n\n"
            "💡 支持输入人民币或欧元，例如：\n"
            "- 输入人民币：`50000元`、`50000rmb`、`¥50000`\n"
            "- 输入欧元：`6500`、`6500欧`、`6500eur`、`€6500`\n\n"
            "输入后系统会自动重新测算所有阶段节点的倒推工时！",
            reply_markup=cancel_btn
        )
    elif data == "set_target":
        user_states[chat_id] = {"action": "set_target"}
        await safe_edit_message(
            query,
            "🎯 请直接回复消息输入 **总目标金额**：\n\n"
            "💡 支持输入人民币或欧元，例如：\n"
            "- 输入人民币：`100000元` 或 `¥100000`\n"
            "- 输入欧元：`13000` 或 `€13000`",
            reply_markup=cancel_btn
        )
    elif data == "set_rate":
        user_states[chat_id] = {"action": "set_rate"}
        await safe_edit_message(
            query,
            "⏱️ 请直接回复消息输入 **你的Freelancer时薪**：\n\n"
            "💡 支持输入美金、人民币或欧元，例如：\n"
            "- 输入美金：`25美元`、`25usd`、`$25`、`25刀`\n"
            "- 输入人民币：`150元` 或 `150rmb`\n"
            "- 输入欧元：`20` 或 `€20`",
            reply_markup=cancel_btn
        )
    elif data == "set_fixed_salary":
        user_states[chat_id] = {"action": "set_fixed_salary_input"}
        profile = db.get_profile(chat_id)
        cur_sal = profile.get("fixed_salary", 0.0) if profile else 0.0
        cur_start = profile.get("fixed_salary_start", "") if profile else ""
        cur_day = profile.get("fixed_salary_day", 1) if profile else 1
        rates = fx.get_exchange_rates()
        curr_desc = f"€{cur_sal:.2f} (从 {cur_start} 起，每月 {cur_day} 日)" if cur_sal > 0 else "未设置"
        await safe_edit_message(
            query,
            f"💼 **设置固定工作月薪与起薪月份**\n\n"
            f"当前设定: `{curr_desc}`\n\n"
            "✍️ 请直接回复消息输入 **月薪金额** 与 **生效月份/发薪日**：\n\n"
            "💡 例如：\n"
            "• `2600欧 12月`（从12月1日起，每月到账 €2600）\n"
            "• `从12月开始我的固定收入是2600欧`\n"
            "• `20000元 12-01 每月10号`\n"
            "• `2600`（按当月1号开始生效）\n"
            "• `关闭` 或 `0`（清空固定工资）\n\n"
            "设置后系统会自动先扣抵固定工资，剩余缺口再由 Freelancer 工时精确冲抵！",
            reply_markup=cancel_btn
        )
    elif data == "add_milestone":
        profile = db.get_profile(chat_id)
        if not profile or profile["hourly_rate"] <= 0:
            await safe_edit_message(
                query,
                "⚠️ 请先设置一个大于 0 的时薪，才能根据时薪计算阶段节点！",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="refresh")]])
            )
            return

        user_states[chat_id] = {"action": "add_milestone_input"}
        prompt = (
            "➕ **添加阶段节点（第一步：名称与金额）**\n\n"
            "✍️ 请直接回复输入 **名称** 和 **金额/工时**（中间空格隔开，**无需输入日期**）：\n\n"
            "例如：\n"
            "• `语言课 650欧`\n"
            "• `相机镜头 800元`\n"
            "• `健身年卡 2500元`\n"
            "• `买电脑 $1200`\n"
            "• `兼职项目 25h`\n\n"
            "📅 **发送后会直接弹出可视化日历**，您直接在日历上点击日期即可设置截止日！"
        )
        await safe_edit_message(query, prompt, reply_markup=cancel_btn)
    
    elif data == "list_milestones" or data.startswith("list_m_page_"):
        user_states.pop(chat_id, None)
        page = int(data.replace("list_m_page_", "")) if data.startswith("list_m_page_") else 0
        panel_text, panel_kb = build_milestone_list_panel(chat_id, page=page)
        await safe_edit_message(query, panel_text, reply_markup=panel_kb)

    elif data.startswith("editmoney_m_"):
        parts = data.replace("editmoney_m_", "").split("_")
        m_id = int(parts[0])
        page = int(parts[1]) if len(parts) > 1 else 0
        milestones = db.get_milestones(chat_id, status="pending")
        target_m = next((m for m in milestones if m["id"] == m_id), None)
        if not target_m:
            await query.answer("⚠️ 未找到该阶段节点", show_alert=True)
            return

        user_states[chat_id] = {
            "action": "edit_milestone_money_input",
            "milestone_id": m_id,
            "title": target_m["title"],
            "page": page
        }
        rates = fx.get_exchange_rates()
        curr_money_str = fx.format_dual_currency(target_m["target_money"], rates)
        prompt = (
            f"💰 **修改节点目标金额**\n\n"
            f"当前节点: 【**{target_m['title']}**】\n"
            f"当前设定金额: `{curr_money_str}` (`{target_m['target_hours']:.1f}h`)\n\n"
            f"✍️ 请直接回复输入 **新的金额或工时**：\n"
            f"- 例如输入欧元：`800`、`800欧`、`€800`\n"
            f"- 例如输入人民币：`6000元`、`¥6000`\n"
            f"- 例如输入工时：`40h` 或 `40小时`\n\n"
            f"输入后将自动保存并返回节点管理列表！"
        )
        back_to_list_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消并返回节点列表", callback_data=f"list_m_page_{page}")]])
        await safe_edit_message(query, prompt, reply_markup=back_to_list_btn)
    
    elif data.startswith("editdate_m_"):
        parts = data.replace("editdate_m_", "").split("_")
        m_id = int(parts[0])
        page = int(parts[1]) if len(parts) > 1 else 0
        user_states[chat_id] = {"msedit_page": page}
        milestones = db.get_milestones(chat_id, status="pending")
        target_m = next((m for m in milestones if m["id"] == m_id), None)
        title = target_m["title"] if target_m else "该节点"
        curr_d = parse_date_safely(target_m["target_date"]) if target_m else get_today()
        cal_kb = build_datepicker_keyboard(
            curr_d.year, curr_d.month, "msedit", extra_id=m_id,
            return_cb=f"list_m_page_{page}", return_text="🔙 取消并返回节点列表"
        )
        await safe_edit_message(
            query,
            f"📅 **修改节点截止日期**\n\n"
            f"当前节点: 【**{title}**】\n"
            f"请在下方日历中直接点击选择**最新截止日期**：",
            reply_markup=cal_kb
        )

    elif data.startswith("msedit_nav_"):
        parts = data.split("_")
        y, m, m_id = int(parts[2]), int(parts[3]), int(parts[4])
        page = user_states.get(chat_id, {}).get("msedit_page", 0)
        cal_kb = build_datepicker_keyboard(
            y, m, "msedit", extra_id=m_id,
            return_cb=f"list_m_page_{page}", return_text="🔙 取消并返回节点列表"
        )
        await query.edit_message_reply_markup(reply_markup=cal_kb)

    elif data.startswith("msedit_date_"):
        parts = data.split("_")
        new_d_str = parts[2]
        m_id = int(parts[3])
        page = user_states.pop(chat_id, {}).get("msedit_page", 0)
        title = db.update_milestone_date(m_id, chat_id, new_d_str)
        try:
            await query.answer(f"✅ 已将【{title}】截止日期更新为 {new_d_str}！", show_alert=False)
        except Exception:
            pass
        notice = f"✅ 已成功将【**{title}**】截止日期更新为 `{new_d_str}`！"
        panel_text, panel_kb = build_milestone_list_panel(chat_id, page=page, notice=notice)
        await safe_edit_message(query, text=panel_text, reply_markup=panel_kb)

    elif data.startswith("mspick_nav_"):
        parts = data.split("_")
        y, m = int(parts[2]), int(parts[3])
        cal_kb = build_datepicker_keyboard(y, m, "mspick")
        await query.edit_message_reply_markup(reply_markup=cal_kb)

    elif data.startswith("mspick_date_"):
        parts = data.split("_")
        chosen_date_str = parts[2]
        state = user_states.get(chat_id)
        if not state or state.get("action") != "pick_milestone_date":
            await query.answer("⚠️ 流程已过期，请重新点击【➕ 添加阶段节点】", show_alert=True)
            await safe_edit_message(query, "⚠️ 流程已过期，请重新选择操作：", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ 重新添加阶段节点", callback_data="add_milestone")],
                [InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")]
            ]))
            return
        title = state["title"]
        hours = state["hours"]
        money_eur = state["money_eur"]
        db.add_milestone(chat_id, title, chosen_date_str, hours, money_eur)
        user_states.pop(chat_id, None)

        rates = fx.get_exchange_rates()
        money_dual = fx.format_dual_currency(money_eur, rates)
        profile = db.get_profile(chat_id)
        rate = profile["hourly_rate"] if profile else 0.0
        hourly_str = fx.format_hourly_rate(rate, rates)

        try:
            await query.answer(f"✅ 成功添加【{title}】（截止: {chosen_date_str}）！", show_alert=False)
        except Exception:
            pass

        res_text = (
            f"✅ **成功添加阶段节点【{title}】！**\n\n"
            f"📅 截止时间: `{chosen_date_str}`\n"
            f"💰 目标金额: `{money_dual}`\n"
            f"⏱️ 换算所需工时: `{hours:.1f}` 小时 (按时薪 {hourly_str})\n\n"
            "您可以继续添加下一个节点，或自行选择返回主看板："
        )
        sub_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ 继续添加下一个节点", callback_data="add_milestone"),
                InlineKeyboardButton("📋 进入节点管理列表", callback_data="list_milestones"),
            ],
            [InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")]
        ])
        await safe_edit_message(query, res_text, reply_markup=sub_kb)
    
    elif data.startswith("done_m_"):
        parts = data.replace("done_m_", "").split("_")
        m_id = int(parts[0])
        page = int(parts[1]) if len(parts) > 1 else 0
        title, deducted_money = db.complete_milestone(m_id, chat_id)
        rates = fx.get_exchange_rates()
        deducted_str = fx.format_dual_currency(deducted_money, rates)
        try:
            await query.answer(f"🎉 已完成【{title}】！", show_alert=False)
        except Exception:
            pass
        notice = (
            f"🎉 **阶段节点【{title}】已完成！**\n"
            f"💰 已按计划从当前总资金中扣除: `-{deducted_str}`"
        )
        panel_text, panel_kb = build_milestone_list_panel(chat_id, page=page, notice=notice)
        await safe_edit_message(query, text=panel_text, reply_markup=panel_kb)
        
    elif data.startswith("del_m_"):
        parts = data.replace("del_m_", "").split("_")
        m_id = int(parts[0])
        page = int(parts[1]) if len(parts) > 1 else 0
        db.delete_milestone(m_id, chat_id)
        try:
            await query.answer("🗑️ 已删除该阶段节点", show_alert=False)
        except Exception:
            pass
        panel_text, panel_kb = build_milestone_list_panel(chat_id, page=page, notice="🗑️ **已删除该阶段节点。**")
        await safe_edit_message(query, text=panel_text, reply_markup=panel_kb)

    elif data == "manage_fixed_expenses" or data.startswith("del_fexp_"):
        user_states.pop(chat_id, None)
        notice = ""
        if data.startswith("del_fexp_"):
            exp_id = int(data.replace("del_fexp_", ""))
            del_title = db.delete_fixed_expense(exp_id, chat_id)
            notice = f"🗑️ **已成功删除固定支出【{del_title}】**"
            try:
                await query.answer(f"🗑️ 已删除固定支出【{del_title}】", show_alert=False)
            except Exception:
                pass

        msg_text, panel_kb = build_fixed_expenses_panel(chat_id, notice=notice)
        await safe_edit_message(query, msg_text, reply_markup=panel_kb)

    elif data == "add_fixed_expense":
        user_states[chat_id] = {"action": "add_fixed_expense_input"}
        prompt = (
            "➕ **添加固定每月支出（支持设置结束期限）**\n\n"
            "✍️ 请直接回复输入 **名称**、**金额**、**每月几号** 及 **到期月份（可选）**：\n\n"
            "💡 示例：\n"
            "• `房租 1000欧 每月1号`（长期固定支出）\n"
            "• `生活费 600欧 每月30号`（长期固定支出）\n"
            "• `学费 650欧 每月30号 到2027-02结束`（带期限，到期自动停扣）\n"
            "• `分期还款 1086.79欧 每月6号 到2027-03结束`\n"
            "• `健身房 500元`（默认每月1号、长期有效）"
        )
        back_to_fexp_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消并返回固定支出列表", callback_data="manage_fixed_expenses")]])
        await safe_edit_message(query, prompt, reply_markup=back_to_fexp_btn)

    elif data == "manage_off_days":
        today = get_today()
        cal_kb = build_calendar_keyboard(chat_id, today.year, today.month)
        text = (
            "📅 **【Freelancer 兼职日历与排班】**\n\n"
            "您现在有全职工作，日历采用**主动点选**逻辑：\n"
            "• **点击任意一天**：直接设为 💼 **兼职开工日**（按照工作来算）\n"
            "• **再次点击**：取消该天的兼职安排\n\n"
            "图标说明：\n"
            "• 💼 = 兼职开工日（计入 Freelancer 可用工作天数）\n"
            "• 📍 = 今天\n"
            "• 普通数字 = 无兼职安排（专注全职/休息）\n\n"
            "设置后点击底部【🔙 返回财务看板】，每日工时将立即自动重测！"
        )
        await safe_edit_message(query, text=text, reply_markup=cal_kb)

    elif data.startswith("cal_nav_"):
        parts = data.split("_")
        nav_year, nav_month = int(parts[2]), int(parts[3])
        cal_kb = build_calendar_keyboard(chat_id, nav_year, nav_month)
        text = (
            "📅 **【Freelancer 兼职日历与排班】**\n\n"
            "您现在有全职工作，日历采用**主动点选**逻辑：\n"
            "• **点击任意一天**：直接设为 💼 **兼职开工日**（按照工作来算）\n"
            "• **再次点击**：取消该天的兼职安排\n\n"
            "图标说明：\n"
            "• 💼 = 兼职开工日（计入 Freelancer 可用工作天数）\n"
            "• 📍 = 今天\n"
            "• 普通数字 = 无兼职安排（专注全职/休息）\n\n"
            "设置后点击底部【🔙 返回财务看板】，每日工时将立即自动重测！"
        )
        await safe_edit_message(query, text=text, reply_markup=cal_kb)

    elif data.startswith("cal_toggle_"):
        parts = data.split("_")
        toggle_date_str = parts[2]
        cur_year, cur_month = int(parts[3]), int(parts[4])

        # 主动点选逻辑：点击哪一天，哪一天就切换为 Freelancer 兼职工作日 (💼)
        work_days = db.get_work_days(chat_id)
        existing_work = next((wd for wd in work_days if wd["work_date"] == toggle_date_str), None)
        if existing_work:
            db.delete_work_day(chat_id, toggle_date_str)
            action_tip = f"已取消 {toggle_date_str} 的兼职工作安排"
        else:
            db.add_work_day(chat_id, toggle_date_str, "日历点选兼职")
            action_tip = f"✅ 已将 {toggle_date_str} 设为兼职工作日 💼！可用天数+1"

        try:
            await query.answer(action_tip, show_alert=False)
        except Exception:
            pass

        # 刷新当前月份日历
        cal_kb = build_calendar_keyboard(chat_id, cur_year, cur_month)
        text = (
            "📅 **【Freelancer 兼职日历与排班】**\n\n"
            "您现在有全职工作，日历采用**主动点选**逻辑：\n"
            "• **点击任意一天**：直接设为 💼 **兼职开工日**（按照工作来算）\n"
            "• **再次点击**：取消该天的兼职安排\n\n"
            "图标说明：\n"
            "• 💼 = 兼职开工日（计入 Freelancer 可用工作天数）\n"
            "• 📍 = 今天\n"
            "• 普通数字 = 无兼职安排（专注全职/休息）\n\n"
            "设置后点击底部【🔙 返回财务看板】，每日工时将立即自动重测！"
        )
        await safe_edit_message(query, text=text, reply_markup=cal_kb)

    elif data == "cal_ignore":
        # 点击了表头或空白，不响应
        pass

def parse_direct_command(text: str, chat_id: int) -> tuple[bool, str]:
    """
    解析直接发送的快捷文本指令（支持电脑休眠期间离线排队，开机自动执行，免去点按钮）：
    - 收入：+300, +300欧, 收入 500, 进账 2000元
    - 支出：-50, -50欧, 支出 100, 花了 200元
    - 总资金：资金 5000, 总资金 6500, 余额 50000元, 存款 3000
    - 时薪：时薪 20, 时薪 25刀, 时薪 $20
    - 固定支出：固定支出 房租 1000欧 每月1号 / 固定支出 学费 650欧 30号 到2027-02结束
    - 完成节点：完成 健身年卡, 搞定 兼职项目
    - 添加节点：租房 800欧 2026-11-01
    - 兼职排班：兼职 2026-11-25 / 开工 11-25 / 休息 11-25
    """
    rates = fx.get_exchange_rates()
    t = text.strip()

    # 1. 增加资金: +300, +300欧, 收入 500, 进账 2000元
    m_inc = re.match(r"^(\+|收入|进账|赚了)\s*(.*)$", t)
    if m_inc:
        val_str = m_inc.group(2) if m_inc.group(2) else m_inc.group(1).replace("+", "")
        if not val_str:
            return False, ""
        try:
            val_eur, _ = fx.parse_currency_input(val_str, rates)
            if val_eur > 0:
                profile = db.get_profile(chat_id)
                cur = profile["current_balance"] if profile else 0.0
                new_bal = cur + val_eur
                db.update_profile(chat_id, current_balance=new_bal)
                return True, f"🎉 **成功记录一笔收入**：`+{fx.format_dual_currency(val_eur, rates)}`！\n💰 最新当前资金：`{fx.format_dual_currency(new_bal, rates)}`"
        except Exception:
            pass

    # 2. 扣除支出: -50, -50欧, 支出 100, 花了 200元
    m_exp = re.match(r"^(-|支出|消费|花了)\s*(.*)$", t)
    if m_exp:
        val_str = m_exp.group(2)
        if not val_str:
            return False, ""
        try:
            val_eur, _ = fx.parse_currency_input(val_str, rates)
            if val_eur > 0:
                profile = db.get_profile(chat_id)
                cur = profile["current_balance"] if profile else 0.0
                new_bal = max(0.0, cur - val_eur)
                db.update_profile(chat_id, current_balance=new_bal)
                return True, f"💸 **成功记录一笔支出**：`-{fx.format_dual_currency(val_eur, rates)}`！\n💰 最新当前资金：`{fx.format_dual_currency(new_bal, rates)}`"
        except Exception:
            pass

    # 3. 设置总资金: 资金 5000, 总资金 6500, 余额 50000元, 存款 3000
    m_bal = re.match(r"^(资金|总资金|当前资金|余额|存款|现在有)\s*(.*)$", t)
    if m_bal:
        val_str = m_bal.group(2)
        try:
            val_eur, _ = fx.parse_currency_input(val_str, rates)
            if val_eur >= 0:
                db.update_profile(chat_id, current_balance=val_eur)
                return True, f"✅ **当前总资金已更新为**：`{fx.format_dual_currency(val_eur, rates)}`"
        except Exception:
            pass

    # 4. 设置时薪: 时薪 20, 时薪 25刀
    m_rate = re.match(r"^时薪\s*(.*)$", t)
    if m_rate:
        val_str = m_rate.group(1)
        try:
            val_eur, _ = fx.parse_currency_input(val_str, rates)
            if val_eur > 0:
                db.update_profile(chat_id, hourly_rate=val_eur)
                return True, f"✅ **当前时薪已更新为**：`{fx.format_hourly_rate(val_eur, rates)} /小时`"
        except Exception:
            pass

    # 5. 完成节点: 完成 健身年卡, 搞定 兼职项目
    m_done = re.match(r"^(完成|搞定|已还|结清)\s*(.+)$", t)
    if m_done:
        kw = m_done.group(2).strip()
        milestones = db.get_milestones(chat_id, status="pending")
        matched = [m for m in milestones if kw.lower() in m["title"].lower() or m["title"].lower() in kw.lower()]
        if not matched:
            return True, f"⚠️ 未找到包含【{kw}】的进行中节点，请核对名称或点击看板查看。"
        target = matched[0]
        title, deducted = db.complete_milestone(target["id"], chat_id)
        return True, f"🎉 恭喜！节点【**{title}**】已标记完成！\n💰 已按计划自动从总资金扣除 `{fx.format_dual_currency(deducted, rates)}`。"

    # 5b. 删除固定支出: 删除固定支出 房租
    m_del_fexp = re.match(r"^(?:删除|取消|移除)(?:固定支出|固定开销)\s+(.+)$", t)
    if m_del_fexp:
        kw = m_del_fexp.group(1).strip()
        exps = db.get_fixed_expenses(chat_id)
        matched = [e for e in exps if kw.lower() in e["title"].lower() or e["title"].lower() in kw.lower()]
        if not matched:
            return True, f"⚠️ 未找到包含【{kw}】的固定支出项。"
        target = matched[0]
        del_title = db.delete_fixed_expense(target["id"], chat_id)
        return True, f"🗑️ 已成功删除固定支出【**{del_title}**】！"

    # 5c. 添加固定支出: 固定支出 房租 1000欧 每月1号 / 固定支出 学费 650欧 30号 到2027-02结束
    if re.match(r"^(?:添加|新增|设置)?(?:固定支出|固定开销|每月支出)\s+", t):
        parsed_exp = parse_fixed_expense_input(t, rates)
        if parsed_exp:
            title, val_eur, pay_day, start_date, end_date = parsed_exp
            db.add_fixed_expense(chat_id, title, val_eur, pay_day, start_date, end_date)
            val_dual = fx.format_dual_currency(val_eur, rates)
            end_desc = f"至 `{end_date[:7]}` 结束" if end_date else "长期有效"
            return True, (
                f"✅ **成功添加固定每月支出**！【**{title}**】\n"
                f"💸 每月金额: `{val_dual}` (每月 `{pay_day}` 日扣减)\n"
                f"📅 期限设置: {end_desc}"
            )

    # 6. 新增带截止日期的节点: 租房 800欧 2026-11-01 或 租房 800欧 11-01
    parts = t.split()
    if len(parts) == 3:
        title, amt_str, date_str = parts[0], parts[1], parts[2]
        try:
            parsed_d = parse_date_safely(date_str)
            if parsed_d != date.max:
                clean_date = parsed_d.strftime("%Y-%m-%d")
                profile = db.get_profile(chat_id)
                rate = profile["hourly_rate"] if profile else 0.0
                
                if amt_str.lower().endswith("h") or amt_str.endswith("小时"):
                    clean_h = amt_str.lower().replace("h", "").replace("小时", "")
                    hours = float(clean_h)
                    money_eur = hours * rate
                else:
                    money_eur, _ = fx.parse_currency_input(amt_str, rates)
                    hours = (money_eur / rate) if rate > 0 else 0.0

                db.add_milestone(chat_id, title, clean_date, hours, money_eur)
                return True, f"✅ 成功添加阶段节点【**{title}**】！\n📅 截止: `{clean_date}` | 💵 目标: `{fx.format_dual_currency(money_eur, rates)}` (`{hours:.1f}h`)"
        except Exception:
            pass

    # 7. 兼职工作日排班或取消: 兼职 2026-11-25 / 开工 11-25 / 加班 11-25 / 休息 11-25 / 请假 11-25
    m_cal = re.match(r"^(兼职|开工|加班|请假|休息|取消兼职)\s+(.+)$", t)
    if m_cal:
        action_type, d_str = m_cal.group(1), m_cal.group(2).strip()
        parsed_cal = parse_date_safely(d_str)
        if parsed_cal != date.max:
            clean_cal = parsed_cal.strftime("%Y-%m-%d")
            if action_type in ["请假", "休息", "取消兼职"]:
                db.delete_work_day(chat_id, clean_cal)
                return True, f"🏖️ 已取消 `{clean_cal}` 的兼职工作安排！"
            else:
                db.add_work_day(chat_id, clean_cal, "文字记录")
                return True, f"💼 已将 `{clean_cal}` 设为 Freelancer 兼职工作日！"

    # 8. 修改节点金额: 改金额 学费 800欧 / 修改金额 还爸妈钱 1500元 / 学费 改为 800欧
    m_mod = re.match(r"^(?:改金额|修改金额|调整金额|改钱)\s+(.+)\s+(.+)$", t)
    if not m_mod:
        m_mod = re.match(r"^(.+)\s+(?:改为|改成|调整为|变更为|改金额为?)\s+(.+)$", t)
    if m_mod:
        kw = m_mod.group(1).strip()
        amt_str = m_mod.group(2).strip()
        milestones = db.get_milestones(chat_id, status="pending")
        matched = [m for m in milestones if kw.lower() in m["title"].lower() or m["title"].lower() in kw.lower()]
        if matched:
            target = matched[0]
            profile = db.get_profile(chat_id)
            rate = profile["hourly_rate"] if profile else 0.0
            try:
                if amt_str.lower().endswith("h") or amt_str.endswith("小时"):
                    clean_h = amt_str.lower().replace("h", "").replace("小时", "").strip()
                    hours = float(clean_h)
                    if hours < 0:
                        return True, "❌ 工时不能为负数。"
                    money_eur = hours * rate
                else:
                    money_eur, _ = fx.parse_currency_input(amt_str, rates)
                    if money_eur < 0:
                        return True, "❌ 金额不能为负数。"
                    hours = (money_eur / rate) if rate > 0 else 0.0

                title = db.update_milestone_money(target["id"], chat_id, money_eur, hours)
                money_dual = fx.format_dual_currency(money_eur, rates)
                return True, f"✅ 已成功将【**{title}**】目标金额更新为 `{money_dual}` (`{hours:.1f}h`)！"
            except Exception:
                return True, f"❌ 金额解析失败，请输入有效金额（例如 `改金额 {kw} 800欧`）。"

    # 9. 设置或关闭固定收入/固定工资
    # 支持: 从12月开始我的固定收入是2600欧 / 固定收入 2600欧 12月 / 固定工作 2600 / 关闭固定收入 / 清空固定工资
    if re.search(r"(固定收入|固定工资|固定工作|月薪)", t):
        parsed = parse_fixed_salary_input(t, rates)
        if parsed:
            val_eur, fixed_start, fixed_day, mode = parsed
            if mode == "closed" or val_eur <= 0:
                db.update_profile(chat_id, fixed_salary=0.0, fixed_salary_start="", fixed_salary_day=1)
                return True, "✅ **已关闭固定工作月薪**，所有节点将完全依赖 Freelancer 工时推算。"
            else:
                db.update_profile(chat_id, fixed_salary=val_eur, fixed_salary_start=fixed_start, fixed_salary_day=fixed_day)
                val_dual = fx.format_dual_currency(val_eur, rates)
                return True, (
                    f"🎉 **固定工作月薪设置成功！**\n\n"
                    f"💼 **每月税后到账**: `{val_dual}`\n"
                    f"📅 **首次生效**: `{fixed_start}` 起 (每月 `{fixed_day}` 日到账)\n\n"
                    "系统已重新测算：固定工资先冲抵未来阶段目标，剩余缺口由 Freelancer 工时补齐！"
                )

    return False, ""

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    state = user_states.get(chat_id)
    if not state:
        handled, reply_text = parse_direct_command(text, chat_id)
        if handled:
            # 判断是否是电脑休眠期间离线积压的消息（发送时间超过 60 秒前）
            is_delayed = False
            if update.message and update.message.date:
                diff_sec = (datetime.now(pytz.utc) - update.message.date).total_seconds()
                if diff_sec > 60:
                    is_delayed = True

            prefix = "📥 **【电脑已开机·自动同步离线记录】**\n\n" if is_delayed else ""
            receipt_text = f"{prefix}{reply_text}"
            receipt_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("👀 查看完整财务看板", callback_data="refresh"),
                    InlineKeyboardButton("💰 记下一笔", callback_data="add_balance"),
                ]
            ])
            await update.message.reply_text(receipt_text, reply_markup=receipt_kb, parse_mode="Markdown")
            return

        tip_text = (
            "💡 **快捷记账与记录提示**（手机直接发文字，即发即记）：\n\n"
            "• **记一笔收入**：直接发送 `+300欧`、`+2000元` 或 `收入 500`\n"
            "• **记一笔支出**：直接发送 `-50欧`、`-200元` 或 `支出 100`\n"
            "• **更新总资金**：发送 `资金 5000` 或 `总资金 50000元`\n"
            "• **更新Freelancer时薪**：发送 `时薪 20` 或 `时薪 25刀`\n"
            "• **设置固定工作月薪**：发送 `从12月开始我的固定收入是2600欧` 或 `固定月薪 2600`\n"
            "• **添加固定每月支出**：发送 `固定支出 房租 1000欧 每月1号` 或 `固定支出 学费 650欧 30号 到2027-02结束`\n"
            "• **修改节点金额**：发送 `改金额 学费 800欧` 或 `学费 改为 5000元`\n"
            "• **完成节点**：发送 `完成 健身卡` 或 `搞定 兼职项目`\n"
            "• **添加节点**：发送 `买电脑 1200欧 12-01` 或 `租房 800欧 2026-11-01`\n"
            "• **安排兼职日**：发送 `兼职 11-25` 或 `休息 11-25`\n\n"
            "📦 **云端数据与备份**：\n"
            "• `/seed` 或 `/export`：生成 Render 永久环境变量 `SEED_DATA`\n"
            "• `/backup`：一键导出本地数据库 `.db` 文件\n\n"
            "输入 /start 可直接调出最新财务看板。"
        )
        await update.message.reply_text(tip_text, reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")
        return

    action = state.get("action")
    rates = fx.get_exchange_rates()
    
    if action in ["add_balance", "set_balance", "set_target", "set_rate"]:
        try:
            val_eur, currency_used = fx.parse_currency_input(text, rates)
            if val_eur < 0:
                await update.message.reply_text("❌ 数值不能为负数，请重新输入：")
                return
            
            dual_str = fx.format_dual_currency(val_eur, rates)
            
            if action == "add_balance":
                profile = db.get_profile(chat_id)
                current = profile["current_balance"] if profile else 0.0
                new_balance = current + val_eur
                db.update_profile(chat_id, current_balance=new_balance)
                msg = f"🎉 **成功增加资金**：`+{dual_str}`！\n💰 **最新当前资金**：`{fx.format_dual_currency(new_balance, rates)}`"
                again_btn = InlineKeyboardButton("➕ 再记一笔新收入", callback_data="add_balance")
            elif action == "set_balance":
                db.update_profile(chat_id, current_balance=val_eur)
                msg = f"✅ **当前资金总额已重置为**：`{dual_str}`"
                again_btn = InlineKeyboardButton("💰 重新修改总资金", callback_data="set_balance")
            elif action == "set_target":
                db.update_profile(chat_id, target_amount=val_eur)
                msg = f"✅ **目标金额已更新为**：`{dual_str}`"
                again_btn = InlineKeyboardButton("🎯 重新修改目标金额", callback_data="set_target")
            elif action == "set_rate":
                db.update_profile(chat_id, hourly_rate=val_eur)
                hourly_str = fx.format_hourly_rate(val_eur, rates)
                msg = f"✅ **当前 Freelancer 时薪已更新为**：`{hourly_str}` /小时"
                again_btn = InlineKeyboardButton("⏱️ 重新修改时薪", callback_data="set_rate")
            
            user_states.pop(chat_id, None)
            sub_kb = InlineKeyboardMarkup([
                [again_btn],
                [InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")]
            ])
            await update.message.reply_text(msg, reply_markup=sub_kb, parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ 输入格式错误，请输入有效的金额（支持带元/欧/刀/USD）：")
            
    elif action == "set_fixed_salary_input":
        parsed = parse_fixed_salary_input(text, rates)
        if not parsed:
            await update.message.reply_text("❌ 未能识别有效金额或月份，请重新输入（例如 `2600欧 12月` 或 `从12月开始我的固定收入是2600欧`）：")
            return
        val_eur, fixed_start, fixed_day, mode = parsed
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💼 重新修改固定工作月薪", callback_data="set_fixed_salary")],
            [InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")]
        ])
        if mode == "closed" or val_eur <= 0:
            db.update_profile(chat_id, fixed_salary=0.0, fixed_salary_start="", fixed_salary_day=1)
            user_states.pop(chat_id, None)
            res_text = "✅ **已关闭固定工作月薪**，所有节点将完全依赖 Freelancer 工时推算。"
            await update.message.reply_text(res_text, reply_markup=sub_kb, parse_mode="Markdown")
            return

        db.update_profile(chat_id, fixed_salary=val_eur, fixed_salary_start=fixed_start, fixed_salary_day=fixed_day)
        user_states.pop(chat_id, None)

        val_dual = fx.format_dual_currency(val_eur, rates)
        res_text = (
            f"🎉 **固定工作月薪设置成功！**\n\n"
            f"💼 **每月税后到账**: `{val_dual}`\n"
            f"📅 **首次生效**: `{fixed_start}` 起 (每月 `{fixed_day}` 日到账)\n\n"
            f"系统已重新测算：固定工资先冲抵未来阶段目标，剩余缺口由 Freelancer 工时补齐！"
        )
        await update.message.reply_text(res_text, reply_markup=sub_kb, parse_mode="Markdown")

    elif action == "add_fixed_expense_input":
        parsed = parse_fixed_expense_input(text, rates)
        if not parsed:
            await update.message.reply_text(
                "❌ 未能识别有效名称和金额，请重新输入！\n"
                "例如：`房租 1000欧 每月1号` 或 `学费 650欧 每月30号 到2027-02结束`"
            )
            return
        title, val_eur, pay_day, start_date, end_date = parsed
        db.add_fixed_expense(chat_id, title, val_eur, pay_day, start_date, end_date)
        user_states.pop(chat_id, None)

        val_dual = fx.format_dual_currency(val_eur, rates)
        end_desc = f"至 `{end_date[:7]}` 结束（到期自动停扣）" if end_date else "长期有效"
        notice = (
            f"🎉 **固定每月支出添加成功！**\n"
            f"📌 项目: 【**{title}**】 | 💸 金额: `{val_dual}/月` (每月 `{pay_day}` 日) | 📅 期限: {end_desc}"
        )
        panel_text, panel_kb = build_fixed_expenses_panel(chat_id, notice=notice)
        await update.message.reply_text(panel_text, reply_markup=panel_kb, parse_mode="Markdown")
            
    elif action == "add_milestone_input":
        profile = db.get_profile(chat_id)
        rate = profile["hourly_rate"] if profile else 0.0
        parts = text.split()

        # 模式 1（推荐）：用户输入 2 项（名称 + 金额/工时），例如：进修课程 650欧 -> 弹出日历点选截止日
        if len(parts) == 2:
            title, amount_or_hours = parts[0], parts[1]
            try:
                if amount_or_hours.lower().endswith("h") or amount_or_hours.endswith("小时"):
                    clean_h = amount_or_hours.lower().replace("h", "").replace("小时", "")
                    hours = float(clean_h)
                    money_eur = hours * rate
                else:
                    money_eur, _ = fx.parse_currency_input(amount_or_hours, rates)
                    hours = money_eur / rate if rate > 0 else 0

                user_states[chat_id] = {
                    "action": "pick_milestone_date",
                    "title": title,
                    "hours": hours,
                    "money_eur": money_eur
                }
                today = get_today()
                money_dual = fx.format_dual_currency(money_eur, rates)
                cal_kb = build_datepicker_keyboard(today.year, today.month, "mspick")
                await update.message.reply_text(
                    f"📌 节点名称: **{title}**\n"
                    f"💵 目标金额: `{money_dual}` (折合 `{hours:.1f}h`)\n\n"
                    f"📅 **请在下方日历中直接点击选择该节点的截止日期**：",
                    reply_markup=cal_kb,
                    parse_mode="Markdown"
                )
                return
            except ValueError:
                await update.message.reply_text("❌ 金额或工时格式错误，例如请输入：`进修课程 650欧` 或 `数码设备 800元` 或 `兼职 20h`")
                return

        # 模式 2：用户输入 3 项（包含手动文字日期），例如：进修课程 2026-11-30 650欧
        elif len(parts) == 3:
            title = parts[0]
            if parse_date_safely(parts[1]) != date.max:
                date_str = parse_date_safely(parts[1]).strftime("%Y-%m-%d")
                amount_or_hours = parts[2]
            elif parse_date_safely(parts[2]) != date.max:
                date_str = parse_date_safely(parts[2]).strftime("%Y-%m-%d")
                amount_or_hours = parts[1]
            else:
                await update.message.reply_text("❌ 未能识别有效日期，只需输入 **名称** 和 **金额**（例如 `进修课程 650欧`），并在弹出的日历上点击选择日期！")
                return

            try:
                if amount_or_hours.lower().endswith("h") or amount_or_hours.endswith("小时"):
                    clean_h = amount_or_hours.lower().replace("h", "").replace("小时", "")
                    hours = float(clean_h)
                    money_eur = hours * rate
                else:
                    money_eur, _ = fx.parse_currency_input(amount_or_hours, rates)
                    hours = money_eur / rate if rate > 0 else 0

                db.add_milestone(chat_id, title, date_str, hours, money_eur)
                user_states.pop(chat_id, None)

                money_dual = fx.format_dual_currency(money_eur, rates)
                hourly_str = fx.format_hourly_rate(rate, rates)

                res_text = (
                    f"✅ **成功添加阶段节点【{title}】！**\n\n"
                    f"📅 截止时间: `{date_str}`\n"
                    f"💰 目标金额: `{money_dual}`\n"
                    f"⏱️ 换算所需工时: `{hours:.1f}` 小时 (按时薪 {hourly_str})\n\n"
                    "您可以继续添加下一个节点，或自行选择返回主看板："
                )
                sub_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("➕ 继续添加下一个节点", callback_data="add_milestone"),
                        InlineKeyboardButton("📋 进入节点管理列表", callback_data="list_milestones"),
                    ],
                    [InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")]
                ])
                await update.message.reply_text(res_text, reply_markup=sub_kb, parse_mode="Markdown")
            except ValueError:
                await update.message.reply_text("❌ 工时或金额解析错误，请重新输入：")
                return
        else:
            await update.message.reply_text(
                "❌ 格式不正确，只需输入 **名称** 和 **金额/工时**（空格分隔），例如：\n"
                "`进修课程 650欧` 或 `数码设备 800元` 或 `设计项目 20h`\n"
                "发送后会直接弹出日历供您点击选择截止日期！"
            )

    elif action == "edit_milestone_money_input":
        m_id = state.get("milestone_id")
        title = state.get("title", "该节点")
        page = state.get("page", 0)
        profile = db.get_profile(chat_id)
        rate = profile["hourly_rate"] if profile else 0.0

        try:
            clean_text = text.strip()
            if clean_text.lower().endswith("h") or clean_text.endswith("小时"):
                clean_h = clean_text.lower().replace("h", "").replace("小时", "").strip()
                hours = float(clean_h)
                if hours < 0:
                    raise ValueError("工时不能为负数")
                money_eur = hours * rate
            else:
                money_eur, _ = fx.parse_currency_input(clean_text, rates)
                if money_eur < 0:
                    raise ValueError("金额不能为负数")
                hours = (money_eur / rate) if rate > 0 else 0.0

            db.update_milestone_money(m_id, chat_id, money_eur, hours)
            user_states.pop(chat_id, None)

            money_dual = fx.format_dual_currency(money_eur, rates)
            notice = (
                f"✅ **成功修改节点【{title}】目标金额为 `{money_dual}` (`{hours:.1f}h`)！**"
            )
            panel_text, panel_kb = build_milestone_list_panel(chat_id, page=page, notice=notice)
            await update.message.reply_text(panel_text, reply_markup=panel_kb, parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ 输入格式错误，请输入有效的金额或工时（支持带元/欧/刀/h）：")

    elif action == "add_off_days_input":
        # 支持: 2026-10-14 生日 或 2026-10-15~2026-10-18 旅行
        parts = text.split(maxsplit=1)
        date_part = parts[0]
        note = parts[1] if len(parts) > 1 else ""

        try:
            added_dates = []
            if "~" in date_part:
                start_s, end_s = date_part.split("~")
                start_d = parse_date_safely(start_s.strip())
                end_d = parse_date_safely(end_s.strip())
                if start_d == date.max or end_d == date.max:
                    raise ValueError("Invalid date")
                if start_d > end_d:
                    start_d, end_d = end_d, start_d
                
                curr = start_d
                while curr <= end_d:
                    d_str = curr.strftime("%Y-%m-%d")
                    db.add_off_day(chat_id, d_str, note)
                    added_dates.append(d_str)
                    curr += timedelta(days=1)
            else:
                target_d = parse_date_safely(date_part.strip())
                if target_d == date.max:
                    raise ValueError("Invalid date")
                d_str = target_d.strftime("%Y-%m-%d")
                db.add_off_day(chat_id, d_str, note)
                added_dates.append(d_str)

            user_states.pop(chat_id, None)
            res_text = (
                f"🏖️ **成功添加不可工作日！**\n"
                f"已排除日期: `{', '.join(added_dates[:7])}`" + (f" 等共 {len(added_dates)} 天\n\n" if len(added_dates) > 7 else "\n\n")
            ) + format_overview(chat_id)
            await update.message.reply_text(res_text, reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text(
                "❌ 日期格式错误，支持 `MM-DD` 或 `YYYY-MM-DD`，例如：\n"
                "`10-14`、`2026-10-14` 或 `10-15~10-18 假期`"
            )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户直接发送的 .db 数据库文件，一键热恢复全部数据"""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id):
        await update.message.reply_text("⛔ 抱歉，仅机器人所有者可导入恢复数据库。")
        return
    doc = update.message.document
    if not doc:
        return
    f_name = doc.file_name or ""
    if f_name.endswith(".db") or f_name.endswith(".sqlite"):
        try:
            tg_file = await doc.get_file()
            db_path = db.DB_FILE
            await tg_file.download_to_drive(custom_path=db_path)
            db.init_db()
            res_text = (
                "🎉 **【数据库一键恢复成功！】**\n\n"
                "您的所有阶段目标、当前资金与时薪已全部无缝恢复！\n\n"
            ) + format_overview(chat_id)
            await update.message.reply_text(res_text, reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to restore db from document: {e}")
            await update.message.reply_text(f"❌ 数据库文件恢复失败: {e}")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - BudgetCheck Bot is alive\n")

    def log_message(self, format, *args):
        pass

def run_health_server(port: int):
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        logger.warning(f"Health server failed on port {port}: {e}")

def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("错误: 请先在 .env 文件中设置 BOT_TOKEN！")
        return

    # 若检测到云平台端口环境变量 PORT，则启动健康检查探针服务
    port_str = os.getenv("PORT")
    if port_str:
        try:
            port = int(port_str)
            t = threading.Thread(target=run_health_server, args=(port,), daemon=True)
            t.start()
            logger.info(f"Cloud health server started on port {port}")
        except Exception as e:
            logger.warning(f"Failed to start health server on port {port_str}: {e}")

    # 初始化本地数据库
    db.init_db()

    # 构建 Telegram Bot 应用
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 注册命令与消息回调
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler(["seed", "export"], seed_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling an update:", exc_info=context.error)

    app.add_error_handler(global_error_handler)

    # 设置每日 09:00 定时提醒
    job_queue = app.job_queue
    if job_queue:
        tz = pytz.timezone(TIMEZONE)
        reminder_time = time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=tz)
        job_queue.run_daily(daily_reminder_job, time=reminder_time)
        print(f"⏰ 已设置每日提醒: 每天 {REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} ({TIMEZONE})")

    print("🚀 Telegram 财务目标机器人已启动！按 Ctrl+C 停止。")
    app.run_polling()

if __name__ == "__main__":
    main()
