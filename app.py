import time
import json
import requests
import threading
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

DATA_FILE = "players.json"
CONFIG_FILE = "config.json"
CHECK_INTERVAL = 60
VALID_PLATFORMS = {"PC", "X1", "PS4", "SWITCH"}

# 翻译 currentStateAsText
STATE_TEXT_MAP = {
    "offline": "离线",
    "inLobby": "在大厅",
    "inMatch": "游戏中",
    "partyLobby": "在队伍大厅",
    "away": "暂时离开",
}

# 加载配置
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

TELEGRAM_BOT_TOKEN = config["TELEGRAM_BOT_TOKEN"]
APEX_API_KEY = config["APEX_API_KEY"]
ALLOWED_USERS = set(config.get("ALLOWED_USERNAMES", []))

# 初始化数据
def load_data():
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"players": {}, "chat_id": None}

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

data = load_data()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def telegram_send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})

def telegram_get_updates(offset=None, timeout=10):
    params = {"timeout": timeout}
    if offset:
        params["offset"] = offset
    return requests.get(f"{TELEGRAM_API}/getUpdates", params=params).json()

def format_duration(ts):
    if ts < 0:
        return "未知"
    now = datetime.now(timezone.utc).timestamp()
    diff = int(now - ts)
    mins, secs = divmod(diff, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}小时{mins}分钟"
    elif mins > 0:
        return f"{mins}分钟"
    else:
        return f"{secs}秒"

def fetch_player_status(player, platform):
    player_enc = urllib.parse.quote(player)
    url = f"https://api.mozambiquehe.re/bridge?auth={APEX_API_KEY}&player={player_enc}&platform={platform}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        j = r.json()
        rt = j.get("realtime", {})
        return {
            "currentState": rt.get("currentState"),
            "currentStateAsText": rt.get("currentStateAsText"),
            "currentStateSince": format_duration(rt.get("currentStateSinceTimestamp", -1)),
        }
    except:
        return None

def is_authorized(username):
    return username and username.lower() in {u.lower() for u in ALLOWED_USERS}

def handle_command(message):
    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    username = user.get("username", "")
    text = message.get("text", "")

    if not is_authorized(username):
        telegram_send_message(chat_id, "未经授权的用户。")
        return

    parts = text.strip().split()
    if not parts:
        return

    cmd = parts[0].lower()
    args = parts[1:]

    if cmd == "/start":
        data["chat_id"] = chat_id
        save_data()
        telegram_send_message(chat_id, "欢迎！使用 /add <玩家名> <平台> 添加监控，例如:\n/add iTzTimmy PC")

    elif cmd == "/help":
        help_text = (
            "Apex 玩家监控机器人使用说明：\n\n"
            "/start - 初始化聊天\n"
            "/add <玩家名> <平台> - 添加监控（平台: PC/X1/PS4/SWITCH）\n"
            "/remove <玩家名> - 移除玩家\n"
            "/list - 当前监控列表\n"
            "/notify <玩家名> on|off - 开关上线通知\n"
            "/status <玩家名> - 查询当前状态\n"
            "/help - 显示本帮助信息\n\n"
            "注意：玩家名区分大小写，必须为 EA ID，不是 Steam 名。支持中日文。"
        )
        telegram_send_message(chat_id, help_text)

    elif cmd == "/add":
        if len(args) != 2:
            telegram_send_message(chat_id, "用法: /add <玩家名> <平台>")
            return
        player, platform = args[0], args[1].upper()
        if platform not in VALID_PLATFORMS:
            telegram_send_message(chat_id, f"平台必须是: {', '.join(VALID_PLATFORMS)}")
            return
        key = player.lower()
        data["players"][key] = {
            "platform": platform,
            "notify": True,
            "last_state": None,
            "original_name": player,
        }
        save_data()
        telegram_send_message(chat_id, f"已添加监控: {player} ({platform})")

    elif cmd == "/remove":
        if len(args) != 1:
            telegram_send_message(chat_id, "用法: /remove <玩家名>")
            return
        key = args[0].lower()
        if key in data["players"]:
            del data["players"][key]
            save_data()
            telegram_send_message(chat_id, "已移除该玩家。")
        else:
            telegram_send_message(chat_id, "未找到该玩家。")

    elif cmd == "/list":
        if not data["players"]:
            telegram_send_message(chat_id, "没有监控的玩家。")
            return
        lines = [f"{info['original_name']} ({info['platform']}) - 通知: {'开' if info['notify'] else '关'}"
                 for info in data["players"].values()]
        telegram_send_message(chat_id, "监控列表：\n" + "\n".join(lines))

    elif cmd == "/notify":
        if len(args) != 2 or args[1].lower() not in {"on", "off"}:
            telegram_send_message(chat_id, "用法: /notify <玩家名> on|off")
            return
        key = args[0].lower()
        if key not in data["players"]:
            telegram_send_message(chat_id, "未找到该玩家。")
            return
        data["players"][key]["notify"] = args[1].lower() == "on"
        save_data()
        telegram_send_message(chat_id, f"已更新通知设置: {args[1].lower()}")

    elif cmd == "/status":
        if len(args) != 1:
            telegram_send_message(chat_id, "用法: /status <玩家名>")
            return
        key = args[0].lower()
        if key not in data["players"]:
            telegram_send_message(chat_id, "未找到该玩家，请先 /add")
            return
        info = data["players"][key]
        status = fetch_player_status(info["original_name"], info["platform"])
        if not status:
            telegram_send_message(chat_id, "获取状态失败，请稍后再试。")
            return
        state = status["currentState"]
        state_text = STATE_TEXT_MAP.get(state, status["currentStateAsText"])
        duration = status["currentStateSince"]
        msg = f"{info['original_name']} 当前状态: \n🟢 {state_text} ({state})\n已持续时间: {duration}"
        telegram_send_message(chat_id, msg)

    else:
        telegram_send_message(chat_id, "未知命令。使用 /help 查看所有命令。")

# 后台监控
def monitor_loop():
    while True:
        chat_id = data.get("chat_id")
        if not chat_id:
            time.sleep(5)
            continue
        for key, info in data["players"].items():
            if not info.get("notify"):
                continue
            status = fetch_player_status(info["original_name"], info["platform"])
            if not status:
                continue
            state = status["currentState"]
            if state != info.get("last_state"):
                info["last_state"] = state
                save_data()
                state_text = STATE_TEXT_MAP.get(state, status["currentStateAsText"])
                duration = status["currentStateSince"]
                msg = f"{info['original_name']} 状态更新:\n🟢 {state_text} ({state})\n已持续时间: {duration}"
                telegram_send_message(chat_id, msg)
        time.sleep(CHECK_INTERVAL)

def main_loop():
    offset = None
    while True:
        updates = telegram_get_updates(offset=offset)
        if not updates.get("ok"):
            time.sleep(2)
            continue
        for u in updates.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message")
            if msg:
                handle_command(msg)

if __name__ == "__main__":
    threading.Thread(target=monitor_loop, daemon=True).start()
    main_loop()
