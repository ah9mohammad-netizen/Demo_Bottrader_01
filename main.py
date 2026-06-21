#!/usr/bin/env python3
"""
Telegram Crypto Signal Demo Trading Bot
SL Optimization - CoinGecko Only (No Binance)
"""

import asyncio
import json
import os
import re
import requests
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Set

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

STARTING_FUND = 100.0
RESERVED_PERCENT = 0.10
ALLOCATION_PERCENT = 0.05
LEVERAGE = 10
FEE_SLIPPAGE = 0.01

TP_LEVELS = {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0}
SL_PERCENT = -1.0

STATE_FILE = "state.json"

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

# ==================== BINANCE PRICE (only for live price) ====================
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

# ==================== COINGECKO HISTORICAL DATA ====================
COINGECKO_ID_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "BNB": "binancecoin", "XRP": "ripple", "DOGE": "dogecoin",
    "TON": "the-open-network", "NOT": "notcoin", "PEPE": "pepe",
    "SHIB": "shiba-inu", "ADA": "cardano", "AVAX": "avalanche-2",
    "LINK": "chainlink", "LTC": "litecoin", "BCH": "bitcoin-cash",
    "DOT": "polkadot", "NEAR": "near", "APT": "aptos", "SUI": "sui",
    "AR": "arweave", "OP": "optimism", "ARB": "arbitrum", "INJ": "injective-protocol",
    "SEI": "sei-network", "JUP": "jupiter-exchange-solana", "WIF": "dogwifcoin",
    "BONK": "bonk", "FLOKI": "floki", "MEME": "memecoin-2",
    "ORDI": "ordinals", "RUNE": "thorchain", "DYDX": "dydx-chain",
    "STRK": "starknet", "TIA": "celestia", "JTO": "jito-governance",
    "PYTH": "pyth-network", "W": "wormhole", "AEVO": "aevo",
    "ALT": "altlayer", "ZRO": "layerzero", "IO": "io-net",
    "TAO": "bittensor", "FET": "fetch-ai", "RNDR": "render-token",
    "GRT": "the-graph", "IMX": "immutable-x", "LDO": "lido-dao",
    "AAVE": "aave", "UNI": "uniswap", "MKR": "maker", "SNX": "synthetix-network-token",
    "CRV": "curve-dao-token", "COMP": "compound-governance-token",
    "YFI": "yearn-finance", "SUSHI": "sushi", "1INCH": "1inch",
    "ZRX": "0x", "BAT": "basic-attention-token", "ENJ": "enjincoin",
    "MANA": "decentraland", "SAND": "the-sandbox", "AXS": "axie-infinity",
    "CHZ": "chiliz", "GALA": "gala", "ILV": "illuvium", "PIXEL": "pixels",
    "PORTAL": "portal", "MAVIA": "mavaverse", "PRIME": "echelon-prime",
    "DYM": "dymension", "STRK": "starknet", "ZETA": "zetachain",
    "OMNI": "omni-network", "REZ": "renzo", "ETHFI": "ether-fi",
    "BOME": "book-of-meme", "MAGA": "maga", "MOG": "mog-coin",
    "BRETT": "based-brett", "TOSHI": "toshi", "DEGEN": "degen-base",
    "HIGHER": "higher", "MOODENG": "moo-deng", "PNUT": "peanut-the-squirrel",
    "GOAT": "goatseus-maximus", "ACT": "act-i-the-ai-prophecy",
    "FARTCOIN": "fartcoin", "POPCAT": "popcat", "MEW": "cat-in-a-dogs-world",
    "BOME": "book-of-meme", "GME": "gme", "TRUMP": "maga", "MAGA": "maga",
    "MOG": "mog-coin", "BRETT": "based-brett", "TOSHI": "toshi",
    "DEGEN": "degen-base", "HIGHER": "higher", "MOODENG": "moo-deng",
    "PNUT": "peanut-the-squirrel", "GOAT": "goatseus-maximus",
    "ACT": "act-i-the-ai-prophecy", "FARTCOIN": "fartcoin",
    "POPCAT": "popcat", "MEW": "cat-in-a-dogs-world"
}

def get_coingecko_id(symbol: str) -> str:
    clean = symbol.upper().replace("USDT", "").replace("USD", "")
    return COINGECKO_ID_MAP.get(clean, clean.lower())

def get_coingecko_klines(symbol: str, start_time: int, end_time: int) -> List:
    """Fetch historical data from CoinGecko"""
    try:
        coin_id = get_coingecko_id(symbol)
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"
        params = {
            "vs_currency": "usd",
            "from": start_time // 1000,
            "to": end_time // 1000
        }
        response = requests.get(url, params=params, timeout=25)
        if response.status_code == 200:
            data = response.json()
            prices = data.get("prices", [])
            if len(prices) < 5:
                return []
            
            # Convert to kline format
            klines = []
            for i in range(len(prices)):
                ts = prices[i][0]
                price = prices[i][1]
                klines.append([ts, price, price, price, price])
            return klines
        return []
    except Exception as e:
        print(f"CoinGecko error for {symbol}: {e}")
        return []

def get_historical_klines(symbol: str, start_time: int, end_time: int) -> List:
    """Main function - CoinGecko only"""
    return get_coingecko_klines(symbol, start_time, end_time)

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

# ==================== SL OPTIMIZATION ====================
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
        return "❌ Not enough historical price data found from CoinGecko."

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
        await event.reply("✅ Bot activated!")

    elif text.startswith("/optimize_sl"):
        parts = text.split()
        days = 365
        if len(parts) > 1:
            try:
                days = int(parts[1])
            except:
                days = 365
        await event.reply(f"🔄 Analyzing optimal Stop Loss for the last {days} days (using CoinGecko)...")
        result = await optimize_stop_loss(client, days)
        await event.reply(result)

    elif text == "/help":
        await event.reply("/optimize_sl [days] - Find best Stop Loss level")

# ==================== MAIN ====================
async def main():
    print("🚀 Starting Crypto Signal Bot...")

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    print("✅ Successfully logged in")

    @client.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r'/'))
    async def command_handler(event):
        await handle_command(client, event)

    print("👂 Listening for commands...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    asyncio.run(main())
