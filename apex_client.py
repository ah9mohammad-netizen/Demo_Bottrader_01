# apex_client.py
import os
from apexomni.http_private_v3 import HttpPrivate_v3
from apexomni.constants import APEX_OMNI_HTTP_MAIN, NETWORKID_MAIN


class ApexClient:
    def __init__(self):
        self.key = os.getenv("APEX_API_KEY")
        self.secret = os.getenv("APEX_API_SECRET")
        self.passphrase = os.getenv("APEX_API_PASSPHRASE")
        self.seeds = os.getenv("APEX_OMNI_KEY_SEED")
        self.l2_key = os.getenv("APEX_L2_KEY", "")

        if not all([self.key, self.secret, self.passphrase, self.seeds]):
            raise ValueError("Missing required ApeX API credentials")

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
        try:
            account = self.client.get_account_v3()
            print("✅ Successfully connected to ApeX Omni")
            print(f"Account ID: {account.get('id')}")
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

    def get_contract_balance(self):
        """Get USDT balance in contract account"""
        try:
            account = self.client.get_account_v3()
            # This may need adjustment based on actual response structure
            return account
        except Exception as e:
            print(f"Error getting balance: {e}")
            return None


if __name__ == "__main__":
    client = ApexClient()
    client.test_connection()
