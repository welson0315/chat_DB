import os
import re
import json
import uuid
from datetime import datetime

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

app = Flask(__name__)

# 🔌 初始化 Socket.IO（使用 eventlet 異步模式）
# - cors_allowed_origins="*"：允許跨網域，方便本地/雲端測試；正式環境可改成特定網域以提升安全性
# - async_mode="eventlet"：配合 eventlet 的協程模型（高併發友善）
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# === 根路由 ===
@app.route("/")
def index():
    # 回傳首頁樣板（templates/index.html）
    return render_template("index.html")

# === 聊天歷史設定 ===
MAX_HISTORY = 100                      # 記憶體/檔案最多保留的訊息數（避免檔案無限成長）
HISTORY_DIR = "chat_history"           # 歷史記錄資料夾
HISTORY_FILE = os.path.join(HISTORY_DIR, "messages.json")  # JSON 落盤位置
os.makedirs(HISTORY_DIR, exist_ok=True)                    # 沒資料夾就建立

# 根據 async_mode 選用正確的鎖
# 為什麼？eventlet 與傳統 threading.Lock 的行為不同，搭錯可能導致死鎖或阻塞
if socketio.async_mode == "eventlet":
    from eventlet.semaphore import Semaphore
    _history_lock = Semaphore(1)  # eventlet 的輕量鎖（合作式排程友善）
else:
    import threading
    _history_lock = threading.Lock()  # 傳統執行緒鎖（適用 gevent/threading）

chat_history = []  # in-memory 緩存：加速讀取 /get_history，並作為寫入檔案的來源

def _load_chat_history():
    """
    啟動時載入歷史檔案到記憶體：
    - 若檔案存在且為 list：取最後 MAX_HISTORY 筆
    - 任何讀取/解析錯誤都不讓系統掛掉，改為使用空陣列
    """
    global chat_history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                chat_history = data[-MAX_HISTORY:]
            else:
                chat_history = []
        except Exception as e:
            print(f"[history] 讀取失敗：{e}")
            chat_history = []
    else:
        chat_history = []

def _save_chat_history():
    """
    將記憶體的 chat_history 寫回磁碟（呼叫端需自行持鎖）
    - ensure_ascii=False：保持中文等非 ASCII 字元
    - indent=2：方便人工檢視
    - 任何寫入錯誤只記錄，不中斷服務（避免在高流量時被檔案 I/O 拖垮）
    """
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(chat_history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[history] 寫入失敗：{e}")

# 服務啟動載一次歷史（讓前端一開頁就能看到舊訊息）
_load_chat_history()

# === 使用者連線狀態 ===
# 用字典記住每個連線（sid）對應的使用者名稱。例：{"<sid>": {"username": "小明"}}
clients = {}

def broadcast_user_count():
    """
    計算有設定 username 的連線數量並廣播給所有人
    - 只算「已設定名稱」的使用者，避免把剛連線尚未 join 的人計入
    """
    emit(
        "user_count",
        {"count": len([c for c in clients.values() if c["username"]])},
        broadcast=True,
    )

@socketio.on("connect")
def on_connect():
    """
    新連線建立：
    - 先把該 SID 註冊起來，username 暫時為 None
    - 前端通常會立刻送 "join" 事件補上名稱
    """
    clients[request.sid] = {"username": None}
    print("Client connect:", request.sid)

@socketio.on("disconnect")
def on_disconnect():
    """
    連線關閉：
    - 從 clients 刪除該 SID
    - 若該連線曾設定過 username，廣播「user_left」並更新線上人數
    """
    info = clients.pop(request.sid, None)
    if info and info["username"]:
        emit("user_left", {"username": info["username"]}, broadcast=True)
        broadcast_user_count()
    print("Client disconnect:", request.sid)

@socketio.on("join")
def on_join(data):
    """
    使用者宣告加入：
    - 設定該 SID 的 username（若未提供則用「匿名」）
    - 廣播「user_joined」給所有人
    - 更新線上人數
    """
    username = data.get("username", "匿名")
    clients[request.sid]["username"] = username
    emit("user_joined", {"username": username}, broadcast=True)
    broadcast_user_count()
    print(username, "joined")

@socketio.on("typing")
def on_typing(data):
    """
    正在輸入指示：
    - 廣播給「其他人」（include_self=False），避免自己也看到自己在輸入
    """
    emit("typing", data, broadcast=True, include_self=False)

@socketio.on("change_username")
def on_change(data):
    """
    變更使用者名稱：
    - 更新 server 端的暱稱記錄
    - 廣播變更事件（讓 UI 顯示「A 更名為 B」）
    """
    old = data.get("oldUsername")
    new = data.get("newUsername")
    if request.sid in clients:
        clients[request.sid]["username"] = new
    emit("user_changed_name", {"oldUsername": old, "newUsername": new}, broadcast=True)

# === 文字訊息主流程：寫入歷史 → 廣播 ===
@socketio.on("send_message")
def on_message(data):
    """
    收到前端送來的訊息：
    1) 取得目前使用者名稱（優先取 server 端 SID 綁定的名稱，其次 data.username）
    2) 清理訊息（去掉你舊格式的前綴）
    3) 組成標準訊息物件（含 id/username/content/timestamp）
    4) 持鎖：append 到記憶體 & 落盤到 JSON（保持臨界區短小）
    5) 廣播給其他使用者（不含自己）
    """
    try:
        username = (clients.get(request.sid, {}) or {}).get("username") or data.get("username") or "匿名"
        raw_content = str(data.get("content", "")).strip()

        # 移除舊格式（若訊息前面曾經自動帶 "user name is ...\ncontent is ..."）
        cleaned_content = re.sub(r"user name is .*?\ncontent is ", "", raw_content, flags=re.IGNORECASE)

        message = {
            "id": str(uuid.uuid4()),                                      # 產生一個訊息 UUID（前端可做 key）
            "username": username,
            "content": cleaned_content,
            "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",  # 用 UTC ISO 字串（Z）
        }

        # ✅ 臨界區：同時更新記憶體與檔案（避免多工下的競爭條件）
        with _history_lock:
            chat_history.append(message)
            # 控制上限：超過 MAX_HISTORY 就從頭刪除多出來的數量
            if len(chat_history) > MAX_HISTORY:
                del chat_history[0 : len(chat_history) - MAX_HISTORY]
            _save_chat_history()  # 落盤（I/O 可能稍慢，所以務必保持臨界區短小）

        # 把這則訊息廣播給「其他人」
        emit("chat_message", message, broadcast=True, include_self=False)

    except Exception as e:
        # 任一環節出錯，只通知「這位送訊息的人」，不影響其他連線
        emit("chat_error", {"message": f"訊息處理失敗：{e}"}, to=request.sid)

# === 歷史 API：提供前端載入/清空 ===
@app.route("/get_history", methods=["GET"])
def get_history():
    """
    回傳目前記憶體的歷史訊息（已經是裁切後的最多 MAX_HISTORY 筆）
    - 這裡直接給記憶體陣列，避免每次都讀檔造成 I/O 壓力
    - 若要確保一致性，也可在此加鎖，但讀取陣列通常是安全的（Python list append/切片具原子性）
    """
    return jsonify(chat_history)

@app.route("/clear_history", methods=["POST"])
def clear_history():
    """
    清空歷史：
    1) 先把記憶體變數 chat_history 清空
    2) 若檔案存在就刪除
    3) 任何刪除錯誤回 500，成功回 success
    """
    global chat_history
    with _history_lock:
        chat_history = []
        try:
            if os.path.exists(HISTORY_FILE):
                os.remove(HISTORY_FILE)
        except Exception as e:
            return jsonify({"status": "error", "message": f"刪除檔案失敗: {e}"}), 500
    return jsonify({"status": "success", "message": "歷史紀錄已清除"})

if __name__ == "__main__":
    # eventlet 模式建議已安裝 eventlet（pip install eventlet）
    # 🔊 注意：雲端（像 Render）要吃環境變數 $PORT，建議改成：
    port = int(os.getenv("PORT", 5000))
    # 並可加上：os.environ.setdefault("EVENTLET_NO_GREENDNS", "1") 避免 DNS/SSL 衝突
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
