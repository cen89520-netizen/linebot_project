import os
import psycopg2
from urllib.parse import urlparse

# 從環境變數讀取 Render/Neon 提供的 DATABASE_URL
DATABASE_URL = os.getenv('DATABASE_URL')

def get_db_connection():
    # 解析並建立 PostgreSQL 連線
    url = urlparse(DATABASE_URL)
    conn = psycopg2.connect(
        dbname=url.path[1:],
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port,
        sslmode='require'
    )
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # 建立表格 (PostgreSQL 語法)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            height REAL,
            weight REAL,
            age INTEGER,
            gender TEXT,
            phase TEXT DEFAULT '維持期',
            goal_calories REAL DEFAULT NULL,
            fasting_start TEXT,
            is_fasting INTEGER DEFAULT 0
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS food_logs (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            food_name TEXT,
            calories INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS weight_logs (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            weight REAL,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS exercise_logs (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            exercise_name TEXT,
            calories_burned INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

def update_user_data(user_id, height=None, weight=None, age=None, gender=None, phase=None, goal_calories=None, fasting_start=None, is_fasting=None):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
    if cur.fetchone() is None:
        cur.execute('''
            INSERT INTO users (user_id, height, weight, age, gender, phase, goal_calories, fasting_start, is_fasting) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (user_id, height, weight, age, gender, phase, goal_calories, fasting_start, is_fasting))
    else:
        if height is not None: cur.execute("UPDATE users SET height = %s WHERE user_id = %s", (height, user_id))
        if weight is not None: cur.execute("UPDATE users SET weight = %s WHERE user_id = %s", (weight, user_id))
        if age is not None: cur.execute("UPDATE users SET age = %s WHERE user_id = %s", (age, user_id))
        if gender is not None: cur.execute("UPDATE users SET gender = %s WHERE user_id = %s", (gender, user_id))
        if phase is not None: cur.execute("UPDATE users SET phase = %s WHERE user_id = %s", (phase, user_id))
        if goal_calories is not None: cur.execute("UPDATE users SET goal_calories = %s WHERE user_id = %s", (goal_calories, user_id))
        if fasting_start is not None: cur.execute("UPDATE users SET fasting_start = %s WHERE user_id = %s", (fasting_start, user_id))
        if is_fasting is not None: cur.execute("UPDATE users SET is_fasting = %s WHERE user_id = %s", (is_fasting, user_id))
            
    conn.commit()
    cur.close()
    conn.close()

def get_user_data(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT height, weight, age, gender, phase, goal_calories, fasting_start, is_fasting FROM users WHERE user_id = %s", (user_id,))
    data = cur.fetchone()
    cur.close()
    conn.close()
    return data

def add_weight_log(user_id, weight):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO weight_logs (user_id, weight) VALUES (%s, %s)", (user_id, weight))
    cur.execute("UPDATE users SET weight = %s WHERE user_id = %s", (weight, user_id))
    conn.commit()
    cur.close()
    conn.close()

def add_food_log(user_id, food_name, calories):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO food_logs (user_id, food_name, calories) VALUES (%s, %s, %s)", (user_id, food_name, calories))
    conn.commit()
    cur.close()
    conn.close()

def add_exercise_log(user_id, exercise_name, calories_burned):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO exercise_logs (user_id, exercise_name, calories_burned) VALUES (%s, %s, %s)", (user_id, exercise_name, calories_burned))
    conn.commit()
    cur.close()
    conn.close()

def get_weight_history(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT weight, date FROM weight_logs WHERE user_id = %s ORDER BY date ASC", (user_id,))
    data = cur.fetchall()
    cur.close()
    conn.close()
    return data

def get_streak(user_id):
    from datetime import date, timedelta
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT date::date AS log_date
        FROM food_logs WHERE user_id = %s
        ORDER BY log_date DESC
    """, (user_id,))
    dates = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()

    if not dates:
        return 0
    today = date.today()
    if dates[0] < today - timedelta(days=1):
        return 0
    streak, expected = 0, dates[0]
    for d in dates:
        if d == expected:
            streak += 1
            expected -= timedelta(days=1)
        else:
            break
    return streak

def get_today_total_calories(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    # PostgreSQL 使用 CURRENT_DATE 獲取日期
    cur.execute('''
        SELECT SUM(calories) 
        FROM food_logs 
        WHERE user_id = %s AND date::date = CURRENT_DATE
    ''', (user_id,))
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    return total if total is not None else 0