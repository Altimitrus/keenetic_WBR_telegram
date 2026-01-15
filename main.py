import requests, hashlib, logging, time, asyncio, paramiko
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from dotenv import dotenv_values
import socket

# ---------------- CONFIG ----------------
config = dotenv_values('.env')
WIFI_HOST = config['WIFI_HOST']  # IP роутера
WIFI_LOGIN = config['WIFI_LOGIN']
WIFI_PASSWORD = config['WIFI_PASSWORD']
TG_BOT_TOKEN = config['TG_BOT_TOKEN']

# SSH параметры
SSH_HOST = config.get('SSH_HOST', WIFI_HOST.replace('http://', '').replace('https://', ''))
SSH_PORT = int(config.get('SSH_PORT', 222))
SSH_USER = config['SSH_USER']
SSH_PASS = config['SSH_PASS']

# Получаем единый список разрешенных пользователей
ALLOWED_USERS_STR = config.get('ALLOWED_USERS', '')
ALLOWED_USERS = [int(user_id.strip()) for user_id in ALLOWED_USERS_STR.split(',') if user_id.strip().isdigit()]

# ---------------- LOGGING ----------------
logging.basicConfig(
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    level=logging.INFO,
    datefmt='%Y.%m.%d %H:%M:%S UTC'
)
logging.Formatter.converter = time.gmtime

# ---------------- GLOBALS ----------------
session = requests.Session()
ACTIVE_CLIENTS = []
PREV_STATUS = {}  # для уведомлений о смене статуса


# ---------------- UTILS ----------------
def is_user_allowed(user_id: int) -> bool:
    """Проверяем, есть ли пользователь в списке разрешенных"""
    return user_id in ALLOWED_USERS

def format_bytes(b: int) -> str:
    """Преобразование байтов в читаемый формат"""
    if b < 1024: 
        return f"{b} B"
    elif b < 1024**2: 
        return f"{b/1024:.2f} KB"
    elif b < 1024**3: 
        return f"{b/1024**2:.2f} MB"
    else: 
        return f"{b/1024**3:.2f} GB"

def format_seconds(s: int) -> str:
    """Форматирование секунд в дни:часы:минуты:секунды"""
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    
    if days > 0:
        return f"{days}д {hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def get_cpu_temp_ssh() -> str:
    """Получаем температуру CPU через SSH"""
    ssh = None
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            password=SSH_PASS,
            timeout=5,
            look_for_keys=False,
            allow_agent=False
        )
        
        stdin, stdout, stderr = ssh.exec_command(
            "awk '{print $1/1000 \" °C\"}' /sys/class/thermal/thermal_zone0/temp",
            timeout=5
        )
        temp = stdout.read().decode().strip()
        if temp and temp.replace('.', '').replace('°C', '').replace(' ', '').isdigit():
            return temp
        else:
            return "—"
        
    except Exception as e:
        logging.error(f"SSH temp error: {e}")
        return "—"
    finally:
        if ssh:
            try:
                ssh.close()
            except:
                pass

def get_pppoe_ip() -> str:
    """Получаем IP PPPoE через RCI"""
    try:
        r = session.get(f'{WIFI_HOST}/rci/show/interface/PPPoE0', 
                       auth=(WIFI_LOGIN, WIFI_PASSWORD), 
                       timeout=5)
        r.raise_for_status()
        data = r.json()
        
        # Пробуем разные возможные ключи
        if 'address' in data:
            return data['address']
        elif 'ip' in data:
            return data['ip']
        elif 'ipv4-address' in data:
            return data['ipv4-address']
        else:
            # Пробуем найти вложенные структуры
            for key, value in data.items():
                if isinstance(value, dict) and 'address' in value:
                    return value['address']
            return "—"
    except Exception as e:
        logging.error(f"PPPoE IP error: {e}")
        return "—"


# ---------------- KEENETIC ----------------
def keen_get(path: str) -> dict:
    try:
        r = session.get(f'{WIFI_HOST}/rci/show/{path}', 
                       auth=(WIFI_LOGIN, WIFI_PASSWORD), 
                       timeout=10)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            logging.error(f"JSON decode error for {path}: {e}")
            return {}
    except requests.RequestException as e:
        logging.error(f"Keenetic request failed ({path}): {e}")
        return {}

def keen_auth() -> bool:
    """Авторизация на роутера"""
    try:
        r = session.get(f'{WIFI_HOST}/auth', timeout=5)
        if r.status_code == 200:
            return True
        elif r.status_code == 401:
            realm = r.headers.get('X-NDM-Realm', '')
            challenge = r.headers.get('X-NDM-Challenge', '')
            
            if not realm or not challenge:
                logging.error("No auth headers in response")
                return False
                
            md5 = hashlib.md5(f"{WIFI_LOGIN}:{realm}:{WIFI_PASSWORD}".encode()).hexdigest()
            sha = hashlib.sha256(f"{challenge}{md5}".encode()).hexdigest()
            
            r2 = session.post(f'{WIFI_HOST}/auth', 
                            json={'login': WIFI_LOGIN, 'password': sha}, 
                            timeout=5)
            return r2.status_code == 200
        else:
            logging.error(f"Auth failed: {r.status_code}")
            return False
    except Exception as e:
        logging.error(f"Auth exception: {e}")
        return False

def update_clients():
    """Обновление списка клиентов"""
    global ACTIVE_CLIENTS
    try:
        data = keen_get('device-list')
        if not data:
            logging.warning("No data from device-list")
            ACTIVE_CLIENTS = []
            return
            
        hosts = data.get('host', [])
        if not hosts:
            logging.warning("No hosts in device-list")
            ACTIVE_CLIENTS = []
            return
            
        # Сортировка по IP
        def ip_sort(dev):
            ip = dev.get('ip', '0.0.0.0')
            try:
                return tuple(int(x) for x in ip.split('.') if x.isdigit())
            except:
                return (0, 0, 0, 0)
                
        ACTIVE_CLIENTS = sorted(hosts, key=ip_sort)
        
    except Exception as e:
        logging.error(f"Error updating clients: {e}")
        ACTIVE_CLIENTS = []


# ---------------- TELEGRAM ----------------
def main_keyboard() -> InlineKeyboardMarkup:
    online = [d for d in ACTIVE_CLIENTS if d.get('active')]
    offline = [d for d in ACTIVE_CLIENTS if not d.get('active')]
    
    # Разделение по типу подключения
    online_wifi = [d for d in online if 'ssid' in d]
    online_wired = [d for d in online if 'ssid' not in d]
    
    offline_wifi = [d for d in offline if 'ssid' in d]
    offline_wired = [d for d in offline if 'ssid' not in d]

    buttons = []
    if online:
        buttons.append([InlineKeyboardButton(
            f"🟢 Онлайн ({len(online)})", 
            callback_data='show_online'
        )])
    if offline:
        buttons.append([InlineKeyboardButton(
            f"🔴 Офлайн ({len(offline)})", 
            callback_data='show_offline'
        )])
    
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data='refresh_main')])

    return InlineKeyboardMarkup(buttons)

def device_keyboard(devices: list, list_type: str) -> InlineKeyboardMarkup:
    """list_type: 'online', 'offline'"""
    buttons = []
    for d in devices:
        status = "🟢" if d.get('active') else "🔴"
        ip = d.get('ip','—')
        name = d.get('name') or d.get('hostname') or ip
        
        # Определяем тип подключения для иконки
        conn_type = "📶" if 'ssid' in d else "🔌"
        
        # Обрезаем длинные имена
        display_name = name[:15] + "..." if len(name) > 15 else name
        button_text = f"{status} {conn_type} {display_name} ({ip})"
        buttons.append([InlineKeyboardButton(button_text, callback_data=f"client_{d['mac']}_{list_type}")])
    
    # Кнопка назад всегда ведет в главное меню
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')])
    return InlineKeyboardMarkup(buttons)

def format_device_info(dev: dict) -> str:
    status = "🟢 Онлайн" if dev.get('active') else "🔴 Офлайн"
    ip = dev.get('ip','—')
    name = dev.get('name') or dev.get('hostname') or '—'
    mac = dev.get('mac','—')
    rx = format_bytes(dev.get('rxbytes',0))
    tx = format_bytes(dev.get('txbytes',0))
    uptime = format_seconds(dev.get('uptime',0))
    
    # Определяем тип подключения
    if 'ssid' in dev:
        conn_type = "📶 Wi-Fi"
        ssid = dev.get('ssid','—')
        rssi = f"{dev.get('rssi')} dBm" if 'rssi' in dev else '—'
        link = dev.get('link','—')
        extra_info = f"📡 SSID: {ssid}\n📶 RSSI: {rssi}\n🔗 Link: {link}"
    else:
        conn_type = "🔌 Проводной"
        extra_info = ""

    return (
        f"📱 Информация об устройстве:\n\n"
        f"📊 Статус: {status}\n"
        f"🏷️ Имя: {name}\n"
        f"🔗 MAC: {mac}\n"
        f"🌐 IP: {ip}\n"
        f"📡 Тип подключения: {conn_type}\n"
        f"📥 Принято: {rx}\n"
        f"📤 Отправлено: {tx}\n"
        f"⏱️ В сети: {uptime}\n"
        f"{extra_info}"
    )


# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    # Проверяем разрешен ли пользователь
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        logging.warning(f"Unauthorized access attempt from user {user_id}")
        if edit:
            try:
                await update.callback_query.message.edit_text("⛔ У вас нет доступа к этому боту.")
            except:
                pass
        else:
            await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return
    
    if not keen_auth():
        msg = "❌ Не удалось авторизоваться на роутере"
        if edit:
            try:
                await update.callback_query.message.edit_text(msg)
            except Exception as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            await update.message.reply_text(msg)
        return

    update_clients()
    
    # Получаем информацию о системе
    sys_info = keen_get('system')
    hostname = sys_info.get('hostname','—')
    cpu_load = sys_info.get('cpuload',0)
    
    # Получаем информацию о памяти из поля "memory"
    memory_str = sys_info.get('memory', '0/0')
    try:
        # Разбираем строку формата "255592/524288"
        used_kb, total_kb = map(int, memory_str.split('/'))
        
        # Конвертируем KB в MB
        used_mb = used_kb / 1024
        total_mb = total_kb / 1024
        
        # Рассчитываем процент использования
        mem_percent = (used_kb / total_kb * 100) if total_kb > 0 else 0
        
    except (ValueError, AttributeError):
        # Если не удалось разобрать строку, используем старые поля как fallback
        mem_total = int(sys_info.get('memtotal',0))  # в килобайтах
        mem_free = int(sys_info.get('memfree',0))    # в килобайтах
        mem_used = mem_total - mem_free
        
        # Конвертируем KB в MB
        used_mb = mem_used / 1024
        total_mb = mem_total / 1024
        
        # Расчет процентов
        mem_percent = (mem_used / mem_total * 100) if mem_total > 0 else 0
    
    uptime_s = int(sys_info.get('uptime',0))
    conns_total = int(sys_info.get('conntotal',0))
    conns_free = int(sys_info.get('connfree',0))
    conns_used = conns_total - conns_free
    
    # Параллельно получаем остальные данные
    wan_ip = get_pppoe_ip()
    cpu_temp = get_cpu_temp_ssh()
    
    # Статистика клиентов
    online = [d for d in ACTIVE_CLIENTS if d.get('active')]
    offline = [d for d in ACTIVE_CLIENTS if not d.get('active')]
    online_wifi = [d for d in online if 'ssid' in d]
    online_wired = [d for d in online if 'ssid' not in d]
    offline_wifi = [d for d in offline if 'ssid' in d]
    offline_wired = [d for d in offline if 'ssid' not in d]
    
    # Расчет процента соединений
    conns_percent = (conns_used / conns_total * 100) if conns_total > 0 else 0

    text = (
        f"📊 Состояние системы\n\n"
        f"🏠 Роутер: {hostname}\n"
        f"🌡️ Температура: {cpu_temp}\n"
        f"⚙️ Нагрузка CPU: {cpu_load}%\n"
        f"🧠 Память: {used_mb:.1f} MB / {total_mb:.1f} MB ({mem_percent:.1f}%)\n"
        f"⏱️ Аптайм: {format_seconds(uptime_s)}\n"
        f"🌐 WAN IP: {wan_ip}\n"
        f"🔗 Соединения: {conns_used}/{conns_total} ({conns_percent:.1f}%)\n\n"
        f"👥 Клиенты:\n"
        f"  🟢 Онлайн: {len(online)}\n"
        f"    📶 Wi-Fi: {len(online_wifi)}\n"
        f"    🔌 Проводные: {len(online_wired)}\n"
        f"  🔴 Офлайн: {len(offline)}\n"
        f"    📶 Wi-Fi: {len(offline_wifi)}\n"
        f"    🔌 Проводные: {len(offline_wired)}"
    )

    if edit:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=main_keyboard())
        except Exception as e:
            # Игнорируем ошибку "Message is not modified"
            if "Message is not modified" not in str(e):
                raise
    else:
        await update.message.reply_text(text, reply_markup=main_keyboard())


# ---------------- BUTTON HANDLER ----------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Проверяем разрешен ли пользователь
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        logging.warning(f"Unauthorized button press from user {user_id}")
        try:
            await query.message.edit_text("⛔ У вас нет доступа к этому боту.")
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise
        return

    if data == 'refresh_main':
        await start(update, context, edit=True)
        return
        
    update_clients()

    if data == 'show_online':
        devices = [d for d in ACTIVE_CLIENTS if d.get('active')]
        try:
            await query.message.edit_text(f"🟢 Онлайн устройства ({len(devices)}):", reply_markup=device_keyboard(devices, 'online'))
        except Exception as e:
            # Игнорируем ошибку "Message is not modified"
            if "Message is not modified" not in str(e):
                raise
    
    elif data == 'show_offline':
        devices = [d for d in ACTIVE_CLIENTS if not d.get('active')]
        try:
            await query.message.edit_text(f"🔴 Офлайн устройства ({len(devices)}):", reply_markup=device_keyboard(devices, 'offline'))
        except Exception as e:
            # Игнорируем ошибку "Message is not modified"
            if "Message is not modified" not in str(e):
                raise
    
    elif data.startswith('client_'):
        parts = data.split('_')
        mac = parts[1]
        list_type = '_'.join(parts[2:])  # может быть 'online', 'offline'
        
        dev = next((d for d in ACTIVE_CLIENTS if d['mac']==mac), None)
        if dev:
            try:
                await query.message.edit_text(
                    format_device_info(dev), 
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Назад", callback_data=f'show_{list_type}')]
                    ])
                )
            except Exception as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            try:
                await query.message.edit_text(
                    "❌ Устройство не найдено", 
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
                    ])
                )
            except Exception as e:
                if "Message is not modified" not in str(e):
                    raise
    
    # Обработка кнопок "Назад"
    elif data == 'back_to_main':
        # Возврат в главное меню
        try:
            await start(update, context, edit=True)
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise


# ---------------- STATUS CHECK ----------------
async def check_status_change(app):
    """Отправка уведомлений всем пользователям из списка ALLOWED_USERS"""
    global PREV_STATUS
    while True:
        try:
            update_clients()
            for d in ACTIVE_CLIENTS:
                mac = d['mac']
                prev = PREV_STATUS.get(mac)
                current = d.get('active', False)
                
                if prev is not None and prev != current:
                    status = "🟢 Онлайн" if current else "🔴 Офлайн"
                    name = d.get('name') or d.get('hostname') or mac
                    ip = d.get('ip', '—')
                    
                    # Определяем тип подключения
                    conn_type = "📶 Wi-Fi" if 'ssid' in d else "🔌 Провод"
                    
                    if current:
                        # Устройство подключилось
                        text = (
                            f"🔔 Устройство подключилось\n\n"
                            f"🏷️ Имя: {name}\n"
                            f"🔗 MAC: {mac}\n"
                            f"🌐 IP: {ip}\n"
                            f"📡 Тип: {conn_type}"
                        )
                    else:
                        # Устройство отключилось
                        text = (
                            f"🔔 Устройство отключилось\n\n"
                            f"🏷️ Имя: {name}\n"
                            f"🔗 MAC: {mac}\n"
                            f"🌐 IP: {ip}\n"
                            f"📡 Тип: {conn_type}"
                        )
                    
                    # Отправляем уведомление всем пользователям из списка
                    for user_id in ALLOWED_USERS:
                        try:
                            await app.bot.send_message(chat_id=user_id, text=text)
                            logging.info(f"Status notification sent to user {user_id}")
                        except Exception as e:
                            logging.error(f"Failed to send status message to {user_id}: {e}")
                
                PREV_STATUS[mac] = current
        except Exception as e:
            logging.error(f"Error in status check: {e}")
        
        await asyncio.sleep(10)  # Проверяем каждые 10 секунд


# ---------------- MAIN ----------------
if __name__ == '__main__':
    # Инициализация перед запуском
    logging.info("Starting Keenetic Monitor Bot...")
    logging.info(f"WIFI_HOST: {WIFI_HOST}")
    
    # Выводим информацию о разрешенных пользователях
    if ALLOWED_USERS:
        logging.info(f"Allowed users: {ALLOWED_USERS}")
    else:
        logging.warning("No allowed users specified! Bot will respond to NO ONE!")
        logging.warning("Please add ALLOWED_USERS to .env file (e.g., ALLOWED_USERS=123456,789012)")
    
    # Тест подключений при запуске
    if keen_auth():
        logging.info("Router auth: SUCCESS")
    else:
        logging.warning("Router auth: FAILED")

    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(button_handler))

    async def start_bg_tasks(app):
        asyncio.create_task(check_status_change(app))

    app.post_init = start_bg_tasks
    
    logging.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling()