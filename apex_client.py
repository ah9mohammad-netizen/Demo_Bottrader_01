# apex_client.py
"""
ApeX Omni Client Wrapper
Handles all interactions with ApeX Protocol (order placement, leverage, positions)
"""

import os
import time
from apexomni.http_private_v3 import HttpPrivate_v3
from apexomni.constants import APEX_OMNI_HTTP_MAIN, NETWORKID_MAIN


class ApexClient:
    """
    Wrapper class for ApeX Omni trading.
    Includes error handling and clear method documentation.
    """

    def __init__(self):
        self.key = os.getenv("APEX_API_KEY")
        self.secret = os.getenv("APEX_API_SECRET")
        self.passphrase = os.getenv("APEX_API_PASSPHRASE")
        self.seeds = os.getenv("APEX_OMNI_KEY_SEED")
        self.l2_key = os.getenv("APEX_L2_KEY", "")

        if not all([self.key, self.secret, self.passphrase, self.seeds]):
            raise ValueError("Missing ApeX API credentials in environment variables")

        self.client = HttpPrivate_v3(
            APEX_OMNI_HTTP_MAIN,
            network_id=NETWORKID_MAIN,
            zk_seeds=self.seeds,
            zk_l2Key=self.l2_key,
            api_key_credentials={
                "key": self.key,
                "secret": self.secret,
                "passphrase": self.passphrase
            }
        )

    def test_connection(self):
        """Test connection to ApeX Omni"""
        try:
            account = self.client.get_account_v3()
            print(f"✅ Connected to ApeX | Account ID: {account.get('id')}")
            return True
        except Exception as e:
            print(f"❌ Connection failed: {e}")
            return False

    def get_account_info(self):
        """Get full account details"""
        try:
            return self.client.get_account_v3()
        except Exception as e:
            print(f"Error getting account info: {e}")
            return None

    def place_market_order_with_tp_sl(
        self,
        symbol: str,
        side: str,           # "BUY" or "SELL"
        size: str,
        leverage: int = 7,
        tp_price: str = None,
        sl_price: str = None
    ):
        """
        Place a market order with optional Take Profit and Stop Loss.
        This is the main method used by the trading bot.
        """
        try:
            current_time = int(time.time())

            order_params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "size": size,
                "timestampSeconds": current_time,
                "price": "0",
            }

            if tp_price or sl_price:
                order_params["isOpenTpslOrder"] = True

                if sl_price:
                    order_params.update({
                        "isSetOpenSl": True,
                        "slPrice": sl_price,
                        "slSide": "SELL" if side == "BUY" else "BUY",
                        "slSize": size,
                        "slTriggerPrice": sl_price,
                    })

                if tp_price:
                    order_params.update({
                        "isSetOpenTp": True,
                        "tpPrice": tp_price,
                        "tpSide": "SELL" if side == "BUY" else "BUY",
                        "tpSize": size,
                        "tpTriggerPrice": tp_price,
                    })

            result = self.client.create_order_v3(**order_params)
            print(f"✅ Order placed: {symbol} {side} | Size: {size}")
            return result

        except Exception as e:
            print(f"❌ Order failed: {e}")
            return None

    def get_open_positions(self):
        """Get current open positions"""
        try:
            return self.client.get_positions_v3()
        except Exception as e:
            print(f"Error getting positions: {e}")
            return None
