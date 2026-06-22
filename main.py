#!/usr/bin/env python3
"""
Telegram Crypto Signal Trading Bot
Main Brain - Full Strategy + TP Listening
"""

import asyncio
import os
import re
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

# ==================== STRATEGY ====================
LEVERAGE = 7
ALLOCATION_PERCENT = 0.05
TP1_PERCENT = 1.0
TP2_PERCENT = 2.0
SL_PERCENT = 2.5

MAX_DAILY_LOSS = 5.0
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_HOURS = 48
MAX_COOLDOWNS = 3

# ==================== FLASK ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Trading Bot Running"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

# ==================== CLIENT ====================
apex = ApexClient()

# ==================== RISK STATE ====================
daily_loss = 0.0
consecutive_losses = 0
cooldown_until = None
cooldown_count = 0
last_trade_date = None

# Track open trades for TP1 monitoring
open_trades = {}

# ==================== TELEGRAM ====================
bot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ==================== RISK MANAGEMENT ====================
def is_in_cooldown():
    global cooldown_until
    return cooldown_until and datetime.now() < cooldown_until

def check_risk_rules():
    global consecutive_losses, cooldown_until, cooldown_count

    if cooldown_count >= MAX_COOLDOWNS:
        return False, "Max cooldowns reached."

    if is_in_cooldown():
        return False, "Bot is in cooldown."

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        cooldown_until = datetime.now() + timedelta(hours=COOLDOWN_HOURS)
        cooldown_count += 1
        consecutive_losses = 0
        return False, "Cooldown activated (48h)."

    return True, "OK"

def calculate_position_size():
    return "10"

# ==================== SIGNAL HANDLING ====================
ENTRY_PATTERN = re.compile(
    r"(?:BINANCE|BITSTAMP):\s*(ENTER-)?(LONG|SHORT)[🟢🔴]*,?\s*([A-Z]+USDT)\s*,?\s*💲current price\s*=\s*([\d.]+)",
    re.IGNORECASE
)

TP_PATTERN = re.compile(
    r"(?:BINANCE|BITSTAMP):\s*(LONG|SHORT)[🟢🔴]-TP(\d+),?\s*([A-Z]+USDT)\s*,?\s*💲current price\s*=\s*([\d.]+)",
    re.IGNORECASE
)

@bot.on(events.NewMessage(chats=SIGNAL_GROUP_ID))
async def on_signal(event):
    global consecutive_losses

    text = event.raw_text

    # === ENTRY SIGNAL ===
    match = ENTRY_PATTERN.search(text)
    if match:
        direction = match.group(2).upper()
        pair = match.group(3).upper()
        entry_price = float(match.group(4))

        can_trade, reason = check_risk_rules()
        if not can_trade:
            await bot.send_message(USER_CHAT_ID, f"⚠️ Trade blocked: {reason}")
            return

        side = "SELL" if direction == "SHORT" else "BUY"
        symbol = pair.replace("USDT", "-USDT")

        # Calculate TP2 and SL
        if direction == "SHORT":
            tp2 = round(entry_price * (1 - TP2_PERCENT / 100), 6)
            sl = round(entry_price * (1 + SL_PERCENT / 100), 6)
        else:
            tp2 = round(entry_price * (1 + TP2_PERCENT / 100), 6)
            sl = round(entry_price * (1 - SL_PERCENT / 100), 6)

        size = calculate_position_size()

        result = apex.place_market_order_with_tp_sl(
            symbol=symbol,
            side=side,
            size=size,
            leverage=LEVERAGE,
            tp_price=str(tp2),
            sl_price=str(sl)
        )

        if result:
            open_trades[symbol] = {
                "entry_price": entry_price,
                "tp2": tp2,
                "sl": sl,
                "size": size,
                "side": side,
                "remaining_size": size
            }
            await bot.send_message(USER_CHAT_ID, f"✅ Opened {direction} {symbol}")
        else:
            await bot.send_message(USER_CHAT_ID, "❌ Failed to open position")
        return

    # === TP SIGNAL HANDLING ===
    tp_match = TP_PATTERN.search(text)
    if tp_match:
        direction = tp_match.group(1).upper()
        tp_level = int(tp_match.group(2))
        pair = tp_match.group(3).upper()
        symbol = pair.replace("USDT", "-USDT")

        if symbol in open_trades:
            trade = open_trades[symbol]

            if tp_level == 1:
                # Close 50% at TP1
                half_size = str(float(trade["size"]) * 0.5)
                apex.close_partial_position(symbol, half_size)
                trade["remaining_size"] = str(float(trade["size"]) * 0.5)
                await bot.send_message(USER_CHAT_ID, f"💰 TP1 hit on {symbol} - 50% closed")

            elif tp_level == 2:
                # Close remaining at TP2
                apex.close_partial_position(symbol, trade["remaining_size"])
                del open_trades[symbol]
                await bot.send_message(USER_CHAT_ID, f"💰 TP2 hit on {symbol} - Position closed")

# ==================== COMMANDS ====================
@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/status'))
async def status(event):
    status = "🟢 Normal"
    if is_in_cooldown():
        status = "🟡 Cooldown"
    elif cooldown_count >= MAX_COOLDOWNS:
        status = "🔴 Paused"

    await event.reply(
        f"**Bot Status**\n"
        f"Status: {status}\n"
        f"Consecutive Losses: {consecutive_losses}/{MAX_CONSECUTIVE_LOSSES}\n"
        f"Cooldowns: {cooldown_count}/{MAX_COOLDOWNS}"
    )

@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/positions'))
async def positions(event):
    pos = apex.get_open_positions()
    await event.reply(f"Positions:\n{pos}" if pos else "No positions")

@bot.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/help'))
async def help_cmd(event):
    await event.reply("/status, /positions, /help")

# ==================== MAIN ====================
async def main():
    print("🚀 Starting Trading Bot...")

    await bot.start()
    print("✅ Telegram connected")

    apex.test_connection()

    print("👂 Listening for signals...")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
