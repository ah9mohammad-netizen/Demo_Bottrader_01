#!/usr/bin/env python3
"""
Telegram Crypto Signal Trading Bot
Full Strategy Implementation with Risk Management
"""

import asyncio
import os
import threading
from datetime import datetime, timedelta

from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from apex_client import ApexClient

# ==================== CONFIG ====================
API_ID = 24749992
API_HASH = "323309715087bdf4e2e132c33b3ee242"
USER_CHAT_ID = 7600450275
SIGNAL_GROUP_ID = -1002344170059

SESSION_STRING = os.getenv("SESSION_STRING")

# ==================== STRATEGY PARAMETERS ====================
LEVERAGE = 7
ALLOCATION_PERCENT = 0.05          # 5% of balance per trade
TP1_PERCENT = 1.0                  # 1% price move
TP2_PERCENT = 2.0                  # 2% price move
SL_PERCENT = 2.5                   # 2.5% price move (Stop Loss)

# Risk Management
MAX_DAILY_LOSS = 5.0               # 5% max daily loss
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_HOURS = 48
MAX_COOLDOWNS = 3

# ==================== FLASK ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Crypto Trading Bot is running"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

# ==================== APEX CLIENT ====================
apex_client = ApexClient()

# ==================== RISK MANAGEMENT STATE ====================
daily_loss = 0.0
consecutive_losses = 0
cooldown_until = None
cooldown_count = 0
last_trade_date = None

# ==================== TELEGRAM BOT ====================
bot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ==================== HELPER FUNCTIONS ====================
def is_in_cooldown():
    global cooldown_until
    if cooldown_until and datetime.now() < cooldown_until:
        return True
    return False

def check_risk_rules():
    """Check if we can open a new position"""
    global consecutive_losses, cooldown_until, cooldown_count

    if cooldown_count >= MAX_COOLDOWNS:
        return False, "Max cooldowns reached. Manual approval required."

    if is_in_cooldown():
        return False, "Currently in cooldown period."

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        cooldown_until = datetime.now() + timedelta(hours=COOLDOWN_HOURS)
        cooldown_count += 1
        consecutive_losses = 0
        return False, f"4 consecutive losses reached. Cooldown started for {COOLDOWN_HOURS} hours."

    return True, "OK"

# ==================== SIGNAL PARSING ====================
ENTRY_PATTERN = re.compile(
    r"(?:BINANCE|BITSTAMP):\s*(ENTER-)?(LONG|SHORT)[🟢🔴]*,?\s*([A-Z]+USDT)\s*,?\s*💲current price\s*=\s*([\d.]+)",
    re.IGNORECASE
)

@bot.on(events.NewMessage(chats=SIGNAL_GROUP_ID))
async def handle_signal(event):
    global consecutive_losses, daily_loss, last_trade_date

    text = event.raw_text
    match = ENTRY_PATTERN.search(text)

    if not match:
        return

    direction = match.group(2).upper()
    pair = match.group(3).upper()
    entry_price = float(match.group(4))

    # Check risk rules
    can_trade, reason = check_risk_rules()
    if not can_trade:
        await bot.send_message(USER_CHAT_ID, f"⚠️ Trade blocked: {reason}")
        return

    side = "SELL" if direction == "SHORT" else "BUY"
    symbol = pair.replace("USDT", "-USDT")

    # Calculate TP and SL prices
    if direction == "SHORT":
        tp1 = round(entry_price * (1 - TP1_PERCENT / 100), 6)
        tp2 = round(entry_price * (1 - TP2_PERCENT / 100), 6)
        sl = round(entry_price * (1 + SL_PERCENT / 100), 6)
    else:
        tp1 = round(entry_price * (1 + TP1_PERCENT / 100), 6)
        tp2 = round(entry_price * (1 + TP2_PERCENT / 100), 6)
        sl = round(entry_price * (1 - SL_PERCENT / 100), 6)

    # Get account balance (simplified)
    account = apex_client.get_account_info()
    # For now, we use fixed size. Later we can calculate 5% of balance.

    size = "10"  # Temporary fixed size for testing

    # Place order
    result = apex_client.place_market_order_with_tp_sl(
        symbol=symbol,
        side=side,
        size=size,
        leverage=LEVERAGE,
        tp_price=str(tp1),
        sl_price=str(sl)
    )

    if result:
        await bot.send_message(
            USER_CHAT_ID,
            f"✅ New Position Opened\n"
            f"{direction} {symbol} @ {entry_price}\n"
            f"Leverage: {LEVERAGE}x\n"
            f"TP1: {tp1} | TP2: {tp2}\n"
            f"SL: {sl}"
        )
    else:
        await bot.send_message(USER_CHAT_ID, "❌ Failed to open position")

# ==================== TELEGRAM COMMANDS ====================
@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/start'))
async def cmd_start(event):
    await event.reply("✅ Bot is active and listening to signals.")

@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/positions'))
async def cmd_positions(event):
    positions = apex_client.get_open_positions()
    if positions:
        await event.reply(f"Open Positions:\n{positions}")
    else:
        await event.reply("No open positions.")

@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/help'))
async def cmd_help(event):
    await event.reply(
        "Commands:\n"
        "/positions - Show open positions\n"
        "/help - This message"
    )

# ==================== MAIN ====================
async def main():
    print("🚀 Starting Trading Bot...")

    await bot.start()
    print("✅ Telegram bot started")

    apex_client.test_connection()

    print("👂 Listening for signals and commands...")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    asyncio.run(main())
