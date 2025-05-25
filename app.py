import os
from flask import Flask, request, jsonify
from datetime import datetime
import hashlib
import json
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

# 啟動時創建表
with app.app_context():
    create_users_table_if_not_exists()

if __name__ == '__main__':
    # 僅在本地開發時運行，Render會使用 gunicorn 運行
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)