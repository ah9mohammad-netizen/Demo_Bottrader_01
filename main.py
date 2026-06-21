#!/usr/bin/env python3
"""
Telegram Crypto Signal Demo Trading Bot
With Improved SL Optimization (Binance + CoinGecko Fallback)
"""

import asyncio
import json
import os
import re
import requests
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, List

from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ==================== FLASK KEEP-ALIVE ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Crypto Signal Bot is running 24/7"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

# ==================== CONFIG ====================
API_ID = 24749992
API_HASH = "323309715087bdf4e2e132c33b3ee242"
USER_CHAT_ID = 7600450275
SIGNAL_GROUP_ID = -1002344170059

SESSION_STRING = os.getenv("SESSION_STRING")

if not SESSION_STRING:
    print("❌ ERROR: SESSION_STRING not found!")
    exit(1)

# Demo trading rules
STARTING_FUND = 100.0
RESERVED_PERCENT = 0.10
ALLOCATION_PERCENT = 0.05
LEVERAGE = 10
FEE_SLIPPAGE = 0.01

TP_LEVELS = {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0}
SL_PERCENT = -1.0

STATE_FILE = "state.json"

# ==================== STATE ====================
def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "fund": STARTING_FUND,
            "reserved": STARTING_FUND * RESERVED_PERCENT,
            "positions": {},
            "stats": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0
            },
            "trade_history": [],
            "last_update": datetime.now().isoformat()
        }

def save_state(state: Dict):
    state["last_update"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

state = load_state()

# ==================== BINANCE PRICE ====================
def get_binance_price(symbol: str) -> Optional[float]:
    try:
        for suffix in ["USDT", "USD"]:
            clean = symbol.upper().replace("USDT", "").replace("USD", "")
            test_symbol = clean + suffix
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={test_symbol}"
            resp = requests.get(url, timeout=8)
            if resp.status_code == 200:
                return float(resp.json()["price"])
        return None
    except:
        return None

# ==================== HISTORICAL PRICE (Binance + CoinGecko) ====================
def get_binance_klines(symbol: str, start_time: int, end_time: int, interval: str = "15m") -> List:
    try:
        clean = symbol.upper().replace("USDT", "").replace("USD", "")
        for suffix in ["USDT", "USD"]:
            test_symbol = clean + suffix
            url = "https://api.binance.com/api/v3/klines"
            params = {
                "symbol": test_symbol,
                "interval": interval,
                "startTime": start_time,
                "endTime": end_time,
                "limit": 1000
            }
            response = requests.get(url, params=params, timeout=20)
            if response.status_code == 200:
                data = response.json()
                if len(data) >= 5:
                    return data
        return []
    except:
        return []

def get_coingecko_klines(symbol: str, start_time: int, end_time: int) -> List:
    try:
        mapping = {
            "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
            "BNB": "binancecoin", "XRP": "ripple", "DOGE": "dogecoin",
            "TON": "the-open-network", "NOT": "notcoin", "PEPE": "pepe",
            "SHIB": "shiba-inu", "ADA": "cardano", "AVAX": "avalanche-2",
            "LINK": "chainlink", "LTC": "litecoin"
        }
        clean = symbol.upper().replace("USDT", "").replace("USD", "")
        coin_id = mapping.get(clean, clean.lower())
        
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"
        params = {
            "vs_currency": "usd",
            "from": start_time // 1000,
            "to": end_time // 1000
        }
        response = requests.get(url, params=params, timeout=20)
        if response.status_code == 200:
            data = response.json()
            prices = data.get("prices", [])
            klines = []
            for i in range(len(prices)):
                ts = prices[i][0]
                price = prices[i][1]
                klines.append([ts, price, price, price, price])
            return klines
        return []
    except:
        return []

def get_historical_klines(symbol: str, start_time: int, end_time: int) -> List:
    """Try Binance first, then CoinGecko"""
    klines = get_binance_klines(symbol, start_time, end_time, "15m")
    if len(klines) >= 5:
        return klines
    
    klines = get_coingecko_klines(symbol, start_time, end_time)
    if len(klines) >= 5:
        return klines
    
    return []

def get_max_adverse_move(entry_price: float, direction: str, klines: List) -> float:
    if not klines or len(klines) < 2:
        return 0.0
    
    max_adverse = 0.0
    for kline in klines:
        low = float(kline[3])
        high = float(kline[2])
        
        if direction == "LONG":
            adverse = ((entry_price - low) / entry_price) * 100
        else:
            adverse = ((high - entry_price) / entry_price) * 100
        
        if adverse > max_adverse:
            max_adverse = adverse
    
    return round(max_adverse, 2)

# ==================== PARSING ====================
ENTRY_PATTERN = re.compile(
    r"(?:BINANCE|BITSTAMP):\s*(ENTER-)?(LONG|SHORT)[🟢🔴]*,?\s*([A-Z]+USDT|[A-Z]+USD)\s*,?\s*💲current price\s*=\s*([\d.]+)",
    re.IGNORECASE
)

TP_PATTERN = re.compile(
    r"(?:BINANCE|BITSTAMP):\s*(LONG|SHORT)[🟢🔴]-TP(\d+),?\s*([A-Z]+USDT|[A-Z]+USD)\s*,?\s*💲current price\s*=\s*([\d.]+)",
    re.IGNORECASE
)

def parse_signal(text: str) -> Optional[Dict]:
    text = text.strip()
    match = ENTRY_PATTERN.search(text)
    if match:
        return {
            "type": "entry",
            "direction": match.group(2).upper(),
            "pair": match.group(3).upper(),
            "signal_price": float(match.group(4)),
        }

    match = TP_PATTERN.search(text)
    if match:
        return {
            "type": "tp",
            "direction": match.group(1).upper(),
            "pair": match.group(3).upper(),
            "tp_level": int(match.group(2)),
            "signal_price": float(match.group(4)),
        }
    return None

# ==================== TRADING LOGIC ====================
def get_available_fund() -> float:
    used = sum(p["margin"] for p in state["positions"].values())
    return state["fund"] - state["reserved"] - used

def calculate_pnl_percent(position: Dict, current_price: float) -> float:
    entry = position["entry_price"]
    lev = position["leverage"]
    if position["direction"] == "LONG":
        return ((current_price / entry) - 1) * lev * 100
    else:
        return (1 - (current_price / entry)) * lev * 100

def open_position(pair: str, direction: str, entry_price: float, signal_price: float):
    if pair in state["positions"]:
        existing = state["positions"][pair]
        if existing["direction"] != direction:
            close_position(pair, reason="REVERSE")
        else:
            return

    allocation = state["fund"] * ALLOCATION_PERCENT
    margin = allocation
    fee = margin * FEE_SLIPPAGE
    actual_margin = margin - fee

    if actual_margin <= 0 or get_available_fund() < actual_margin:
        return

    position = {
        "pair": pair,
        "direction": direction,
        "entry_price": entry_price,
        "margin": actual_margin,
        "leverage": LEVERAGE,
        "size_percent": 100.0,
        "opened_at": datetime.now().isoformat(),
        "signal_price": signal_price,
        "partial_closes": []
    }

    state["positions"][pair] = position
    state["stats"]["total_trades"] += 1
    save_state(state)

def handle_tp(pair: str, tp_level: int, current_price: float):
    if pair not in state["positions"]:
        return

    pos = state["positions"][pair]
    pnl = calculate_pnl_percent(pos, current_price)
    target = TP_LEVELS.get(tp_level, 0)

    if pnl < target * 0.9:
        return

    close_amount = 25.0
    remaining = pos["size_percent"]
    close_amount = min(close_amount, remaining)

    portion = close_amount / 100.0
    profit = (pnl / 100) * pos["margin"] * (pos["leverage"] / 10) * portion

    pos["partial_closes"].append({
        "tp_level": tp_level,
        "pnl_percent": round(pnl, 2),
        "closed_percent": close_amount,
        "price": current_price,
        "time": datetime.now().isoformat()
    })

    pos["size_percent"] -= close_amount
    state["fund"] += profit
    state["stats"]["total_pnl"] += profit

    if pos["size_percent"] <= 0.1:
        state["positions"].pop(pair)
        state["stats"]["wins"] += 1

def close_position(pair: str, reason: str = "MANUAL", current_price: Optional[float] = None):
    if pair not in state["positions"]:
        return

    pos = state["positions"][pair]
    if current_price is None:
        current_price = get_binance_price(pair) or pos["entry_price"]

    pnl = calculate_pnl_percent(pos, current_price)
    profit = (pnl / 100) * pos["margin"] * (pos["leverage"] / 10) * (pos["size_percent"] / 100)

    state["fund"] += profit
    state["stats"]["total_pnl"] += profit

    if pnl >= 0:
        state["stats"]["wins"] += 1
    else:
        state["stats"]["losses"] += 1

    state["positions"].pop(pair)
    save_state(state)

async def check_sl(client):
    while True:
        try:
            for pair in list(state["positions"].keys()):
                pos = state["positions"].get(pair)
                if not pos:
                    continue
                current_price = get_binance_price(pair)
                if not current_price:
                    continue
                pnl = calculate_pnl_percent(pos, current_price)
                if pnl <= SL_PERCENT:
                    close_position(pair, reason="STOP_LOSS", current_price=current_price)
        except:
            pass
        await asyncio.sleep(30)

async def send_notification(client, message: str):
    try:
        await client.send_message(USER_CHAT_ID, message)
    except:
        pass

# ==================== IMPROVED SL OPTIMIZATION ====================
async def optimize_stop_loss(client, days: int = 365):
    since_date = datetime.now() - timedelta(days=days)
    
    adverse_moves = []
    successful = 0
    failed = 0
    
    open_positions = {}
    
    async for message in client.iter_messages(SIGNAL_GROUP_ID, offset_date=since_date, reverse=True):
        if not message.text:
            continue
            
        text = message.text
        msg_date = message.date
        timestamp = int(msg_date.timestamp() * 1000)
        
        entry_match = ENTRY_PATTERN.search(text)
        if entry_match:
            direction = entry_match.group(2).upper()
            pair = entry_match.group(3).upper()
            entry_price = float(entry_match.group(4))
            
            open_positions[pair] = {
                "direction": direction,
                "entry_price": entry_price,
                "entry_time": timestamp
            }
            continue
        
        tp_match = TP_PATTERN.search(text)
        if tp_match:
            direction = tp_match.group(1).upper()
            tp_level = int(tp_match.group(2))
            pair = tp_match.group(3).upper()
            
            if pair not in open_positions:
                continue
                
            pos = open_positions[pair]
            if pos["direction"] != direction:
                continue
            
            if tp_level == 2:
                klines = get_historical_klines(pair, pos["entry_time"], timestamp)
                
                if len(klines) >= 5:
                    max_adverse = get_max_adverse_move(pos["entry_price"], direction, klines)
                    adverse_moves.append(max_adverse)
                    successful += 1
                else:
                    failed += 1
            
            del open_positions[pair]

    if not adverse_moves:
        return "❌ Not enough historical price data found for meaningful analysis."

    total = len(adverse_moves)
    sl_levels = [0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]
    
    msg = f"📊 **SL Optimization Analysis ({days} Days)**\n\n"
    msg += f"**Winning Trades Analyzed:** {successful}\n"
    msg += f"**Insufficient Data:** {failed}\n\n"
    msg += "SL Level | Protection % | Recommendation\n"
    msg += "---------|-------------|----------------\n"
    
    best_sl = None
    best_pct = 0
    
    for sl in sl_levels:
        protected = sum(1 for m in adverse_moves if m <= sl)
        pct = (protected / total) * 100
        
        if pct >= 85:
            rec = "Recommended"
            if pct > best_pct:
                best_pct = pct
                best_sl = sl
        elif pct >= 75:
            rec = "Acceptable"
        else:
            rec = "Risky"
        
        msg += f"-{sl}%    | {pct:.1f}%       | {rec}\n"
    
    msg += f"\n**Recommended SL:** -{best_sl}% (protects ~{best_pct:.1f}% of winning trades)"
    
    return msg

# ==================== COMMAND HANDLER ====================
async def handle_command(client, event):
    text = event.raw_text.strip().lower()

    if text == "/start":
        await event.reply("✅ Bot activated! You will receive trade notifications here.\n\nUse /balance, /stats, /positions, /closeall, /analyze, /optimize_sl, /help")

    elif text == "/balance":
        used = sum(p["margin"] for p in state["positions"].values())
        available = state["fund"] - state["reserved"] - used
        msg = (
            f"💰 <b>Account Balance</b>\n"
            f"Total Fund: ${state['fund']:.2f}\n"
            f"Reserved (10%): ${state['reserved']:.2f}\n"
            f"Used in Positions: ${used:.2f}\n"
            f"Available: ${available:.2f}"
        )
        await event.reply(msg, parse_mode="html")

    elif text == "/stats":
        s = state["stats"]
        winrate = (s["wins"] / s["total_trades"] * 100) if s["total_trades"] > 0 else 0
        msg = (
            f"📊 <b>Trading Stats</b>\n"
            f"Total Trades: {s['total_trades']}\n"
            f"Wins: {s['wins']} | Losses: {s['losses']}\n"
            f"Winrate: {winrate:.1f}%\n"
            f"Total PNL: ${s['total_pnl']:.2f}"
        )
        await event.reply(msg, parse_mode="html")

    elif text == "/positions":
        if not state["positions"]:
            await event.reply("No open positions.")
            return

        msg = "📍 <b>Open Positions</b>\n\n"
        for pair, pos in state["positions"].items():
            current = get_binance_price(pair) or pos["entry_price"]
            pnl = calculate_pnl_percent(pos, current)
            msg += (
                f"{pair} {pos['direction']}\n"
                f"Entry: ${pos['entry_price']:.6f} | Current: ${current:.6f}\n"
                f"Margin: ${pos['margin']:.2f} | Lev: {pos['leverage']}x\n"
                f"Remaining: {pos['size_percent']:.0f}% | PNL: {pnl:.2f}%\n\n"
            )
        await event.reply(msg, parse_mode="html")

    elif text == "/closeall":
        if not state["positions"]:
            await event.reply("No positions to close.")
            return
        for pair in list(state["positions"].keys()):
            close_position(pair, reason="MANUAL")
        await event.reply("All positions closed.")

    elif text.startswith("/analyze"):
        parts = text.split()
        days = 365
        if len(parts) > 1:
            try:
                days = int(parts[1])
            except:
                days = 365
        await event.reply(f"🔄 Running simulation for the last {days} days...")
        result = await simulate_trading_strategy(client, days)
        await event.reply(result)

    elif text.startswith("/optimize_sl"):
        parts = text.split()
        days = 365
        if len(parts) > 1:
            try:
                days = int(parts[1])
            except:
                days = 365
        await event.reply(f"🔄 Analyzing optimal Stop Loss for the last {days} days...")
        result = await optimize_stop_loss(client, days)
        await event.reply(result)

    elif text == "/help":
        await event.reply(
            "Commands:\n"
            "/balance - Show fund\n"
            "/stats - Trading statistics\n"
            "/positions - Open positions\n"
            "/closeall - Close everything\n"
            "/analyze [days] - Strategy simulation\n"
            "/optimize_sl [days] - Find best Stop Loss level\n"
            "/help - This message"
        )

    else:
        await event.reply("Unknown command. Use /help")

# ==================== MAIN ====================
async def main():
    print("🚀 Starting Crypto Signal Bot...")

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    print("✅ Successfully logged in with session string")

    await send_notification(client, "✅ Crypto Demo Bot is now running and listening to signals.")

    asyncio.create_task(check_sl(client))

    @client.on(events.NewMessage(chats=SIGNAL_GROUP_ID))
    async def signal_handler(event):
        signal = parse_signal(event.raw_text)
        if not signal:
            return

        pair = signal["pair"]
        direction = signal["direction"]
        price = signal["signal_price"]

        if signal["type"] == "entry":
            open_position(pair, direction, price, price)
        elif signal["type"] == "tp":
            handle_tp(pair, signal["tp_level"], price)

    @client.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/'))
    async def command_handler(event):
        await handle_command(client, event)

    print("👂 Listening for signals and commands...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    asyncio.run(main())
