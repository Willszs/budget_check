import sqlite3
import os
import json
import base64
import zlib
from datetime import datetime

DB_FILE = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "budget_data.db"))

def restore_from_seed(seed_str: str) -> bool:
    """从环境变量 SEED_DATA 恢复数据库记录"""
    try:
        raw_bytes = base64.b64decode(seed_str)
        try:
            decompressed = zlib.decompress(raw_bytes).decode("utf-8")
        except Exception:
            decompressed = raw_bytes.decode("utf-8")
        data = json.loads(decompressed)
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        if "user_profile" in data:
            cursor.execute("DELETE FROM user_profile")
            for r in data["user_profile"]:
                cursor.execute("""
                INSERT OR REPLACE INTO user_profile (chat_id, current_balance, target_amount, hourly_rate, updated_at, fixed_salary, fixed_salary_start, fixed_salary_day)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r["chat_id"],
                    r["current_balance"],
                    r["target_amount"],
                    r["hourly_rate"],
                    r["updated_at"],
                    r.get("fixed_salary", 0.0),
                    r.get("fixed_salary_start", ""),
                    r.get("fixed_salary_day", 1)
                ))
        if "milestones" in data:
            cursor.execute("DELETE FROM milestones")
            for r in data["milestones"]:
                cursor.execute("""
                INSERT OR REPLACE INTO milestones (id, chat_id, title, target_date, target_hours, target_money, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (r["id"], r["chat_id"], r["title"], r["target_date"], r["target_hours"], r["target_money"], r["status"], r["created_at"]))
        if "off_days" in data:
            cursor.execute("DELETE FROM off_days")
            for r in data["off_days"]:
                cursor.execute("""
                INSERT OR REPLACE INTO off_days (id, chat_id, off_date, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """, (r.get("id"), r["chat_id"], r["off_date"], r.get("note", ""), r.get("created_at", "")))
        if "extra_work_days" in data:
            cursor.execute("DELETE FROM extra_work_days")
            for r in data["extra_work_days"]:
                cursor.execute("""
                INSERT OR REPLACE INTO extra_work_days (id, chat_id, work_date, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """, (r.get("id"), r["chat_id"], r["work_date"], r.get("note", ""), r.get("created_at", "")))
        if "fixed_expenses" in data:
            cursor.execute("DELETE FROM fixed_expenses")
            for r in data["fixed_expenses"]:
                cursor.execute("""
                INSERT OR REPLACE INTO fixed_expenses (id, chat_id, title, amount, pay_day, start_date, end_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (r.get("id"), r["chat_id"], r["title"], r["amount"], r.get("pay_day", 1), r.get("start_date", ""), r.get("end_date", ""), r.get("created_at", "")))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error restoring seed data: {e}")
        return False

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # 用户基本财务配置表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_profile (
        chat_id INTEGER PRIMARY KEY,
        current_balance REAL DEFAULT 0.0,
        target_amount REAL DEFAULT 0.0,
        hourly_rate REAL DEFAULT 0.0,
        updated_at TEXT,
        fixed_salary REAL DEFAULT 0.0,
        fixed_salary_start TEXT DEFAULT '',
        fixed_salary_day INTEGER DEFAULT 1
    )
    """)

    # 自动升级表结构：若旧表缺少新字段则追加
    try:
        cursor.execute("ALTER TABLE user_profile ADD COLUMN fixed_salary REAL DEFAULT 0.0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE user_profile ADD COLUMN fixed_salary_start TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE user_profile ADD COLUMN fixed_salary_day INTEGER DEFAULT 1")
    except Exception:
        pass

    # 阶段目标节点表（模式B：截止日期、目标工时、目标金额、说明、是否完成）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS milestones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        title TEXT,
        target_date TEXT,
        target_hours REAL,
        target_money REAL,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )
    """)

    # 休息日/请假不可工作日表（排除指定日期）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS off_days (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        off_date TEXT, -- 具体日期 YYYY-MM-DD
        note TEXT,
        created_at TEXT
    )
    """)

    # 兼职开工日表（用户主动点选的 Freelancer 工作日）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS extra_work_days (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        work_date TEXT, -- 具体日期 YYYY-MM-DD
        note TEXT,
        created_at TEXT
    )
    """)

    # 固定每月支出表（支持设置到期日/期限 end_date）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fixed_expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        title TEXT,
        amount REAL,
        pay_day INTEGER DEFAULT 1,
        start_date TEXT DEFAULT '',
        end_date TEXT DEFAULT '',
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

    # 若配置了 SEED_DATA，且数据库为空，自动无缝灌入预填数据
    seed = os.getenv("SEED_DATA")
    if seed:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM milestones")
        cnt = cursor.fetchone()[0]
        conn.close()
        if cnt == 0:
            restore_from_seed(seed)

def get_profile(chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT current_balance, target_amount, hourly_rate, fixed_salary, fixed_salary_start, fixed_salary_day 
    FROM user_profile WHERE chat_id = ?
    """, (chat_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "current_balance": row[0],
            "target_amount": row[1],
            "hourly_rate": row[2],
            "fixed_salary": row[3] if row[3] is not None else 0.0,
            "fixed_salary_start": row[4] if row[4] is not None else "",
            "fixed_salary_day": row[5] if row[5] is not None else 1
        }
    return None

def update_profile(chat_id: int, current_balance=None, target_amount=None, hourly_rate=None, fixed_salary=None, fixed_salary_start=None, fixed_salary_day=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    
    profile = get_profile(chat_id)
    if not profile:
        cursor.execute("""
        INSERT INTO user_profile (chat_id, current_balance, target_amount, hourly_rate, updated_at, fixed_salary, fixed_salary_start, fixed_salary_day)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id,
            current_balance if current_balance is not None else 0.0,
            target_amount if target_amount is not None else 0.0,
            hourly_rate if hourly_rate is not None else 0.0,
            now_str,
            fixed_salary if fixed_salary is not None else 0.0,
            fixed_salary_start if fixed_salary_start is not None else "",
            fixed_salary_day if fixed_salary_day is not None else 1
        ))
    else:
        new_balance = current_balance if current_balance is not None else profile["current_balance"]
        new_target = target_amount if target_amount is not None else profile["target_amount"]
        new_rate = hourly_rate if hourly_rate is not None else profile["hourly_rate"]
        new_fixed_salary = fixed_salary if fixed_salary is not None else profile.get("fixed_salary", 0.0)
        new_fixed_start = fixed_salary_start if fixed_salary_start is not None else profile.get("fixed_salary_start", "")
        new_fixed_day = fixed_salary_day if fixed_salary_day is not None else profile.get("fixed_salary_day", 1)
        cursor.execute("""
        UPDATE user_profile
        SET current_balance = ?, target_amount = ?, hourly_rate = ?, updated_at = ?, fixed_salary = ?, fixed_salary_start = ?, fixed_salary_day = ?
        WHERE chat_id = ?
        """, (new_balance, new_target, new_rate, now_str, new_fixed_salary, new_fixed_start, new_fixed_day, chat_id))
        
    conn.commit()
    conn.close()

def add_milestone(chat_id: int, title: str, target_date: str, target_hours: float, target_money: float):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute("""
    INSERT INTO milestones (chat_id, title, target_date, target_hours, target_money, status, created_at)
    VALUES (?, ?, ?, ?, ?, 'pending', ?)
    """, (chat_id, title, target_date, target_hours, target_money, now_str))
    milestone_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return milestone_id

def get_milestones(chat_id: int, status="pending"):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if status == "all":
        cursor.execute("SELECT id, title, target_date, target_hours, target_money, status FROM milestones WHERE chat_id = ? ORDER BY target_date ASC", (chat_id,))
    else:
        cursor.execute("SELECT id, title, target_date, target_hours, target_money, status FROM milestones WHERE chat_id = ? AND status = ? ORDER BY target_date ASC", (chat_id, status))
    rows = cursor.fetchall()
    conn.close()
    res = []
    for r in rows:
        res.append({
            "id": r[0],
            "title": r[1],
            "target_date": r[2],
            "target_hours": r[3],
            "target_money": r[4],
            "status": r[5]
        })
    return res

def delete_milestone(milestone_id: int, chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM milestones WHERE id = ? AND chat_id = ?", (milestone_id, chat_id))
    conn.commit()
    conn.close()

def update_milestone_date(milestone_id: int, chat_id: int, new_date: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE milestones SET target_date = ? WHERE id = ? AND chat_id = ?", (new_date, milestone_id, chat_id))
    cursor.execute("SELECT title FROM milestones WHERE id = ?", (milestone_id,))
    row = cursor.fetchone()
    title = row[0] if row else ""
    conn.commit()
    conn.close()
    return title

def update_milestone_money(milestone_id: int, chat_id: int, new_money: float, new_hours: float):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE milestones SET target_money = ?, target_hours = ? WHERE id = ? AND chat_id = ?", (new_money, new_hours, milestone_id, chat_id))
    cursor.execute("SELECT title FROM milestones WHERE id = ?", (milestone_id,))
    row = cursor.fetchone()
    title = row[0] if row else ""
    conn.commit()
    conn.close()
    return title

def get_milestone(milestone_id: int, chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, target_date, target_hours, target_money, status FROM milestones WHERE id = ? AND chat_id = ?", (milestone_id, chat_id))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "title": row[1],
            "target_date": row[2],
            "target_hours": row[3],
            "target_money": row[4],
            "status": row[5]
        }
    return None

def complete_milestone(milestone_id: int, chat_id: int):
    """完成节点：标记状态为 completed，并自动从总资金中扣除该节点的金额"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # 查询该节点的金额和标题
    cursor.execute("SELECT target_money, title FROM milestones WHERE id = ? AND chat_id = ?", (milestone_id, chat_id))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None, 0.0

    target_money = row[0]
    title = row[1]

    # 标记完成
    cursor.execute("UPDATE milestones SET status = 'completed' WHERE id = ? AND chat_id = ?", (milestone_id, chat_id))
    
    # 从用户当前资金中扣除该节点金额（代表该笔资金已按计划支出/结算）
    cursor.execute("SELECT current_balance FROM user_profile WHERE chat_id = ?", (chat_id,))
    prof = cursor.fetchone()
    current_balance = prof[0] if prof else 0.0
    new_balance = max(0.0, current_balance - target_money)
    now_str = datetime.now().isoformat()

    cursor.execute("UPDATE user_profile SET current_balance = ?, updated_at = ? WHERE chat_id = ?", (new_balance, now_str, chat_id))

    conn.commit()
    conn.close()
    return title, target_money


def add_off_day(chat_id: int, off_date: str, note: str = ""):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    # 避免重复添加同一天
    cursor.execute("SELECT id FROM off_days WHERE chat_id = ? AND off_date = ?", (chat_id, off_date))
    if not cursor.fetchone():
        cursor.execute("""
        INSERT INTO off_days (chat_id, off_date, note, created_at)
        VALUES (?, ?, ?, ?)
        """, (chat_id, off_date, note, now_str))
    conn.commit()
    conn.close()

def get_off_days(chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, off_date, note FROM off_days WHERE chat_id = ? ORDER BY off_date ASC", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "off_date": r[1], "note": r[2]} for r in rows]

def delete_off_day(off_day_id: int, chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM off_days WHERE id = ? AND chat_id = ?", (off_day_id, chat_id))
    conn.commit()
    conn.close()

def add_work_day(chat_id: int, work_date: str, note: str = ""):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute("SELECT id FROM extra_work_days WHERE chat_id = ? AND work_date = ?", (chat_id, work_date))
    if not cursor.fetchone():
        cursor.execute("""
        INSERT INTO extra_work_days (chat_id, work_date, note, created_at)
        VALUES (?, ?, ?, ?)
        """, (chat_id, work_date, note, now_str))
    conn.commit()
    conn.close()

def get_work_days(chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, work_date, note FROM extra_work_days WHERE chat_id = ? ORDER BY work_date ASC", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "work_date": r[1], "note": r[2]} for r in rows]

def delete_work_day(chat_id: int, work_date: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM extra_work_days WHERE chat_id = ? AND work_date = ?", (chat_id, work_date))
    conn.commit()
    conn.close()

def add_fixed_expense(chat_id: int, title: str, amount: float, pay_day: int = 1, start_date: str = "", end_date: str = ""):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute("""
    INSERT INTO fixed_expenses (chat_id, title, amount, pay_day, start_date, end_date, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (chat_id, title, amount, pay_day, start_date, end_date, now_str))
    exp_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return exp_id

def get_fixed_expenses(chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, title, amount, pay_day, start_date, end_date
    FROM fixed_expenses WHERE chat_id = ? ORDER BY pay_day ASC, id ASC
    """, (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "title": r[1],
            "amount": r[2],
            "pay_day": r[3] if r[3] is not None else 1,
            "start_date": r[4] if r[4] is not None else "",
            "end_date": r[5] if r[5] is not None else ""
        }
        for r in rows
    ]

def delete_fixed_expense(expense_id: int, chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT title FROM fixed_expenses WHERE id = ? AND chat_id = ?", (expense_id, chat_id))
    row = cursor.fetchone()
    title = row[0] if row else ""
    cursor.execute("DELETE FROM fixed_expenses WHERE id = ? AND chat_id = ?", (expense_id, chat_id))
    conn.commit()
    conn.close()
    return title




