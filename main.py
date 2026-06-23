#!/usr/bin/env python3
"""
main.py — Brain & Monitoring Layer
==================================
Decision maker + monitor. It does NOT talk to ApeX directly; it issues
instructions to apex_client.ApexClient (the Execution Layer).

Behaviour contract (verified):
  - Entry signal  -> open MARKET position at live price with LEVERAGE, SL attached,
                     TP2 attached (safety net). TP1 handled on its own signal.
  - TP1 signal    -> close 50% of the ORIGINAL size.
  - TP2 signal    -> close the remainder (recorded as a WIN -> resets loss streak).
  - SL hit        -> counted as a LOSS (consecutive loss + daily loss).
  - Reverse signal on an open pair -> close the existing position, then open the
                     new one (close is always allowed; the new open is risk-gated).
  - Same-direction duplicate -> ignored.
  - Sizing: 5% of balance per position as margin; 10% of balance always reserved
             and never traded into.
  - Risk: max 5% daily loss  AND  max 4 consecutive SL-losses. Either -> 48h cooldown.
          3 cooldowns -> full stop, notify user, wait for /start.

Win/loss detection lives HERE (the Brain), because the SDK has no
"was this an SL hit?" method. We classify a vanished position using the realized
PnL record's exitPrice vs the known SL/TP2 levels (with liquidate detection).
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
ALLOCATION_PERCENT = 0.05   # 5% of balance used as margin per position
RESERVE_PERCENT = 0.10      # 10% of balance ALWAYS reserved (never traded)

# ---- take-profit / stop-loss (percent of PRICE move from entry) ----
TP1_PERCENT = 1.0           # +1% price  -> ~+7% PnL @7x ; close 50%
TP2_PERCENT = 2.0           # +2% price  -> ~+14% PnL @7x ; close 100%
SL_PERCENT = 2.5            # -2.5% price -> ~-17.5% PnL @7x ; stop loss

# ---- risk management ----
MAX_DAILY_LOSS = 5.0        # % of day-start balance
MAX_CONSECUTIVE_LOSSES = 4  # SL hits in a row
COOLDOWN_HOURS = 48
MAX_COOLDOWNS = 3

# ---- monitoring ----
MONITOR_INTERVAL = 30       # seconds between position checks

# ---- misc ----
STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")
TEST_SYMBOL = os.getenv("APEX_TEST_SYMBOL", "BTC-USDT")


def _to_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


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
#  SIGNAL PATTERNS  (emoji/spacing tolerant; [^A-Za-z0-9] separator)
# =========================================================================== #
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
        self.daily_loss_amount = 0.0           # gross SL-losses today (USD)
        self.day_start_equity = 0.0
        self.current_day = self._today()
        self.consecutive_losses = 0
        self.cooldown_until = None              # datetime or None
        self.cooldown_count = 0
        self.manual_approval_required = False

        # ---- trade tracking ----
        # open_trades[symbol] = {
        #    "side","direction","entry_price","size","remaining_size",
        #    "tp1","tp2","sl","tp1_done","opened_at" (epoch s)
        # }
        self.open_trades = {}
        self.last_seen_unrealized = {}          # symbol -> last unrealizedPnl

        self.load_state()

    # ------------------------------------------------------------------ #
    #  state persistence (survives Railway restarts)
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
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "daily_loss_amount": self.daily_loss_amount,
                    "day_start_equity": self.day_start_equity,
                    "current_day": self.current_day,
                    "consecutive_losses": self.consecutive_losses,
                    "cooldown_count": self.cooldown_count,
                    "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
                    "manual_approval_required": self.manual_approval_required,
                    "open_trades": self.open_trades,
                }, f, indent=2)
        except Exception as e:
            print(f"[Brain] could not save state: {e}")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _today():
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async def notify(self, msg):
        try:
            await self.tg.send_message(USER_CHAT_ID, msg)
        except Exception as e:
            print(f"[Brain] notify failed: {e}")

    # ------------------------------------------------------------------ #
    async def initialize(self):
        attempt = 0
        while True:
            attempt += 1
            try:
                self.apex = ApexClient()
                if self.apex.test_connection():
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
            self.day_start_equity = self.apex.get_equity() if self.apex else 0.0
            self.save_state()
            print(f"[Brain] new trading day — day_start_equity={self.day_start_equity}")

    def is_in_cooldown(self):
        return (self.cooldown_until is not None
                and datetime.now(timezone.utc) < self.cooldown_until)

    def activate_cooldown(self, reason):
        """Start a 48h cooldown. If this is the MAX_COOLDOWNS-th, halt fully."""
        self.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)
        self.cooldown_count += 1
        self.consecutive_losses = 0
        halted = self.cooldown_count >= MAX_COOLDOWNS
        if halted:
            self.manual_approval_required = True
        self.save_state()
        if halted:
            return (f"🔴 Cooldown #{self.cooldown_count}/{MAX_COOLDOWNS} ({reason}). "
                    f"MAX COOLDOWNS reached — TRADING HALTED. Send /start to resume.")
        return (f"🟡 Cooldown #{self.cooldown_count}/{MAX_COOLDOWNS} activated ({reason}) "
                f"for {COOLDOWN_HOURS}h. No new positions.")

    def check_risk_rules(self):
        """Return (allowed: bool, reason: str)."""
        self.reset_daily_if_needed()

        # 1) hard halt (after MAX_COOLDOWNS) -> needs manual /start
        if self.manual_approval_required:
            return False, "⛔ Trading halted — manual approval required. Send /start to resume."

        if self.cooldown_count >= MAX_COOLDOWNS:
            self.manual_approval_required = True
            self.save_state()
            return False, "⛔ Max cooldowns reached. Halted until manual approval (/start)."

        # 2) active cooldown
        if self.is_in_cooldown():
            remain = self.cooldown_until - datetime.now(timezone.utc)
            return False, f"🟡 In cooldown ({int(remain.total_seconds()//3600)}h left)."

        # 3) daily loss backstop (cooldown is normally activated in on_loss, this
        #    is a safety net in case state was edited manually)
        if self.day_start_equity > 0:
            if (self.daily_loss_amount / self.day_start_equity) * 100 >= MAX_DAILY_LOSS:
                return False, f"🔴 Daily loss limit reached ({MAX_DAILY_LOSS}%)."

        return True, "OK"

    def on_loss(self, symbol, pnl):
        """An SL hit closed a position. Count it; maybe trigger cooldown."""
        self.consecutive_losses += 1
        self.daily_loss_amount += abs(pnl)
        self.save_state()

        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return self.activate_cooldown(f"{MAX_CONSECUTIVE_LOSSES} consecutive SL losses")
        if self.day_start_equity > 0 and \
                (self.daily_loss_amount / self.day_start_equity) * 100 >= MAX_DAILY_LOSS:
            return self.activate_cooldown(f"daily loss limit {MAX_DAILY_LOSS}% reached")
        return None

    def on_win(self, symbol, pnl):
        """A TP closed a position. Reset the loss streak."""
        self.consecutive_losses = 0
        self.save_state()
        return None

    # ================================================================== #
    #  POSITION SIZING  (5% per position; 10% reserve enforced)
    # ================================================================== #
    def calculate_position_size(self, symbol, entry_price):
        equity = self.apex.get_equity() if self.apex else 0.0
        if equity <= 0 or entry_price <= 0:
            return None

        reserve = equity * RESERVE_PERCENT          # 10% never traded
        margin = equity * ALLOCATION_PERCENT         # 5% of balance per position

        # Never let a new position dip into the reserve: after taking `margin`,
        # the free balance must still cover the reserve.
        avail = self.apex.get_available_balance()
        if avail - margin < reserve:
            margin = avail - reserve                  # clamp to what's safely usable
        if margin <= 0:
            print(f"[Brain] no usable margin above reserve for {symbol}; skipping")
            return None

        notional = margin * LEVERAGE
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

        if direction == "LONG":
            tp1 = entry_price * (1 + TP1_PERCENT / 100)
            tp2 = entry_price * (1 + TP2_PERCENT / 100)
            sl = entry_price * (1 - SL_PERCENT / 100)
        else:
            tp1 = entry_price * (1 - TP1_PERCENT / 100)
            tp2 = entry_price * (1 - TP2_PERCENT / 100)
            sl = entry_price * (1 + SL_PERCENT / 100)

        # ---- existing position on same pair? ----
        existing = self.open_trades.get(symbol)
        if existing:
            if existing["direction"] == direction:
                await self.notify(f"⚠️ Already {direction} on {symbol}; ignoring duplicate signal.")
                return
            # REVERSE: close the existing position (always allowed), then open new.
            await self.notify(
                f"🔁 Reverse on {symbol}: closing {existing['direction']} to open {direction}.")
            cr = self.apex.close_position(symbol)     # full close -> cancels TP/SL too
            self.open_trades.pop(symbol, None)        # neutral: NOT a win or loss
            if not cr["success"]:
                await self.notify(
                    f"❌ Reverse close failed on {symbol}: {cr['error']}. New position NOT opened.")
                return
            # fall through to open the new position (risk-gated below)

        # ---- risk gate (applies to the new open, incl. after a reverse) ----
        allowed, reason = self.check_risk_rules()
        if not allowed:
            await self.notify(f"⚠️ {direction} {symbol} blocked: {reason}")
            return

        size = self.calculate_position_size(symbol, entry_price)
        if not size:
            await self.notify(f"⚠️ Could not size {symbol} (equity/reserve/min-size). Skipping.")
            return

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
                f"   size: {size} @ ~{entry_price}  ({LEVERAGE}x)\n"
                f"   TP1: {tp1:.4f} (close 50%)\n"
                f"   TP2: {tp2:.4f} (close 100%)\n"
                f"   SL:  {sl:.4f} (stop)")
        else:
            await self.notify(f"❌ Failed to open {direction} {symbol}: {r['error']}")

    async def handle_tp(self, tp_level, pair):
        symbol = self.apex.resolve_symbol(pair)
        trade = self.open_trades.get(symbol)
        if not trade:
            return  # not tracking a position here

        if tp_level == 1 and not trade["tp1_done"]:
            # close 50% of the ORIGINAL size
            half = str(float(trade["size"]) * 0.5)
            r = self.apex.close_partial(symbol, half, position_side=trade["side"])
            if r["success"]:
                trade["tp1_done"] = True
                trade["remaining_size"] = str(float(trade["size"]) * 0.5)
                self.save_state()
                await self.notify(f"💰 TP1 on {symbol} — closed 50% ({half})")
            else:
                await self.notify(f"⚠️ TP1 close failed on {symbol}: {r['error']}")

        elif tp_level >= 2:
            # close the remainder and record a WIN (resets loss streak)
            r = self.apex.close_position(symbol)     # full close -> cancels TP/SL
            if r["success"]:
                self.open_trades.pop(symbol, None)
                self.on_win(symbol, 0.0)
                self.save_state()
                await self.notify(f"💰 TP{tp_level} on {symbol} — position closed (WIN).")
            else:
                await self.notify(
                    f"⚠️ TP{tp_level} close failed on {symbol}: {r['error']} "
                    f"(exchange-level TP may still trigger)")

    # ================================================================== #
    #  POSITION MONITORING  (detect exchange-side SL / TP auto-triggers)
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
        """Detect positions that vanished WITHOUT us explicitly closing them
        (i.e. exchange-side SL or TP2 fired). Explicit closes (TP2 signal,
        reverse, /closeall) pop the trade first, so they are not reclassified here."""
        if not self.apex:
            return
        apex_positions = self.apex.get_open_positions()
        live = {p.get("symbol") for p in apex_positions
                if _to_float(p.get("size")) != 0}

        for p in apex_positions:
            sym = p.get("symbol")
            if sym in self.open_trades:
                self.last_seen_unrealized[sym] = _to_float(p.get("unrealizedPnl"))

        for symbol in list(self.open_trades.keys()):
            if symbol not in live:
                await self.handle_position_closed(symbol)

    def classify_closure(self, symbol, trade):
        """Decide if a vanished position was an SL hit (loss) or TP hit (win).

        The SDK exposes no 'closure reason', so the Brain infers it from the
        realized-PnL record: exitPrice vs the known SL/TP2, plus isLiquidate.
        Returns (outcome, pnl, reason).
        """
        opened_ms = int(trade.get("opened_at", 0) * 1000)
        rec = self.apex.get_realized_pnl(symbol, since_ms=opened_ms)  # dict or None
        pnl = None
        tol = 0.005  # 0.5% tolerance on price

        if rec:
            pnl = _to_float(rec.get("totalPnl"), None)
            if rec.get("isLiquidate"):
                return ("loss", pnl if pnl is not None else 0.0, "liquidation")
            exit_price = _to_float(rec.get("exitPrice"))
            sl, tp2 = trade.get("sl"), trade.get("tp2")
            direction = trade.get("direction")
            if exit_price > 0 and sl and tp2:
                if direction == "LONG":
                    if exit_price <= sl * (1 + tol):
                        return ("loss", pnl if pnl is not None else 0.0, "sl_hit")
                    if exit_price >= tp2 * (1 - tol):
                        return ("win", pnl if pnl is not None else 0.0, "tp_hit")
                else:  # SHORT
                    if exit_price >= sl * (1 - tol):
                        return ("loss", pnl if pnl is not None else 0.0, "sl_hit")
                    if exit_price <= tp2 * (1 + tol):
                        return ("win", pnl if pnl is not None else 0.0, "tp_hit")

        # fallback: use realized PnL sign, else last seen unrealized
        if pnl is None:
            pnl = self.last_seen_unrealized.get(symbol, 0.0)
        return (("win" if (pnl or 0) >= 0 else "loss"), pnl or 0.0, "pnl_fallback")

    async def handle_position_closed(self, symbol):
        trade = self.open_trades.pop(symbol, None)
        if not trade:
            return
        outcome, pnl, reason = self.classify_closure(symbol, trade)
        self.last_seen_unrealized.pop(symbol, None)

        if outcome == "loss":
            extra = self.on_loss(symbol, pnl)
            base = (f"❌ {trade['direction']} {symbol} SL HIT | "
                    f"PnL ≈ {pnl:.2f} USD ({reason})")
        else:
            extra = self.on_win(symbol, pnl)
            base = (f"✅ {trade['direction']} {symbol} TP HIT | "
                    f"PnL ≈ +{pnl:.2f} USD ({reason})")
        await self.notify(base + (f"\n{extra}" if extra else ""))

    # ================================================================== #
    #  TELEGRAM COMMANDS
    # ================================================================== #
    def register_commands(self):

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/start"))
        async def _start(event):
            if self.manual_approval_required:
                self.manual_approval_required = False
                self.cooldown_count = 0
                self.consecutive_losses = 0
                self.cooldown_until = None
                self.save_state()
                await event.reply("✅ Trading resumed. Cooldown counters reset.")
            else:
                await event.reply("🟢 Bot running. Commands: /status /positions "
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
                f"Consecutive SL losses: {self.consecutive_losses}/{MAX_CONSECUTIVE_LOSSES}\n"
                f"Daily loss: {self.daily_loss_amount:.2f} USD ({loss_pct:.2f}%/{MAX_DAILY_LOSS}%)\n"
                f"Cooldowns: {self.cooldown_count}/{MAX_COOLDOWNS}\n"
                f"Open trades: {len(self.open_trades)}")

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
                    f"entry={p.get('entryPrice')}")
            await event.reply("\n".join(lines))

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/balance"))
        async def _balance(event):
            if not self.apex:
                await event.reply("ApeX not connected.")
                return
            equity = self.apex.get_equity()
            reserve = equity * RESERVE_PERCENT
            await event.reply(
                f"**Account Balance**\n"
                f"Equity: {equity:.2f}\n"
                f"Available: {self.apex.get_available_balance():.2f}\n"
                f"Reserved (never traded): {reserve:.2f}")

        @self.tg.on(events.NewMessage(from_users=USER_CHAT_ID, pattern=r"/test_order"))
        async def _test_order(event):
            """Open + immediately close a min-size order to verify the pipeline.
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
                await event.reply("✅ Test order opened & closed. Pipeline OK.")
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
            # manual close -> neutral: do NOT count wins/losses
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
                "/balance — equity, available & reserve\n"
                "/test_order — open+close a min-size order\n"
                "/closeall — close everything\n"
                "/help — this message")

    # ================================================================== #
    #  TELEGRAM SIGNAL LISTENER
    # ================================================================== #
    def register_signal_listener(self):

        @self.tg.on(events.NewMessage(chats=SIGNAL_GROUP_ID))
        async def _on_signal(event):
            if not self.apex:
                return
            text = event.raw_text or ""

            # TP checked BEFORE entry (TP signals also contain direction+price).
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
