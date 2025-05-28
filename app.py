import os
from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta
import hashlib
import json
import random
import uuid
import psycopg2 # 用於連接PostgreSQL資料庫
from psycopg2 import extras # 用於字典遊標

app = Flask(__name__)

# --- 環境變量配置 ---
# Render 會自動將 DATABASE_URL 注入到環境變量中
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    # 僅用於本地測試，實際部署時 Render 會提供 DATABASE_URL
    print("警告：DATABASE_URL 環境變量未設置。請在 Render 上配置。")
    # 可以設置一個本地的 PostgreSQL 連接字符串用於測試
    # DATABASE_URL = "postgresql://user:password@localhost:5432/mydatabase"

# --- 資料庫連接輔助函數 ---
def get_db_connection():
    """獲取資料庫連接和字典遊標"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        # 使用 RealDictCursor 讓查詢結果以字典形式返回
        cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
        return cursor, conn
    except Exception as e:
        print(f"資料庫連接失敗：{e}")
        return None, None

def close_db_connection(cursor, conn):
    """關閉資料庫連接"""
    if cursor:
        cursor.close()
    if conn:
        conn.close()

# --- 密碼雜湊輔助函數 ---
def hash_password(password):
    """使用 SHA256 雜湊密碼"""
    return hashlib.sha256(password.encode()).hexdigest()

# --- 資料庫操作邏輯 ---
def create_users_table_if_not_exists():
    """檢查並創建 users 表"""
    cursor, conn = get_db_connection()
    if not cursor or not conn:
        return False

    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                last_logout_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                game_data JSONB DEFAULT '{}'
            );
        """)
        conn.commit()
        print("users 表已檢查/創建。")
        return True
    except Exception as e:
        print(f"創建 users 表失敗：{e}")
        return False
    finally:
        close_db_connection(cursor, conn)

def save_user_data_to_db(username, password_hash, last_logout_time, game_data):
    """保存或更新用戶數據"""
    cursor, conn = get_db_connection()
    if not cursor or not conn:
        return False

    try:
        cursor.execute("""
            INSERT INTO users (username, password_hash, last_logout_time, game_data)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (username) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                last_logout_time = EXCLUDED.last_logout_time,
                game_data = EXCLUDED.game_data;
        """, (username, password_hash, last_logout_time, json.dumps(game_data)))
        conn.commit()
        return True
    except Exception as e:
        print(f"保存用戶數據失敗：{e}")
        return False
    finally:
        close_db_connection(cursor, conn)

def get_user_data_from_db(username):
    """從資料庫獲取用戶數據"""
    cursor, conn = get_db_connection()
    if not cursor or not conn:
        return None

    try:
        cursor.execute("SELECT id, username, password_hash, last_logout_time, game_data FROM users WHERE username = %s", (username,))
        user_data = cursor.fetchone()
        return user_data
    except Exception as e:
        print(f"獲取用戶數據失敗：{e}")
        return None
    finally:
        close_db_connection(cursor, conn)

def get_area_data_from_db(name):
    """從資料庫獲取遊戲數據"""
    cursor, conn = get_db_connection()
    if not cursor or not conn:
        return None
    try:
        cursor.execute("SELECT id, areaname, time, cost, product FROM areas_detail WHERE areaname = %s", (name,))
        area_data = cursor.fetchone()
        return area_data
    except Exception as e:
        print(f"獲取區域數據失敗: {e}")
        return None
    finally:
        close_db_connection(cursor, conn)


# --- 核心邏輯：計算離線收益、處理排程動作、更新遊戲數據 ---
def calculate_and_update_game_data(user_data: dict) -> dict:
    current_time = datetime.now(timezone.utc)
    last_action_time = user_data['last_logout_time'] # 假設這是上次伺服器更新或登出的時間

    if last_action_time and last_action_time.tzinfo is None:
        last_action_time = last_action_time.replace(tzinfo=timezone.utc)
    
    game_data = user_data.get('game_data', {})
    if not isinstance(game_data, dict):
        game_data = {}

    time_elapsed_seconds = 0
    if last_action_time:
        time_elapsed_seconds = (current_time - last_action_time).total_seconds()
        if time_elapsed_seconds < 0:
            time_elapsed_seconds = 0

    print(f"上次動作時間: {last_action_time}, 當前時間: {current_time}, 經過時長: {time_elapsed_seconds:.2f}秒")

    # 確保 resources 字典存在
    game_data.setdefault('resources', {})
    game_data.setdefault('scheduled_actions', []) # 確保行動序列存在

    # TODO: 加入其他被動收益計算 (例如農場產量等)

    # --- 1. 處理已完成的排程動作 (從序列中移除並給予收益) ---
    completed_actions_count = 0
    new_scheduled_actions = []
    
    # 遍歷行動序列（需要從第一個開始處理，因為它是「當前執行」的）
    for i, action in enumerate(list(game_data['scheduled_actions'])): # 迭代副本，避免修改時出錯
        action_end_time_timestamp = action.get('end_time')
        if action_end_time_timestamp:
            action_end_time = datetime.fromtimestamp(action_end_time_timestamp, tz=timezone.utc)
            
            if current_time >= action_end_time:
                # 動作已完成，執行完成邏輯
                print(f"處理完成動作: {action.get('type')}, ID: {action.get('action_id')}")
                completed_actions_count += 1

                # 根據 action_type 處理不同類型行動的完成獎勵/結果
                if action.get('type') == 'mining':
                    mined_resource_type = action.get('resource_type')
                    mined_amount = action.get('amount', 0)
                    game_data['resources'][mined_resource_type] = game_data['resources'].get(mined_resource_type, 0) + mined_amount
                    print(f"獲得 {mined_amount} {mined_resource_type}")
                    
                elif action.get('type') == 'farming':
                    crop_type = action.get('crop_type')
                    farmed_amount = action.get('amount', 0)
                    game_data['resources'][crop_type] = game_data['resources'].get(crop_type, 0) + farmed_amount
                    print(f"獲得 {farmed_amount} {crop_type}")
                    
                elif action.get('type') == 'animal_husbandry':
                    animal_type = action.get('animal_type')
                    product_type = action.get('product_type')
                    product_amount = action.get('amount', 0)
                    game_data['resources'][product_type] = game_data['resources'].get(product_type, 0) + product_amount
                    print(f"獲得 {product_amount} {product_type} (from {animal_type})")
                    
                elif action.get('type') == 'building_upgrade':
                    building_id = action.get('building_id')
                    target_level = action.get('target_level')
                    # 確保建築字典和建築本身存在
                    if 'buildings' not in game_data:
                        game_data['buildings'] = {}
                    if building_id not in game_data['buildings']:
                        game_data['buildings'][building_id] = {}
                    game_data['buildings'][building_id]['level'] = target_level
                    print(f"建築 {building_id} 升級到 {target_level} 級完成！")
                # TODO: 處理其他行動類型完成後的結果

            else:
                # 動作未完成，保留在列表中
                new_scheduled_actions.append(action)
        else:
            # 如果沒有結束時間，也保留它 (或者根據你的遊戲規則處理不完整的行動定義)
            new_scheduled_actions.append(action)
            
    game_data['scheduled_actions'] = new_scheduled_actions # 更新序列

    # 返回更新後的數據
    user_data['game_data'] = game_data
    user_data['last_logout_time'] = current_time # 將上次更新時間設定為當前伺服器時間

    return user_data

# --- API 路由 ---

@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"message": "Username and password are required"}), 400

    if get_user_data_from_db(username):
        return jsonify({"message": "User already exists"}), 409

    hashed_pass = hash_password(password)
    # 新用戶首次註冊，遊戲數據為空字典，上次登出時間為當前時間
    success = save_user_data_to_db(username, hashed_pass, datetime.now(), {})
    if success:
        return jsonify({"message": "Registration successful"}), 201
    else:
        return jsonify({"message": "Registration failed, please try again"}), 500

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"message": "Username and password are required"}), 400

    user_data = get_user_data_from_db(username)

    if user_data and user_data['password_hash'] == hash_password(password):
        last_logout_time = user_data.get('last_logout_time')
        game_data = user_data.get('game_data', {})

        offline_income = {"gold": 0} # 預設離線收益
        if last_logout_time:
            current_time = datetime.now(last_logout_time.tzinfo) # 確保時區一致
            offline_duration_seconds = (current_time - last_logout_time).total_seconds()

            # --- 離線收益計算邏輯 (伺服器端計算) ---
            # 這裡可以根據你的遊戲數據 (game_data) 來計算更複雜的收益
            # 假設每秒產生1個金幣，且有建築等級可以提升收益
            # 簡單示例：每秒1個基礎金幣 + 建築等級 * 0.5
            building_level = game_data.get('buildings', {}).get('gold_mine_level', 0)
            income_per_second = 1 + (building_level * 0.5)

            offline_gold = int(offline_duration_seconds * income_per_second)
            offline_income = {"gold": offline_gold}
            # --- 離線收益計算邏輯結束 ---

            # 更新資料庫中的 last_logout_time 為當前時間
            save_user_data_to_db(username, user_data['password_hash'], current_time, game_data)

        return jsonify({
            "message": "Login successful",
            "user_id": user_data['id'], # 返回實際的用戶ID
            "username": user_data['username'],
            "offline_income": offline_income,
            "game_data": game_data
        }), 200
    else:
        return jsonify({"message": "Invalid username or password"}), 401

@app.route('/logout', methods=['POST'])
def logout():
    data = request.get_json()
    username = data.get('username') # 使用 username 作為標識
    game_data = data.get('game_data')

    if not username or not game_data:
        return jsonify({"message": "Username and game data are required"}), 400

    user_data = get_user_data_from_db(username)
    if user_data:
        # 保存遊戲數據和最新的登出時間
        success = save_user_data_to_db(username, user_data['password_hash'], datetime.now(), game_data)
        if success:
            return jsonify({"message": "Logout successful, data saved"}), 200
        else:
            return jsonify({"message": "Failed to save data on logout"}), 500
    else:
        return jsonify({"message": "User not found"}), 404

@app.route('/save_game', methods=['POST'])
def save_game():
    data = request.get_json()
    username = data.get('username')
    game_data = data.get('game_data')

    if not username or not game_data:
        return jsonify({"message": "Username and game data are required"}), 400

    user_data = get_user_data_from_db(username)
    if user_data:
        # 只更新 game_data，不改變 last_logout_time
        success = save_user_data_to_db(username, user_data['password_hash'], user_data['last_logout_time'], game_data)
        if success:
            return jsonify({"message": "Game data saved"}), 200
        else:
            return jsonify({"message": "Failed to save game data"}), 500
    else:
        return jsonify({"message": "User not found"}), 404

@app.route('/add_actions_to_queue', methods=['POST'])
def add_actions_to_queue():
    data = request.get_json()
    username = data.get('username')
    user_id = data.get('user_id')
    action_name = data.get('action_name')
    quantity = data.get('quantity')
    specific_params = {k: v for k, v in data.items() if k not in ['username', 'user_id', 'action_name', 'quantity']} # 獲取其他特定參數

    if not all([username, user_id, action_name, isinstance(quantity, int) and quantity > 0]):
        return jsonify({"message": "缺少必要參數或數量無效"}), 400

    user_data = get_user_data_from_db(username)
    if not user_data or user_data['id'] != user_id:
        return jsonify({"message": "用戶驗證失敗"}), 401

    # **最重要：在執行任何新動作前，先計算並更新玩家的所有過往數據**
    # 這處理了所有離線收益和先前已完成的排程任務
    user_data = calculate_and_update_game_data(user_data)
    game_data = user_data['game_data']

    max_queue_size = 3 # 假設行動序列最大長度
    messages = []
    area_data = get_area_data_from_db(action_name)

    # 遍歷並嘗試添加指定數量的行動

    # 1. 檢查序列是否已滿
    if len(game_data.get('scheduled_actions', [])) >= max_queue_size:
        return jsonify({"message": "行動序列已滿，無法加入新的行動。"}), 500
        
    # 定義新行動的開始時間和結束時間
    # 如果序列是空的，新行動立刻開始
    # 否則，新行動從前一個行動結束的時間開始
    last_action_end_time_timestamp = int(datetime.now(timezone.utc).timestamp()) # 預設為當前時間
    if game_data.get('scheduled_actions'):
        last_action_end_time_timestamp = game_data['scheduled_actions'][-1].get('end_time', last_action_end_time_timestamp)
        
    action_start_time = datetime.fromtimestamp(last_action_end_time_timestamp, tz=timezone.utc)
        
    # 計算實際行動持續時間和預期產量
    actual_duration_seconds = area_data.get('time', 60) * quantity            
    action_end_time = action_start_time + timedelta(seconds=actual_duration_seconds)
        
    new_action_entry = {
        "action_id": str(uuid.uuid4()), # 給每個行動一個唯一ID，方便客戶端追踪
        "name": action_name,
        "start_time": int(action_start_time.timestamp()),
        "end_time": int(action_end_time.timestamp()),
        "duration": actual_duration_seconds, # 實際花費時間
        "status": "pending", # 可以是 pending, executing, completed
        **specific_params # 添加行動特有參數
    }
    game_data['scheduled_actions'].append(new_action_entry)
    messages.append(f"成功將 {action_name} 加入序列。")

    # 保存更新後的遊戲數據
    success = save_user_data_to_db(username, user_data['password_hash'], user_data['last_logout_time'], game_data)

    if success:
        # 準備返回給客戶端的響應數據
        current_executing_action = None
        if game_data.get('scheduled_actions'):
            # 第一個行動是正在執行的行動
            current_executing_action = game_data['scheduled_actions'][0]
        
        response_message = "。\n".join(messages) if messages else "行動序列已更新。"

        return jsonify({
            "message": response_message,
            "user_id": user_id,
            "username": username,
            "added_count": quantity,
            "action_name": action_name, # 返回請求的行動名稱
            "total_in_queue": len(game_data['scheduled_actions']), # 返回序列總長度
            "current_executing_action": current_executing_action, # 返回當前執行中的行動資訊
            "game_data": game_data # 返回最新的完整遊戲數據
        }), 200
    else:
        return jsonify({"message": "保存數據失敗，請重試"}), 500

# 啟動時創建表
with app.app_context():
    create_users_table_if_not_exists()

if __name__ == '__main__':
    # 僅在本地開發時運行，Render會使用 gunicorn 運行
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
