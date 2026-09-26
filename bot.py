import os
import json
import base64
import requests
import asyncio
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    filters, 
    ContextTypes
)
from telegram.request import HTTPXRequest

# ==================== سيرفر ويب وهمي لمنع نوم البوت على Render ====================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

    def log_message(self, format, *args):
        return

def run_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()
# ==============================================================================

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==================== البيانات الأساسية ====================
TELEGRAM_TOKEN = "8991765991:AAGQ_eY8KYcCH5I5d7FW6Uzspr8_4GN8a0w"
GEMINI_API_KEY = "AQ.Ab8RN6J-H_LFDHGpCWQuttf8gqQqWj8GPZPwrJnPacETemK0MQ"
ADMIN_ID = 1133558968

USERS_FILE = "allowed_users.json"
KEYS_FILE = "valid_keys.json"
ACTIVE_TRADES_FILE = "active_trades.json"

def load_data(file_path):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_data(file_path, data):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

allowed_users = load_data(USERS_FILE)
valid_keys = load_data(KEYS_FILE)

def get_image_mime(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.png':
        return 'image/png'
    elif ext == '.webp':
        return 'image/webp'
    return 'image/jpeg'

# ==================== الاتصال المباشر بـ Google Gemini API (معدل للـ OAuth Token) ====================
def call_google_gemini_direct(image_path, prompt_text):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    mime_type = get_image_mime(image_path)
    
    try:
        with open(image_path, "rb") as img_file:
            base64_image = base64.b64encode(img_file.read()).decode('utf-8')
            
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt_text},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64_image
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 1024
            }
        }
        
        # التعديل هنا: إرسال التوكن كـ Bearer في الـ Headers عشان يتوافق مع صيغة AQ.
        headers = {
            "Authorization": f"Bearer {GEMINI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=45)
        
        if response.status_code == 200:
            result = response.json()
            try:
                return result['candidates'][0]['content']['parts'][0]['text']
            except (KeyError, IndexError):
                return "❌ تعذر استخراج التحليل من رد سيرفر جوجل."
        else:
            return f"❌ خطأ من سيرفر جوجل ({response.status_code}):\n{response.text}"
            
    except requests.exceptions.Timeout:
        return "❌ انتهت مهلة الاتصال بالسيرفر. يرجى إعادة المحاولة."
    except Exception as e:
        return f"❌ حدث خطأ أثناء المعالجة: {str(e)}"

# ==================== جلب الأسعار الحية للأسواق ====================
def get_live_price(symbol):
    try:
        clean_symbol = symbol.upper().strip()
        if "XAU" in clean_symbol or "GOLD" in clean_symbol:
            res = requests.get("https://api.gold-api.com/price/XAU", timeout=10)
            if res.status_code == 200:
                return float(res.json().get("price", 0))
        else:
            res = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
            if res.status_code == 200:
                rates = res.json().get("rates", {})
                for currency in rates:
                    if currency in clean_symbol:
                        return float(rates[currency])
    except Exception:
        pass
    return None

# ==================== دالة موحدة لعرض الصفقات النشطة ====================
async def show_user_trades(target_obj, user_id):
    trades = load_data(ACTIVE_TRADES_FILE)
    user_trades = [t for t in trades if t["chat_id"] == user_id]
    
    if not user_trades:
        await target_obj.reply_text("📭 ليس لديك أي صفقات نشطة قيد التتبع حالياً.\nNo active trades to monitor right now.")
        return
        
    for i, t in enumerate(user_trades):
        text = (
            f"📈 **صفقة نشطة Active Trade #{i+1}**\n"
            f"🔹 الرمز Symbol: {t['symbol']}\n"
            f"🔹 الاتجاه Direction: {t['direction']}\n"
            f"🔹 الدخول Entry: {t['entry']}\n"
            f"🎯 TP1: {t['tp1']} | TP2: {t['tp2']} | TP3: {t['tp3']}\n"
            f"🛑 SL: {t['sl']}"
        )
        keyboard = [[InlineKeyboardButton("❌ Close Trade / إلغاء متابعة الصفقة", callback_data=f"close_{t['symbol']}_{t['entry']}")]]
        await target_obj.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# ==================== نظام المتابعة الخلفي للسوق الحي ====================
async def background_trade_monitor(application):
    await asyncio.sleep(10)
    while True:
        try:
            trades = load_data(ACTIVE_TRADES_FILE)
            if trades:
                updated_trades = []
                for trade in trades:
                    chat_id = trade["chat_id"]
                    symbol = trade["symbol"]
                    direction = trade["direction"].upper()
                    tp1 = trade["tp1"]
                    tp2 = trade["tp2"]
                    tp3 = trade["tp3"]
                    sl = trade["sl"]
                    status = trade.get("status", {
                        "near_tp1_warned": False,
                        "tp1": False, 
                        "tp2": False, 
                        "tp3": False
                    })

                    current_price = get_live_price(symbol)
                    if current_price:
                        is_buy = "BUY" in direction
                        
                        if not status["near_tp1_warned"]:
                            distance = (tp1 - current_price) if is_buy else (current_price - tp1)
                            if 0 < distance <= 10.0:
                                status["near_tp1_warned"] = True
                                await application.bot.send_message(
                                    chat_id=chat_id, 
                                    text=f"⚠️ **تنبيه استباقي | Proactive Alert ({symbol})**:\nالسعر اقترب من TP1 بـ 10 نقاط! 🎯\n*استعد لتحريك الستوب لوز (SL) إلى نقطة الدخول (Entry) وتأمين الأرباح.*"
                                )

                        targets = [
                            (tp1, "tp1", f"تم تحقيق التيك بروفيت الأول (TP1) لصفقة {symbol} بنجاح! 🚀\n*تم نقل الستوب لوز إلى منطقة الأمان.*"),
                            (tp2, "tp2", f"تم تحقيق التيك بروفيت الثاني (TP2) لصفقة {symbol} بنجاح! 🔥"),
                            (tp3, "tp3", f"🏆 **تنبيه صفقة {symbol}**:\nتم تحقيق التيك بروفيت الثالث والأخير (TP3)! مبروك الأرباح الكبرى 💰")
                        ]
                        
                        hit_tp3 = False
                        for target_price, key, msg in targets:
                            if not status[key]:
                                reached = (current_price >= target_price) if is_buy else (current_price <= target_price)
                                if reached:
                                    status[key] = True
                                    await application.bot.send_message(chat_id=chat_id, text=msg)
                                    if key == "tp3":
                                        hit_tp3 = True
                        
                        if hit_tp3:
                            continue

                        hit_sl = (current_price <= sl) if is_buy else (current_price >= sl)
                        if hit_sl:
                            await application.bot.send_message(chat_id=chat_id, text=f"❌ **تنبيه صفقة {symbol}**:\nللأسف تم ضرب الستوب لوز (SL). Stop Loss Hit.")
                            continue

                    trade["status"] = status
                    updated_trades.append(trade)
                
                save_data(ACTIVE_TRADES_FILE, updated_trades)
        except Exception as e:
            print(f"Error in background monitor: {e}")
            
        await asyncio.sleep(30)

# ==================== الأوامر واللوحات التفاعلية ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in allowed_users or user_id == ADMIN_ID:
        keyboard = [
            [InlineKeyboardButton("📊 My Trades / صفقاتي النشطة", callback_data="my_trades")],
            [InlineKeyboardButton("💡 Help / التعليمات", callback_data="help_info")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "أهلاً بك في بوت التحليل المالي المؤسسي المتقدم (Smart Money & Price Action Sniper) 📈🤖\n\n"
            "أرسل صورة الشارت لتفعيل التحليل الهيكلي الشامل عبر جوجل جيميني:",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "🔒 **هذا البوت مدفوع ومخصص للمشتركين فقط!**\n\n"
            "قم بالتفعيل باستخدام كود التفعيل الخاص بك:\n"
            "/activate YOUR_KEY"
        )

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("⚠️ يرجى كتابة كود التفعيل بعد الأمر، مثال:\n/activate CODE123")
        return
    
    user_key = context.args[0].strip()
    global valid_keys, allowed_users
    
    if user_key in valid_keys:
        valid_keys.remove(user_key)
        save_data(KEYS_FILE, valid_keys)
        
        if user_id not in allowed_users:
            allowed_users.append(user_id)
            save_data(USERS_FILE, allowed_users)
            
        keyboard = [[InlineKeyboardButton("📊 My Trades / صفقاتي النشطة", callback_data="my_trades")]]
        await update.message.reply_text("✅ تم تفعيل اشتراكك بنجاح! يمكنك الآن إرسال صور الشارتات.", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text("❌ كود التفعيل غير صحيح أو تم استخدامه مسبقاً.")

async def generate_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("⚠️ اكتب اسم الكود المراد إنشاؤه، مثال:\n/genkey PASS_99")
        return
        
    new_key = context.args[0].strip()
    global valid_keys
    
    if new_key in valid_keys:
        await update.message.reply_text("⚠️ هذا الكود موجود بالفعل.")
        return

    valid_keys.append(new_key)
    save_data(KEYS_FILE, valid_keys)
    await update.message.reply_text(f"🔑 تم إنشاء كود جديد:\n{new_key}")

async def my_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_user_trades(update.message, update.effective_user.id)

async def analyze_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in allowed_users and user_id != ADMIN_ID:
        await update.message.reply_text("🔒 عذراً، يجب تفعيل الاشتراك أولاً.")
        return

    msg = await update.message.reply_text("⚡ جاري تفكيك الشارت والتحليل عبر Google Gemini Vision... ⏳")
    
    unique_suffix = os.urandom(4).hex()
    image_path = f"chart_{user_id}_{unique_suffix}.jpg"
    
    try:
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        elif update.message.document:
            photo_file = await update.message.document.get_file()
            ext = os.path.splitext(update.message.document.file_name or "")[1].lower()
            if ext in ['.png', '.webp', '.jpeg', '.jpg']:
                image_path = f"chart_{user_id}_{unique_suffix}{ext}"
        else:
            await msg.edit_text("رجاءً أرسل صورة شارت صالحة.")
            return

        await photo_file.download_to_drive(image_path)
        
        prompt = """
        أنت محلل فني مؤسسي محترف وخبير في الـ Price Action والـ Smart Money Concepts. قم بتحليل الشارت الحالي بدقة متناهية عبر المراحل التسلسلية التالية دون تخطي أي مرحلة:

        1. مرحلة الزونات (Zones): حدد بدقة قمة وقاع الشارت الحالي ومنطقة العرض (Supply) أو الطلب (Demand) المؤثرة.
        2. مرحلة الترند (Trend & Path): حدد اتجاه الترند المسيطر ومسار الحركة السعرية نحو الزون.
        3. مرحلة مؤشرات الـ EMA: افحص وضع السعر بالنسبة لخطوط الـ EMAs الظاهرة (مثل 50 / 200 / 800). (ملاحظة هامة: إذا لم تكن خطوط الـ EMA ظاهرة أو واضحة في الصورة، اكتب حصراً "غير متوفرة" ولا تقم باختلاقها).
        4. مرحلة شمعة السيولة (Liquidity Candle): افحص هل ظهرت شمعة اندفاعية قامت بسحب السيولة واختراق قمة/قاع؟ (إذا لم تكن واضحة، اكتب "غير ظاهرة").
        5. مرحلة التأكيد والسلوك السعري (Retest & Engulfing): 
           - افحص هل حدث ريتست مع ظهور شمعة ابتلاعية (Engulfing Candle) مؤكدة؟
           - إذا كان الريتست أو شمعة الابتلاع على وشك الحدوث خلال وقت قصير جداً، انصح بـ: "انتظر تأكيد الريتست والابتلاع".
           - إذا لم يتحقق ذلك، قم بالبناء على التحليل الفني المتاح حالياً بدقة وموضوعية.
        6. مرحلة إدارة المخاطر (Risk Management): احسب الأهداف بـ R:R لا تقل عن 1:2 مع تحديد ستوب لوز آمن خلف الزون.

        قواعد صارمة جداً لمنع الأخطاء والهلوسة:
        - التزم حصراً بالأرقام والمعطيات المرئية أمامك في الشارت. ممنوع نهائياً تقريب أو اختلاق أرقام غير موجودة.
        - أعطني النتيجة مباشرة وبدون أي مقدمات، وبناءً على التنسيق الحرفي التالي فقط ودون أي حرف زيادة:

        - Symbol: [اسم الزوج مثل EURUSD أو XAUUSD]
        - Style: [الفريم والأسلوب مثل: M5 Scalping أو H4 Swing]
        - Direction: [BUY أو SELL]
        - Step1_Zone: [تحديد القمة، القاع، والمنطقة بدقة]
        - Step2_TrendPath: [تحليل الترند والمسار الحالي]
        - Step3_EMAs: [تحليل وضع السعر بالنسبة لخطوط الـ EMA أو كتابة "غير متوفرة"]
        - Step4_LiquidityCandle: [حالة شمعة السيولة أو كتابة "غير ظاهرة"]
        - Step5_Confirmation: [حالة الريتست والشموع الابتلاعية: هل ظهرت، وشيكة، أو الاعتماد على التحليل المتاح]
        - Entry: [سعر الدخول الفعلي بدقة]
        - TP1: [الهدف الأول]
        - TP2: [الهدف الثاني]
        - TP3: [الهدف الثالث والأخير]
        - SL: [الستوب لوز الآمن]
        - RR: [مثال: 1:3]
        - Management Plan: [تنبيه استباقي قبل TP1 بـ 10 نقاط لتحريك الستوب لوز إلى Entry وتأمين الصفقة]
        """
        
        loop = asyncio.get_running_loop()
        analysis_result = await loop.run_in_executor(None, call_google_gemini_direct, image_path, prompt)
        
        keyboard = [[InlineKeyboardButton("📊 My Trades / صفقاتي النشطة", callback_data="my_trades")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await msg.edit_text(analysis_result, reply_markup=reply_markup)
        
        try:
            lines = analysis_result.split('\n')
            symbol, direction, entry, tp1, tp2, tp3, sl = "XAUUSD", "BUY", 0.0, 0.0, 0.0, 0.0, 0.0
            for line in lines:
                if "Symbol" in line: symbol = line.split(":")[-1].strip()
                if "Direction" in line: direction = line.split(":")[-1].strip()
                if "Entry" in line: entry = float(''.join(c for c in line.split(":")[-1] if c.isdigit() or c=='.'))
                if "TP1" in line: tp1 = float(''.join(c for c in line.split(":")[-1] if c.isdigit() or c=='.'))
                if "TP2" in line: tp2 = float(''.join(c for c in line.split(":")[-1] if c.isdigit() or c=='.'))
                if "TP3" in line: tp3 = float(''.join(c for c in line.split(":")[-1] if c.isdigit() or c=='.'))
                if "SL" in line: sl = float(''.join(c for c in line.split(":")[-1] if c.isdigit() or c=='.'))

            trades = load_data(ACTIVE_TRADES_FILE)
            trades.append({
                "chat_id": update.effective_chat.id,
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
                "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl,
                "status": {"near_tp1_warned": False, "tp1": False, "tp2": False, "tp3": False}
            })
            save_data(ACTIVE_TRADES_FILE, trades)
        except Exception:
            pass
        
    except Exception as e:
        await msg.edit_text(f"حدث خطأ أثناء معالجة الصورة: {e}")
        
    finally:
        if os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception:
                pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    if data == "my_trades":
        await show_user_trades(query.message, user_id)
            
    elif data == "help_info":
        await query.message.reply_text(
            "💡 **طريقة الاستخدام الاحترافي (Professional Usage):**\n"
            "1. أرسل صورة الشارت لأي زوج عملات أو ذهب.\n"
            "2. سيحلل البوت القمة والقاع، الترند، المتوسطات، شمعة السيولة، والريتست والابتلاع بسرعة فائقة.\n"
            "3. سيتم تتبّع الصفقة أوتوماتيكياً وإرسال تنبيه استباقي قبل TP1 بـ 10 نقاط لتحريك الستوب لوز وتأمين الأرباح!"
        )
        
    elif data.startswith("close_"):
        parts = data.split("_")
        target_symbol = parts[1]
        target_entry = float(parts[2])
        
        trades = load_data(ACTIVE_TRADES_FILE)
        new_trades = [t for t in trades if not (t["chat_id"] == user_id and t["symbol"] == target_symbol and t["entry"] == target_entry)]
        
        if len(trades) != len(new_trades):
            save_data(ACTIVE_TRADES_FILE, new_trades)
            await query.message.edit_text("✅ تم إلغاء متابعة وحذف الصفقة من النظام بنجاح.\nTrade successfully closed/removed.")
        else:
            await query.message.edit_text("⚠️ هذه الصفقة غير موجودة أو تم حذفها مسبقاً.\nTrade not found or already closed.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📊 My Trades / صفقاتي النشطة", callback_data="my_trades")]]
    await update.message.reply_text(
        "📸 أهلاً بك! يرجى إرسال صورة الشارت للبدء في التحليل السريع والمؤسسي.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

if __name__ == '__main__':
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(request).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("genkey", generate_key))
    app.add_handler(CommandHandler("mytrades", my_trades_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    image_filter = (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND
    app.add_handler(MessageHandler(image_filter, analyze_chart))
    
    text_filter = filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(text_filter, handle_text))
    
    async def main():
        await app.initialize()
        await app.start()
        asyncio.create_task(background_trade_monitor(app))
        print("🟢 البوت يعمل الآن بكفاءة عالية عبر جوجل جيميني المباشر...")
        await app.updater.start_polling(bootstrap_retries=-1)
        
        await asyncio.Future()

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
