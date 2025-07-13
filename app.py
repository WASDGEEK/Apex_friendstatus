import time
import json
import requests
import threading
import urllib.parse
import re
from pathlib import Path
from datetime import datetime, timezone

# ---------- 配置 & 常量 ----------
DATA_FILE       = "players.json"
CONFIG_FILE     = "config.json"
CHECK_INTERVAL  = 60
VALID_PLATFORMS = ["PC", "X1", "PS4", "SWITCH"]

# 按钮总字符宽度（含填充）
BUTTON_WIDTH    = 30
# 用“韩文占位符”做填充，Telegram iOS 不会删它
FILLER          = "\u3164"

STATE_TEXT_MAP = {
    "offline":     "离线",
    "inLobby":     "在大厅",
    "inMatch":     "游戏中",
    "partyLobby":  "队伍大厅",
    "away":        "暂时离开",
}

# ---------- 读取配置 ----------
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    cfg = json.load(f)
TELEGRAM_BOT_TOKEN = cfg["TELEGRAM_BOT_TOKEN"]
APEX_API_KEY       = cfg["APEX_API_KEY"]
ALLOWED_USERS      = set(cfg.get("ALLOWED_USERNAMES", []))
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ---------- 本地数据载入 ----------
data = {"players": {}, "chat_id": None, "adding_player": {}}
if Path(DATA_FILE).exists():
    data.update(json.loads(Path(DATA_FILE).read_text(encoding="utf-8")))

def save_data():
    Path(DATA_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- MarkdownV2 转义 ----------
def md_v2_escape(text: str) -> str:
    return re.sub(r'([_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!])', r'\\\1', text)

# ---------- Telegram API Helpers ----------
def telegram_send_message(chat_id, text, reply_markup=None):
    txt = md_v2_escape(text)
    payload = {"chat_id": chat_id, "text": txt, "parse_mode": "MarkdownV2"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)
    print("sendMessage →", resp.status_code, resp.text)

def telegram_edit_message(chat_id, msg_id, text, reply_markup=None):
    txt = md_v2_escape(text)
    payload = {"chat_id": chat_id, "message_id": msg_id, "text": txt, "parse_mode": "MarkdownV2"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    resp = requests.post(f"{TELEGRAM_API}/editMessageText", json=payload)
    print("editMessageText →", resp.status_code, resp.text)

def telegram_answer_callback(callback_id):
    resp = requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_id})
    print("answerCallbackQuery →", resp.status_code, resp.text)

def telegram_get_updates(offset=None, timeout=10):
    params = {"timeout": timeout}
    if offset: params["offset"] = offset
    return requests.get(f"{TELEGRAM_API}/getUpdates", params=params).json()

# ---------- 工具函数 ----------
def is_authorized(username):
    return username and username.lower() in {u.lower() for u in ALLOWED_USERS}

def format_duration(ts):
    if ts is None or ts < 0:
        return "未知"
    diff = int(datetime.now(timezone.utc).timestamp() - ts)
    h, rem = divmod(diff, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分钟"
    if m:
        return f"{m}分钟"
    return f"{s}秒"

def fetch_player_status(player, platform):
    url = (
        "https://api.mozambiquehe.re/bridge"
        f"?auth={APEX_API_KEY}&player={urllib.parse.quote(player)}&platform={platform}"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        rt = r.json().get("realtime", {})
        return {
            "currentState": rt.get("currentState"),
            "currentStateAsText": rt.get("currentStateAsText"),
            "currentStateSince": format_duration(rt.get("currentStateSinceTimestamp", -1)),
        }
    except:
        return None

# ---------- 按钮排版 & 菜单 ----------
def make_button(text, callback_data):
    # 根据 BUTTON_WIDTH 及 text 长度左右居中填充 FILLER
    pad_total = max(BUTTON_WIDTH - len(text), 0)
    left = pad_total // 2
    right = pad_total - left
    label = FILLER * left + text + FILLER * right
    return {"text": label, "callback_data": callback_data}

def get_main_menu():
    return {"inline_keyboard": [
        [make_button("➕ 添加新玩家", "add_start")],
        [make_button("📋 查看玩家列表", "list")],
        [make_button("❌ 取消操作", "cancel")],
    ]}

def get_platform_selection_menu(player):
    kb = [[make_button(f"{p} 平台", f"add_platform|{player}|{p}")] for p in VALID_PLATFORMS]
    kb.append([make_button("❌ 取消添加", "cancel")])
    kb.append([make_button("🏠 返回主菜单", "menu")])
    return {"inline_keyboard": kb}

def get_player_list_menu():
    kb = []
    if not data["players"]:
        kb.append([make_button("暂无监控玩家", "none")])
    else:
        for k, info in data["players"].items():
            kb.append([make_button(f"{info['original_name']}  ({info['platform']})", f"player|{k}")])
    kb.append([make_button("🏠 返回主菜单", "menu")])
    return {"inline_keyboard": kb}

def get_player_action_menu(key):
    info = data["players"][key]
    notify_label = "🔔 通知开启" if info["notify"] else "🔕 通知关闭"
    return {"inline_keyboard": [
        [make_button("🛰 查询当前状态",      f"status|{key}")],
        [make_button(notify_label,        f"toggle_notify|{key}")],
        [make_button("🗑 移除该玩家",     f"remove|{key}")],
        [make_button("🔙 返回玩家列表",   "list")],
        [make_button("🏠 返回主菜单",     "menu")],
    ]}

# ---------- 消息 & 回调处理 ----------
def handle_message(msg):
    cid  = msg["chat"]["id"]
    user = msg.get("from", {}).get("username", "")
    text = msg.get("text", "").strip()

    if not is_authorized(user):
        telegram_send_message(cid, "🚫 未授权用户。")
        return

    data["chat_id"] = cid
    save_data()

    if cid in data.get("adding_player", {}):
        data["adding_player"][cid] = text
        save_data()
        telegram_send_message(
            cid,
            f"已收到玩家名：{text}\n请选择该玩家的游戏平台：",
            get_platform_selection_menu(text)
        )
    else:
        telegram_send_message(
            cid,
            "欢迎使用 Apex 状态监控机器人！请选择功能：",
            get_main_menu()
        )

def handle_callback(cb):
    query = cb["data"]
    cid   = cb["message"]["chat"]["id"]
    mid   = cb["message"]["message_id"]
    cbid  = cb["id"]

    telegram_answer_callback(cbid)

    if query == "menu":
        telegram_edit_message(cid, mid, "主菜单 - 请选择功能：", get_main_menu())

    elif query == "add_start":
        telegram_edit_message(cid, mid,
            "请输入想要添加的玩家名（支持中/英/大小写）：\n\n"
            "输入后将选择游戏平台。"
        )
        data.setdefault("adding_player", {})[cid] = None
        save_data()

    elif query.startswith("add_platform|"):
        _, name, pf = query.split("|", 2)
        pf = pf.upper()
        if pf not in VALID_PLATFORMS:
            telegram_edit_message(cid, mid,
                f"选择的平台无效：{pf}，请重试。",
                get_platform_selection_menu(name)
            )
        else:
            key = name.lower()
            data["players"][key] = {
                "platform": pf, "notify": True,
                "last_state": None, "original_name": name
            }
            data["adding_player"].pop(cid, None)
            save_data()
            telegram_edit_message(cid, mid,
                f"✅ 成功添加玩家：{name} （{pf}）",
                get_main_menu()
            )

    elif query == "cancel":
        data["adding_player"].pop(cid, None)
        save_data()
        telegram_edit_message(cid, mid, "已取消操作。", get_main_menu())

    elif query == "list":
        telegram_edit_message(cid, mid, "当前监控玩家列表：", get_player_list_menu())

    elif query.startswith("player|"):
        key = query.split("|",1)[1]
        if key in data["players"]:
            info = data["players"][key]
            telegram_edit_message(
                cid, mid,
                f"玩家详情：{info['original_name']} （{info['platform']})\n请选择操作：",
                get_player_action_menu(key)
            )
        else:
            telegram_edit_message(cid, mid, "未找到该玩家。", get_player_list_menu())

    elif query.startswith("status|"):
        key = query.split("|",1)[1]
        info = data["players"].get(key)
        if info:
            st = fetch_player_status(info["original_name"], info["platform"])
            if st:
                txt = (
                    f"玩家 {info['original_name']} 当前状态：\n"
                    f"🟢 状态：{STATE_TEXT_MAP.get(st['currentState'], st['currentStateAsText'])}\n"
                    f"⏰ 持续时间：{st['currentStateSince']}"
                )
            else:
                txt = "❌ 无法获取玩家状态，请稍后再试。"
            telegram_edit_message(cid, mid, txt, get_player_action_menu(key))

    elif query.startswith("toggle_notify|"):
        key = query.split("|",1)[1]
        if key in data["players"]:
            info = data["players"][key]
            info["notify"] = not info["notify"]
            save_data()
            telegram_edit_message(
                cid, mid,
                f"🔔 通知已{'开启' if info['notify'] else '关闭'}：{info['original_name']}",
                get_player_action_menu(key)
            )

    elif query.startswith("remove|"):
        key = query.split("|",1)[1]
        if key in data["players"]:
            name = data["players"][key]["original_name"]
            del data["players"][key]
            save_data()
            telegram_edit_message(cid, mid, f"✅ 已移除玩家：{name}", get_player_list_menu())

# ---------- 后台监控线程 ----------
def monitor_loop():
    while True:
        cid = data.get("chat_id")
        if not cid:
            time.sleep(5)
            continue
        for key, info in list(data["players"].items()):
            if not info.get("notify"):
                continue
            st = fetch_player_status(info["original_name"], info["platform"])
            if st and st["currentState"] != info.get("last_state"):
                info["last_state"] = st["currentState"]
                save_data()
                telegram_send_message(
                    cid,
                    f"玩家 {info['original_name']} 状态变更为：{STATE_TEXT_MAP.get(st['currentState'], '未知')}"
                )
        time.sleep(CHECK_INTERVAL)

# ---------- 主循环 ----------
def run():
    offset = None
    threading.Thread(target=monitor_loop, daemon=True).start()
    while True:
        upd = telegram_get_updates(offset)
        if not upd.get("ok"):
            time.sleep(1)
            continue
        for u in upd.get("result", []):
            offset = u["update_id"] + 1
            if "message" in u:
                handle_message(u["message"])
            elif "callback_query" in u:
                handle_callback(u["callback_query"])

if __name__ == "__main__":
    run()
