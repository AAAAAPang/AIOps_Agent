import sys
import os
import json
import sqlite3
import paramiko
import asyncio
import base64
import uuid
import re
import time
import webbrowser
import threading
import smtplib
import ssl
import socket
import tkinter as tk
from tkinter import filedialog
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, status, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from cryptography.fernet import Fernet

# ================= 1. 路径与基础配置 =================
if getattr(sys, 'frozen', False):
    APPLICATION_PATH = os.path.dirname(sys.executable)
    BASE_DIR = sys._MEIPASS
else:
    APPLICATION_PATH = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = APPLICATION_PATH

DB_PATH = os.path.join(APPLICATION_PATH, "system.db")
IMG_DIR = os.path.join(APPLICATION_PATH, "uploads_images")
LOG_DIR = os.path.join(APPLICATION_PATH, "logs")

os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

def load_encryption_key() -> bytes:
    """加载用于加密主机密码的 Fernet 密钥。

    优先级：环境变量 AIOPS_SECRET_KEY > 程序目录下的 secret.key > 自动生成。
    自动生成的密钥只留在本机，千万不要提交到代码仓库。
    """
    env_key = os.environ.get("AIOPS_SECRET_KEY", "").strip()
    if env_key:
        return env_key.encode()

    key_file = os.path.join(APPLICATION_PATH, "secret.key")
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            key = f.read().strip()
        if key:
            return key

    key = Fernet.generate_key()
    with open(key_file, "wb") as f:
        f.write(key)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


cipher = Fernet(load_encryption_key())


def encrypt_data(data: str) -> str:
    if not data: return ""
    return cipher.encrypt(data.encode()).decode()


def decrypt_data(data: str) -> str:
    if not data: return ""
    try:
        return cipher.decrypt(data.encode()).decode()
    except:
        return ""


# ================= 2. 数据库与日志 =================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''CREATE TABLE IF NOT EXISTS servers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, host TEXT, port INTEGER, user TEXT, password TEXT, group_name TEXT, remark TEXT)''')
    try:
        c.execute("ALTER TABLE servers ADD COLUMN remark TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute(
        '''CREATE TABLE IF NOT EXISTS ai_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, api_key TEXT, base_url TEXT, model TEXT)''')
    c.execute(
        '''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, level TEXT, content TEXT)''')
    c.execute(
        '''CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER, role TEXT, content TEXT, time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS kb_dirs (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS server_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)''')
    c.execute("INSERT OR IGNORE INTO server_groups (name) VALUES ('未分组')")
    conn.commit()
    conn.close()


init_db()


def get_setting(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def add_log(level, content):
    try:
        conn = get_db()
        conn.execute("INSERT INTO logs (time, level, content) VALUES (?, ?, ?)",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, content))
        conn.commit()
        conn.close()
    except:
        pass


def add_chat_history(server_id, role, content):
    conn = get_db()
    conn.execute("INSERT INTO chat_history (server_id, role, content, time) VALUES (?, ?, ?, ?)",
                 (server_id, role, content, datetime.now().strftime("%H:%M:%S")))
    conn.commit()
    conn.close()
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"{today}.log")
    time_str = datetime.now().strftime("%H:%M:%S")
    if content.strip():
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time_str}] [{role.upper()}] {content}\n")


# ================= 3. SSH 执行引擎 =================
DANGEROUS_PATTERNS = [
    r"mkfs", r"dd\s+if=", r">\s*/dev/sd",
    r"bash\s+-i", r"sh\s+-i", r"nc\s+-e", r"ncat\s+-e",
    r"curl.*\|.*bash", r"wget.*\|.*bash", r"curl.*-o.*/tmp/.*",
    r"chmod\s+\+x.*\s+/tmp/", r"chattr\s+-i", r"crontab",
]


def execute_ssh(default_server, command, target_host_name=None, stop_event=None):
    actual_server = default_server
    if target_host_name and target_host_name != default_server['name']:
        conn = get_db()
        row = conn.execute("SELECT * FROM servers WHERE name=?", (target_host_name,)).fetchone()
        if row:
            actual_server = dict(row)
        conn.close()

    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            add_log("ERROR", f"安全沙箱拦截: [{actual_server['name']}] 尝试执行 {command}")
            return f"【安全沙箱拦截】包含高危/后门操作，已直接拒绝：{command}", 1

    ssh = None
    channel = None
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        real_pwd = decrypt_data(actual_server['password'])
        ssh.connect(actual_server['host'], port=actual_server['port'], username=actual_server['user'],
                    password=real_pwd, timeout=5, look_for_keys=False, allow_agent=False)

        transport = ssh.get_transport()
        channel = transport.open_session()
        channel.exec_command(command)

        res = ""
        err = ""

        while True:
            if stop_event and stop_event.is_set():
                channel.close()
                ssh.close()
                add_log("WARNING", f"[{actual_server['name']}] 用户强制中断命令: {command}")
                return "【系统提示】用户强制停止了命令。", -1

            while channel.recv_ready():
                res += channel.recv(4096).decode('utf-8', errors='ignore')
            while channel.recv_stderr_ready():
                err += channel.recv_stderr(4096).decode('utf-8', errors='ignore')

            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                while channel.recv_ready(): res += channel.recv(4096).decode('utf-8', errors='ignore')
                while channel.recv_stderr_ready(): err += channel.recv_stderr(4096).decode('utf-8', errors='ignore')
                break

            time.sleep(0.1)

        ssh.close()
        final_res = res if res else (err if err else "执行成功")
        add_log("INFO", f"[{actual_server['name']}] $ {command}")
        return final_res, 0 if channel.exit_status_ready() and channel.recv_exit_status() == 0 else 1
    except Exception as e:
        if ssh:
            try:
                ssh.close()
            except:
                pass
        add_log("ERROR", f"SSH失败 ({actual_server['name']}): {str(e)}")
        return f"[{actual_server['name']}] SSH失败: {str(e)}", 1


# ================= 4. 监控任务 =================
MONITOR_RUNNING = False
MONITOR_DATA = {}
ALERT_CACHE = {}


def check_server_metrics(server):
    cmd = """
    cpu=$(grep 'cpu ' /proc/stat | awk '{usage=($2+$4)*100/($2+$4+$5)} END {print usage}');
    mem=$(free | grep Mem | awk '{print $3/$2 * 100.0}');
    disk=$(df / | awk 'NR==2 {print $5}' | sed 's/%//');
    swap=$(free | grep Swap | awk '{if($2==0) print 0; else print $3/$2 * 100.0}');
    echo "$cpu|$mem|$disk|$swap"
    """
    res, code = execute_ssh(server, cmd)
    if code != 0: return None
    try:
        clean = res.strip().split('|')
        return {"cpu": round(float(clean[0]), 1), "mem": round(float(clean[1]), 1), "disk": round(float(clean[2]), 1),
                "swap": round(float(clean[3]), 1), "status": "online"}
    except:
        return None


def send_email_util(subject, content):
    smtp_host = get_setting("smtp_host")
    smtp_port = get_setting("smtp_port")
    smtp_user = get_setting("smtp_user")
    smtp_pass = get_setting("smtp_pass")
    to_email = get_setting("to_email")
    if not all([smtp_host, smtp_port, smtp_user, smtp_pass, to_email]):
        return False, "SMTP 配置不完整，请检查发信账号和密码"
    msg = MIMEText(content, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = smtp_user
    msg['To'] = to_email
    recipients = [email.strip() for email in to_email.split(',') if email.strip()]

    # 核心修复：强制 IPv4 解析，解决部分网络下 IPv6 导致的 SSL 握手失败
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4

    try:
        # 默认校验证书；个别环境证书链异常时可用 AIOPS_SMTP_INSECURE=1 跳过（不推荐）
        if os.environ.get("AIOPS_SMTP_INSECURE") == "1":
            context = ssl._create_unverified_context()
        else:
            context = ssl.create_default_context()

        if str(smtp_port) == "465":
            server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=10, context=context)
        else:
            server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=10)
            server.starttls(context=context)

        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, recipients, msg.as_string())
        server.quit()
        return True, "发送成功"
    except smtplib.SMTPAuthenticationError:
        return False, "认证失败：请检查邮箱授权码是否正确"
    except Exception as e:
        return False, str(e)
    finally:
        # 恢复全局 socket，避免影响其他功能
        socket.getaddrinfo = original_getaddrinfo


async def monitor_task():
    global MONITOR_DATA, ALERT_CACHE
    while True:
        if MONITOR_RUNNING:
            interval = int(get_setting("interval", "30"))
            thresholds = {
                "cpu": float(get_setting("th_cpu", "90")), "mem": float(get_setting("th_mem", "90")),
                "disk": float(get_setting("th_disk", "85")), "swap": float(get_setting("th_swap", "70"))
            }
            conn = get_db()
            servers = [dict(row) for row in conn.execute("SELECT * FROM servers").fetchall()]
            conn.close()

            metric_name_map = {"CPU": "CPU", "MEM": "内存", "DISK": "硬盘", "SWAP": "交换分区"}

            async def process_server_monitor(srv):
                try:
                    metrics = await asyncio.to_thread(check_server_metrics, srv)
                    if metrics:
                        MONITOR_DATA[srv['id']] = metrics
                        now = asyncio.get_event_loop().time()
                        exceeded = []
                        for k, v in metrics.items():
                            if k != 'status' and v > thresholds[k]:
                                exceeded.append({"key": k, "value": v, "threshold": thresholds[k]})
                        if exceeded:
                            if srv['id'] not in ALERT_CACHE or now - ALERT_CACHE[srv['id']] > 1800:
                                ALERT_CACHE[srv['id']] = now
                                first = exceeded[0]
                                zh_name = metric_name_map.get(first['key'].upper(), first['key'].upper())
                                subject = f"🚨 告警: {srv['name']} {zh_name} 超限"
                                body = f"主机：{srv['name']}\nCPU：{metrics.get('cpu', 0)}%\n内存：{metrics.get('mem', 0)}%\n硬盘：{metrics.get('disk', 0)}%\nSwap：{metrics.get('swap', 0)}%\n\n"
                                for item in exceeded:
                                    zh = metric_name_map.get(item['key'].upper(), item['key'].upper())
                                    body += f"警告！{srv['name']} 的{zh}使用率达到{item['value']}%，已经超出阈值{item['threshold']}%，请立刻排查。\n"
                                await asyncio.to_thread(send_email_util, subject, body)
                                add_log("WARNING", f"主机 {srv['name']} 触发告警：{subject}")
                    else:
                        MONITOR_DATA[srv['id']] = {"cpu": 0, "mem": 0, "disk": 0, "swap": 0, "status": "offline"}
                except Exception as e:
                    MONITOR_DATA[srv['id']] = {"cpu": 0, "mem": 0, "disk": 0, "swap": 0, "status": "offline"}

            await asyncio.gather(*(process_server_monitor(srv) for srv in servers))
            await asyncio.sleep(interval if interval >= 5 else 5)
        else:
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(monitor_task())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])
app.mount("/uploads_images", StaticFiles(directory=IMG_DIR), name="uploads_images")


# ================= 5. API 路由 =================
@app.post("/api/test-email")
def test_email():
    success, msg = send_email_util("🧪 AIOps_Agent - 测试邮件",
                                   "这是一封测试邮件。如果您收到此邮件，说明您的 SMTP 配置完全正确！")
    if success:
        return {"status": "ok", "msg": msg}
    else:
        raise HTTPException(status_code=500, detail=msg)


@app.post("/api/shutdown")
async def shutdown_system():
    async def kill_later():
        await asyncio.sleep(1)
        os._exit(0)

    asyncio.create_task(kill_later())
    return {"status": "ok", "msg": "系统正在关闭"}


@app.get("/api/servers")
def get_servers():
    conn = get_db()
    rows = conn.execute("SELECT id, name, host, port, user, group_name, remark FROM servers").fetchall()
    conn.close()
    return [dict(row) for row in rows]


@app.post("/api/servers")
def add_server(name: str = Form(...), host: str = Form(...), port: int = Form(...), user: str = Form(...),
               password: str = Form(...), group_name: str = Form("未分组"), remark: str = Form("")):
    conn = get_db()
    encrypted_pwd = encrypt_data(password)
    conn.execute(
        "INSERT INTO servers (name, host, port, user, password, group_name, remark) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, host, port, user, encrypted_pwd, group_name, remark))
    if group_name:
        try:
            conn.execute("INSERT INTO server_groups (name) VALUES (?)", (group_name,))
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    add_log("INFO", f"添加主机: {name}")
    return {"status": "ok"}


@app.delete("/api/servers/{server_id}")
def delete_server(server_id: int):
    conn = get_db()
    conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
    conn.commit()
    conn.close()
    add_log("INFO", f"删除服务器，ID: {server_id}")
    return {"status": "ok"}


@app.get("/api/groups")
def get_groups():
    conn = get_db()
    groups = [row["name"] for row in conn.execute("SELECT name FROM server_groups").fetchall()]
    conn.close()
    return groups


@app.post("/api/groups")
async def add_group(request: Request):
    data = await request.json()
    name = data.get("name", "").strip()
    if not name: raise HTTPException(400, "分组名不能为空")
    conn = get_db()
    try:
        conn.execute("INSERT INTO server_groups (name) VALUES (?)", (name,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return {"status": "ok"}


@app.delete("/api/groups/{name}")
def delete_group(name: str):
    if name == '未分组': raise HTTPException(400, "不能删除默认分组")
    conn = get_db()
    conn.execute("UPDATE servers SET group_name='未分组' WHERE group_name=?", (name,))
    conn.execute("DELETE FROM server_groups WHERE name=?", (name,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/ai")
def get_ai_configs():
    conn = get_db()
    res = [dict(row) for row in conn.execute("SELECT id, name, base_url, model, api_key FROM ai_configs").fetchall()]
    conn.close()
    return res


@app.post("/api/ai")
def add_ai(name: str = Form(...), api_key: str = Form(...), base_url: str = Form(...), model: str = Form(...)):
    conn = get_db()
    conn.execute("INSERT INTO ai_configs (name, api_key, base_url, model) VALUES (?, ?, ?, ?)",
                 (name, api_key, base_url, model))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/api/ai/{ai_id}")
def delete_ai(ai_id: int):
    conn = get_db()
    conn.execute("DELETE FROM ai_configs WHERE id=?", (ai_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/api/history/{server_id}")
def clear_chat_history(server_id: int):
    conn = get_db()
    conn.execute("DELETE FROM chat_history WHERE server_id=?", (server_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/history/{server_id}")
def get_chat_history(server_id: int):
    conn = get_db()
    rows = conn.execute("SELECT role, content FROM chat_history WHERE server_id=? ORDER BY id ASC LIMIT 50",
                        (server_id,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


@app.get("/api/logs")
def get_logs():
    conn = get_db()
    rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 500").fetchall()
    conn.close()
    return [dict(row) for row in rows]


@app.get("/api/settings")
def get_all_settings():
    return {
        "interval": get_setting("interval", "30"), "max_rounds": get_setting("max_rounds", "10000"),
        "max_context": get_setting("max_context", "100"), "keep_context": get_setting("keep_context", "45"),
        "th_cpu": get_setting("th_cpu", "90"), "th_mem": get_setting("th_mem", "90"),
        "th_disk": get_setting("th_disk", "85"), "th_swap": get_setting("th_swap", "70"),
        "smtp_host": get_setting("smtp_host"), "smtp_port": get_setting("smtp_port"),
        "smtp_user": get_setting("smtp_user"), "smtp_pass": get_setting("smtp_pass"),
        "to_email": get_setting("to_email")
    }


@app.post("/api/settings")
async def save_all_settings(request: Request):
    data = await request.json()
    for k, v in data.items(): set_setting(k, str(v))
    global MONITOR_RUNNING
    if MONITOR_RUNNING: asyncio.create_task(run_monitor_once())
    return {"status": "ok"}


@app.post("/api/monitor/toggle")
async def toggle_monitor():
    global MONITOR_RUNNING, MONITOR_DATA, ALERT_CACHE
    MONITOR_RUNNING = not MONITOR_RUNNING
    if MONITOR_RUNNING:
        ALERT_CACHE = {}
        asyncio.create_task(run_monitor_once())
    else:
        MONITOR_DATA = {}
    return {"running": MONITOR_RUNNING}


async def run_monitor_once():
    global MONITOR_DATA
    conn = get_db()
    servers = [dict(row) for row in conn.execute("SELECT * FROM servers").fetchall()]
    conn.close()

    async def process_one(srv):
        try:
            metrics = await asyncio.to_thread(check_server_metrics, srv)
            if metrics:
                MONITOR_DATA[srv['id']] = metrics
            else:
                MONITOR_DATA[srv['id']] = {"cpu": 0, "mem": 0, "disk": 0, "swap": 0, "status": "offline"}
        except Exception as e:
            MONITOR_DATA[srv['id']] = {"cpu": 0, "mem": 0, "disk": 0, "swap": 0, "status": "offline"}

    await asyncio.gather(*(process_one(srv) for srv in servers))


@app.get("/api/monitor/data")
def get_monitor_data(): return {"running": MONITOR_RUNNING, "data": MONITOR_DATA}


@app.get("/api/kb/select_dir")
def select_local_dir():
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder_path = filedialog.askdirectory(title="请选择要挂载的知识库文件夹")
        root.destroy()
        if folder_path:
            folder_path = os.path.normpath(folder_path)
            return {"status": "ok", "path": folder_path}
        return {"status": "cancel"}
    except Exception as e:
        return {"status": "error", "msg": f"弹窗失败: {str(e)}"}


@app.get("/api/kb/dirs")
def get_kb_dirs():
    conn = get_db()
    dirs = [dict(row) for row in conn.execute("SELECT * FROM kb_dirs").fetchall()]
    conn.close()
    return dirs


@app.post("/api/kb/dirs")
async def add_kb_dir(request: Request):
    data = await request.json()
    path = data.get("path", "").strip()
    if not path or not os.path.exists(path) or not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="路径不存在或不是目录")
    conn = get_db()
    try:
        conn.execute("INSERT INTO kb_dirs (path) VALUES (?)", (path,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return {"status": "ok"}


@app.delete("/api/kb/dirs/{dir_id}")
def delete_kb_dir(dir_id: int):
    conn = get_db()
    conn.execute("DELETE FROM kb_dirs WHERE id=?", (dir_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/kb/tree")
def get_kb_tree():
    conn = get_db()
    dirs = [dict(row) for row in conn.execute("SELECT * FROM kb_dirs").fetchall()]
    conn.close()

    def build_tree(path):
        name = os.path.basename(path) or path
        node = {"name": name, "type": "dir", "children": []}
        try:
            for entry in os.scandir(path):
                if entry.name.startswith('.'): continue
                if entry.is_dir():
                    if entry.name in ['node_modules', '__pycache__', 'venv', '.git']: continue
                    node["children"].append(build_tree(entry.path))
                else:
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in ['.txt', '.md', '.py', '.json', '.yaml', '.yml', '.log', '.cfg', '.conf', '.pdf', '.pptx',
                               '.docx']:
                        node["children"].append({"name": entry.name, "type": "file", "path": entry.path})
        except PermissionError:
            pass
        return node

    result = []
    for d in dirs:
        if os.path.exists(d['path']):
            tree = build_tree(d['path'])
            tree['id'] = d['id']
            tree['root_path'] = d['path']
            result.append(tree)
    return result


@app.get("/api/kb/content")
def get_kb_content(path: str):
    if not os.path.exists(path) or not os.path.isfile(path):
        return {"content": "文件不存在"}
    ext = os.path.splitext(path)[1].lower()
    if ext in ['.pptx', '.docx', '.pdf']:
        return {"content": f"当前文件格式 ({ext}) 暂不支持网页直接预览，但 AI 已在后台读取了它的内容（仅限文本类）。"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {"content": f.read()[:5000]}
    except UnicodeDecodeError:
        return {"content": "无法预览（可能是二进制文件或非 UTF-8 编码）"}
    except Exception as e:
        return {"content": f"读取失败: {str(e)}"}


# ================= 6. WebSocket =================
agent_tasks = {}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    stop_event = asyncio.Event()
    agent_tasks[id(websocket)] = {"stop_event": stop_event, "messages": None}

    try:
        while True:
            data = json.loads(await websocket.receive_text())

            if data.get("action") == "clear_context":
                agent_tasks[id(websocket)]["messages"] = None
                await websocket.send_json({"type": "status", "content": "✅ 后端记忆上下文已清理"})
                continue

            if data.get("action") == "stop":
                stop_event.set()
                await websocket.send_json(
                    {"type": "message", "content": "⏸️ 任务已暂停。你可以输入“继续”让我接着做，或者发送新指令。"})
                continue
            if data.get("action") in ["yes", "no"]: continue

            text = data.get("text")
            image_base64 = data.get("image_base64")
            server_id = data.get("server_id")
            ai_id = data.get("ai_id")

            if not server_id or not ai_id:
                await websocket.send_json({"type": "message", "content": "⚠️ 请先在左侧选择服务器和AI模型！"})
                continue

            if text == "继续" and agent_tasks[id(websocket)].get("messages"):
                messages = agent_tasks[id(websocket)]["messages"]
                messages.append({"role": "user", "content": "请从刚才中断的地方继续执行任务。"})
            else:
                stop_event.clear()
                if text and len(text) > 5000: text = text[:5000]

                saved_image_name = None
                if image_base64:
                    try:
                        format_str, imgstr = image_base64.split(';base64,')
                        ext = format_str.split('/')[-1]
                        saved_image_name = f"{uuid.uuid4().hex}.{ext}"
                        with open(os.path.join(IMG_DIR, saved_image_name), "wb") as f:
                            f.write(base64.b64decode(imgstr))
                    except:
                        pass

                db_content = text if not saved_image_name else f"[IMAGE]{saved_image_name}[/IMAGE]{text}"
                add_chat_history(server_id, "user", db_content)

                conn = get_db()
                server = dict(conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone())
                ai_cfg = dict(conn.execute("SELECT * FROM ai_configs WHERE id=?", (ai_id,)).fetchone())

                kb_context = ""
                kb_dirs = conn.execute("SELECT path FROM kb_dirs").fetchall()
                if kb_dirs:
                    kb_context += "\n【本地挂载知识库参考】（你可以参考这些本地文件的内容来回答问题）：\n"
                    for kd in kb_dirs:
                        root_path = kd['path']
                        if not os.path.exists(root_path): continue
                        kb_context += f"\n目录: {root_path}\n"
                        file_count = 0
                        for root, dirs, files in os.walk(root_path):
                            for file in files:
                                if file.endswith(('.txt', '.md', '.py', '.yaml', '.yml', '.json')):
                                    f_path = os.path.join(root, file)
                                    try:
                                        with open(f_path, 'r', encoding='utf-8') as f:
                                            content = f.read(1500)
                                            kb_context += f"--- 文件: {file} ---\n{content}\n"
                                            file_count += 1
                                    except:
                                        pass
                                    if file_count > 15: break
                            if file_count > 15: break

                all_srv = conn.execute("SELECT name, host FROM servers").fetchall()
                all_srv_str = "\n".join([f"- {s['name']} ({s['host']})" for s in all_srv])

                max_context = int(get_setting("max_context", "100"))
                keep_context = int(get_setting("keep_context", "45"))
                history = conn.execute(
                    "SELECT role, content FROM chat_history WHERE server_id=? ORDER BY id DESC LIMIT ?",
                    (server_id, max_context)).fetchall()
                conn.close()

                system_prompt = f"""你是运行在 Web 端的专业运维助手，当前用户正在界面中与你对话。

【🚨 极其重要 - 当前操作目标 🚨】：
用户当前在左侧选中的主机是：【{server['name']}】 (IP: {server['host']})。
除非用户在本次提问中明确说了“去网站上看”、“去监控机看”等明确切换目标的话，否则**你的所有分析、排查、执行命令，都必须且只能在【{server['name']}】上进行！**
⚠️ 警告：如果你发现当前选中的【{server['name']}】连不上，请如实报告该主机失联，**绝对不允许**擅自将目标切换到历史记录里提到过的其他机器去执行操作或得出结论！

【全局网络拓扑（你的操作范围）】：
目前受管的主机列表如下：
{all_srv_str}

{kb_context}

【核心规则】：
1. 当用户明确要求你在**其他服务器**上执行操作时，请在调用 `execute_ssh_command` 工具时，将 `target_host` 参数设置为目标主机的准确名称。如果不填 `target_host`，系统默认在【{server['name']}】上执行。
2. 绝不允许在正文中要求用户回复“确认”、“同意”！必须调用 `execute_ssh_command`。系统会自动弹窗。
3. 绝不允许编造执行结果！只有工具真实返回的内容才是可信的。
4. 如果用户的问题与上述【本地挂载知识库参考】有关，请务必优先结合你读到的本地文件内容来回答。
【连续执行机制】：如果是复杂任务，你必须连续多次调用工具！每次得到结果后，直接发起下一次调用。直到彻底完成，才输出 Markdown 总结表格。
"""
                messages = [{"role": "system", "content": system_prompt}]
                for row in reversed(history[:keep_context]):
                    if row['role'] in ['user', 'assistant']:
                        content_clean = re.sub(r'\[IMAGE\].*?\[/IMAGE\]', '', row['content'])
                        content_clean = re.sub(r'<[^>]+>', '', content_clean)
                        if content_clean.strip(): messages.append({"role": row['role'], "content": content_clean})

                if image_base64:
                    messages.append({"role": "user", "content": [{"type": "text", "text": text}, {"type": "image_url",
                                                                                                  "image_url": {
                                                                                                      "url": image_base64}}]})
                else:
                    messages.append({"role": "user", "content": text})

                agent_tasks[id(websocket)]["messages"] = messages
                agent_tasks[id(websocket)]["server"] = server
                agent_tasks[id(websocket)]["ai_cfg"] = ai_cfg

            messages = agent_tasks[id(websocket)]["messages"]
            server = agent_tasks[id(websocket)]["server"]
            ai_cfg = agent_tasks[id(websocket)]["ai_cfg"]

            client = OpenAI(api_key=ai_cfg['api_key'], base_url=ai_cfg['base_url'])
            max_rounds = int(get_setting("max_rounds", "10000"))
            tools = [{
                "type": "function",
                "function": {
                    "name": "execute_ssh_command",
                    "description": "在指定的远程Linux服务器上执行Shell命令。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "要执行的Shell命令"},
                            "target_host": {"type": "string",
                                            "description": "目标主机的名称（如：'网站', '监控'）。【注意：如果用户没有明确指定要在哪台机器操作，请务必留空！系统默认会在当前选中的机器执行。绝对不要在用户排查当前机器时乱填其他机器！】"}
                        },
                        "required": ["command"]
                    }
                }
            }]

            try:
                round_count = 0
                while round_count < max_rounds:
                    if stop_event.is_set(): break
                    response = client.chat.completions.create(model=ai_cfg['model'], messages=messages, tools=tools,
                                                              tool_choice="auto")
                    msg = response.choices[0].message
                    messages.append(msg)

                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            if stop_event.is_set(): break
                            cmd_to_exec = "";
                            target_host = None
                            if tc.function.name == "execute_ssh_command":
                                try:
                                    args = json.loads(tc.function.arguments)
                                    cmd_to_exec = args.get("command", "")
                                    target_host = args.get("target_host", None)
                                except:
                                    pass
                            if not cmd_to_exec:
                                messages.append({"role": "tool", "tool_call_id": tc.id, "content": "解析命令失败。"})
                                continue

                            display_host = target_host if target_host else server['name']
                            await websocket.send_json(
                                {"type": "cmd_start", "id": tc.id, "cmd": cmd_to_exec, "host": display_host})

                            dangerous_ui = ["rm -rf", "mkfs", "shutdown", "reboot"]
                            if any(d in cmd_to_exec for d in dangerous_ui):
                                await websocket.send_json(
                                    {"type": "confirm", "command": cmd_to_exec, "tc_id": tc.id, "host": display_host})
                                confirm_msg = json.loads(await websocket.receive_text())
                                if confirm_msg.get("action") != "yes":
                                    messages.append(
                                        {"role": "tool", "tool_call_id": tc.id, "content": "用户拒绝了执行该高危命令。"})
                                    await websocket.send_json(
                                        {"type": "cmd_end", "id": tc.id, "code": -1, "res": "用户拒绝"})
                                    continue

                            res, code = await asyncio.to_thread(execute_ssh, server, cmd_to_exec, target_host,
                                                                stop_event)
                            await websocket.send_json({"type": "cmd_end", "id": tc.id, "code": code, "res": res})

                            if code == -1:
                                messages.append(
                                    {"role": "tool", "tool_call_id": tc.id, "content": "用户已强制终止该命令。"})
                                break
                            elif code != 0:
                                res += f"\n\n【系统提示】：退出码为 {code}。如果失败，请分析原因并尝试替代方案。"
                            messages.append({"role": "tool", "tool_call_id": tc.id, "content": res})
                        round_count += 1
                    else:
                        final_answer = msg.content or ""
                        break
                else:
                    final_answer = f"⚠️ 达到最大连续执行轮数限制（{max_rounds}轮），已自动暂停。"

                if stop_event.is_set(): final_answer = "已停止当前任务（对话上下文保留，可输入“继续”恢复）。"
                final_answer = re.sub(r'<[^>]+>', '', final_answer)
                final_answer = re.sub(r'\n{3,}', '\n\n', final_answer).strip()

                add_chat_history(server_id, "assistant", final_answer)
                await websocket.send_json({"type": "message", "content": final_answer})
            except Exception as e:
                await websocket.send_json({"type": "message", "content": f"模型调用失败：{str(e)}"})
    except WebSocketDisconnect:
        pass
    finally:
        if id(websocket) in agent_tasks: del agent_tasks[id(websocket)]


@app.get("/")
async def get():
    file_path = os.path.join(BASE_DIR, "index.html")
    with open(file_path, "r", encoding="utf-8") as f: return HTMLResponse(f.read())


# ================= 7. 启动项与控制台输出 =================
if __name__ == "__main__":
    import uvicorn

    PORT = int(os.environ.get("AIOPS_PORT", "8010"))

    try:
        conn = get_db()
        server_count = conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
        conn.close()
        interval = get_setting("interval", "30")
    except Exception:
        server_count = 0
        interval = "30"

    print("\033[96m" + "=" * 65 + "\033[0m")
    print("\033[92m[开源] AIOps_Agent · 欢迎使用与贡献\033[0m")
    print(f"AIOps_Agent  Web GUI：\033[93mhttp://127.0.0.1:{PORT}\033[0m（Ctrl+C 退出）")
    print(f"[webapp] 监测器已就绪：可值守 {server_count} 台主机，间隔{interval}s（默认未值守，点「一键值守」启动）")
    print("[webapp] 告警邮件通知已启用：值守告警将同时发送邮件")
    print("\033[96m" + "=" * 65 + "\033[0m")
    print("\033[90m提示：关闭此命令行窗口，软件将自动停止并退出。\033[0m\n")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(('127.0.0.1', PORT)) == 0:
            webbrowser.open(f"http://127.0.0.1:{PORT}")
            time.sleep(1)
            sys.exit(0)
        else:
            def open_browser():
                time.sleep(1)
                webbrowser.open(f"http://127.0.0.1:{PORT}")


            threading.Thread(target=open_browser, daemon=True).start()
            uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")