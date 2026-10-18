import sqlite3
import json
import zlib
import base64
import os

DB_FILE = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "budget_data.db"))

def generate_seed() -> str:
    if not os.path.exists(DB_FILE):
        print(f"Error: {DB_FILE} not found.")
        return ""

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT chat_id, current_balance, target_amount, hourly_rate, updated_at FROM user_profile")
    user_profile = [dict(r) for r in c.fetchall()]

    c.execute("SELECT id, chat_id, title, target_date, target_hours, target_money, status, created_at FROM milestones")
    milestones = [dict(r) for r in c.fetchall()]

    c.execute("SELECT id, chat_id, off_date, note, created_at FROM off_days")
    off_days = [dict(r) for r in c.fetchall()]

    c.execute("SELECT id, chat_id, work_date, note, created_at FROM extra_work_days")
    extra_work_days = [dict(r) for r in c.fetchall()]

    conn.close()

    data = {
        "user_profile": user_profile,
        "milestones": milestones,
        "off_days": off_days,
        "extra_work_days": extra_work_days
    }

    dumped = json.dumps(data, ensure_ascii=False)
    compressed = zlib.compress(dumped.encode("utf-8"))
    seed_str = base64.b64encode(compressed).decode("utf-8")
    return seed_str

if __name__ == "__main__":
    seed = generate_seed()
    if seed:
        print("=" * 60)
        print("SEED_DATA string generated successfully! (Length: " + str(len(seed)) + ")")
        print("Copy the string below and paste it into Render Environment Variables as SEED_DATA:")
        print("=" * 60)
        print(seed)
        print("=" * 60)

