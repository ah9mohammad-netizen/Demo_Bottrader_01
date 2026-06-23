#!/usr/bin/env python3
"""
diag.py — Run this on Railway (e.g. add temporarily as the start command)
to print the installed apexomni version and available method names.

Usage: python diag.py
"""
import apexomni

print("=" * 60)
print(f"apexomni version: {getattr(apexomni, '__version__', 'UNKNOWN')}")
try:
    import importlib.metadata as md
    print(f"installed version: {md.version('apexomni')}")
except Exception:
    pass
print("=" * 60)

from apexomni.http_private_sign import HttpPrivateSign

# List all public (non-underscore) methods
methods = sorted(m for m in dir(HttpPrivateSign) if not m.startswith("_"))
print("\nPublic methods on HttpPrivateSign:")
for m in methods:
    print(f"  {m}")

# Check specific methods we depend on
print("\n" + "=" * 60)
print("Method availability check:")
checks = [
    "get_account_v3", "get_account_balance_v3", "get_account_balance",
    "get_worst_price_v3", "get_worst_price",
    "set_initial_margin_rate_v3", "set_initial_margin_rate",
    "create_order_v3", "create_order",
    "configs_v3", "configs",
    "historical_pnl_v3", "historical_pnl",
    "delete_open_orders_v3", "delete_open_orders",
    "_get", "_post",  # low-level (should always exist)
    "set_default_account_type",
]
for name in checks:
    present = hasattr(HttpPrivateSign, name)
    print(f"  {'✅' if present else '❌'} {name}")
print("=" * 60)
