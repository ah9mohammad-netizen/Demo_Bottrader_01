#!/usr/bin/env python3
"""
Telegram Crypto Signal Bot + ApeX Trading
"""

import asyncio
import os
import threading
from datetime import datetime

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

# Risk Management Parameters
MAX_DAILY_LOSS = 5.0
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

# ==================== TELEGRAM BOT ====================
bot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# Risk tracking variables
daily_loss = 0.0
consecutive_losses = 0
cooldown_until = None
cooldown_count = 0

# ==================== COMMANDS ====================
@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/start'))
async def cmd_start(event):
    await event.reply("✅ Bot is active. Use /test_order to place a test trade.")

@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/test_order'))
async def cmd_test_order(event):
    """Test order: ENTER-SHORT WIFUSDT @ 0.1603 with 7x leverage"""
    await event.reply("🔄 Placing test order on ApeX...")

    symbol = "WIF-USDT"
    side = "SELL"           # SHORT
    size = "10"             # Small test size (adjust as needed)
    leverage = 7
    entry_price = 0.1603

    # Calculate TP and SL prices
    tp1_price = round(entry_price * 0.99, 6)   # 1% profit
    tp2_price = round(entry_price * 0.98, 6)   # 2% profit
    sl_price = round(entry_price * 1.025, 6)   # 2.5% loss

    result = apex_client.place_market_order_with_tp_sl(
        symbol=symbol,
        side=side,
        size=size,
        leverage=leverage,
        tp_price=str(tp1_price),
        sl_price=str(sl_price)
    )

    if result:
        await event.reply(
            f"✅ Test order placed!\n\n"
            f"Symbol: {symbol}\n"
            f"Side: SHORT (7x)\n"
            f"Size: {size}\n"
            f"TP1: {tp1_price}\n"
            f"TP2: {tp2_price}\n"
            f"SL: {sl_price}"
        )
    else:
        await event.reply("❌ Failed to place test order. Check logs.")

@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/balance'))
async def cmd_balance(event):
    account = apex_client.get_account_info()
    if account:
        await event.reply(f"Account Info:\n{account}")
    else:
        await event.reply("Failed to get account info.")

@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/positions'))
async def cmd_positions(event):
    positions = apex_client.get_open_positions()
    if positions:
        await event.reply(f"Open Positions:\n{positions}")
    else:
        await event.reply("No open positions or failed to fetch.")

@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/help'))
async def cmd_help(event):
    await event.reply(
        "Commands:\n"
        "/test_order - Place test SHORT on WIFUSDT\n"
        "/balance - Show account info\n"
        "/positions - Show open positions\n"
        "/help - This message"
    )

# ==================== MAIN ====================
async def main():
    print("🚀 Starting Telegram + ApeX Trading Bot...")

    await bot.start()
    print("✅ Telegram bot started")

    # Test ApeX connection
    apex_client.test_connection()

    print("👂 Listening for commands...")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    # Start Flask in background
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    asyncio.run(main())
