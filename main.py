#!/usr/bin/env python3
"""
main.py — Brain & Monitoring Layer
==================================
Decision maker + monitor. It does NOT talk to ApeX directly; it issues
instructions to apex_client.ApexClient (the Execution Layer).

Responsibilities
  - Connect to Telegram, listen to the signal group
  - Parse ENTRY / TP signals, compute TP1/TP2/SL
  - Enforce risk rules (daily loss, consecutive losses, cooldowns)
  - Size positions (5% of equity as margin * leverage)
  - Instruct the execution layer to open / scale-out / close
  - Monitor open positions, track wins & losses
  - Telegram commands: /start /test_order /status /positions /balance /closeall /help
  - Reset daily loss on a new day; halt + require manual approval after max cooldowns
"""

import asyncio
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from apex_client import ApexClient

# =========================================================================== #
#  CONFIG
# =========================================================================== #
API_ID = 24749992
API_HASH = "323309715087bdf4e2e132c33b3ee242"
USER_CHAT_ID = 7600450275
SIGNAL_GROUP_ID = -1002344170059
SESSION_STRING = os.getenv("SESSION_STRING")

# ---- strategy ----
LEVERAGE = 7
ALLOCATION_PERCENT = 0.05   # 5% of equity used as margin per trade

# ---- take-profit / stop-loss ----
TP1_PERCENT = 1.0           # first partial target (% from entry)
TP2_PERCENT = 2.0           # final target  (% from entry)  — attached to order
SL_PERCENT = 2.5            # stop loss     (% from entry)  — attached to order
SLIPPAGE_TOLERANCE = 0.01   # buffer for partial-close sizing

# ---- risk management ----
MAX_DAILY_LOSS = 5.0        # % of day-start equity
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_HOURS = 48
MAX_COOLDOWNS = 3

# ---- monitoring ----
MONITOR_INTERVAL = 30       # seconds between position checks

# ---- misc ----
STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")
TEST_SYMBOL = os.getenv("APEX_TEST_SYMBOL", "BTC-USDT")

# =========================================================================== #
#  FLASK (Railway health check)
# =========================================================================== #
app = Flask(__name__)


@app.route("/")
def home():
    return "✅ Trading Bot Running"


@app.route("/health")
def health():
    return {"status": "ok"}


def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))


# =========================================================================== #
#  SIGNAL PATTERNS
#  (Lenient: tolerate emoji/spacing variations from the signal provider.)
# =========================================================================== #
# [^A-Za-z0-9] is a literal character class (immune to IGNORECASE), so the
# separator between direction and symbol can never swallow the symbol itself.
ENTRY_PATTERN = re.compile(
    r"(?:BINANCE|BITSTAMP)\s*:\s*(?:ENTER[-\s]*)?(LONG|SHORT)[^A-Za-z0-9]*"
    r"([A-Z0-9]{2,15}USDT)[^A-Za-z0-9]*"
    r"(?:current\s*)?price\s*[=:]\s*[\$💰\s]*([\d.]+)",
    re.IGNORECASE,
)
TP_PATTERN = re.compile(
    r"(?:BINANCE|BITSTAMP)\s*:\s*(LONG|SHORT)[^A-Za-z0-9]*TP\s*(\d+)[^A-Za-z0-9]*"
    r"([A-Z0-9]{2,15}USDT)[^A-Za-z0-9]*"
    r"(?:current\s*)?price\s*[=:]\s*[\$💰\s]*([\d.]+)",
    re.IGNORECASE,
)


# =========================================================================== #
#  BRAIN
# =========================================================================== #
class TradingBot:
    def __init__(self):
        self.apex = None                       # ApexClient (lazy-init)
        self.tg = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

        # ---- risk state ----
        self.daily_loss_amount = 0.0           # realized losses today (USD)
        self.day_start_equity = 0.0
        self.current_day = self._today()
        self.consecutive_losses = 0
        self.cooldown_until = None              # datetime or None
        self.cooldown_count = 0
        self.manual_approval_required = False

        # ---- trade tracking ----
        # open_trades[symbol] = {
        #    "side": "BUY"/"SELL", "direction": "LONG"/"SHORT",
        #    "entry_price": float, "size": str, "remaining_size": str,
        #    "tp1": float, "tp2": float, "sl": float,
        #    "tp1_done": bool, "opened_at": float (epoch),
        # }
        self.open_trades = {}
        self.last_seen_unrealized = {}          # symbol -> last unrealizedPnl

        self.load_state()

    # ------------------------------------------------------------------ #
    #  state persistence  (survives Railway restarts; see note in README)
    # ------------------------------------------------------------------ #
    def load_state(self):
        try:
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
            self.daily_loss_amount = s.get("daily_loss_amount", 0.0)
            self.day_start_equity = s.get("day_start_equity", 0.0)
            self.current_day = s.get("current_day", self._today())
            self.consecutive_losses = s.get("consecutive_losses", 0)
            self.cooldown_count = s.get("cooldown_count", 0)
            cu = s.get("cooldown_until")
            self.cooldown_until = datetime.fromisoformat(cu) if cu else None
            self.manual_approval_required = s.get("manual_approval_required", False)
            self.open_trades = s.get("open_trades", {})
            print(f"[Brain] state loaded — day_loss={self.daily_loss_amount}, "
                  f"consec_loss={self.consecutive_losses}, cooldowns={self.cooldown_count}")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[Brain] could not load state: {e}")

    def save_state(self):
        try:
            s = {
                "daily_loss_amount": self.daily_loss_amount,
                "day_start_equity": self.day_start_equity,
                "current_day": self.current_day,
                "consecutive_losses": self.consecutive_losses,
                "cooldown_count": self.cooldown_count,
                "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
                "manual_approval_required": self.manual_approval_required,
                "open_trades": self.open_trades,
            }
            with open(STATE_FILE, "w") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            print(f"[Brain] could not save state: {e}")

    # ------------------------------------------------------------------ #
    #  small helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _today():
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async def notify(self, msg):
        """Send a message to the user's private chat."""
        try:
            await self.tg.send_message(USER_CHAT_ID, msg)
        except Exception as e:
            print(f"[Brain] notify failed: {e}")

    # ------------------------------------------------------------------ #
    #  initialization
    # ------------------------------------------------------------------ #
    async def initialize(self):
        """Connect to ApeX (with retry) and capture day-start equity."""
        attempt = 0
        while True:
            attempt += 1
            try:
                self.apex = ApexClient()
                ok = self.apex.test_connection()
                if ok:
                    self.reset_daily_if_needed()
                    await self.notify("🚀 Bot started and connected to ApeX.")
                    return True
            except Exception as e:
                print(f"[Brain] ApeX init attempt {attempt} failed: {e}")
                await self.notify(f"⚠️ ApeX init failed (attempt {attempt}): {e}")
            await asyncio.sleep(10)

    # ================================================================== #
    #  RISK MANAGEMENT
    # ================================================================== #
    def reset_daily_if_needed(self):
        """Reset the daily loss counter at the start of a new UTC day."""
        today = self._today()
        if today != self.current_day:
            self.current_day = today
            self.daily_loss_amount = 0.0
            # refresh day-start equity
            eq = self.apex.get_equity() if self.apex else 0.0
            self.day_start_equity = eq
            self.save_state()
            print(f"[Brain] new trading day — day_start_equity={eq}")

    def is_in_cooldown(self):
        return self.cooldown_until is not None and datetime.now(timezone.utc) < self.cooldown_until

    def check_risk_rules(self):
        """Return (allowed: bool, reason: str)."""
        self.reset_daily_if_needed()

        # 1) manual approval gate (after MAX_COOLDOWNS reached)
        if self.manual_approval_required:
            return False, "⛔ Trading halted — manual approval required. Send /start to resume."

        # 2) max cooldowns reached
        if self.cooldown_count >= MAX_COOLDOWNS:
            self.manual_approval_required = True
            self.save_state()
            return False, "⛔ Max cooldowns reached. Trading halted until manual approval (/start)."

        # 3) active cooldown
        if self.is_in_cooldown():
            remain = self.cooldown_until - datetime.now(timezone.utc)
            return False, f"🟡 In cooldown ({int(remain.total_seconds()//3600)}h left)."

        # 4) daily loss limit
        if self.day_start_equity > 0:
            daily_loss_pct = (self.daily_loss_amount / self.day_start_equity) * 100
            if daily_loss_pct >= MAX_DAILY_LOSS:
                return False, f"🔴 Daily loss limit reached ({daily_loss_pct:.2f}%)."

        return True, "OK"

    def on_loss(self, symbol, pnl):
        """A tracked position closed at a loss."""
        self.consecutive_losses += 1
        self.daily_loss_amount += abs(pnl)
        self.save_state()

        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)
            self.cooldown_count += 1
            self.consecutive_losses = 0
            self.save_state()
            return (f"🔴 {MAX_CONSECUTIVE_LOSSES} consecutive losses → "
                    f"{COOLDOWN_HOURS}h cooldown started (#{self.cooldown_count}/{MAX_COOLDOWNS}).")
        return None

    def on_win(self, symbol, pnl):
        """A tracked position closed at a profit."""
        self.consecutive_losses = 0
        self.save_state()
        return None

    # ================================================================== #
    #  POSITION SIZING
    # ================================================================== #
    def calculate_position_size(self, symbol, entry_price):
        """5% of equity as margin, * leverage, / price -> base units (stepSize rounded).

        Returns a string size, or None if it can't be computed / too small.
        """
        equity = self.apex.get_equity() if self.apex else 0.0
        if equity <= 0 or entry_price <= 0:
            return None

        margin = equity * ALLOCATION_PERCENT          # capital at risk
        notional = margin * LEVERAGE                  # position value
        raw_size = notional / entry_price

        size = self.apex.round_size(symbol, raw_size)
        min_size = self.apex.get_min_size(symbol)
        if min_size > 0 and float(size) < min_size:
            print(f"[Brain] computed size {size} < min {min_size} for {symbol}; skipping")
            return None
        return size

    # ================================================================== #
    #  SIGNAL HANDLING
    # ================================================================== #
    async def handle_entry(self, direction, pair, entry_price):
        side = "BUY" if direction == "LONG" else "SELL"
        symbol = self.apex.resolve_symbol(pair)

        # compute targets
        if direction == "LONG":
            tp1 = entry_price * (1 + TP1_PERCENT / 100)
            tp2 = entry_price * (1 + TP2_PERCENT / 100)
            sl = entry_price * (1 - SL_PERCENT / 100)
        else:
            tp1 = entry_price * (1 - TP1_PERCENT / 100)
            tp2 = entry_price * (1 - TP2_PERCENT / 100)
            sl = entry_price * (1 + SL_PERCENT / 100)

        # risk gate
        allowed, reason = self.check_risk_rules()
        if not allowed:
            await self.notify(f"⚠️ {direction} {symbol} blocked: {reason}")
            return

        # don't stack on an existing position for the same symbol
        if symbol in self.open_trades:
            await self.notify(f"⚠️ Already in a trade on {symbol}; skipping duplicate signal.")
            return

        size = self.calculate_position_size(symbol, entry_price)
        if not size:
            await self.notify(f"⚠️ Could not size position for {symbol} "
                              f"(equity/price/min-size). Skipping.")
            return

        # open with SL + TP2 attached (safety nets). TP1 handled on its signal.
        r = self.apex.open_position(
            symbol=symbol, side=side, size=size, leverage=LEVERAGE,
            tp_price=str(round(tp2, 6)), sl_price=str(round(sl, 6)),
        )

        if r["success"]:
            self.open_trades[symbol] = {
                "side": side, "direction": direction,
                "entry_price": entry_price, "size": size, "remaining_size": size,
                "tp1": tp1, "tp2": tp2, "sl": sl,
                "tp1_done": False, "opened_at": datetime.now(timezone.utc).timestamp(),
            }
            self.save_state()
            await self.notify(
                f"✅ Opened {direction} {symbol}\n"
                f"   size: {size} @ ~{entry_price}\n"
                f"   TP1: {tp1:.4f} (50%)\n"
                f"   TP2: {tp2:.4f} (attached)\n"
                f"   SL:  {sl:.4f} (attached)\n"
                f"   {LEVERAGE}x"
            )
        else:
            await self.notify(f"❌ Failed to open {direction} {symbol}: {r['error']}")

    async def handle_tp(self, tp_level, pair):
        symbol = self.apex.resolve_symbol(pair)
        trade = self.open_trades.get(symbol)
        if not trade:
            return  # not a position we're tracking

        if tp_level == 1 and not trade["tp1_done"]:
            # close 50% of the original size
            half = str(float(trade["size"]) * 0.5)
            r = self.apex.close_partial(symbol, half, position_side=trade["side"])
            if r["success"]:
                trade["tp1_done"] = True
                trade["remaining_size"] = str(float(trade["size"]) * 0.5)
                self.save_state()
                await self.notify(f"💰 TP1 hit on {symbol} — closed 50% ({half})")
            else:
                await self.notify(f"⚠️ TP1 close failed on {symbol}: {r['error']}")

        elif tp_level >= 2:
            # close the remainder
            r = self.apex.close_position(symbol, size=trade["remaining_size"])
            if r["success"]:
                await self.notify(f"💰 TP{tp_level} hit on {symbol} — closed remaining position")
            else:
                await self.notify(f"⚠️ TP{tp_level} close failed on {symbol}: {r['error']} "
                                  f"(position-level TP may still trigger)")

    # ================================================================== #
    #  POSITION MONITORING  (win/loss tracking, SL-hit detection)
    # ================================================================== #
    async def monitor_loop(self):
        while True:
            await asyncio.sleep(MONITOR_INTERVAL)
            try:
                await self.check_positions()
                self.reset_daily_if_needed()
            except Exception as e:
                print(f"[Brain] monitor error: {e}")

    async def check_positions(self):
        if not self.apex:
            return
        apex_positions = self.apex.get_open_positions()
        live = {p.get("symbol") for p in apex_positions if float(p.get("size") or 0) != 0}

        # remember unrealized PnL while open (fallback for win/loss if pnl query fails)
        for p in apex_positions:
            sym = p.get("symbol")
            if sym in self.open_trades:
                upnl = float(p.get("unrealizedPnl") or 0)
                self.last_seen_unrealized[sym] = upnl

        # detect closes
        for symbol in list(self.open_trades.keys()):
            if symbol in live:
                continue
            # position is gone -> it closed (SL, TP2, or manual). classify it.
            await self.handle_position_closed(symbol)

    async def handle_position_closed(self, symbol):
        trade = self.open_trades.pop(symbol, None)
        if not trade:
            return

        opened_ms = int(trade.get("opened_at", 0) * 1000)
        pnl = self.apex.get_realized_pnl(symbol, since_ms=opened_ms)
        if pnl is None:
            # fallback to last seen unrealized PnL
            pnl = self.last_seen_unrealized.pop(symbol, 0.0)
        else:
            self.last_seen_unrealized.pop(symbol, None)

        if pnl >= 0:
            msg_extra = self.on_win(symbol, pnl)
            base = f"✅ Closed {trade['direction']} {symbol} | PnL ≈ +{pnl:.2f} USD"
        else:
            msg_extra = self.on_loss(symbol, pnl)
            base = f"❌ Closed {trade['direction']} {symbol} | PnL ≈ {pnl:.2f} USD"

        await self.notify(base + (f"\n{msg_extra}" if msg_extra else ""))

    # ================================================================== #
    #  TELEGRAM COMMANDS
    # ================================================================== #
    def register_commands(self):

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/start"))
        async def _start(event):
            """Resume trading after a halt (manual approval reset)."""
            if self.manual_approval_required:
                self.manual_approval_required = False
                self.cooldown_count = 0
                self.consecutive_losses = 0
                self.cooldown_until = None
                self.save_state()
                await event.reply("✅ Trading resumed. Cooldown counters reset.")
            else:
                await event.reply("🟢 Bot is running. Commands: /status /positions "
                                  "/balance /test_order /closeall /help")

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/status"))
        async def _status(event):
            self.reset_daily_if_needed()
            if self.manual_approval_required:
                status = "🔴 Halted (manual approval required)"
            elif self.is_in_cooldown():
                remain = self.cooldown_until - datetime.now(timezone.utc)
                status = f"🟡 Cooldown ({int(remain.total_seconds()//3600)}h left)"
            else:
                status = "🟢 Active"
            loss_pct = ((self.daily_loss_amount / self.day_start_equity) * 100
                        if self.day_start_equity else 0)
            await event.reply(
                f"**Bot Status**\n"
                f"State: {status}\n"
                f"Consecutive losses: {self.consecutive_losses}/{MAX_CONSECUTIVE_LOSSES}\n"
                f"Daily loss: {self.daily_loss_amount:.2f} USD ({loss_pct:.2f}%/{MAX_DAILY_LOSS}%)\n"
                f"Cooldowns: {self.cooldown_count}/{MAX_COOLDOWNS}\n"
                f"Open trades: {len(self.open_trades)}"
            )

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/positions"))
        async def _positions(event):
            if not self.apex:
                await event.reply("ApeX not connected.")
                return
            pos = self.apex.get_open_positions()
            if not pos:
                await event.reply("No open positions.")
                return
            lines = ["**Open Positions:**"]
            for p in pos:
                lines.append(
                    f"• {p.get('symbol')} {p.get('side')} size={p.get('size')} "
                    f"entry={p.get('entryPrice')}"
                )
            await event.reply("\n".join(lines))

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/balance"))
        async def _balance(event):
            if not self.apex:
                await event.reply("ApeX not connected.")
                return
            await event.reply(
                f"**Account Balance**\n"
                f"Equity: {self.apex.get_equity():.2f}\n"
                f"Available: {self.apex.get_available_balance():.2f}"
            )

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/test_order"))
        async def _test_order(event):
            """Open + immediately close a minimum-size order to verify the pipeline.
            NOTE: this is a REAL trade (tiny size, incurs fees)."""
            if not self.apex:
                await event.reply("ApeX not connected.")
                return
            symbol = self.apex.resolve_symbol(TEST_SYMBOL.replace("-", ""))
            min_size = self.apex.get_min_size(symbol)
            size = str(min_size) if min_size > 0 else self.apex.round_size(symbol, 1)
            await event.reply(f"🧪 Test order: opening {size} {symbol} LONG...")
            r = self.apex.open_position(symbol=symbol, side="BUY", size=size, leverage=LEVERAGE)
            if not r["success"]:
                await event.reply(f"❌ Test open failed: {r['error']}")
                return
            await asyncio.sleep(3)
            cr = self.apex.close_position(symbol)
            if cr["success"]:
                await event.reply("✅ Test order opened & closed successfully. Pipeline OK.")
            else:
                await event.reply(f"⚠️ Test close failed: {cr['error']} (position is open)")

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/closeall"))
        async def _closeall(event):
            if not self.apex:
                await event.reply("ApeX not connected.")
                return
            await event.reply("🔁 Closing all positions & cancelling orders...")
            r = self.apex.close_all_positions()
            data = r.get("data") or {}
            self.open_trades.clear()
            self.save_state()
            await event.reply(f"✅ Close-all done: {data.get('closed', 0)} position(s).")

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/help"))
        async def _help(event):
            await event.reply(
                "**Commands**\n"
                "/start — resume trading after halt\n"
                "/status — risk state & counters\n"
                "/positions — open positions\n"
                "/balance — equity & available margin\n"
                "/test_order — open+close a min-size order\n"
                "/closeall — close everything\n"
                "/help — this message"
            )

    # ================================================================== #
    #  TELEGRAM SIGNAL LISTENER
    # ================================================================== #
    def register_signal_listener(self):

        @self.tg.on(events.NewMessage(chats=SIGNAL_GROUP_ID))
        async def _on_signal(event):
            if not self.apex:
                return
            text = event.raw_text or ""

            # Check TP BEFORE entry: TP signals also contain a direction + price,
            # so they would otherwise match the entry pattern.
            t = TP_PATTERN.search(text)
            if t:
                tp_level = int(t.group(2))
                pair = t.group(3).upper()
                await self.handle_tp(tp_level, pair)
                return

            m = ENTRY_PATTERN.search(text)
            if m:
                direction = m.group(1).upper()
                pair = m.group(2).upper()
                price = float(m.group(3))
                await self.handle_entry(direction, pair, price)
                return

    # ================================================================== #
    #  RUN
    # ================================================================== #
    async def run(self):
        await self.initialize()
        self.register_commands()
        self.register_signal_listener()

        await self.tg.start()
        print("[Brain] Telegram connected.")

        asyncio.create_task(self.monitor_loop())
        print("[Brain] 🎧 Listening for signals & monitoring positions...")
        await self.tg.run_until_disconnected()


# =========================================================================== #
#  ENTRYPOINT
# =========================================================================== #
bot = TradingBot()


def main():
    print("🚀 Starting Trading Bot...")
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
