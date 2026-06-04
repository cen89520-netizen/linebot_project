import re, json, os, time, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime
from flask import Flask, request, abort, send_from_directory
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage, ImageMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai
from dotenv import load_dotenv
from flask_apscheduler import APScheduler
from linebot.v3.messaging import PushMessageRequest
from db_manager import init_db, update_user_data, get_user_data, add_food_log, add_weight_log, add_exercise_log, get_weight_history, get_today_total_calories

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
@scheduler.task('cron', id='water_reminder', hour='9-21', minute=0)
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
@scheduler.task('cron', id='night_reminder', hour=22, minute=0)
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

def generate_weight_chart(user_id):
    history = get_weight_history(user_id)
    if not history or len(history) < 2: return None
    weights, dates = [h[0] for h in history], [datetime.strptime(h[1], '%Y-%m-%d %H:%M:%S') for h in history]
    plt.figure(figsize=(8, 4))
    plt.plot(dates, weights, marker='o', linestyle='-', color='b')
    plt.title("Weight Trend")
    plt.grid(True)
    if not os.path.exists('static'): os.makedirs('static')
    filename = f"static/chart_{user_id}.png"
    plt.savefig(filename)
    plt.close()
    return filename

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
        # --- 修正開始 ---
        from db_manager import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        # PostgreSQL 使用 CURRENT_DATE
        cursor.execute("SELECT food_name FROM food_logs WHERE user_id = %s AND date::date = CURRENT_DATE", (user_id,))
        food_list = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        # --- 修正結束 ---
        
        food_str = "、".join(food_list) if food_list else "尚未記錄任何食物"
        today_total = get_today_total_calories(user_id)
        
        reply_text = (
            f"🥗 請告訴我你吃了什麼或做了什麼運動！\n"
            f"------------------\n"
            f"📝 今日已紀錄：{food_str}\n"
            f"📊 今日累計熱量：{today_total} kcal"
        )
    elif "我的報表" in user_message:
        data = get_user_data(user_id)
        if data:
            h, w, age, gender, phase, goal_calories, start_time, is_fasting = data
            
            # 給予預設值，防止變數未定義
            final_goal = 0
            source = "🤖 系統自動建議值"
            
            # 判斷目標熱量
            if goal_calories and goal_calories > 0:
                final_goal = goal_calories
                source = "🎯 您設定的每日目標"
            else:
                bmr = calculate_bmr(h, w, age or 25, gender or '女')
                phase_adj = {'減脂期': -300, '增肌期': 300, '維持期': 0}
                final_goal = (bmr * 1.2) + phase_adj.get(phase or '維持期', 0)
            
            bmi = w / ((h / 100) ** 2)
            
            reply_text = (
                f"📊 健康報表 ({phase or '維持期'})\n"
                f"👤 {gender or '女'} | {age or 25} 歲\n"
                f"📏 BMI: {bmi:.1f}\n"
                f"------------------\n"
                f"{source}: {final_goal:.0f} kcal"
            )
        else:
            reply_text = "尚未設定資料，請先輸入資訊。"

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
        path = generate_weight_chart(user_id)
        if path:
            chart_url = f"https://linebot-project-df3w.onrender.com/{path}"
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="📈 這是您的體重變化趨勢："), ImageMessage(originalContentUrl=chart_url, previewImageUrl=chart_url)]
                ))
            return 
        reply_text = "📈 體重紀錄不足，請多記錄幾次體重喔！"
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
    elif "幫助" in user_message:
        reply_text = (
            "👋 歡迎使用健康管家！我是您的專屬營養健身教練。\n"
            "不用擔心紀錄複雜，直接告訴我您的生活細節，我會幫您整理！\n\n"
            "【📊 數據與紀錄】\n"
            "• 初始化資料：「我的身高170，體重65，年齡25，我是女生，現在是減脂期」\n"
            "• 快速記錄體重：「體重是 65.5」\n"
            "• 查詢趨勢圖表：「體重趨勢」\n"
            "• 查看健康評估：「我的報表」\n\n"
            "【🥗 飲食與運動】\n"
            "• 紀錄吃喝與運動：「午餐吃雞胸肉沙拉300大卡，跑步30分鐘」\n"
            "• 查看今日總表：「今日紀錄」\n"
            "• 斷食管理：「開始斷食」、「斷食狀態」、「結束斷食」\n\n"
            "【💡 健康小百科】\n"
            "• 查看分類：「營養知識」 (進入後輸入：營養-增肌、營養-減脂...)\n\n"
            "🔔 **您的貼心秘書**：\n"
            "1. 每日 21:30：我會提醒您記錄體重，保持數據連續性。\n"
            "2. 每日 22:00：我會總結您今日的飲食與運動清單。\n"
            "3. 斷食期間：開始斷食後，我會在 16 小時後主動提醒您開餐！\n\n"
            "💡 小提示：若輸入格式有誤，請再完整敘述一次即可覆蓋更新資料喔！"
        )
        send_reply(event.reply_token, reply_text)
        return

    # 2. 如果不是上面的指令，就交給 AI 處理飲食/運動分析
    else:
        # 你的 Prompt 設定保持不變
        prompt = f"""
        你是一位營養與健身教練。請分析使用者的輸入，同時提取「飲食」與「運動」。
        請嚴格回傳以下 JSON 格式，不要包含 ```json 或任何額外說明：
        {{
            "food_summary": "飲食內容摘要，若無則填無",
            "total_calories": 數字,
            "exercise_summary": "運動內容摘要，若無則填無",
            "exercise_calories": 數字,
            "advice": "建議"
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
            if cal > 0:
                add_food_log(user_id, data.get('food_summary', '無'), cal)
            
            # --- 【修正】：使用 get_db_connection ---
            from db_manager import get_db_connection
            conn = get_db_connection()
            cursor = conn.cursor()
            # PostgreSQL 使用 CURRENT_DATE
            cursor.execute("SELECT food_name FROM food_logs WHERE user_id = %s AND date::date = CURRENT_DATE", (user_id,))
            food_list = [row[0] for row in cursor.fetchall()]
            cursor.close()
            conn.close()
            # ----------------------------------------
            
            food_str = "、".join(food_list) if food_list else "無"
            today_total = get_today_total_calories(user_id)

            reply_text = (
                f"收到！幫您記錄：{data.get('food_summary', '無')}\n\n"
                f"🔥 本次熱量：{cal} kcal\n"
                f"📊 今日累計攝取：{today_total} kcal\n"
                f"📝 今日飲食清單：{food_str}\n\n"
                f"💡 營養師小叮嚀：{data.get('advice', '維持健康生活！')}"
            )
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
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)