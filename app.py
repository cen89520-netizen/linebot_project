import re, json, os, time, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
from flask import Flask, request, abort, send_from_directory
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, MessagingApiBlob, ReplyMessageRequest, TextMessage, ImageMessage, FlexMessage, FlexContainer, QuickReply, QuickReplyItem, MessageAction
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent
from google import genai
from google.genai import types
import io
from PIL import Image
import cloudinary
import cloudinary.uploader
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET')
)
from dotenv import load_dotenv
from flask_apscheduler import APScheduler
from linebot.v3.messaging import PushMessageRequest
from db_manager import init_db, update_user_data, get_user_data, add_food_log, add_weight_log, add_exercise_log, get_weight_history, get_today_total_calories, get_streak

load_dotenv()
app = Flask(__name__)
init_db()
print("【DEBUG】: 資料庫表格初始化完成")

if not os.getenv('CHANNEL_ACCESS_TOKEN') or not os.getenv('GEMINI_API_KEY'):
    print("【警告】: 環境變數設定缺失，請檢查 .env 檔案！")
# =============================================================

configuration = Configuration(access_token=os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))
ai_client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

# 初始化排程器
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()


def send_push_message(user_id, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        # 使用傳入的 user_id，而不是固定寫死的字串
        line_bot_api.push_message(PushMessageRequest(
            to=user_id, 
            messages=[TextMessage(text=text)]
        ))

# --- 排程任務 ---
# 9:00 到 21:00 每小時提醒喝水
# 1. 喝水廣播 (簡單、不吃資料庫資源)
# 1. 喝水廣播 (修正版)
@scheduler.task('cron', id='water_reminder', hour='1,4,7,10,13', minute=0)
def job_water():
    # 改用我們剛寫好的 PostgreSQL 連線方式
    from db_manager import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT user_id FROM users")
    all_users = [row[0] for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    
    for uid in all_users:
        try:
            send_push_message(uid, "💧 喝水時間到！多補充水分能提升代謝喔！")
        except Exception as e:
            print(f"【DEBUG】: 推播失敗: {e}")

# 2. 晚上10點總結 (修正版)
@scheduler.task('cron', id='night_reminder', hour=14, minute=0)
def job_night():
    from db_manager import get_db_connection
    # 1. 抓取所有用戶
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT user_id FROM users")
    all_users = [row[0] for row in cursor.fetchall()]
    cursor.close()
    conn.close()

    # 2. 對每位用戶進行個別推播
    for uid in all_users:
        conn = get_db_connection()
        cursor = conn.cursor()
        # PostgreSQL 使用 CURRENT_DATE 或 date(date)
        cursor.execute("SELECT food_name FROM food_logs WHERE user_id = %s AND date::date = CURRENT_DATE", (uid,))
        foods = [r[0] for r in cursor.fetchall()]
        cursor.close()
        conn.close()
        
        food_summary = "、".join(foods) if foods else "尚未記錄"
        msg = f"💧 晚上10點了，喝杯水休息吧！\n\n📝 今日飲食清單：{food_summary}\n\n記得記錄體重，明天繼續努力！"
        
        send_push_message(uid, msg)

# 3. 每週一早上 9 點（UTC 1:00）發送週報
@scheduler.task('cron', id='weekly_report', day_of_week='mon', hour=1, minute=0)
def job_weekly_report():
    from db_manager import get_db_connection
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT user_id FROM users")
    all_users = [row[0] for row in cursor.fetchall()]
    cursor.close()
    conn.close()

    for uid in all_users:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT SUM(calories), COUNT(DISTINCT date::date)
                FROM food_logs
                WHERE user_id = %s AND date::date BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE - 1
            """, (uid,))
            food_row = cursor.fetchone()
            total_cal = food_row[0] or 0
            log_days = food_row[1] or 0

            cursor.execute("""
                SELECT COUNT(*), COALESCE(SUM(calories_burned), 0)
                FROM exercise_logs
                WHERE user_id = %s AND date::date BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE - 1
            """, (uid,))
            ex_row = cursor.fetchone()
            ex_count = ex_row[0] or 0
            ex_cal = ex_row[1] or 0

            cursor.execute("""
                SELECT weight FROM weight_logs WHERE user_id = %s
                AND date::date BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE - 1
                ORDER BY date ASC LIMIT 1
            """, (uid,))
            first_w = cursor.fetchone()
            cursor.execute("""
                SELECT weight FROM weight_logs WHERE user_id = %s
                AND date::date BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE - 1
                ORDER BY date DESC LIMIT 1
            """, (uid,))
            last_w = cursor.fetchone()
            cursor.close()
            conn.close()

            avg_cal = int(total_cal / 7) if total_cal else 0

            if log_days >= 6:
                performance = "🌟 記錄非常完整，超棒！"
            elif log_days >= 4:
                performance = "👍 不錯！這週試著每天都記錄看看！"
            else:
                performance = "💪 繼續加油，多記錄讓數據幫助你！"

            weight_line = ""
            if first_w and last_w and first_w[0] != last_w[0]:
                diff = last_w[0] - first_w[0]
                arrow = "↓" if diff < 0 else "↑"
                weight_line = f"\n⚖️ 體重變化：{arrow} {abs(diff):.1f} kg"

            msg = (
                f"📊 上週健康週報\n"
                f"{'─' * 18}\n"
                f"📅 記錄天數：{log_days} / 7 天\n"
                f"🔥 平均每日攝取：{avg_cal} kcal\n"
                f"🏃 運動次數：{ex_count} 次\n"
                f"💪 運動消耗：{ex_cal} kcal"
                f"{weight_line}\n"
                f"{'─' * 18}\n"
                f"{performance}"
            )
            send_push_message(uid, msg)
        except Exception as e:
            print(f"【DEBUG】: 週報推播失敗 {uid}: {e}")

def calculate_bmr(h, w, age, gender):
    # 預設性別為女，若為男則調整公式
    if gender == '男':
        return 10 * w + 6.25 * h - 5 * age + 5
    else:
        return 10 * w + 6.25 * h - 5 * age - 161

def send_reply(reply_token, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)]
        ))
        print("【DEBUG】: 斷食訊息已成功發送")

def send_flex_reply(reply_token, alt_text, bubble_dict):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[FlexMessage(alt_text=alt_text, contents=FlexContainer.from_dict(bubble_dict))]
        ))

def build_log_bubble(food_summary, cal, ex_summary, ex_cal, today_total, food_str, ex_str, advice, food_breakdown='', streak=0):
    if streak >= 14:
        streak_badge = f"　🏆 連續 {streak} 天"
    elif streak >= 7:
        streak_badge = f"　🔥🔥 連續 {streak} 天"
    elif streak >= 3:
        streak_badge = f"　🔥 連續 {streak} 天"
    elif streak >= 1:
        streak_badge = f"　🌱 第 {streak} 天"
    else:
        streak_badge = ""

    body = []
    if cal > 0:
        body += [
            {"type": "text", "text": "🍽️ 飲食", "weight": "bold", "color": "#4A90D9", "size": "sm"},
            {"type": "text", "text": food_summary, "wrap": True, "size": "md", "margin": "xs"},
        ]
        if food_breakdown and food_breakdown != '無':
            body.append({"type": "text", "text": food_breakdown, "wrap": True, "size": "xs", "color": "#999999", "margin": "xs"})
        body.append({"type": "box", "layout": "horizontal", "margin": "xs", "contents": [
            {"type": "text", "text": "攝取熱量", "size": "xs", "color": "#888888", "flex": 1},
            {"type": "text", "text": f"{cal} kcal", "size": "xs", "color": "#4A90D9", "weight": "bold", "align": "end", "flex": 1}
        ]})
    if ex_cal > 0 and ex_summary != '無':
        if cal > 0:
            body.append({"type": "separator", "margin": "md"})
        body += [
            {"type": "text", "text": "🏃 運動", "weight": "bold", "color": "#27AE60", "size": "sm", "margin": "md"},
            {"type": "text", "text": ex_summary, "wrap": True, "size": "md", "margin": "xs"},
            {"type": "box", "layout": "horizontal", "margin": "xs", "contents": [
                {"type": "text", "text": "消耗熱量", "size": "xs", "color": "#888888", "flex": 1},
                {"type": "text", "text": f"{ex_cal} kcal", "size": "xs", "color": "#27AE60", "weight": "bold", "align": "end", "flex": 1}
            ]}
        ]
    body += [
        {"type": "separator", "margin": "lg"},
        {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
            {"type": "text", "text": "今日累計攝取", "size": "sm", "color": "#555555", "flex": 1},
            {"type": "text", "text": f"{today_total} kcal", "size": "sm", "weight": "bold", "color": "#333333", "align": "end", "flex": 1}
        ]},
        {"type": "text", "text": f"📝 {food_str}", "size": "xs", "color": "#aaaaaa", "wrap": True, "margin": "xs"},
        {"type": "text", "text": f"🏋️ {ex_str}", "size": "xs", "color": "#aaaaaa", "wrap": True, "margin": "xs"},
    ]
    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#4A90D9", "paddingAll": "15px",
                   "contents": [{"type": "text", "text": f"✅ 記錄成功{streak_badge}", "color": "#ffffff", "weight": "bold", "size": "lg", "wrap": True}]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "15px", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "backgroundColor": "#f8f9fa", "paddingAll": "12px",
                   "contents": [{"type": "text", "text": f"💡 {advice}", "wrap": True, "color": "#888888", "size": "xs"}]}
    }

def build_today_bubble(food_str, today_total, ex_str):
    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#27AE60", "paddingAll": "15px",
                   "contents": [{"type": "text", "text": "📋 今日紀錄", "color": "#ffffff", "weight": "bold", "size": "lg"}]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "15px", "contents": [
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": "今日累計熱量", "size": "sm", "color": "#555555", "flex": 1},
                {"type": "text", "text": f"{today_total} kcal", "size": "sm", "weight": "bold", "color": "#27AE60", "align": "end", "flex": 1}
            ]},
            {"type": "separator", "margin": "lg"},
            {"type": "text", "text": "🥗 今日飲食", "weight": "bold", "size": "sm", "margin": "lg", "color": "#333333"},
            {"type": "text", "text": food_str, "size": "sm", "color": "#666666", "wrap": True, "margin": "xs"},
            {"type": "separator", "margin": "lg"},
            {"type": "text", "text": "🏋️ 今日運動", "weight": "bold", "size": "sm", "margin": "lg", "color": "#333333"},
            {"type": "text", "text": ex_str, "size": "sm", "color": "#666666", "wrap": True, "margin": "xs"},
        ]}
    }

def build_report_bubble(gender, age, bmi, phase, final_goal, source):
    if bmi < 18.5:
        bmi_label, bmi_color = "偏輕", "#3498DB"
    elif bmi < 24:
        bmi_label, bmi_color = "正常", "#27AE60"
    elif bmi < 27:
        bmi_label, bmi_color = "過重", "#E67E22"
    else:
        bmi_label, bmi_color = "肥胖", "#E74C3C"
    icon = "👨" if gender == "男" else "👩"
    return {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#8E44AD", "paddingAll": "15px",
                   "contents": [{"type": "text", "text": "📊 健康報表", "color": "#ffffff", "weight": "bold", "size": "lg"}]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "15px", "contents": [
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": f"{icon} {gender}", "size": "sm", "color": "#555555", "flex": 1},
                {"type": "text", "text": f"{age} 歲", "size": "sm", "color": "#555555", "align": "center", "flex": 1},
                {"type": "text", "text": phase, "size": "sm", "color": "#8E44AD", "weight": "bold", "align": "end", "flex": 1}
            ]},
            {"type": "separator", "margin": "lg"},
            {"type": "box", "layout": "horizontal", "margin": "lg", "contents": [
                {"type": "text", "text": "BMI", "size": "sm", "color": "#555555", "flex": 1},
                {"type": "text", "text": f"{bmi:.1f}", "size": "xl", "weight": "bold", "color": bmi_color, "align": "end", "flex": 1},
                {"type": "text", "text": bmi_label, "size": "sm", "color": bmi_color, "align": "end", "flex": 1}
            ]},
            {"type": "separator", "margin": "lg"},
            {"type": "text", "text": "每日目標熱量", "size": "sm", "color": "#555555", "margin": "lg"},
            {"type": "box", "layout": "horizontal", "margin": "xs", "contents": [
                {"type": "text", "text": source, "size": "xs", "color": "#aaaaaa", "flex": 2, "wrap": True},
                {"type": "text", "text": f"{final_goal:.0f} kcal", "size": "lg", "weight": "bold", "color": "#8E44AD", "align": "end", "flex": 1}
            ]}
        ]}
    }

def generate_weight_chart(user_id):
    history = get_weight_history(user_id)
    if not history or len(history) < 3:
        return None

    weights = [h[0] for h in history]
    dates = [h[1] if isinstance(h[1], datetime) else datetime.strptime(str(h[1]), '%Y-%m-%d %H:%M:%S') for h in history]

    change = weights[-1] - weights[0]
    change_str = f"+{change:.1f}" if change >= 0 else f"{change:.1f}"
    min_w, max_w = min(weights), max(weights)

    fig, ax = plt.subplots(figsize=(9, 5))
    fig.patch.set_facecolor('#f8f9fa')
    ax.set_facecolor('#f8f9fa')

    ax.plot(dates, weights, marker='o', linestyle='-', color='#4a90d9',
            linewidth=2.5, markersize=7, zorder=3)
    ax.fill_between(dates, weights, min_w - 1, alpha=0.15, color='#4a90d9')

    # 標出最低點
    min_idx = weights.index(min_w)
    ax.annotate(f'{min_w} kg', xy=(dates[min_idx], min_w),
                xytext=(0, -18), textcoords='offset points',
                ha='center', fontsize=9, color='#27ae60', fontweight='bold')

    # 標出最高點（與最低點不同位置時才標）
    max_idx = weights.index(max_w)
    if max_idx != min_idx:
        ax.annotate(f'{max_w} kg', xy=(dates[max_idx], max_w),
                    xytext=(0, 10), textcoords='offset points',
                    ha='center', fontsize=9, color='#e74c3c', fontweight='bold')

    # 標出最新體重（紅點）
    ax.plot(dates[-1], weights[-1], marker='o', color='#e74c3c', markersize=11, zorder=4)
    ax.annotate(f'Latest: {weights[-1]} kg', xy=(dates[-1], weights[-1]),
                xytext=(-10, 12), textcoords='offset points',
                ha='right', fontsize=10, color='#e74c3c', fontweight='bold')

    padding = max((max_w - min_w) * 0.4, 1.5)
    ax.set_ylim(min_w - padding, max_w + padding)

    ax.set_title(f'Weight Trend  ({change_str} kg)', fontsize=14,
                 fontweight='bold', color='#333333', pad=15)
    ax.set_ylabel('Weight (kg)', fontsize=11, color='#555555')
    ax.set_xlabel('Date', fontsize=11, color='#555555')

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    plt.xticks(rotation=30, ha='right', fontsize=9)

    ax.grid(True, linestyle='--', alpha=0.5, color='#cccccc')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close()
    buf.seek(0)

    result = cloudinary.uploader.upload(
        buf,
        public_id=f"linebot_chart_{user_id}",
        overwrite=True,
        resource_type='image'
    )
    return result['secure_url']

def handle_fasting_logic(user_message, user_id):
    """專門處理斷食相關指令，回傳字串結果，若不是斷食指令則回傳 None"""
    if "開始斷食" in user_message:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        update_user_data(user_id, fasting_start=now, is_fasting=1)
        return (f"⏳ 斷食已開始：{now}\n\n"
                "💡 營養師小叮嚀：斷食期間建議多喝水，避免含糖飲料。若感到強烈不適請立即停止！期待你達成目標！")

    elif "結束斷食" in user_message:
        data = get_user_data(user_id)
        if data and data[6]: # data[6] 是 fasting_start
            start_time = datetime.strptime(data[6], '%Y-%m-%d %H:%M:%S')
            hours = (datetime.now() - start_time).total_seconds() / 3600
            update_user_data(user_id, is_fasting=0, fasting_start=None)
            res = f"✅ 斷食結束！累計時長：{hours:.1f} 小時。"
            return res + ("\n🎉 太棒了，達成 16 小時！" if hours >= 16 else "\n下次再接再厲！")
        return "您目前沒有進行中的斷食紀錄。"

    elif "斷食狀態" in user_message:
        data = get_user_data(user_id)
        if data and data[7] == 1: # data[7] 是 is_fasting
            start_time = datetime.strptime(data[6], '%Y-%m-%d %H:%M:%S')
            hours = (datetime.now() - start_time).total_seconds() / 3600
            bar_len = min(int(hours // 2), 8)
            bar = "█" * bar_len + "░" * (8 - bar_len)
            return f"⏳ 斷食進行中\n進度：[{bar}] {hours:.1f} 小時"
        return "您目前沒有開啟斷食，輸入「開始斷食」即可計時。"
    
    return None # 如果不是斷食指令，回傳 None
@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)
@app.route("/", methods=['GET', 'HEAD'])
def health_check():
    # 當有人訪問網址首頁時，直接回傳 OK，不需要任何簽名檢查
    return "OK", 200
@app.route("/callback", methods=['POST'])
def callback():
    print("【DEBUG】: 收到來自 LINE 的 Webhook")
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    try:
        # 只呼叫一次 handler.handle
        handler.handle(body, signature)
        print("【DEBUG】: 訊息已處理完成")
    except InvalidSignatureError:
        print("【DEBUG】: 簽名錯誤")
        abort(400)
    except Exception as e:
        print(f"【DEBUG】: 處理錯誤: {e}")
        
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    
    # 1. 斷食指令優先
    fasting_reply = handle_fasting_logic(user_message, user_id)
    if fasting_reply:
        send_reply(event.reply_token, fasting_reply)
        return

    # --- 這裡加入體重快速紀錄 ---
    if "體重是" in user_message or "我量體重" in user_message:
        w_match = re.search(r"(\d+\.?\d*)", user_message)
        if w_match:
            weight = float(w_match.group(1))
            add_weight_log(user_id, weight) # 確保這裡呼叫的是正確的 db_manager 函數
            send_reply(event.reply_token, f"✅ 已成功記錄體重：{weight} kg！明天也要繼續加油喔！")
            return

    if "今日紀錄" in user_message:
        from db_manager import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT food_name FROM food_logs WHERE user_id = %s AND date::date = CURRENT_DATE", (user_id,))
        food_list = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT exercise_name FROM exercise_logs WHERE user_id = %s AND date::date = CURRENT_DATE", (user_id,))
        ex_list = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()

        food_str = "、".join(food_list) if food_list else "尚未記錄"
        ex_str = "、".join(ex_list) if ex_list else "尚未記錄"
        today_total = get_today_total_calories(user_id)

        send_flex_reply(event.reply_token, "今日紀錄", build_today_bubble(food_str, today_total, ex_str))
        return
    elif "我的報表" in user_message:
        data = get_user_data(user_id)
        if data:
            h, w, age, gender, phase, goal_calories, start_time, is_fasting = data
            source = "🤖 系統自動建議值"
            if goal_calories and goal_calories > 0:
                final_goal = goal_calories
                source = "🎯 您設定的每日目標"
            else:
                bmr = calculate_bmr(h, w, age or 25, gender or '女')
                phase_adj = {'減脂期': -300, '增肌期': 300, '維持期': 0}
                final_goal = (bmr * 1.2) + phase_adj.get(phase or '維持期', 0)
            bmi = w / ((h / 100) ** 2)
            send_flex_reply(event.reply_token, "健康報表",
                            build_report_bubble(gender or '女', age or 25, bmi, phase or '維持期', final_goal, source))
            return
        else:
            send_reply(event.reply_token, "尚未設定資料，請先輸入資訊。")
            return

    elif "營養知識" in user_message:
        reply_text = (
            "🥦 歡迎來到健康小學堂！請輸入以下指令查看詳細內容：\n\n"
            "【查詢指令】\n"
            "🔎 營養增肌\n🔎 營養減脂\n🔎 營養斷食\n🔎 營養外食\n🔎 營養運動"
        )
        send_reply(event.reply_token, reply_text)
        return

    elif "營養" in user_message and "增肌" in user_message:
        reply_text = ("💪 【增肌期蛋白質計算】\n"
                      "1. 蛋白質：體重 × 1.6~2.0倍 (克)。\n"
                      "2. 黃金比例：運動後碳水:蛋白質 = 3:1。\n"
                      "3. 脂肪：每天補充一把堅果，維持荷爾蒙運作。")
        send_reply(event.reply_token, reply_text)
        return

    elif "營養" in user_message and "減脂" in user_message:
        reply_text = ("🔥 【減脂期營養重點】\n"
                      "1. 蛋白質：維持體重 × 1.2~1.5倍，防肌肉流失。\n"
                      "2. 聰明補油：避開炸物，選擇鮭魚、酪梨、堅果。\n"
                      "3. 油脂建議佔總熱量 20~30%。")
        send_reply(event.reply_token, reply_text)
        return

    elif "營養" in user_message and "斷食" in user_message:
        reply_text = ("⏳ 【168斷食建議】\n"
                      "1. 進食窗口：先吃蛋白質與蔬菜，穩定血糖。\n"
                      "2. 斷食期間：只能喝水、黑咖啡、無糖茶。\n"
                      "3. 安全提醒：孕婦、發育中學生、糖尿病患者不建議斷食。")
        send_reply(event.reply_token, reply_text)
        return

    elif "營養" in user_message and "外食" in user_message:
        reply_text = ("🍱 【外食族拳頭法】\n"
                      "1. 蔬菜：每餐 1 個拳頭。\n"
                      "2. 蛋白質：每餐 1 個手掌心。\n"
                      "3. 主食：每餐 1 個拳頭 (多選非精緻澱粉)。")
        send_reply(event.reply_token, reply_text)
        return

    elif "營養" in user_message and "運動" in user_message:
        reply_text = ("🏃 【運動習慣建議】\n"
                      "1. 中強度(會喘但能說話)：每週 150 分鐘。\n"
                      "2. 高強度(很喘)：每週 75 分鐘。\n"
                      "3. 建議：重訓搭配有氧，堅持最重要！")
        send_reply(event.reply_token, reply_text)
        return

    elif "體重趨勢" in user_message:
        chart_url = generate_weight_chart(user_id)
        if chart_url:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="📈 這是您的體重變化趨勢："), ImageMessage(originalContentUrl=chart_url, previewImageUrl=chart_url)]
                ))
            return
        reply_text = "📈 體重紀錄不足，請至少記錄 3 次體重後再查看趨勢圖喔！"
    elif "資料設定" == user_message:
        reply_text = (
            "📏 請輸入您的個人資訊，例如：\n"
            "「我的身高170，體重65，年齡25，我是女生，現在是減脂期」\n\n"
            "您可以一次輸入多項設定，我會自動幫您更新！"
        )

    # 執行資料更新 (當輸入包含設定關鍵字時)
    elif any(k in user_message for k in ["身高", "體重", "年齡", "性別", "增肌期", "減脂期", "維持期", "目標熱量", "熱量設定"]):
        h_match = re.search(r"身高.*?(\d+\.?\d*)", user_message)
        w_match = re.search(r"體重.*?(\d+\.?\d*)", user_message)
        a_match = re.search(r"年齡.*?(\d+)", user_message)
        g_match = re.search(r"(男|女)生?", user_message)
        p_match = re.search(r"(增肌期|減脂期|維持期)", user_message)
        c_match = re.search(r"(目標熱量|熱量設定|每日目標).*?(\d+)", user_message)
        update_fields = {}
        if h_match: update_fields['height'] = float(h_match.group(1))
        if w_match: update_fields['weight'] = float(w_match.group(1))
        if a_match: 
            age = int(a_match.group(1))
            if 0 < age < 120: update_fields['age'] = age
        if g_match: update_fields['gender'] = '男' if '男' in g_match.group(1) else '女'
        if p_match: update_fields['phase'] = p_match.group(1)
        if c_match: update_fields['goal_calories'] = float(c_match.group(2))

        if update_fields:
            update_user_data(user_id, **update_fields)
            if 'weight' in update_fields:
                add_weight_log(user_id, update_fields['weight'])

            lines = ["✅ 資料已更新："]
            if 'height' in update_fields: lines.append(f"📏 身高: {update_fields['height']} cm")
            if 'weight' in update_fields: lines.append(f"⚖️ 體重: {update_fields['weight']} kg")
            if 'age' in update_fields: lines.append(f"🎂 年齡: {update_fields['age']} 歲")
            if 'gender' in update_fields: lines.append(f"🚻 性別: {update_fields['gender']}")
            if 'phase' in update_fields: lines.append(f"🔄 當前目標: {update_fields['phase']}")
            if 'goal_calories' in update_fields: lines.append(f"🎯 目標熱量: {update_fields['goal_calories']} kcal")
            reply_text = "\n".join(lines)
        else:
            reply_text = "格式有誤，請參考範例：我的身高170，體重64，年齡19，我是女生，現在是增肌期"
    elif user_message == "幫助-數據":
        send_reply(event.reply_token,
            "📊 數據與紀錄\n"
            "─────────────\n"
            "• 設定資料：「身高170，體重65，年齡25，女生，減脂期」\n"
            "• 記錄體重：「體重是 65.5」\n"
            "• 體重趨勢圖：「體重趨勢」（需 3 筆以上）\n"
            "• 健康報表（BMI、目標熱量）：「我的報表」\n"
            "• 今日飲食運動總覽：「今日紀錄」")
        return

    elif user_message == "幫助-飲食":
        send_reply(event.reply_token,
            "🥗 飲食與運動記錄\n"
            "─────────────\n"
            "• 文字記錄：直接說「午餐雞腿飯，跑步 30 分鐘」\n"
            "• 拍照記錄：傳送食物照片，AI 自動辨識熱量\n"
            "• 記錄後顯示本次熱量、今日累計、逐項明細\n"
            "• 連續記錄天數：🌱 第 1 天 → 🔥 3 天 → 🏆 14 天")
        return

    elif user_message == "幫助-斷食":
        send_reply(event.reply_token,
            "⏳ 斷食功能\n"
            "─────────────\n"
            "• 開始計時：「開始斷食」\n"
            "• 查看進度：「斷食狀態」\n"
            "• 結束計時：「結束斷食」\n"
            "• 達成 16 小時會特別告知，未達標也會鼓勵你")
        return

    elif user_message == "幫助-知識":
        send_reply(event.reply_token,
            "💡 營養知識\n"
            "─────────────\n"
            "輸入以下關鍵字查看詳細說明：\n"
            "• 營養增肌\n"
            "• 營養減脂\n"
            "• 營養斷食\n"
            "• 營養外食\n"
            "• 營養運動")
        return

    elif user_message == "幫助-提醒":
        send_reply(event.reply_token,
            "🔔 自動提醒\n"
            "─────────────\n"
            "每天固定推播：\n"
            "• 每 3 小時（9am / 12pm / 3pm / 6pm / 9pm）：喝水提醒\n"
            "• 每晚 10 點：今日飲食清單總結\n\n"
            "每週：\n"
            "• 週一早上 9 點：上週健康週報\n"
            "  （記錄天數、平均熱量、運動次數、體重變化）")
        return

    elif "幫助" in user_message:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(
                    text="👋 我是你的健康管家！\n直接說出飲食、運動或體重，我幫你記錄 📋\n\n想了解哪個功能？",
                    quick_reply=QuickReply(items=[
                        QuickReplyItem(action=MessageAction(label="📊 數據與紀錄", text="幫助-數據")),
                        QuickReplyItem(action=MessageAction(label="🥗 飲食與運動", text="幫助-飲食")),
                        QuickReplyItem(action=MessageAction(label="⏳ 斷食功能", text="幫助-斷食")),
                        QuickReplyItem(action=MessageAction(label="💡 營養知識", text="幫助-知識")),
                        QuickReplyItem(action=MessageAction(label="🔔 自動提醒", text="幫助-提醒")),
                    ])
                )]
            ))
        return

    # 2. 如果不是上面的指令，就交給 AI 處理飲食/運動分析
    else:
        user_data = get_user_data(user_id)
        user_context = ""
        ref_weight = 60
        if user_data:
            h, w, age, gender, phase, goal_calories, _, _ = user_data
            if w: ref_weight = w
            parts = []
            if w: parts.append(f"體重{w}kg")
            if phase: parts.append(f"目標：{phase}")
            if parts:
                user_context = f"\n【使用者資料】{'、'.join(parts)}，請依此調整建議與運動消耗估算。"

        prompt = f"""
        你是一位專業的台灣營養師，請精確分析使用者輸入中的飲食與運動。

        【熱量計算原則】
        1. 依台灣常見份量估算：一碗白飯≈280kcal、便當≈700kcal、雞腿≈250kcal、雞胸肉100g≈165kcal
        2. 烹調方式影響：油炸＞煎＞炒＞烤＞水煮，油炸比水煮多40~60%熱量
        3. 注意隱藏熱量：炒菜用油（每匙≈45kcal）、醬料、勾芡、全糖飲料（珍奶≈670kcal）
        4. 使用者有標示份量或大卡數時，以該數字為準
        5. 運動消耗依{ref_weight}kg體重與時長估算（跑步每分鐘約{round(ref_weight*0.13)}kcal、快走約{round(ref_weight*0.07)}kcal）{user_context}

        請嚴格回傳以下 JSON 格式，不要包含 ```json 或任何額外說明：
        {{
            "food_summary": "飲食內容摘要（含份量），若無則填無",
            "food_breakdown": "各品項熱量，例：雞腿250kcal、白飯280kcal，若無飲食則填無",
            "total_calories": 數字,
            "exercise_summary": "運動內容摘要，若無則填無",
            "exercise_calories": 數字,
            "advice": "針對使用者目標的具體建議，20字以內"
        }}
        使用者輸入: {user_message}
        """
        
        try:
            # 只執行一次，不使用迴圈
            response = ai_client.models.generate_content(model='models/gemini-3.5-flash', contents=prompt)
            raw = response.text.replace('```json', '').replace('```', '').strip()
            start, end = raw.find('{'), raw.rfind('}') + 1
            data = json.loads(raw[start:end])
            
            cal = int(data.get('total_calories', 0))
            ex_cal = int(data.get('exercise_calories', 0))
            food_summary = data.get('food_summary', '無')
            ex_summary = data.get('exercise_summary', '無')

            if cal > 0:
                add_food_log(user_id, food_summary, cal)
            if ex_cal > 0 and ex_summary != '無':
                add_exercise_log(user_id, ex_summary, ex_cal)

            from db_manager import get_db_connection
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT food_name FROM food_logs WHERE user_id = %s AND date::date = CURRENT_DATE", (user_id,))
            food_list = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT exercise_name FROM exercise_logs WHERE user_id = %s AND date::date = CURRENT_DATE", (user_id,))
            ex_list = [row[0] for row in cursor.fetchall()]
            cursor.close()
            conn.close()

            food_str = "、".join(food_list) if food_list else "無"
            ex_str = "、".join(ex_list) if ex_list else "無"
            today_total = get_today_total_calories(user_id)

            streak = get_streak(user_id)
            send_flex_reply(event.reply_token, "記錄成功",
                            build_log_bubble(food_summary, cal, ex_summary, ex_cal,
                                             today_total, food_str, ex_str,
                                             data.get('advice', '維持健康生活！'),
                                             data.get('food_breakdown', ''),
                                             streak))
            return
        except Exception as e:
            error_msg = str(e)
            print(f"【DEBUG】: 錯誤詳細內容: {error_msg}")
            
            # 如果是 Gemini 伺服器忙碌 (503)
            if "503" in error_msg or "UNAVAILABLE" in error_msg:
                reply_text = "營養師現在比較忙（伺服器繁忙），可以請您簡短一點描述，或稍候再試嗎？"
            elif "429" in error_msg:
                reply_text = "今天問太多啦！營養師先休息一下，明天再繼續吧。"
            elif "database" in error_msg.lower():
                reply_text = "資料庫寫入失敗，請檢查系統設定。"
            else:
                reply_text = "系統思考中遇到阻礙，請再傳送一次。"
    if reply_text:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            ))
            print("【DEBUG】: 訊息已成功發送")
    else:
        print("【DEBUG】: reply_text 為空，未執行發送")

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id

    try:
        # 從 LINE 下載圖片
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)
            raw = blob_api.get_message_content(message_id)
            # 相容 bytes 和 iterable chunks 兩種回傳格式
            image_bytes = raw if isinstance(raw, bytes) else b''.join(raw)

        img = Image.open(io.BytesIO(image_bytes)).convert('RGB')

        prompt = """
        你是一位專業營養師，請分析這張食物照片。
        請嚴格回傳以下 JSON 格式，不要包含 ```json 或任何額外說明：
        {
            "food_summary": "食物名稱與描述",
            "total_calories": 數字（估算總熱量kcal，整數）,
            "advice": "一句營養建議"
        }
        若圖片中沒有食物，請回傳：{"food_summary": "無", "total_calories": 0, "advice": "請拍攝食物照片"}
        """

        response = ai_client.models.generate_content(
            model='models/gemini-3.5-flash',
            contents=[img, prompt]
        )

        raw = response.text.replace('```json', '').replace('```', '').strip()
        start, end = raw.find('{'), raw.rfind('}') + 1
        data = json.loads(raw[start:end])

        cal = int(data.get('total_calories', 0))
        food_name = data.get('food_summary', '無')

        if cal > 0:
            add_food_log(user_id, food_name, cal)
            today_total = get_today_total_calories(user_id)
            reply_text = (
                f"📸 辨識結果：{food_name}\n\n"
                f"🔥 本次熱量：{cal} kcal\n"
                f"📊 今日累計攝取：{today_total} kcal\n\n"
                f"💡 營養師小叮嚀：{data.get('advice', '維持健康飲食！')}"
            )
        else:
            reply_text = f"📸 {data.get('advice', '圖片中未偵測到食物，請重新拍攝！')}"

    except Exception as e:
        error_msg = str(e)
        print(f"【DEBUG】: 圖片辨識錯誤: {type(e).__name__}: {e}", flush=True)
        if "503" in error_msg or "UNAVAILABLE" in error_msg:
            reply_text = "📸 AI 現在有點忙，請稍候幾分鐘再傳一次照片！"
        elif "429" in error_msg:
            reply_text = "📸 今日辨識次數已達上限，請改用文字輸入食物名稱。"
        else:
            reply_text = "📸 圖片辨識失敗，請稍後再試或改用文字輸入食物名稱。"

    send_reply(event.reply_token, reply_text)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)