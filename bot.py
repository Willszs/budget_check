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

def get_today() -> date:
    """获取当前配置时区（例如 Europe/Berlin 柏林时间）的实际日期"""
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).date()

def calculate_workdays(start_date: date, end_date: date, off_dates: set = None, include_weekends: bool = False, extra_work_dates: set = None) -> int:
    """计算从 start_date 到 end_date 之间的可用工作天数（可选是否包含全周末，扣除自定义请假休息日，增加周末特别开工日）"""
    if start_date > end_date:
        return 0
    if off_dates is None:
        off_dates = set()
    if extra_work_dates is None:
        extra_work_dates = set()
    workdays = 0
    curr = start_date
    while curr <= end_date:
        curr_str = curr.strftime("%Y-%m-%d")
        if include_weekends:
            is_working_day = (curr_str not in off_dates)
        else:
            if curr.weekday() < 5:
                is_working_day = (curr_str not in off_dates)
            else:
                is_working_day = (curr_str in extra_work_dates)
        if is_working_day:
            workdays += 1
        curr += timedelta(days=1)
    return workdays

def parse_date_safely(d_str: str) -> date:
    """安全解析各种日期格式并返回 date 对象"""
    try:
        parts = d_str.strip().replace('/', '-').split('-')
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return date.max

def check_overload_alert(chat_id: int) -> str | None:
    """
    超载防护检测（扣除当前总资金）：
    确保到每一个节点截止日，工作日所需日均工时都不超过 8 小时红线。
    若超过 8 小时，触发弹窗警报并推荐周末分摊工时。
    """
    profile = db.get_profile(chat_id)
    if not profile or profile["hourly_rate"] <= 0:
        return None
    balance = profile["current_balance"]
    rate = profile["hourly_rate"]
    milestones = db.get_milestones(chat_id, status="pending")
    if not milestones:
        return None
    
    milestones = sorted(milestones, key=lambda x: parse_date_safely(x['target_date']))
    user_off_days = db.get_off_days(chat_id)
    off_date_set = {od["off_date"] for od in user_off_days}
    user_work_days = db.get_work_days(chat_id)
    extra_work_set = {wd["work_date"] for wd in user_work_days}
    today = get_today()

    cumulative_money = 0.0
    max_daily_h = 0.0
    overload_title = None
    weekend_relief_hours = 0.0

    for m in milestones:
        cumulative_money += m['target_money']
        gap_money = max(0.0, cumulative_money - balance)
        gap_hours = gap_money / rate
        try:
            target_dt = datetime.strptime(m['target_date'], "%Y-%m-%d").date()
            if target_dt >= today:
                workdays = calculate_workdays(today, target_dt, off_date_set, include_weekends=False, extra_work_dates=extra_work_set)
                if workdays > 0:
                    daily_h = gap_hours / workdays
                    if daily_h > 8.0 and daily_h > max_daily_h:
                        max_daily_h = daily_h
                        overload_title = m['title']
                        total_days_all = calculate_workdays(today, target_dt, off_date_set, include_weekends=True, extra_work_dates=extra_work_set)
                        weekend_relief_hours = (gap_hours / total_days_all) if total_days_all > 0 else daily_h
        except Exception:
            pass

    if overload_title and max_daily_h > 8.0:
        return (
            f"🚨 超载预警：【{overload_title}】截止日前工作日需每天 {max_daily_h:.1f}h(已超8小时上限)！\n"
            f"💡 建议在日历中点击周末开启加班分摊，可降至 {weekend_relief_hours:.1f}h/天。"
        )

    return None


def build_calendar_keyboard(chat_id: int, year: int, month: int):
    """生成带有状态切换的 Telegram 交互式日历组件（支持请假与周末开工）"""
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

    # 获取当前用户的休息日与周末开工日
    off_days = db.get_off_days(chat_id)
    off_date_set = {od["off_date"] for od in off_days}
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
                is_off = d_str in off_date_set
                is_extra_work = d_str in work_date_set
                is_weekend = calendar.weekday(year, month, day_num) >= 5

                # 显示图标：
                # 🏖️ 请假休息日
                # 💼 周末加班开工日
                # 💤 默认周末休息
                # 📍 今天
                # 正常数字
                if is_off:
                    btn_text = f"🏖️{day_num}"
                elif is_extra_work:
                    btn_text = f"💼{day_num}"
                elif is_weekend:
                    btn_text = f"💤{day_num}"
                elif d_str == today_str:
                    btn_text = f"📍{day_num}"
                else:
                    btn_text = f"{day_num}"

                row.append(InlineKeyboardButton(btn_text, callback_data=f"cal_toggle_{d_str}_{year}_{month}"))
        keyboard.append(row)

    # 底部图例与返回按钮
    keyboard.append([
        InlineKeyboardButton("🏖️请假 | 💼周末开工 | 💤周末休息 (点击切换)", callback_data="cal_ignore")
    ])
    keyboard.append([
        InlineKeyboardButton("🔙 返回财务看板", callback_data="refresh")
    ])

    return InlineKeyboardMarkup(keyboard)

def build_datepicker_keyboard(year: int, month: int, prefix: str = "mspick", extra_id: int = 0):
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
        InlineKeyboardButton("🔙 取消并返回看板", callback_data="refresh")
    ])
    return InlineKeyboardMarkup(keyboard)

def format_overview(chat_id: int) -> str:
    profile = db.get_profile(chat_id)
    if not profile:
        return "👋 欢迎使用财务与目标追踪机器人！\n目前尚未配置任何数据，请点击下方按钮初始化数据。"
    
    rates = fx.get_exchange_rates()
    cny_rate = rates["CNY"]
    usd_rate = rates["USD"]
    
    balance = profile["current_balance"]
    rate = profile["hourly_rate"]

    text = (
        "📊 **【财务与目标多币种看板】**\n"
        f"💱 **实时汇率**: `1 EUR ≈ {cny_rate:.4f} CNY` | `1 EUR ≈ {usd_rate:.4f} USD`\n"
        "─────────────────\n"
        f"💰 **当前资金**: `{fx.format_dual_currency(balance, rates)}`\n"
        f"⏱️ **当前时薪**: `{fx.format_hourly_rate(rate, rates)} /小时`\n"
    )
    
    # 获取阶段节点
    milestones = db.get_milestones(chat_id, status="pending")
    text += "\n📅 **【进行中的阶段节点】**:\n"
    if not milestones:
        text += "暂无阶段节点，点击【➕ 添加阶段节点】新建。\n"
    else:
        # 按截止日期由近及远排序（使用 parse_date_safely 防止字符串排序问题）
        milestones = sorted(milestones, key=lambda x: parse_date_safely(x['target_date']))

        # 阶段节点列表展示 (若超过 8 项展示前 8 项，避免消息超长)
        show_ms = milestones[:8] if len(milestones) > 8 else milestones
        for idx, m in enumerate(show_ms, 1):
            money_dual = fx.format_dual_currency(m['target_money'], rates)
            text += f"{idx}. 📌 **{m['title']}** (`{m['target_date']}`): `{money_dual}` (`{m['target_hours']:.1f}h`)\n"
        if len(milestones) > 8:
            text += f"   *(...其余 {len(milestones) - 8} 项远期节点，可在【📋 管理/完成节点】中查看完整列表)*\n"

        # 智能瓶颈推算：资金优先冲销最近节点
        user_off_days = db.get_off_days(chat_id)
        off_date_set = {od["off_date"] for od in user_off_days}
        user_work_days = db.get_work_days(chat_id)
        extra_work_set = {wd["work_date"] for wd in user_work_days}
        today = get_today()

        # 核心算法：滚动累计强度（Rolling Cumulative Workload）分析
        cumulative_target_money = 0.0
        analyzed_stages = []
        bottleneck_stage = None
        max_daily_req = 0.0

        for m in milestones:
            m_money = m['target_money']
            m_date_str = m['target_date']
            cumulative_target_money += m_money
            cum_gap_money = max(0.0, cumulative_target_money - balance)
            cum_gap_hours = (cum_gap_money / rate) if rate > 0 else 0.0

            workdays = 0
            is_overdue = False
            try:
                target_dt = datetime.strptime(m_date_str, "%Y-%m-%d").date()
                if target_dt < today:
                    is_overdue = True
                else:
                    workdays = calculate_workdays(today, target_dt, off_date_set, extra_work_dates=extra_work_set)
            except ValueError:
                workdays = 0

            daily_req = (cum_gap_hours / workdays) if workdays > 0 else 0.0
            stage_info = {
                "title": m['title'],
                "date": m_date_str,
                "target_money": m_money,
                "cum_gap_money": cum_gap_money,
                "cum_gap_hours": cum_gap_hours,
                "workdays": workdays,
                "daily_req": daily_req,
                "is_overdue": is_overdue
            }
            analyzed_stages.append(stage_info)
            if not is_overdue and daily_req > max_daily_req:
                max_daily_req = daily_req
                bottleneck_stage = stage_info

        if not bottleneck_stage and analyzed_stages:
            bottleneck_stage = analyzed_stages[0]

        text += (
            "─────────────────\n"
            "🛡️ **【核心宗旨：节点100%全覆盖 · 严格控制工作区间 5.0h ~ 8.0h】**:\n"
        )

        MIN_DAILY_FLOOR = 5.0

        if max_daily_req <= 0:
            text += f"🎉 **太棒了！现有资金已完全覆盖所有节点目标！建议非紧急期保持每天工作 {MIN_DAILY_FLOOR:.1f} 小时下限积累资金。**\n"
        else:
            recommended_hours = max_daily_req
            if recommended_hours > 8.0:
                try:
                    target_dt = datetime.strptime(bottleneck_stage['date'], "%Y-%m-%d").date()
                    total_days_with_weekends = calculate_workdays(today, target_dt, off_date_set, include_weekends=True, extra_work_dates=extra_work_set)
                    weekend_relief_hours = (bottleneck_stage['cum_gap_hours'] / total_days_with_weekends) if total_days_with_weekends > 0 else recommended_hours
                except Exception:
                    weekend_relief_hours = recommended_hours

                warning_box = (
                    f"🚨 **【严重超载预警：工作日工作已超每天 8 小时上限！】**\n"
                    f"若仅在周一至周五工作，到【{bottleneck_stage['title']}】每天需干 **`{recommended_hours:.1f}` 小时**（已破8小时上限）！\n\n"
                    f"💡 **【执行方案：启动周末分摊机制】**:\n"
                    f"为坚守每天不超过 8 小时，建议点击下方【🏖️ 请假与周末开工日历】将周末设为加班日（若全开共 `{total_days_with_weekends}` 天），每天工时立刻降至: **`{weekend_relief_hours:.1f}` 小时/天**！\n"
                )
            elif recommended_hours >= MIN_DAILY_FLOOR:
                intensity_name = "中高强度攻坚期" if recommended_hours > 6.0 else "平稳健康期"
                warning_box = f"⚡ **【{intensity_name}】** 每日需工作 `{recommended_hours:.1f}` 小时（位于 5.0h 下限 ~ 8.0h 上限内），请保持专注！\n"
            else:
                warning_box = f"🟢 **【非紧急储备期】** 节点实际仅需 `{recommended_hours:.1f}h/天`，**建议保持下限 `{MIN_DAILY_FLOOR:.1f} 小时/天`**，提前储备资金！\n"

            effective_display_hours = max(MIN_DAILY_FLOOR, recommended_hours)
            text += (
                f"🎯 **当前决定性节点:【{bottleneck_stage['title']}】**\n"
                f"   └ 截止日期: `{bottleneck_stage['date']}` (扣除周末与请假剩 `{bottleneck_stage['workdays']}` 工作日)\n"
                f"   └ 扣减总资金后净缺口: `{fx.format_dual_currency(bottleneck_stage['cum_gap_money'], rates)}` (`{bottleneck_stage['cum_gap_hours']:.1f}h`)\n"
                f"   👉 **当前建议工时: 每天工作 `{effective_display_hours:.1f}` 小时** (工作区间 5.0h~8.0h)\n"
                f"   💡 *(说明: 此工时仅为今天至 `{bottleneck_stage['date']}` 攻坚期要求，攻克后日常工时降至 5.0h 下限)*\n\n"
                f"{warning_box}\n"
            )

        # 阶段区间衔接与真实工时防护 (全量列出所有区间)
        date_groups = {}
        for m in milestones:
            d_obj = parse_date_safely(m['target_date'])
            d_str = d_obj.strftime("%Y-%m-%d")
            if d_str not in date_groups:
                date_groups[d_str] = {"date": d_str, "dt": d_obj, "titles": [], "total_money": 0.0}
            date_groups[d_str]["titles"].append(m['title'])
            date_groups[d_str]["total_money"] += m['target_money']

        sorted_dates = sorted(date_groups.keys(), key=lambda d: parse_date_safely(d))
        bottleneck_dt_str = bottleneck_stage['date'] if bottleneck_stage else ""
        b_dt = parse_date_safely(bottleneck_dt_str)

        dates_after = [d for d in sorted_dates if parse_date_safely(d) > b_dt]
        post_bottleneck_pace = MIN_DAILY_FLOOR
        if dates_after:
            start_post = b_dt + timedelta(days=1)
            w_to_b = calculate_workdays(today, b_dt, off_date_set, extra_work_dates=extra_work_set)
            cash_at_b = max(0.0, balance + (w_to_b * max_daily_req * rate) - sum(date_groups[d]['total_money'] for d in sorted_dates if parse_date_safely(d) <= b_dt))
            cum_post_m = 0.0
            raw_post_pace = 0.0
            for d in dates_after:
                grp = date_groups[d]
                cum_post_m += grp['total_money']
                gap_post = max(0.0, cum_post_m - cash_at_b)
                gap_post_h = gap_post / rate
                w_post = calculate_workdays(start_post, grp['dt'], off_date_set, extra_work_dates=extra_work_set)
                r_post = gap_post_h / w_post if w_post > 0 else 0
                if r_post > raw_post_pace:
                    raw_post_pace = r_post
            post_bottleneck_pace = max(MIN_DAILY_FLOOR, raw_post_pace)

        interval_lines = []
        cur_cash = balance
        prev_d = today

        for d_str in sorted_dates:
            try:
                grp = date_groups[d_str]
                d_dt = grp["dt"]
                if d_dt < today:
                    continue
                start_calc = (prev_d + timedelta(days=1)) if prev_d != today else prev_d
                int_workdays = calculate_workdays(start_calc, d_dt, off_date_set, include_weekends=False, extra_work_dates=extra_work_set)
                
                grp_names = " + ".join(grp["titles"])
                prev_name = "今天" if prev_d == today else prev_d.strftime("%m-%d")
                curr_name = d_dt.strftime("%m-%d")

                if d_dt <= b_dt:
                    pace_hours = max_daily_req if max_daily_req > 0 else MIN_DAILY_FLOOR
                    earned_in_interval = int_workdays * pace_hours * rate
                    avail_money = cur_cash + earned_in_interval
                    due_money = grp["total_money"]
                    surplus = avail_money - due_money
                    cur_cash = max(0.0, surplus)

                    if pace_hours > 8.0:
                        interval_lines.append(f"• 🚨 `[{prev_name} ➔ {curr_name}]` ({int_workdays}工作日 | {grp_names}): 需 `{pace_hours:.1f}h/天`(超8h)！")
                    else:
                        surplus_str = f"，达成后结余 `{fx.format_dual_currency(surplus, rates)}`" if surplus > 0.01 else "，全额达成"
                        interval_lines.append(f"• 🔥攻坚期 `[{prev_name} ➔ {curr_name}]` ({int_workdays}工作日 | {grp_names}): 需 `{pace_hours:.1f}h/天`{surplus_str}")
                else:
                    pace_hours = post_bottleneck_pace if post_bottleneck_pace > 0 else MIN_DAILY_FLOOR
                    earned_in_interval = int_workdays * pace_hours * rate
                    avail_money = cur_cash + earned_in_interval
                    due_money = grp["total_money"]
                    surplus = avail_money - due_money
                    cur_cash = max(0.0, surplus)

                    if pace_hours > 8.0:
                        interval_lines.append(f"• 🚨 `[{prev_name} ➔ {curr_name}]` ({int_workdays}工作日 | {grp_names}): 日需 `{pace_hours:.1f}h/天`(超8h)！")
                    else:
                        surplus_str = f"，达成后结余 `{fx.format_dual_currency(surplus, rates)}`" if surplus > 0.01 else "，全额达成"
                        interval_lines.append(f"• 🟢平稳期 `[{prev_name} ➔ {curr_name}]` ({int_workdays}工作日 | {grp_names}): `{pace_hours:.1f}h/天`{surplus_str}")
                prev_d = d_dt
            except Exception:
                pass

        if interval_lines:
            text += "🛡️ **【阶段区间衔接与真实工时防护】**:\n"
            text += "\n".join(interval_lines) + "\n"

        # 4. 统计日历调度
        upcoming_offs = [od["off_date"] for od in user_off_days if od["off_date"] >= today.strftime("%Y-%m-%d")]
        upcoming_works = [wd["work_date"] for wd in user_work_days if wd["work_date"] >= today.strftime("%Y-%m-%d")]
        cal_summary = []
        if upcoming_offs:
            cal_summary.append(f"🏖️ 排除 `{len(upcoming_offs)}` 天不可工作日")
        if upcoming_works:
            cal_summary.append(f"💼 开启 `{len(upcoming_works)}` 天周末开工加班日")
        if cal_summary:
            text += f"\n📅 **日历调度**: {' | '.join(cal_summary)}\n"

    text += "─────────────────\n请选择你要进行的操作："
    return text

def get_main_keyboard(chat_id: int):
    keyboard = [
        [
            InlineKeyboardButton("💰 更新今天的总资金", callback_data="set_balance"),
            InlineKeyboardButton("➕ 记录一笔新收入", callback_data="add_balance"),
        ],
        [
            InlineKeyboardButton("⏱️ 修改当前时薪", callback_data="set_rate"),
            InlineKeyboardButton("➕ 添加阶段节点", callback_data="add_milestone"),
        ],
        [
            InlineKeyboardButton("📋 管理/完成节点", callback_data="list_milestones"),
            InlineKeyboardButton("🏖️ 请假与周末开工日历", callback_data="manage_off_days"),
        ],
        [
            InlineKeyboardButton("🔄 刷新看板与汇率", callback_data="refresh"),
        ]
    ]
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
        return
    db_path = db.DB_FILE
    if os.path.exists(db_path):
        with open(db_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="budget_data.db",
                caption="📦 **【数据库备份文件】**\n\n云端上线后，直接在 Telegram 中将此文件发送给机器人，即可一键恢复所有预算与阶段目标！"
            )
    else:
        await update.message.reply_text("⚠️ 数据库文件暂不存在。")

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

    # 预先检查弹窗预警
    alert_msg = check_overload_alert(chat_id)

    if data == "refresh":
        user_states.pop(chat_id, None)
        if alert_msg:
            try:
                await query.answer(text=alert_msg, show_alert=True)
            except Exception:
                pass
        else:
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
            "⏱️ 请直接回复消息输入 **你的时薪**：\n\n"
            "💡 支持输入美金、人民币或欧元，例如：\n"
            "- 输入美金：`25美元`、`25usd`、`$25`、`25刀`\n"
            "- 输入人民币：`150元` 或 `150rmb`\n"
            "- 输入欧元：`20` 或 `€20`",
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
    
    elif data == "list_milestones":
        milestones = db.get_milestones(chat_id, status="pending")
        if not milestones:
            await safe_edit_message(
                query,
                "当前没有进行中的节点！",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回看板", callback_data="refresh")]])
            )
            return

        keyboard = []
        for m in milestones:
            keyboard.append([
                InlineKeyboardButton(f"✅ 完成: {m['title']}", callback_data=f"done_m_{m['id']}"),
                InlineKeyboardButton("📅 改日期", callback_data=f"editdate_m_{m['id']}"),
                InlineKeyboardButton("🗑️ 删除", callback_data=f"del_m_{m['id']}")
            ])
        keyboard.append([InlineKeyboardButton("🔙 返回看板", callback_data="refresh")])
        await safe_edit_message(
            query,
            "📋 **阶段节点管理**：\n点击即可完成、修改截止日历或删除对应节点：",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data.startswith("editdate_m_"):
        m_id = int(data.replace("editdate_m_", ""))
        milestones = db.get_milestones(chat_id, status="pending")
        target_m = next((m for m in milestones if m["id"] == m_id), None)
        title = target_m["title"] if target_m else "该节点"
        curr_d = parse_date_safely(target_m["target_date"]) if target_m else get_today()
        cal_kb = build_datepicker_keyboard(curr_d.year, curr_d.month, "msedit", extra_id=m_id)
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
        cal_kb = build_datepicker_keyboard(y, m, "msedit", extra_id=m_id)
        await query.edit_message_reply_markup(reply_markup=cal_kb)

    elif data.startswith("msedit_date_"):
        parts = data.split("_")
        new_d_str = parts[2]
        m_id = int(parts[3])
        title = db.update_milestone_date(m_id, chat_id, new_d_str)
        alert_msg = check_overload_alert(chat_id)
        if alert_msg:
            await query.answer(alert_msg, show_alert=True)
        else:
            await query.answer(f"✅ 已将【{title}】截止日期更新为 {new_d_str}！", show_alert=False)
        text = f"✅ 已成功将【**{title}**】截止日期更新为 `{new_d_str}`！\n\n" + format_overview(chat_id)
        await safe_edit_message(query, text=text, reply_markup=get_main_keyboard(chat_id))

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
            await safe_edit_message(query, "⚠️ 流程已过期，请返回主看板重新添加。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回看板", callback_data="refresh")]]))
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

        alert_msg = check_overload_alert(chat_id)
        if alert_msg:
            await query.answer(alert_msg, show_alert=True)
        else:
            await query.answer(f"✅ 成功添加【{title}】（截止: {chosen_date_str}）！", show_alert=False)

        res_text = (
            f"✅ 成功添加阶段节点 **{title}**！\n"
            f"📅 截止时间: `{chosen_date_str}`\n"
            f"💰 目标金额: `{money_dual}`\n"
            f"⏱️ 换算所需工时: `{hours:.1f}` 小时 (按时薪 {hourly_str})\n\n"
        ) + format_overview(chat_id)
        await safe_edit_message(query, res_text, reply_markup=get_main_keyboard(chat_id))
    
    elif data.startswith("done_m_"):
        m_id = int(data.replace("done_m_", ""))
        title, deducted_money = db.complete_milestone(m_id, chat_id)
        rates = fx.get_exchange_rates()
        deducted_str = fx.format_dual_currency(deducted_money, rates)
        text = (
            f"🎉 **阶段节点【{title}】已完成！**\n"
            f"💰 已按计划从当前总资金中扣除: `-{deducted_str}`\n\n"
        ) + format_overview(chat_id)
        await safe_edit_message(query, text=text, reply_markup=get_main_keyboard(chat_id))
        
    elif data.startswith("del_m_"):
        m_id = int(data.replace("del_m_", ""))
        db.delete_milestone(m_id, chat_id)
        text = "🗑️ 已删除该阶段节点。\n\n" + format_overview(chat_id)
        await safe_edit_message(query, text=text, reply_markup=get_main_keyboard(chat_id))

    elif data == "manage_off_days":
        today = get_today()
        cal_kb = build_calendar_keyboard(chat_id, today.year, today.month)
        text = (
            "📅 **【工作日历与工时调度】**\n\n"
            "直接点击日历中的日期即可切换工作/休息状态：\n"
            "• **周一至周五**：点击设为 🏖️ 请假休息（扣除可用天数）\n"
            "• **周六周日**：点击设为 💼 周末加班开工（**增加可用工时，减轻每日压力**）\n\n"
            "图标说明：\n"
            "• 🏖️ = 请假/休息日（已排除）\n"
            "• 💼 = 周末加班开工日（已加入可用工作日）\n"
            "• 💤 = 常规周末休息（默认排除）\n"
            "• 📍 = 今天\n\n"
            "设置后点击底部【🔙 返回财务看板】，每日工时将立即自动重测！"
        )
        await safe_edit_message(query, text=text, reply_markup=cal_kb)

    elif data.startswith("cal_nav_"):
        parts = data.split("_")
        nav_year, nav_month = int(parts[2]), int(parts[3])
        cal_kb = build_calendar_keyboard(chat_id, nav_year, nav_month)
        text = (
            "📅 **【工作日历与工时调度】**\n\n"
            "直接点击日历中的日期即可切换工作/休息状态：\n"
            "• **周一至周五**：点击设为 🏖️ 请假休息（扣除可用天数）\n"
            "• **周六周日**：点击设为 💼 周末加班开工（**增加可用工时，减轻每日压力**）\n\n"
            "图标说明：\n"
            "• 🏖️ = 请假/休息日（已排除）\n"
            "• 💼 = 周末加班开工日（已加入可用工作日）\n"
            "• 💤 = 常规周末休息（默认排除）\n"
            "• 📍 = 今天\n\n"
            "设置后点击底部【🔙 返回财务看板】，每日工时将立即自动重测！"
        )
        await safe_edit_message(query, text=text, reply_markup=cal_kb)

    elif data.startswith("cal_toggle_"):
        parts = data.split("_")
        toggle_date_str = parts[2]
        cur_year, cur_month = int(parts[3]), int(parts[4])
        toggle_dt = parse_date_safely(toggle_date_str)
        is_weekend = (toggle_dt.weekday() >= 5)

        if is_weekend:
            # 周末：点击切换 默认休息(💤) <-> 特别开工工作日(💼)
            work_days = db.get_work_days(chat_id)
            existing_work = next((wd for wd in work_days if wd["work_date"] == toggle_date_str), None)
            if existing_work:
                db.delete_work_day(chat_id, toggle_date_str)
                action_tip = f"已将 {toggle_date_str} (周末) 恢复为常规休息 💤"
            else:
                db.add_work_day(chat_id, toggle_date_str, "周末加班开工")
                action_tip = f"✅ 已将 {toggle_date_str} (周末) 设为加班工作日 💼！可用天数+1"
        else:
            # 周一至周五工作日：点击切换 正常工作日 <-> 请假休息(🏖️)
            off_days = db.get_off_days(chat_id)
            existing_off = next((od for od in off_days if od["off_date"] == toggle_date_str), None)
            if existing_off:
                db.delete_off_day(existing_off["id"], chat_id)
                action_tip = f"✅ 已取消 {toggle_date_str} 的请假，恢复为工作日"
            else:
                db.add_off_day(chat_id, toggle_date_str, "日历点选")
                action_tip = f"已将 {toggle_date_str} 设为请假休息 🏖️"

        # 检查点选后是否导致严重超载
        post_alert = check_overload_alert(chat_id)
        if post_alert:
            await query.answer(text=post_alert, show_alert=True)
        else:
            await query.answer(action_tip, show_alert=False)

        # 刷新当前月份日历
        cal_kb = build_calendar_keyboard(chat_id, cur_year, cur_month)
        text = (
            "📅 **【工作日历与工时调度】**\n\n"
            "直接点击日历中的日期即可切换工作/休息状态：\n"
            "• **周一至周五**：点击设为 🏖️ 请假休息（扣除可用天数）\n"
            "• **周六周日**：点击设为 💼 周末加班开工（**增加可用工时，减轻每日压力**）\n\n"
            "图标说明：\n"
            "• 🏖️ = 请假/休息日（已排除）\n"
            "• 💼 = 周末加班开工日（已加入可用工作日）\n"
            "• 💤 = 常规周末休息（默认排除）\n"
            "• 📍 = 今天\n\n"
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
    - 完成节点：完成 健身年卡, 搞定 兼职项目
    - 添加节点：租房 800欧 2026-11-01
    - 请假/加班：请假 2026-10-25 / 加班 2026-10-18
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

    # 6. 新增带截止日期的节点: 租房 800欧 2026-11-01
    parts = t.split()
    if len(parts) == 3:
        title, amt_str, date_str = parts[0], parts[1], parts[2]
        try:
            clean_date = date_str.strip().replace('/', '-')
            datetime.strptime(clean_date, "%Y-%m-%d")
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

    # 7. 请假或周末开工: 请假 2026-10-25 / 加班 2026-10-18
    m_cal = re.match(r"^(请假|休息|加班|开工)\s+(.+)$", t)
    if m_cal:
        action_type, d_str = m_cal.group(1), m_cal.group(2).strip().replace('/', '-')
        try:
            datetime.strptime(d_str, "%Y-%m-%d").date()
            if action_type in ["请假", "休息"]:
                db.add_off_day(chat_id, d_str, "文字记录")
                return True, f"🏖️ 已将 `{d_str}` 设为请假休息日！"
            else:
                db.add_work_day(chat_id, d_str, "文字记录")
                return True, f"💼 已将 `{d_str}` 设为周末加班开工日！"
        except ValueError:
            pass

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
            new_text = f"{prefix}{reply_text}\n\n" + format_overview(chat_id)
            await update.message.reply_text(new_text, reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")
            return

        tip_text = (
            "💡 **快捷记账与记录提示**（手机上直接发文字，电脑休眠也能排队记录）：\n\n"
            "• **记一笔收入**：直接发送 `+300欧`、`+2000元` 或 `收入 500`\n"
            "• **记一笔支出**：直接发送 `-50欧`、`-200元` 或 `支出 100`\n"
            "• **更新总资金**：发送 `资金 5000` 或 `总资金 50000元`\n"
            "• **更新时薪**：发送 `时薪 20` 或 `时薪 25刀`\n"
            "• **完成节点**：发送 `完成 健身卡` 或 `搞定 兼职项目`\n"
            "• **添加节点**：发送 `买电脑 1200欧 2026-12-01`\n"
            "• **请假/加班**：发送 `请假 2026-10-25` 或 `加班 2026-10-18`\n\n"
            "📱 **电脑未开机时**：您直接在手机发以上文字，电脑开机后会自动批量同步、入库并重新测算！\n"
            "输入 /start 可直接查看最新财务看板。"
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
                msg = f"🎉 成功增加资金：`+{dual_str}`！\n💰 最新当前资金：`{fx.format_dual_currency(new_balance, rates)}`"
            elif action == "set_balance":
                db.update_profile(chat_id, current_balance=val_eur)
                msg = f"✅ 当前资金总额已重置为：`{dual_str}`"
            elif action == "set_target":
                db.update_profile(chat_id, target_amount=val_eur)
                msg = f"✅ 目标金额已更新为：`{dual_str}`"
            elif action == "set_rate":
                db.update_profile(chat_id, hourly_rate=val_eur)
                hourly_str = fx.format_hourly_rate(val_eur, rates)
                msg = f"✅ 当前时薪已更新为：`{hourly_str}` /小时"
            
            user_states.pop(chat_id, None)
            new_text = f"{msg}\n\n" + format_overview(chat_id)
            await update.message.reply_text(new_text, reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ 输入格式错误，请输入有效的金额（支持带元/欧/刀/USD）：")
            
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
                    f"✅ 成功添加阶段节点 **{title}**！\n"
                    f"📅 截止时间: `{date_str}`\n"
                    f"💰 目标金额: `{money_dual}`\n"
                    f"⏱️ 换算所需工时: `{hours:.1f}` 小时 (按时薪 {hourly_str})\n\n"
                ) + format_overview(chat_id)
                await update.message.reply_text(res_text, reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")
            except ValueError:
                await update.message.reply_text("❌ 工时或金额解析错误，请重新输入：")
                return
        else:
            await update.message.reply_text(
                "❌ 格式不正确，只需输入 **名称** 和 **金额/工时**（空格分隔），例如：\n"
                "`进修课程 650欧` 或 `数码设备 800元` 或 `设计项目 20h`\n"
                "发送后会直接弹出日历供您点击选择截止日期！"
            )

    elif action == "add_off_days_input":
        # 支持: 2026-10-14 生日 或 2026-10-15~2026-10-18 旅行
        parts = text.split(maxsplit=1)
        date_part = parts[0]
        note = parts[1] if len(parts) > 1 else ""

        try:
            added_dates = []
            if "~" in date_part:
                start_s, end_s = date_part.split("~")
                start_d = datetime.strptime(start_s.strip(), "%Y-%m-%d").date()
                end_d = datetime.strptime(end_s.strip(), "%Y-%m-%d").date()
                if start_d > end_d:
                    start_d, end_d = end_d, start_d
                
                curr = start_d
                while curr <= end_d:
                    d_str = curr.strftime("%Y-%m-%d")
                    db.add_off_day(chat_id, d_str, note)
                    added_dates.append(d_str)
                    curr += timedelta(days=1)
            else:
                target_d = datetime.strptime(date_part.strip(), "%Y-%m-%d").date()
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
                "❌ 日期格式错误，请按照 `YYYY-MM-DD` 格式输入，例如：\n"
                "`2026-10-14` 或 `2026-10-15~2026-10-18 假期`"
            )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户直接发送的 .db 数据库文件，一键热恢复全部数据"""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id):
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
