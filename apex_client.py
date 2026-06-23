# apex_client.py
"""
ApeX Omni Execution Layer
Based on official apexpro-openapi examples
"""

import os
import time
from apexomni.http_private_v3 import HttpPrivateSign
from apexomni.constants import APEX_OMNI_HTTP_MAIN, NETWORKID_MAIN


class ApexClient:
    def __init__(self):
        self.key = os.getenv("APEX_API_KEY")
        self.secret = os.getenv("APEX_API_SECRET")
        self.passphrase = os.getenv("APEX_API_PASSPHRASE")
        self.seeds = os.getenv("APEX_OMNI_KEY_SEED")
        self.l2_key = os.getenv("APEX_L2_KEY", "")   # Optional

        if not all([self.key, self.secret, self.passphrase, self.seeds]):
            raise ValueError("Missing ApeX API credentials")

        self.client = HttpPrivateSign(
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
        try:
            account = self.client.get_account_v3()
            print(f"✅ Connected to ApeX | Account ID: {account.get('id')}")
            return True
        except Exception as e:
            print(f"❌ Connection failed: {e}")
            return False

    def get_account_info(self):
        try:
            return self.client.get_account_v3()
        except Exception as e:
            print(f"Error: {e}")
            return None

    def place_market_order_with_tp_sl(
        self, symbol: str, side: str, size: str, leverage: int = 7,
        tp_price: str = None, sl_price: str = None
    ):
        """
        Place market order with Stop Loss and Take Profit.
        Uses the correct method from the official examples.
        """
        try:
            current_time = int(time.time())

            params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "size": size,
                "timestampSeconds": current_time,
                "price": "0",
            }

            if sl_price:
                params.update({
                    "isOpenTpslOrder": True,
                    "isSetOpenSl": True,
                    "slPrice": sl_price,
                    "slSide": "SELL" if side == "BUY" else "BUY",
                    "slSize": size,
                    "slTriggerPrice": sl_price,
                })

            if tp_price:
                params.update({
                    "isSetOpenTp": True,
                    "tpPrice": tp_price,
                    "tpSide": "SELL" if side == "BUY" else "BUY",
                    "tpSize": size,
                    "tpTriggerPrice": tp_price,
                })

            result = self.client.create_order_v3(**params)
            print(f"✅ Order placed: {symbol} {side}")
            return result

        except Exception as e:
            print(f"❌ Order failed: {e}")
            return None

    def get_open_positions(self):
        try:
            return self.client.get_positions_v3()
        except Exception as e:
            print(f"Error getting positions: {e}")
            return None

    def close_partial_position(self, symbol: str, size: str):
        try:
            print(f"Closing {size} of position on {symbol}")
            # TODO: Implement based on ApeX API
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False
