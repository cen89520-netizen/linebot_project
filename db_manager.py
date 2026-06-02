import sqlite3

def init_db():
    conn = sqlite3.connect('health_bot.db')
    cursor = conn.cursor()
    
    # 修改：在 users 表格中加入 phase 欄位，預設為 '維持期'
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            height REAL,
            weight REAL,
            age INTEGER,
            gender TEXT,
            phase TEXT DEFAULT '維持期',
            goal_calories REAL DEFAULT NULL,
            fasting_start TEXT, is_fasting INTEGER DEFAULT 0
        )
    ''')
    
    # 其他表格保持不變
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS food_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            food_name TEXT,
            calories INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            weight REAL,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS exercise_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            exercise_name TEXT,
            calories_burned INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'fasting_start' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN fasting_start TEXT")
    if 'is_fasting' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_fasting INTEGER DEFAULT 0")
    
    conn.commit()
    conn.close()

# 修改後的 update_user_data
def update_user_data(user_id, height=None, weight=None, age=None, gender=None, phase=None, goal_calories=None, fasting_start=None, is_fasting=None):
    conn = sqlite3.connect('health_bot.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cursor.fetchone() is None:
        # 在這裡補上了 goal_calories, fasting_start, is_fasting
        cursor.execute('''
            INSERT INTO users (user_id, height, weight, age, gender, phase, goal_calories, fasting_start, is_fasting) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, height, weight, age, gender, phase, goal_calories, fasting_start, is_fasting))
    else:
        if height is not None: cursor.execute("UPDATE users SET height = ? WHERE user_id = ?", (height, user_id))
        if weight is not None: cursor.execute("UPDATE users SET weight = ? WHERE user_id = ?", (weight, user_id))
        if age is not None: cursor.execute("UPDATE users SET age = ? WHERE user_id = ?", (age, user_id))
        if gender is not None: cursor.execute("UPDATE users SET gender = ? WHERE user_id = ?", (gender, user_id))
        if phase is not None: cursor.execute("UPDATE users SET phase = ? WHERE user_id = ?", (phase, user_id))
        if goal_calories is not None: 
            print(f"【DEBUG】: 準備更新目標熱量為 {goal_calories}") # 增加此處 Debug
            cursor.execute("UPDATE users SET goal_calories = ? WHERE user_id = ?", (goal_calories, user_id))
        if fasting_start is not None: cursor.execute("UPDATE users SET fasting_start = ? WHERE user_id = ?", (fasting_start, user_id))
        if is_fasting is not None: cursor.execute("UPDATE users SET is_fasting = ? WHERE user_id = ?", (is_fasting, user_id))
            
    conn.commit()
    conn.close()


def get_user_data(user_id):
    conn = sqlite3.connect('health_bot.db')
    cursor = conn.cursor()
    # 增加讀取 fasting_start 和 is_fasting
    cursor.execute("SELECT height, weight, age, gender, phase, goal_calories, fasting_start, is_fasting FROM users WHERE user_id = ?", (user_id,))
    data = cursor.fetchone()
    conn.close()
    return data

# 其餘的 log 函數保持不變...
def add_weight_log(user_id, weight):
    conn = sqlite3.connect('health_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO weight_logs (user_id, weight) VALUES (?, ?)", (user_id, weight))
    cursor.execute("UPDATE users SET weight = ? WHERE user_id = ?", (weight, user_id))
    conn.commit()
    conn.close()

def add_food_log(user_id, food_name, calories):
    conn = sqlite3.connect('health_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO food_logs (user_id, food_name, calories) VALUES (?, ?, ?)",
                   (user_id, food_name, calories))
    conn.commit()
    conn.close()

def add_exercise_log(user_id, exercise_name, calories_burned):
    conn = sqlite3.connect('health_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO exercise_logs (user_id, exercise_name, calories_burned) VALUES (?, ?, ?)",
                   (user_id, exercise_name, calories_burned))
    conn.commit()
    conn.close()
def get_weight_history(user_id):
    """取得該使用者的所有體重紀錄，用於繪製趨勢圖"""
    conn = sqlite3.connect('health_bot.db')
    cursor = conn.cursor()
    # 依照日期排序，取得體重與日期
    cursor.execute("SELECT weight, date FROM weight_logs WHERE user_id = ? ORDER BY date ASC", (user_id,))
    data = cursor.fetchall() 
    conn.close()
    return data
def get_today_total_calories(user_id):
    """計算使用者當日累計總熱量"""
    conn = sqlite3.connect('health_bot.db')
    cursor = conn.cursor()
    # 使用 date('now', 'localtime') 確保日期是以本地時間為準
    cursor.execute('''
        SELECT SUM(calories) 
        FROM food_logs 
        WHERE user_id = ? AND date(date) = date('now', 'localtime')
    ''', (user_id,))
    
    total = cursor.fetchone()[0]
    conn.close()
    return total if total is not None else 0