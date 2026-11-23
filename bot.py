import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
import requests
import time
import json
import os
import logging

# CONFIGURACIÓN
BOT_TOKEN = '9224399752:AAFI8DXfG42h1UuaZKtE6_vbdTGyfIH4yWQ'
# URL Base de la API interna de Netflix (funciona desde cualquier IP, no solo la de los servidores de NF)
NETFLIX_API_BASE = "https://www.netflix.com/api/shakti/apps/device"
# Endpoint para validar si la cookie es válida (perfil de usuario)
NETFLIX_PROFILE_URL = "https://www.netflix.com/profiles/children" 

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- GESTIÓN DE LA BASE DE DATOS (BAÚL) ---
DB_FILE = 'cookies.db'

def init_db():
    conn = __import__('sqlite3').connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS cookies 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, cookie TEXT, valid BOOLEAN DEFAULT 0, last_checked TIMESTAMP)''')
    conn.commit()
    conn.close()

def save_cookie(cookie_str):
    conn = __import__('sqlite3').connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO cookies (cookie, valid) VALUES (?, ?)", (cookie_str, 0))
    conn.commit()
    conn.close()

def get_valid_cookies():
    conn = __import__('sqlite3').connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT cookie FROM cookies WHERE valid = 1")
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

# --- VALIDACIÓN DE COOKIE ---
def validate_cookie(cookie_str):
    """Prueba si la cookie tiene sesión activa."""
    headers = {
        'Cookie': cookie_str,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    try:
        # Intentamos cargar la página de perfiles. Si carga bien (status 200) y no redirige a login, está activa.
        r = requests.get('https://www.netflix.com/browse', headers=headers, timeout=10)
        if r.status_code == 200 and 'login-netflix' not in r.url:
            return True
        return False
    except Exception as e:
        logger.error(f"Error validando cookie: {e}")
        return False

# --- FLUJO DE ACTIVACIÓN ---
def activate_device(device_code, valid_cookie):
    """
    1. Registra el dispositivo con el código.
    2. Espera a que el usuario autorice en la TV.
    3. Usa la cookie para finalizar la conexión.
    """
    headers_register = {
        'Content-Type': 'application/json; charset=utf-8',
        'User-Agent': 'Mozilla/5.0 (Smart-TV)',
        'X-Netflix': 'device-registration'
    }
    
    # Paso 1: Registrar el código de 8 dígitos
    payload = {
        "clientType": "SMART_TV",
        "code": device_code
    }
    
    try:
        # Registro inicial
        reg_response = requests.post(NETFLIX_API_BASE, json=payload, headers=headers_register, timeout=10)
        
        if reg_response.status_code != 200:
            return f"Error al registrar el código. Status: {reg_response.status_code}"

        auth_token = reg_response.json().get('authToken')
        if not auth_token:
            return "No se obtuvo un token de autorización temporal."

        # Paso 2: Polling para esperar a que el usuario pulse 'OK' en la TV
        # La API de polling suele ser GET con el authToken
        polling_url = f"https://api.netflix.com/cuesessions/{auth_token}" # Nota: A veces varía según la región/version
        
        # Usaremos un método más genérico y probado para obtener el estado de autorización
        # La API real que usa el cliente web para verificar el estado es:
        check_url = f"https://www.netflix.com/api/shakti/apps/device/{auth_token}"
        
        max_attempts = 60 # Esperar hasta 5 minutos (60 intentos * 5 seg)
        attempt = 0
        
        while attempt < max_attempts:
            time.sleep(5)
            attempt += 1
            
            # Verificamos el estado
            status_req = requests.get(check_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            status_data = status_req.json()
            
            state = status_data.get('state', '')
            
            if state == 'AUTHORIZED':
                # ¡Éxito! Ahora usamos la cookie para vincularla
                return finalize_activation(auth_token, valid_cookie)
            elif state == 'EXPIRED':
                return "El código de la TV ha expirado. Inténtalo de nuevo."
                
        return "Tiempo de espera agotado. No se detectó autorización en la TV."

    except Exception as e:
        return f"Error en el proceso: {str(e)}"

def finalize_activation(auth_token, cookie):
    """Usa la cookie para autenticar la sesión del dispositivo."""
    headers_auth = {
        'Cookie': cookie,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'X-Netflix': 'device-auth'
    }
    
    # Petición final para asociar la cookie con el token del dispositivo
    # A menudo requiere un POST a un endpoint de autenticación de sesión
    auth_endpoint = f"https://www.netflix.com/api/shakti/auth/login?locale=en-US&deviceVersion=1.0"
    
    # Enviamos la cookie para que Netflix reconozca la cuenta y autorice el dispositivo
    response = requests.post(auth_endpoint, headers=headers_auth, data={"authToken": auth_token}, timeout=10)
    
    if response.status_code == 200:
        return "¡ACTIVACIÓN COMPLETA! Tu TV ahora tiene acceso a tu cuenta Netflix."
    else:
        return f"Cookie usada, pero hubo un error final: {response.status_code}. Prueba con otra cookie."

# --- LÓGICA DEL BOT ---

class NetflixBot:
    def __init__(self):
        self.app = ApplicationBuilder().token(BOT_TOKEN).build()
        self.conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', self.start)],
            states={
                STATE_UPLOAD_COOKIES: [MessageHandler(filters.TEXT | filters.Document.TEXT | filters.Document.PDF | filters.Document.DOC, self.handle_upload)],
                STATE_WAIT_CODE: [CommandHandler('activate', self.ask_for_code)],
                STATE_INPUT_CODE: [MessageHandler(filters.TEXT, self.process_code)],
                STATE_SELECT_COOKIE: [CallbackQueryHandler(self.select_cookie)]
            },
            fallbacks=[CommandHandler('cancel', self.cancel)]
        )
        self.app.add_handler(self.conv_handler)
        self.app.add_handler(CommandHandler("list", self.list_cookies))
        self.init_commands()

    def init_commands(self):
        self.app.add_handler(CommandHandler("upload", self.upload_mode))
        self.app.add_handler(CommandHandler("activate", self.activate_mode))
        self.app.add_handler(CommandHandler("baul", self.show_baul))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Hola. Soy tu gestor de Netflix TV.\n\n/enviar: Sube tus cookies (.txt)\n/activar: Activa tu TV\n/baul: Ver cookies válidas")

    async def upload_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Mándame el archivo .txt o .zip con las cookies.")
        return STATE_UPLOAD_COOKIES

    async def handle_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Procesar texto o archivo adjunto
        text = update.message.text
        if update.message.document:
            file = update.message.document.get_file()
            await file.download_to_drive("temp_file.txt")
            with open("temp_file.txt", "r", encoding="utf-8") as f:
                lines = f.readlines()
            text = "".join(lines)
        
        count = 0
        for line in text.split('\n'):
            line = line.strip()
            if line and 'Netflix=' in line: # Identificador común de cookies de Netflix
                save_cookie(line)
                count += 1
        
        await update.message.reply_text(f"He guardado {count} cookies en el baúl. Ahora puedes usar /activar.")
        return ConversationHandler.END

    async def show_baul(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cookies = get_valid_cookies()
        if not cookies:
            await update.message.reply_text("Tu baúl está vacío o no hay cookies validadas aún.")
            return
        
        msg = "Cookies válidas en el baúl:\n"
        for i, c in enumerate(cookies):
            short = c[:30] + "..."
            msg += f"{i+1}. {short}\n"
        await update.message.reply_text(msg)

    async def activate_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cookies = get_valid_cookies()
        if not cookies:
            await update.message.reply_text("Primero sube cookies con /enviar.")
            return
        
        keyboard = []
        for i, c in enumerate(cookies):
            short = c[:20] + "..."
            keyboard.append([InlineKeyboardButton(f"Cookie {i+1}", callback_data=f"sel_{i}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Elige una cookie del baúl para usar:", reply_markup=reply_markup)
        return STATE_SELECT_COOKIE

    async def select_cookie(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        idx = int(query.data.split('_')[1])
        cookies = get_valid_cookies()
        context.user_data['selected_cookie'] = cookies[idx]
        
        await query.edit_message_text("Cookie seleccionada. Ahora dame el código de 8 dígitos de tu TV.")
        return STATE_WAIT_CODE

    async def ask_for_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Escribe el código de 8 dígitos:")
        return STATE_INPUT_CODE

    async def process_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        code = update.message.text.strip()
        if len(code) != 8 or not code.isdigit():
            await update.message.reply_text("Ese no parece un código válido de 8 dígitos.")
            return STATE_INPUT_CODE
        
        cookie = context.user_data.get('selected_cookie')
        if not cookie:
            # Si no se eligió por teclado, pedirlo de nuevo o usar la primera disponible
            cookies = get_valid_cookies()
            if cookies:
                cookie = cookies[0]
            else:
                await update.message.reply_text("No hay cookies válidas.")
                return ConversationHandler.END

        await update.message.reply_text(f"Procesando código {code}... Por favor, acepta la autorización en tu TV ahora mismo.")
        
        result = activate_device(code, cookie)
        await update.message.reply_text(result)
        
        return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        return ConversationHandler.END

# Constantes para el estado del conversacional
STATE_UPLOAD_COOKIES = 1
STATE_WAIT_CODE = 2
STATE_INPUT_CODE = 3
STATE_SELECT_COOKIE = 4

if __name__ == '__main__':
    init_db()
    bot = NetflixBot()
    print("Bot iniciado. Esperando comandos...")
    bot.app.run_polling()
