import os
import logging
import time
import threading
import queue
import collections
import asyncio
import math
import re
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

import telegram_settings

class TelegramBot:
    def __init__(self):
        self.bot_token = None
        self.chat_id = None
        self.application = None
        self.bot_thread = None
        self.send_queue = queue.Queue()
        self.send_thread = None
        self.stop_event = threading.Event()
        self.fetchers = {}
        self.message_history = collections.deque(maxlen=50)
        self.main_event_loop = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)
        self.is_configured_for_sending = False
        self.is_configured_for_receiving = False

    def update_credentials(self, token, chat_id):
        self.bot_token = token
        self.chat_id = chat_id
        
        self.is_configured_for_sending = bool(token and chat_id)
        self.is_configured_for_receiving = bool(token)
        
        logging.info(f"Credentials updated. Configured for receiving: {self.is_configured_for_receiving}")
        
        if self.is_configured_for_receiving:

            if self.application and self.application.running:
                try:
                    logging.info("Bot is already running, attempting restart...")
                    self.stop() 
                    time.sleep(1) 
                    self.stop_event.clear() 
                    self.start_threads() 
                except Exception as e:
                    logging.error(f"Error restarting bot with new credentials: {e}")
            elif not self.bot_thread or not self.bot_thread.is_alive():

                self.start_threads()
        elif not self.is_configured_for_receiving:
            logging.warning("Bot token not provided. Bot will not be started.")
            self.stop() 

    def register_fetchers(self, fetcher_dict):
        self.fetchers = fetcher_dict
        logging.info("Data fetcher dictionary registered.")

    async def _send_message_async(self, bot, chat_id, text, parse_mode, reply_markup, msg_type):
        """A coroutine to safely send a message."""
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            self.logger.info(f"Telegram message sent successfully to chat_id {chat_id}.")
            self._log_history('out', text, msg_type)
        except Exception as e:
            self.logger.error(f"Telegram API connection error (async task): {e}")


    def _send_worker(self):
        logging.info("TelegramBot send worker thread started.")
        while not self.stop_event.is_set():
            try:
                message_data = self.send_queue.get(timeout=1.0)
                if message_data is None:
                    continue
                
                chat_id = message_data.get('chat_id', self.chat_id)
                message = message_data.get('message')
                parse_mode = message_data.get('parse_mode', None)
                reply_markup = message_data.get('reply_markup', None)

                if not self.is_configured_for_sending or not chat_id:
                    self.logger.warning(f"Skipping message send (not configured): {message[:20]}...")
                    continue

                if not self.application or not self.application.bot:
                    self.logger.warning("Bot application not ready, requeueing message.")
                    self.send_queue.put(message_data)
                    time.sleep(2)
                    continue

                self.logger.debug(f"Queueing async send for message to {chat_id}: {message[:30]}...")
                

                if self.main_event_loop and self.main_event_loop.is_running():
                    coro = self._send_message_async(
                        self.application.bot,
                        chat_id,
                        message,
                        parse_mode,
                        reply_markup,
                        message_data.get('msg_type', 'info')
                    )
                    asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
                elif not self.main_event_loop:
                    self.logger.error("Cannot send message, main event loop is not running.")
                    self.send_queue.put(message_data) # Requeue
                    time.sleep(2)
                else:
                    self.logger.error("Cannot send message, bot application is not initialized.")

            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Error in send worker: {e}", exc_info=True)
        
        logging.info("Send worker shutting down.")

    def _poll_worker(self):
        logging.info("TelegramBot polling worker thread started.")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.main_event_loop = loop 
        
        while not self.stop_event.is_set():
            if self.is_configured_for_receiving and not (self.application and self.application.running):
                try:
                    self.logger.info("Initializing bot application...")
                    self.application = Application.builder().token(self.bot_token).build()
                    
                    self.application.add_handler(CommandHandler("start", self._start))
                    self.application.add_handler(CommandHandler("help", self._help))
                    self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
                    
                    self.application.add_error_handler(self._error_handler)
                    
                    self.logger.info("Starting bot polling...")
                    
                    loop.run_until_complete(self.application.initialize())
                    loop.run_until_complete(self.application.updater.start_polling())
                    loop.run_until_complete(self.application.start())
                    
                    while not self.stop_event.is_set() and self.application.running:
                        loop.run_until_complete(asyncio.sleep(1))
                        
                    self.logger.info("Bot polling stopped.")
                
                except Exception as e:
                    self.logger.error(f"Error in poll worker (setup/run): {e}", exc_info=True)
                    self.application = None
                    time.sleep(10)
            elif not self.is_configured_for_receiving:
                time.sleep(5)
            else:
                time.sleep(1)
        
        if self.application:
            try:
                self.logger.info("Stopping application polling...")
                if self.application.running:
                    loop.run_until_complete(self.application.stop())
                    loop.run_until_complete(self.application.updater.stop())
                    loop.run_until_complete(self.application.shutdown())
            except Exception as e:
                self.logger.error(f"Error during application stop: {e}")
        
        loop.close()
        self.main_event_loop = None # Clear the loop
        logging.info("Poll worker shutting down.")


    def start_threads(self):
     
        if not self.is_configured_for_receiving:
            logging.warning("Cannot start bot threads: Token not configured.")
            return

        logging.info(f"Bot is running: {self.application and self.application.running}")
        
        if self.send_thread and self.send_thread.is_alive():
            logging.warning("Send thread already running.")
        else:
            self.send_thread = threading.Thread(target=self._send_worker, daemon=True, name="TelegramBotSendWorker")
            self.send_thread.start()

        if self.bot_thread and self.bot_thread.is_alive():
            logging.warning("Poll thread already running.")
        else:
            self.bot_thread = threading.Thread(target=self._poll_worker, daemon=True, name="TelegramBotPollWorker")
            self.bot_thread.start()

    def stop(self):
        logging.info("Stopping TelegramBot threads...")
        self.stop_event.set()
        
        if self.application:
            try:
                if self.application.running:
                    logging.info("Application is stopping. This might take a moment.")
                    # The poll worker's loop will handle the shutdown
            except Exception as e:
                self.logger.error(f"Error stopping bot application: {e}")
        
        if self.send_thread and self.send_thread.is_alive():
            self.send_thread.join(timeout=2.0)
        
        if self.bot_thread and self.bot_thread.is_alive():
            self.bot_thread.join(timeout=5.0)
            
        self.application = None
        logging.info("TelegramBot stopped.")

    def _log_history(self, direction, message, msg_type):
        log_entry = {
            "timestamp": time.time(),
            "direction": direction,
            "message": message,
            "type": msg_type
        }
        self.message_history.append(log_entry)

    def queue_message(self, message, msg_type='info', chat_id=None, parse_mode=None, reply_markup=None):
        if not chat_id:
            chat_id = self.chat_id
        
        if not chat_id:
            self.logger.warning(f"Cannot queue message, no Chat ID configured. Msg: {message[:20]}...")
            return

        self.send_queue.put({
            "chat_id": chat_id,
            "message": message,
            "msg_type": msg_type,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup
        })

    def send_test_message(self):
        if not self.is_configured_for_sending:
            return {"error": True, "message": "Bot is not configured to send messages."}
        
        message = "This is a test message from your AtmosEye device!"
        self.queue_message(message, msg_type='test')
        return {"error": False, "message": "Test message queued successfully."}

    def get_message_history(self):
        return list(self.message_history)

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._help(update, context)

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat_id = update.effective_chat.id
        self._log_history('in', f"/help from {user.first_name} ({chat_id})", 'command')
        
        menu_keyboard = [
            [KeyboardButton("Live Data"), KeyboardButton("System Health")],
            [KeyboardButton("Predictions"), KeyboardButton("Insight")]
        ]
        reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True, one_time_keyboard=False)

        message = (
            f"Welcome to your AtmosEye Bot, *{user.first_name}*!\n\n"
            "You can use the buttons below or type commands:\n\n"
            "• *Live Data*: Get the latest sensor readings.\n"
            "• *System Health*: Check device CPU, RAM, and uptime.\n"
            "• *Predictions*: See 30-min AI trend forecasts.\n"
            "• *Insight*: Get a plain-English summary of your air quality."
        )
        
        self.queue_message(
            message=message,
            chat_id=chat_id,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            msg_type='response'
        )

    def _format_insight_text(self, plain_text):
        """
        Formats the plain text insight into a structured Markdown message.
        Assumes the insight is structured with phrases separated by periods.
        """
        
        if not isinstance(plain_text, str):
            logging.error(f"Insight formatter received non-string data: {type(plain_text)}")
            return "⚠️ *Status Alert*\nReceived invalid insight data format from module."

        sentences = re.split(r'\.\s*', plain_text.strip())
        
        
        formatted_message = []
        
        
        if "No historical data found" in plain_text or "Error reading log data" in plain_text or "System is calibrating" in plain_text:
             return f"⚠️ *Status Alert*\n{plain_text}"
        
        
        for i, sentence in enumerate(sentences):
            if not sentence:
                continue
            
            sentence = sentence.strip()
            
            
            if i == 0 and ("Air quality" in sentence or "Analysis for the period" in sentence or "Your air has been" in sentence):
                formatted_message.append(f"**Overall Summary:** {sentence}")
            elif "peaking at" in sentence or "brief spike to" in sentence or "was generally" in sentence:
                
                formatted_message.append(f"• {sentence}")
            elif "Environmental conditions were mostly" in sentence or "Temperature and humidity have been" in sentence:
                formatted_message.append(f"\n**Environmental Stability:** {sentence}")
            else:
                
                if formatted_message and formatted_message[-1].startswith("•"):
                     formatted_message[-1] = f"{formatted_message[-1]} {sentence}" 
                else:
                     formatted_message.append(f"• {sentence}")

        
        final_text = "\n".join(formatted_message)
        
        
        if final_text.count('**') < 2:
            final_text = f"**Insight:** {plain_text}"
            
        return final_text


    def _start_insight_thread(self, chat_id):
        """Starts a new thread to generate the slow Insight, returning immediately."""
        
        def insight_generator():
            try:
                if 'insight' in self.fetchers:
                    logging.info(f"Insight thread started for chat {chat_id}")
                    
                 
                    insight_data = self.fetchers['insight'](tone='friendly')
                    plain_text = insight_data.get('insight', 'Error: Insight data was empty.')
                    
                    if not plain_text:
                        plain_text = "Insight generation returned no text."

                    formatted_message_body = self._format_insight_text(plain_text)
                    
                    
                    message = f"💡 *AtmosInsight Report*\n\n{formatted_message_body}"
                    self.queue_message(message, msg_type='response', chat_id=chat_id, parse_mode=ParseMode.MARKDOWN)
                    logging.info(f"Insight generated and queued for chat {chat_id}")
                else:
                    self.queue_message("Insight fetcher not available.", msg_type='error', chat_id=chat_id)
            except Exception as e:
                logging.error(f"Error generating insight in background thread: {e}", exc_info=True)
                self.queue_message(f"An error occurred while generating the Insight.", msg_type='error', chat_id=chat_id)
        
        
        threading.Thread(target=insight_generator, daemon=True, name="InsightGenerator").start()


    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.lower()
        user = update.effective_user
        chat_id = update.effective_chat.id
        self.logger.info(f"Received message from {user.first_name} (Chat ID: {chat_id}): '{update.message.text}'")
        self._log_history('in', update.message.text, 'message')

        try:
            if 'live' in text or 'data' in text or 'status' in text:
                if 'live' in self.fetchers:
                    data = self.fetchers['live']()
                    message = self._format_live_data(data)
                    self.queue_message(message, msg_type='response', chat_id=chat_id, parse_mode=ParseMode.MARKDOWN)
                else:
                    self.queue_message("Live data fetcher not available.", msg_type='error', chat_id=chat_id)

            elif 'health' in text:
                if 'health' in self.fetchers:
                    health_data = self.fetchers['health']()
                    message = self._format_health_data(health_data)
                    self.queue_message(message, msg_type='response', chat_id=chat_id, parse_mode=ParseMode.MARKDOWN)
                else:
                    self.queue_message("Health data fetcher not available.", msg_type='error', chat_id=chat_id)

            elif 'predict' in text or 'forecast' in text:
                if 'predict' in self.fetchers and 'history' in self.fetchers:
                    history = self.fetchers['history']()
                    if len(history) < 10:
                        self.queue_message("Not enough data for a prediction yet. Please wait.", msg_type='response', chat_id=chat_id)
                    else:
                        preds = self.fetchers['predict'](history)
                        message = self._format_prediction_data(preds)
                        self.queue_message(message, msg_type='response', chat_id=chat_id, parse_mode=ParseMode.MARKDOWN)
                else:
                    self.queue_message("Prediction fetcher not available.", msg_type='error', chat_id=chat_id)
            
            elif 'insight' in text or 'summary' in text:
                
                self.queue_message(
                    "I'm analyzing the logs and generating your insight now. Please hold...",
                    msg_type='info',
                    chat_id=chat_id
                )
                self._start_insight_thread(chat_id)
                
            else:
                self.queue_message("Sorry, I don't understand that. Try one of the menu buttons or /help.", msg_type='response', chat_id=chat_id)

        except Exception as e:
            self.logger.error(f"Error handling command '{text}': {e}", exc_info=True)
            self.queue_message(f"An error occurred while handling your request: {e}", msg_type='error', chat_id=chat_id)

    def _format_live_data(self, data):
        if not data:
            return "*No live data available yet. Please wait.*"
        
        state = data.get('state', 'SAFE')
        state_emoji = "✅"
        if state == 'SMOKE':
            state_emoji = "🚨 *SMOKE DETECTED* 🚨"
        elif state == 'WARNING':
            state_emoji = "⚠️ *WARNING*"
        elif state == 'TEST':
            state_emoji = "🧪 *TESTING*"
        
        message = (
            f"*{state_emoji} AtmosEye Live Status*\n\n"
            f"```\n"
            f"IAQ Index: {data.get('iaq', 0):.0f} ({data.get('iaq_level', 'N/A')})\n"
            f"AQI (PM2.5): {data.get('aqi', 0):.0f} ({data.get('pm25_level', 'N/A')})\n"
            f"VOC Index: {data.get('voc_index', 0):.0f}\n"
            f"CO2 Equiv: {data.get('co2_equivalent', 0):.0f} ppm\n"
            f"----------------------\n"
            f"Temperature: {data.get('temperature', 0):.1f} °C\n"
            f"Humidity: {data.get('humidity', 0):.0f} %\n"
            f"Pressure: {data.get('pressure', 0):.0f} hPa\n"
            f"----------------------\n"
            f"PM 1.0: {data.get('pm1', 0):.0f} µg/m³\n"
            f"PM 2.5: {data.get('pm25', 0):.0f} µg/m³\n"
            f"PM 10: {data.get('pm10', 0):.0f} µg/m³\n"
            f"```"
        )
        return message

    def _format_health_data(self, data):
        if not data:
            return "*Could not retrieve system health.*"
        
        uptime_str = str(data.get('uptime', 'N/A'))
        if '.' in uptime_str:
            uptime_str = up