from flask import Flask, request, jsonify

app = Flask(__name__)

# 暫時把玩家資料存在記憶體（日後可接資料庫）
player_data = {}

@app.route('/')
def home():
    return 'Idle Game Server is Running!'

@app.route('/update', methods=['POST'])
def update_data():
    content = request.json
    user_id = content.get('userId')
    data = content.get('data')
    if not user_id or not data:
        return jsonify({'error': 'Missing userId or data'}), 400
    player_data[user_id] = data
    return jsonify({'status': 'ok'})

@app.route('/player/<user_id>', methods=['GET'])
def get_player(user_id):
    data = player_data.get(user_id)
    if data:
        return jsonify(data)
    else:
        return jsonify({'error': 'Player not found'}), 404

if __name__ == '__main__':
    app.run(debug=True)
