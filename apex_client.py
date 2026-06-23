# apex_client.py
"""
ApeX Omni — Execution Layer
===========================
This module ONLY talks to ApeX. It performs actions when instructed by main.py
(the Brain). It does not make any trading decisions.

Verified against:
  * apexomni==3.3.1  (apexpro-openapi SDK)
  * https://api-docs.pro.apex.exchange/  (Omni API docs)

Every trade method returns a standardized dict:
    {"success": bool, "data": <api response>, "error": "<msg or None>"}
so the Brain can branch cleanly on success/failure.
"""

import os
import time
import importlib

from apexomni.http_private_sign import HttpPrivateSign
from apexomni.constants import APEX_OMNI_HTTP_MAIN, NETWORKID_MAIN

# Raw REST endpoint paths (v3 — from the ApeX Omni API docs). Used when the SDK
# wrapper methods are missing in the installed version. NEVER use the non-_v3
# wrapper methods (get_account_balance, get_worst_price, get_account, ...) —
# those are deprecated v1-era and hit /api/v1/... -> 409 Conflict.
EP_ACCOUNT = "/api/v3/account"
EP_ACCOUNT_BALANCE = "/api/v3/account-balance"
EP_WORST_PRICE = "/api/v3/get-worst-price"
EP_SET_MARGIN_RATE = "/api/v3/set-initial-margin-rate"
EP_HISTORICAL_PNL = "/api/v3/historical-pnl"
EP_DELETE_ORDERS = "/api/v3/delete-open-orders"


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _to_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _derive_l2_key(seeds_hex):
    """Derive the public L2 key (zkKey) from the registered zk seed string.

    Mirrors the SDK's internal ZkLinkSigner.new_from_seed -> public_key().
    Needed for L2 order signing when APEX_L2_KEY is not provided.
    """
    seeds_hex = (seeds_hex or "").removeprefix("0x")
    sdk = None
    for mod in (
        "apexomni.pc.linux_x86.zklink_sdk",
        "apexomni.pc.linux_arm.zklink_sdk",
        "apexomni.arm.zklink_sdk",
        "apexomni.pc.zklink_sdk",
    ):
        try:
            sdk = importlib.import_module(mod)
            break
        except Exception:
            continue
    if sdk is None:
        raise RuntimeError(
            "Could not load native zklink_sdk signer for this platform. "
            "Set the APEX_L2_KEY env var to your registered public L2 key."
        )
    signer = sdk.ZkLinkSigner.new_from_seed(bytes.fromhex(seeds_hex))
    return signer.public_key()


# --------------------------------------------------------------------------- #
#  Execution Layer
# --------------------------------------------------------------------------- #
class ApexClient:
    def __init__(self):
        self.key = os.getenv("APEX_API_KEY")
        self.secret = os.getenv("APEX_API_SECRET")
        self.passphrase = os.getenv("APEX_API_PASSPHRASE")
        self.seeds = os.getenv("APEX_OMNI_KEY_SEED")
        self.l2_key = os.getenv("APEX_L2_KEY", "").strip()

        if not all([self.key, self.secret, self.passphrase, self.seeds]):
            raise ValueError(
                "Missing ApeX credentials. Set: APEX_API_KEY, APEX_API_SECRET, "
                "APEX_API_PASSPHRASE, APEX_OMNI_KEY_SEED"
            )

        # NOTE: we intentionally do NOT pass default_to_rwa= here.
        # That kwarg is version-dependent — on some apexomni builds
        # HttpPrivateSign.__init__(*args, rwa_prefix=None, **kwargs) forwards it
        # straight to HTTP.__init__, causing:
        #   "HTTP.__init__() got an unexpected keyword argument 'default_to_rwa'"
        # Instead we set the account type AFTER construction (see below).
        self.client = HttpPrivateSign(
            APEX_OMNI_HTTP_MAIN,
            network_id=NETWORKID_MAIN,
            zk_seeds=self.seeds,
            zk_l2Key=self.l2_key or None,
            api_key_credentials={
                "key": self.key,
                "secret": self.secret,
                "passphrase": self.passphrase,
            },
        )

        # Force the PRIMARY perpetual account (NOT the RWA/stock sub-account).
        # set_default_account_type is available on all versions and is the
        # version-safe way to override the default, which some builds set to RWA.
        try:
            self.client.set_default_account_type("primary")
        except Exception:
            pass  # older builds without RWA support already default to primary

        # L2 public key is required for order signing.
        if not getattr(self.client, "zk_l2Key", None):
            self.client.zk_l2Key = _derive_l2_key(self.seeds)

        # create_order_v3() REQUIRES both config + account snapshot to be loaded,
        # otherwise it raises. Pre-load them once.
        # Version-proof: use whichever config/account method exists.
        if hasattr(self.client, "configs_v3"):
            self.client.configs_v3()
        elif hasattr(self.client, "configs"):
            self.client.configs()
        else:
            raw = self.client._get("/api/v3/symbols", {})
            self.client.configV3 = self._unwrap(raw)

        if hasattr(self.client, "get_account_v3"):
            self.client.get_account_v3()
        else:
            raw = self.client._get(EP_ACCOUNT, {}, account_type="primary")
            self._unwrap(raw)

    # ---- internal response helper ---------------------------------------- #
    @staticmethod
    def _unwrap(resp):
        """ApeX wraps most payloads in {'data': ...}; list endpoints sometimes
        do not. Return the inner object if present, else the whole response."""
        if isinstance(resp, dict) and "data" in resp:
            inner = resp["data"]
            if isinstance(inner, (dict, list)):
                return inner
        return resp

    @staticmethod
    def _result(success, data=None, error=None):
        return {"success": success, "data": data, "error": error}

    # ===================================================================== #
    #  CONNECTION
    # ===================================================================== #
    def test_connection(self):
        """Verify the connection and print the account id. Returns True/False."""
        try:
            account = self.client.get_account_v3()
            print(f"✅ Connected to ApeX | Account ID: {account.get('id')}")
            return True
        except Exception as e:
            print(f"❌ ApeX connection failed: {e}")
            return False

    # ===================================================================== #
    #  ACCOUNT & BALANCE
    # ===================================================================== #
    def get_account_info(self):
        """Raw account data (positions, equity, etc.).

        Two-tier only: _v3 wrapper, else raw _get on the v3 endpoint.
        NOTE: never use the non-_v3 get_account (deprecated v1 -> 409).
        """
        try:
            if hasattr(self.client, "get_account_v3"):
                return self.client.get_account_v3()
            else:
                raw = self.client._get(EP_ACCOUNT, {}, account_type="primary")
                return self._unwrap(raw)
        except Exception as e:
            print(f"[ApeX] get_account_info error: {e}")
            return None

    def get_account_balance(self):
        """Unwrapped account balance payload.

        Confirmed fields: totalEquityValue, availableBalance, initialMargin,
        maintenanceMargin, symbolToOraclePrice.

        Two-tier only: _v3 wrapper, else raw _get on the v3 endpoint.
        NOTE: never use the non-_v3 wrapper (get_account_balance) — that is a
        deprecated v1-era method that hits /api/v1/account-balance -> 409.
        """
        try:
            if hasattr(self.client, "get_account_balance_v3"):
                raw = self.client.get_account_balance_v3()
            else:
                raw = self.client._get(EP_ACCOUNT_BALANCE, {}, account_type="primary")
            return self._unwrap(raw)
        except Exception as e:
            print(f"[ApeX] get_account_balance error: {e}")
            return None

    def get_equity(self):
        """Total account equity (USD)."""
        bal = self.get_account_balance()
        if not bal:
            return 0.0
        return _to_float(bal.get("totalEquityValue"))

    def get_available_balance(self):
        """Available (free) margin (USD)."""
        bal = self.get_account_balance()
        if not bal:
            return 0.0
        return _to_float(bal.get("availableBalance"))

    def get_oracle_price(self, symbol):
        """Latest oracle/mark price for a symbol from the balance payload."""
        bal = self.get_account_balance()
        if not bal:
            return None
        mapping = bal.get("symbolToOraclePrice") or {}
        info = mapping.get(symbol)
        if isinstance(info, dict):
            return _to_float(info.get("oraclePrice")) or None
        return None

    # ===================================================================== #
    #  POSITIONS
    # ===================================================================== #
    def get_open_positions(self):
        """Return a list of open positions (empty list if none).

        Positions live under get_account_v3()['positions'] (Omni) — handled
        defensively in case the key differs ('positionInfo').
        """
        account = self.get_account_info()
        if not account:
            return []
        positions = account.get("positions") or account.get("positionInfo") or []
        return positions or []

    def get_position(self, symbol):
        """Return the open position dict for `symbol`, or None."""
        for p in self.get_open_positions():
            if p.get("symbol") == symbol and _to_float(p.get("size")) != 0:
                return p
        return None

    # ===================================================================== #
    #  SYMBOL CONFIG HELPERS
    # ===================================================================== #
    def get_symbol_config(self, symbol):
        """Per-symbol config (stepSize, tickSize, minOrderSize, ...)."""
        try:
            cc = self.client.configV3.get("contractConfig", {})
            for bucket in ("perpetualContract", "prelaunchContract",
                           "predictionContract", "stockContract"):
                for v in cc.get(bucket, []) or []:
                    if v.get("symbol") == symbol or v.get("symbolDisplayName") == symbol:
                        return v
        except Exception:
            pass
        return None

    def resolve_symbol(self, pair):
        """'BTCUSDT' -> 'BTC-USDT' (or 'BTC-USDC' if USDT not listed)."""
        base = pair.upper().replace("USDT", "").replace("USDC", "")
        candidates = [f"{base}-USDT", f"{base}-USDC"]
        for sym in candidates:
            if self.get_symbol_config(sym):
                return sym
        # default to USDT form even if not confirmed (order will fail loudly)
        return candidates[0]

    def round_size(self, symbol, size):
        """Round order size down to the symbol's stepSize (avoids ORDER_SIZE_INVALID)."""
        try:
            cfg = self.get_symbol_config(symbol)
            if cfg:
                step = _to_float(cfg.get("stepSize"))
                if step > 0:
                    return str((float(size) // step) * step)
        except Exception:
            pass
        return str(size)

    def round_price(self, symbol, price):
        """Round price to the symbol's tickSize (avoids PRICE_INVALID)."""
        try:
            cfg = self.get_symbol_config(symbol)
            if cfg:
                tick = _to_float(cfg.get("tickSize"))
                if tick > 0:
                    return str(round(round(float(price) / tick) * tick, 10))
        except Exception:
            pass
        return str(price)

    def get_min_size(self, symbol):
        """Minimum order size for a symbol."""
        cfg = self.get_symbol_config(symbol)
        if cfg:
            return _to_float(cfg.get("minOrderSize"))
        return 0.0

    # ===================================================================== #
    #  LEVERAGE & MARKET PRICE
    # ===================================================================== #
    def set_leverage(self, symbol, leverage):
        """Set effective leverage for a symbol.

        Docs: initialMarginRate = "the reciprocal of the opening leverage".
        So 7x -> initialMarginRate = 1/7 = 0.142857.

        Two-tier only: _v3 wrapper, else raw _post on the v3 endpoint.
        """
        try:
            imr = round(1.0 / float(leverage), 6)
            data = {"symbol": symbol, "initialMarginRate": str(imr)}
            if hasattr(self.client, "set_initial_margin_rate_v3"):
                self.client.set_initial_margin_rate_v3(**data)
            else:
                self.client._post(EP_SET_MARGIN_RATE, data, account_type="primary")
            return True
        except Exception as e:
            print(f"[ApeX] set_leverage({symbol}, {leverage}) warning: {e}")
            return False

    def get_worst_price(self, symbol, side, size):
        """Worst acceptable fill price for a market order (slippage cap).

        Two-tier only: _v3 wrapper, else raw _get on the v3 endpoint.
        NOTE: never use the non-_v3 get_worst_price (deprecated v1 -> 409).
        """
        try:
            params = {"symbol": symbol, "side": side, "size": str(size)}
            if hasattr(self.client, "get_worst_price_v3"):
                raw = self.client.get_worst_price_v3(**params)
            else:
                raw = self.client._get(EP_WORST_PRICE, params, account_type="primary")
            data = self._unwrap(raw)
            wp = _to_float(data.get("worstPrice")) if isinstance(data, dict) else 0.0
            if wp > 0:
                return self.round_price(symbol, wp)
        except Exception as e:
            print(f"[ApeX] get_worst_price warning: {e}")
        # Fallback: oracle price +/- 1% slippage buffer.
        oracle = self.get_oracle_price(symbol)
        if oracle:
            buf = oracle * 0.01
            fallback = oracle + buf if side == "BUY" else oracle - buf
            return self.round_price(symbol, fallback)
        return "0"

    # ===================================================================== #
    #  TRADING — OPEN
    # ===================================================================== #
    def open_position(self, symbol, side, size, leverage=7,
                      tp_price=None, sl_price=None):
        """Open a MARKET position with attached SL and (optional) TP.

        - side:            'BUY' (long) or 'SELL' (short)
        - size:            base units (will be rounded to stepSize)
        - leverage:        integer, e.g. 7  (sets initialMarginRate = 1/leverage)
        - tp_price:        take-profit price (TP2 / final target). Attached to the
                           order so the position auto-closes even if the bot is down.
        - sl_price:        stop-loss price. Always attached as a safety net.

        Because the ApeX order API supports only ONE TP per order, TP1 (the first
        partial target) is handled by the Brain via close_partial() when the TP1
        signal arrives.
        """
        try:
            size = self.round_size(symbol, size)
            if _to_float(size) <= 0:
                return self._result(False, error="Size is zero after rounding")

            # 1) leverage
            self.set_leverage(symbol, leverage)

            # 2) worst-acceptable price (MARKET slippage cap)
            price = self.get_worst_price(symbol, side, size)
            if price == "0":
                return self._result(False, error="Could not determine market price")

            close_side = "SELL" if side == "BUY" else "BUY"
            params = dict(
                symbol=symbol,
                side=side,
                type="MARKET",
                size=size,
                price=price,
                timestampSeconds=int(time.time()),
                account_type="primary",
            )

            # 3) attach SL + TP as position-level OCO legs
            if sl_price:
                params.update(
                    isOpenTpslOrder=True,
                    isSetOpenSl=True,
                    slPrice=self.round_price(symbol, sl_price),
                    slSide=close_side,
                    slSize=size,
                    slTriggerPrice=self.round_price(symbol, sl_price),
                )
            if tp_price:
                params.update(
                    isSetOpenTp=True,
                    tpPrice=self.round_price(symbol, tp_price),
                    tpSide=close_side,
                    tpSize=size,
                    tpTriggerPrice=self.round_price(symbol, tp_price),
                )

            res = self.client.create_order_v3(**params)
            print(f"[ApeX] OPEN {side} {size} {symbol} @~{price} "
                  f"(lev {leverage}x, tp={tp_price}, sl={sl_price})")
            return self._result(True, data=res)

        except Exception as e:
            print(f"[ApeX] open_position error: {e}")
            return self._result(False, error=str(e))

    # ===================================================================== #
    #  TRADING — CLOSE
    # ===================================================================== #
    def _reduce_market(self, symbol, side, size):
        """Internal: place a reduce-only MARKET order (for closing)."""
        size = self.round_size(symbol, size)
        if _to_float(size) <= 0:
            return self._result(False, error="Close size is zero")
        price = self.get_worst_price(symbol, side, size)
        if price == "0":
            return self._result(False, error="Could not determine market price")
        res = self.client.create_order_v3(
            symbol=symbol,
            side=side,
            type="MARKET",
            size=size,
            reduceOnly=True,
            price=price,
            timestampSeconds=int(time.time()),
            account_type="primary",
        )
        return self._result(True, data=res)

    def close_partial(self, symbol, size, position_side=None):
        """Reduce an open position by `size` (reduce-only MARKET on the opposite side).

        position_side: 'LONG'/'BUY' or 'SHORT'/'SELL' of the open position.
        If None, it is inferred from the current account position.
        """
        try:
            if position_side is None:
                pos = self.get_position(symbol)
                if not pos:
                    return self._result(False, error=f"No open position on {symbol}")
                position_side = pos.get("side")

            close_side = "SELL" if str(position_side).upper() in ("LONG", "BUY") else "BUY"
            r = self._reduce_market(symbol, close_side, size)
            if r["success"]:
                print(f"[ApeX] CLOSE partial {close_side} {size} {symbol}")
            return r
        except Exception as e:
            print(f"[ApeX] close_partial error: {e}")
            return self._result(False, error=str(e))

    def close_position(self, symbol, size=None):
        """Fully close (or close `size` of) a position. size=None => close entire position.

        On a FULL close, attached TP/SL orders are cancelled first (they are
        reduce-only and now pointless). On a partial close they are left intact.
        """
        try:
            pos = self.get_position(symbol)
            if not pos:
                return self._result(False, error=f"No open position on {symbol}")
            if size is None:
                self.cancel_all_orders(symbol)            # drop TP/SL legs
            pos_side = pos.get("side")
            close_side = "SELL" if str(pos_side).upper() in ("LONG", "BUY") else "BUY"
            close_size = size if size else str(abs(_to_float(pos.get("size"))))
            return self._reduce_market(symbol, close_side, close_size)
        except Exception as e:
            print(f"[ApeX] close_position error: {e}")
            return self._result(False, error=str(e))

    def cancel_all_orders(self, symbol=None):
        """Cancel all open + conditional orders (removes attached TP/SL legs).

        Two-tier only: _v3 wrapper, else raw _post on the v3 endpoint.
        """
        try:
            data = {}
            if symbol:
                data["symbol"] = symbol
            if hasattr(self.client, "delete_open_orders_v3"):
                self.client.delete_open_orders_v3(**data)
            else:
                self.client._post(EP_DELETE_ORDERS, data, account_type="primary")
            return True
        except Exception as e:
            print(f"[ApeX] cancel_all_orders warning: {e}")
            return False

    def close_all_positions(self):
        """Close every open position (used by /closeall).

        Cancels all open orders first (so TP/SL legs don't re-open), then sends a
        reduce-only market order for each remaining position.
        """
        results = []
        try:
            self.cancel_all_orders()  # drop TP/SL legs
            positions = self.get_open_positions()
            if not positions:
                return self._result(True, data={"closed": 0}, error="No open positions")

            for pos in positions:
                sym = pos.get("symbol")
                pos_size = abs(_to_float(pos.get("size")))
                if pos_size <= 0:
                    continue
                pos_side = pos.get("side")
                close_side = "SELL" if str(pos_side).upper() in ("LONG", "BUY") else "BUY"
                r = self._reduce_market(sym, close_side, pos_size)
                results.append({"symbol": sym, "result": r})
            closed = sum(1 for r in results if r["result"]["success"])
            return self._result(True, data={"closed": closed, "details": results})
        except Exception as e:
            print(f"[ApeX] close_all_positions error: {e}")
            return self._result(False, error=str(e), data={"details": results})

    # ===================================================================== #
    #  PnL / WIN-LOSS DETECTION
    # ===================================================================== #
    def get_realized_pnl(self, symbol, since_ms=None):
        """Most recent realized PnL record for a symbol from historical-pnl.

        Used by the Brain to classify a closed trade as a WIN or LOSS.
        Returns the FULL record dict, or None if nothing found.

        Two-tier only: _v3 wrapper, else raw _get on the v3 endpoint.
        """
        try:
            params = {"symbol": symbol, "limit": 5, "page": 0}
            if since_ms:
                params["beginTimeInclusive"] = str(int(since_ms))
            if hasattr(self.client, "historical_pnl_v3"):
                raw = self.client.historical_pnl_v3(**params)
            else:
                raw = self.client._get(EP_HISTORICAL_PNL, params, account_type="primary")
            data = self._unwrap(raw)
            records = (data or {}).get("historicalPnl") or []
            # most recent first (largest createdAt)
            records.sort(key=lambda r: r.get("createdAt", 0), reverse=True)
            for rec in records:
                if rec.get("type") in ("CLOSE_POSITION", "LIQUIDATE", None):
                    # Return the FULL record so the Brain can classify the closure
                    # (SL vs TP) using exitPrice / isLiquidate / totalPnl.
                    return rec
            return None
        except Exception as e:
            print(f"[ApeX] get_realized_pnl warning: {e}")
            return None
