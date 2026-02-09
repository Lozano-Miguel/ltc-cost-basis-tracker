#!/usr/bin/env python3
"""
LTC Cost Basis Tracker
======================
Automatically fetches transactions from your LTC addresses,
records the LTC price at the time of each transaction,
and calculates your weighted average cost basis + target sell price.

Usage:
  python tracker.py          # Normal run (sync new transactions)
  python tracker.py --full   # Fetch ALL transactions (ignore limits)
  python tracker.py --reset  # Delete data and start fresh

APIs used (all free, no API keys needed):
  - Blockcypher: LTC transaction history & balances
  - CryptoCompare: LTC price (current + historical)
"""

import json
import re
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("\n  [!] Missing 'requests' library.\n")
    print("  Install it with:")
    print("    pip install requests")
    print("  Or:")
    print("    pip install -r requirements.txt\n")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
DATA_PATH = SCRIPT_DIR / "data.json"
DATA_JS_PATH = SCRIPT_DIR / "data.js"

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
BLOCKCYPHER_BASE = "https://api.blockcypher.com/v1/ltc/main"
CRYPTOCOMPARE_BASE = "https://min-api.cryptocompare.com/data"

# Cache for historical prices (avoids duplicate API calls within a run)
_price_cache = {}


# ---------------------------------------------------------------------------
# First-run interactive setup
# ---------------------------------------------------------------------------
def interactive_setup():
    """Guide the user through first-time configuration."""
    print()
    print("=" * 55)
    print("  LTC Cost Basis Tracker — First Time Setup")
    print("=" * 55)
    print()
    print("  This tool tracks your Litecoin transactions and")
    print("  calculates your weighted average cost basis.")
    print()
    print("  You'll need your LTC receiving addresses.")
    print("  (Find them in Electrum → Addresses tab)")
    print()

    addresses = []
    print("  Paste your LTC addresses one by one.")
    print("  Press Enter on an empty line when done.\n")

    while True:
        prompt = f"  Address #{len(addresses) + 1} (or Enter to finish): "
        addr = input(prompt).strip()

        if not addr:
            if len(addresses) == 0:
                print("  [!] You need at least one address.\n")
                continue
            break

        # Basic validation: LTC addresses start with L, M, 3, or ltc1
        if not re.match(r'^(ltc1|[LM3])[a-zA-Z0-9]{25,62}$', addr):
            print("  [!] That doesn't look like a valid LTC address. Try again.\n")
            continue

        if addr in addresses:
            print("  [!] Duplicate address, skipping.\n")
            continue

        addresses.append(addr)
        print(f"  [✓] Added ({len(addresses)} total)\n")

    # Target profit
    print()
    while True:
        target_input = input("  Target profit % [default: 3]: ").strip()
        if not target_input:
            target_profit = 3.0
            break
        try:
            target_profit = float(target_input)
            if target_profit <= 0:
                print("  [!] Must be a positive number.\n")
                continue
            break
        except ValueError:
            print("  [!] Enter a number (e.g. 3, 5.5, 10).\n")

    # Currency
    currency = input("  Currency [default: usd]: ").strip().lower() or "usd"

    config = {
        "addresses": addresses,
        "target_profit_percent": target_profit,
        "currency": currency
    }

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\n  [✓] Config saved to config.json")
    print(f"  [i] Tracking {len(addresses)} address(es) with {target_profit}% target\n")

    return config


# ---------------------------------------------------------------------------
# Config & data management
# ---------------------------------------------------------------------------
def load_config():
    """Load config.json or run interactive setup if it doesn't exist."""
    if not CONFIG_PATH.exists():
        return interactive_setup()

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    # Check for placeholder addresses
    if not cfg.get("addresses") or any(a.startswith("YOUR_") for a in cfg["addresses"]):
        print("\n  [!] config.json contains placeholder addresses.")
        response = input("  Run interactive setup? (Y/n): ").strip().lower()
        if response in ("", "y", "yes"):
            return interactive_setup()
        else:
            print("  Edit config.json manually and run again.")
            sys.exit(0)

    return cfg


def load_data():
    """Load existing data.json or return empty structure."""
    if DATA_PATH.exists():
        with open(DATA_PATH) as f:
            return json.load(f)
    return {
        "transactions": [],
        "seen_txids": [],
        "summary": {},
        "last_updated": None
    }


def save_data(data):
    """Save data.json and data.js (for the dashboard)."""
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)

    # Write data.js so dashboard.html works when opened directly via file://
    with open(DATA_JS_PATH, "w") as f:
        f.write("// Auto-generated by tracker.py — do not edit\n")
        f.write("var LTC_DATA = ")
        json.dump(data, f, indent=2)
        f.write(";\n")

    print(f"[✓] Saved data.json + data.js")


def reset_data():
    """Delete data files and optionally config."""
    for f in [DATA_PATH, DATA_JS_PATH]:
        if f.exists():
            f.unlink()
            print(f"  [✓] Deleted {f.name}")
        else:
            print(f"  [i] {f.name} not found, skipping")

    response = input("  Also delete config.json? (y/N): ").strip().lower()
    if response in ("y", "yes"):
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
            print("  [✓] Deleted config.json")

    print("\n  [✓] Reset complete. Run again to start fresh.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Price API helpers (CryptoCompare — free, no key needed)
# ---------------------------------------------------------------------------
def get_current_price(currency="usd"):
    """Fetch current LTC price."""
    try:
        r = requests.get(
            f"{CRYPTOCOMPARE_BASE}/price",
            params={"fsym": "LTC", "tsyms": currency.upper()},
            timeout=10
        )
        r.raise_for_status()
        return r.json()[currency.upper()]
    except Exception as e:
        print(f"[!] Failed to fetch current price: {e}")
        return None


def get_historical_price(date_str, currency="usd"):
    """Fetch LTC price for a specific date (YYYY-MM-DD). Cached per-run."""
    if date_str in _price_cache:
        return _price_cache[date_str]

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    ts = int(dt.replace(tzinfo=timezone.utc).timestamp())

    try:
        time.sleep(0.3)
        r = requests.get(
            f"{CRYPTOCOMPARE_BASE}/pricehistorical",
            params={"fsym": "LTC", "tsyms": currency.upper(), "ts": ts},
            timeout=15
        )
        r.raise_for_status()
        price = r.json()["LTC"][currency.upper()]
        _price_cache[date_str] = price
        return price
    except Exception as e:
        print(f"[!] Failed to fetch price for {date_str}: {e}")
        return None


# ---------------------------------------------------------------------------
# Blockchain API helpers (Blockcypher — free, no key needed)
# ---------------------------------------------------------------------------
def fetch_address_txs(address, max_txs=500, after_block=None):
    """
    Fetch transactions for an LTC address. Handles pagination.

    Args:
        address: LTC address
        max_txs: Stop after fetching this many transactions (0 = unlimited)
        after_block: Only fetch transactions in blocks after this height
    """
    txs = []
    url = f"{BLOCKCYPHER_BASE}/addrs/{address}/full"
    params = {"limit": 50}

    # If we have a last-known block height, start from there
    if after_block:
        params["after"] = after_block

    while url:
        try:
            time.sleep(0.5)
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()

            txs.extend(data.get("txs", []))

            # Check if we hit our limit
            if max_txs > 0 and len(txs) >= max_txs:
                txs = txs[:max_txs]
                break

            if data.get("hasMore"):
                last_tx = data["txs"][-1]
                params["before"] = last_tx.get("block_height", 0)
            else:
                break
        except Exception as e:
            print(f"[!] Error fetching txs for {address[:12]}...: {e}")
            break

    return txs


def fetch_address_balance(address):
    """Fetch confirmed balance and tx count for a single address."""
    try:
        time.sleep(0.5)
        r = requests.get(f"{BLOCKCYPHER_BASE}/addrs/{address}/balance", timeout=15)
        r.raise_for_status()
        data = r.json()
        balance = data.get("balance", 0) / 1e8
        n_tx = data.get("n_tx", 0)
        return balance, n_tx
    except Exception as e:
        print(f"    [!] Could not fetch balance: {e}")
        return None, 0


# ---------------------------------------------------------------------------
# Transaction parsing
# ---------------------------------------------------------------------------
def parse_all_transactions(all_raw_txs, tracked_addresses):
    """
    Parse raw Blockcypher transactions considering ALL tracked addresses
    together. This correctly handles:
      - Internal transfers between your own addresses (cancel out to ~0)
      - Change outputs going back to your own addresses
      - Multi-output transactions paying several of your addresses

    For each unique txid, we sum ALL inputs from tracked addresses and
    ALL outputs TO tracked addresses, so internal moves net to zero
    (minus the tx fee).
    """
    tracked_set = set(tracked_addresses)
    parsed = []

    # Deduplicate: same tx appears when fetching from multiple addresses
    unique_txs = {}
    for tx in all_raw_txs:
        txid = tx.get("hash", "")
        if txid and txid not in unique_txs:
            unique_txs[txid] = tx

    for txid, tx in unique_txs.items():
        confirmed = tx.get("confirmed")
        if not confirmed:
            continue

        dt = datetime.fromisoformat(confirmed.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d")
        timestamp = dt.isoformat()

        total_received = 0
        total_sent = 0
        involved_addresses = set()

        for out in tx.get("outputs", []):
            for addr in out.get("addresses", []):
                if addr in tracked_set:
                    total_received += out.get("value", 0)
                    involved_addresses.add(addr)

        for inp in tx.get("inputs", []):
            for addr in inp.get("addresses", []):
                if addr in tracked_set:
                    # Blockcypher uses "output_value" for inputs (the value
                    # of the previous output being spent), NOT "value"
                    total_sent += inp.get("output_value", inp.get("value", 0))
                    involved_addresses.add(addr)

        net_litoshis = total_received - total_sent
        net_ltc = net_litoshis / 1e8

        # Skip internal transfers (net ≈ 0) and dust
        if abs(net_ltc) < 0.00001:
            continue

        tx_type = "receive" if net_ltc > 0 else "spend"

        parsed.append({
            "txid": txid,
            "date": date_str,
            "timestamp": timestamp,
            "addresses": sorted(involved_addresses),
            "type": tx_type,
            "amount_ltc": abs(net_ltc),
            "net_ltc": net_ltc,
            "price_usd": None,
        })

    return parsed


# ---------------------------------------------------------------------------
# Summary calculation
# ---------------------------------------------------------------------------
def calculate_summary(transactions, current_price, target_profit_pct):
    """Calculate weighted average cost basis and P/L."""
    receives = sorted(
        [t for t in transactions if t["type"] == "receive" and t.get("price_usd")],
        key=lambda t: t["timestamp"]
    )
    spends = sorted(
        [t for t in transactions if t["type"] == "spend" and t.get("price_usd")],
        key=lambda t: t["timestamp"]
    )

    total_received_ltc = sum(t["amount_ltc"] for t in receives)
    total_cost = sum(t["amount_ltc"] * t["price_usd"] for t in receives)
    total_spent_ltc = sum(t["amount_ltc"] for t in spends)

    balance_ltc = total_received_ltc - total_spent_ltc

    # Average cost method
    avg_cost_basis = total_cost / total_received_ltc if total_received_ltc > 0 else 0
    remaining_cost = avg_cost_basis * balance_ltc if balance_ltc > 0 else 0
    target_sell_price = avg_cost_basis * (1 + target_profit_pct / 100)

    current_value = balance_ltc * current_price if current_price else None
    unrealized_pl = (current_value - remaining_cost) if current_value else None
    unrealized_pl_pct = (
        (unrealized_pl / remaining_cost * 100)
        if remaining_cost and unrealized_pl is not None
        else None
    )

    return {
        "balance_ltc": round(balance_ltc, 8),
        "total_received_ltc": round(total_received_ltc, 8),
        "total_spent_ltc": round(total_spent_ltc, 8),
        "total_cost_usd": round(total_cost, 2),
        "remaining_cost_usd": round(remaining_cost, 2),
        "avg_cost_basis_usd": round(avg_cost_basis, 2),
        "target_profit_pct": target_profit_pct,
        "target_sell_price_usd": round(target_sell_price, 2),
        "current_price_usd": current_price,
        "current_value_usd": round(current_value, 2) if current_value else None,
        "unrealized_pl_usd": round(unrealized_pl, 2) if unrealized_pl is not None else None,
        "unrealized_pl_pct": round(unrealized_pl_pct, 2) if unrealized_pl_pct is not None else None,
        "total_transactions": len(receives) + len(spends),
        "total_receives": len(receives),
        "total_spends": len(spends),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Handle flags
    if "--reset" in sys.argv:
        reset_data()

    full_sync = "--full" in sys.argv

    print()
    print("=" * 55)
    print("  LTC Cost Basis Tracker")
    print("=" * 55)

    config = load_config()
    data = load_data()
    addresses = config["addresses"]
    target_profit = config.get("target_profit_percent", 3.0)
    currency = config.get("currency", "usd")
    max_txs = config.get("max_transactions_per_address", 500)

    if full_sync:
        max_txs = 0
        print("\n[i] Full sync mode — fetching ALL transactions (no limit)")

    seen_txids = set(data.get("seen_txids", []))
    existing_txs = data.get("transactions", [])
    last_block = data.get("last_block_height", None)
    new_count = 0
    is_first_run = len(existing_txs) == 0

    # --- Fetch transactions + balances from all addresses ---
    all_raw_txs = []
    api_balance_total = 0
    highest_block = last_block or 0
    skipped_addresses = []

    for addr in addresses:
        short = addr[:10] + "..." + addr[-6:]
        print(f"\n[→] {short}")

        balance, n_tx = fetch_address_balance(addr)
        if balance is not None:
            api_balance_total += balance
            print(f"    Balance: {balance:.8f} LTC  ({n_tx} txs on-chain)")

        # Warn about very large addresses
        if n_tx > 5000 and max_txs > 0 and is_first_run:
            print(f"    ⚠️  This address has {n_tx:,} transactions!")
            print(f"    Fetching most recent {max_txs} only.")
            print(f"    Use --full flag to fetch everything (will be slow).")

        # On re-runs, only fetch transactions after the last known block
        after_block = None if is_first_run else last_block

        raw_txs = fetch_address_txs(addr, max_txs=max_txs, after_block=after_block)
        fetched = len(raw_txs)

        if after_block and fetched == 0:
            print(f"    No new transactions since last sync")
        else:
            print(f"    Fetched: {fetched} transactions")

        # Track highest block height we've seen
        for tx in raw_txs:
            bh = tx.get("block_height", 0)
            if bh and bh > highest_block:
                highest_block = bh

        all_raw_txs.extend(raw_txs)

    print(f"\n[i] Total on-chain balance: {api_balance_total:.4f} LTC")

    # --- Parse together (handles internal transfers correctly) ---
    parsed = parse_all_transactions(all_raw_txs, addresses)

    for tx in parsed:
        if tx["txid"] not in seen_txids:
            existing_txs.append(tx)
            seen_txids.add(tx["txid"])
            new_count += 1

    print(f"[+] {new_count} new transaction(s)")

    # --- Fetch historical prices for new dates ---
    dates_needed = set()
    for tx in existing_txs:
        if tx.get("price_usd") is None:
            dates_needed.add(tx["date"])

    if dates_needed:
        print(f"\n[→] Fetching prices for {len(dates_needed)} date(s)...")
        for date_str in sorted(dates_needed):
            price = get_historical_price(date_str, currency)
            if price:
                print(f"    {date_str}: ${price:.2f}")
                for tx in existing_txs:
                    if tx["date"] == date_str and tx.get("price_usd") is None:
                        tx["price_usd"] = price
            else:
                print(f"    {date_str}: unavailable (will retry next run)")

    # --- Current price ---
    print(f"\n[→] Current LTC price...")
    current_price = get_current_price(currency)
    if current_price:
        print(f"    ${current_price:.2f}")

    # --- Calculate & save ---
    summary = calculate_summary(existing_txs, current_price, target_profit)

    data["transactions"] = sorted(existing_txs, key=lambda t: t["timestamp"])
    data["seen_txids"] = list(seen_txids)
    data["summary"] = summary
    data["last_block_height"] = highest_block
    data["config"] = {
        "addresses": addresses,
        "target_profit_percent": target_profit,
        "currency": currency,
    }
    save_data(data)

    # --- Print summary ---
    print()
    print("=" * 55)
    print("  SUMMARY")
    print("=" * 55)
    print(f"  Balance (calculated): {summary['balance_ltc']:.4f} LTC")
    print(f"  Balance (on-chain):   {api_balance_total:.4f} LTC")

    diff = abs(summary["balance_ltc"] - api_balance_total)
    if diff > 0.01:
        print(f"  ⚠️  Mismatch: {diff:.4f} LTC")
    else:
        print(f"  ✅ Balances match")

    print(f"\n  Avg Cost Basis:       ${summary['avg_cost_basis_usd']:.2f}")
    print(f"  Target Sell ({target_profit}%):   ${summary['target_sell_price_usd']:.2f}")

    if current_price:
        print(f"  Current Price:        ${current_price:.2f}")
        if summary["current_value_usd"] is not None:
            print(f"  Portfolio Value:      ${summary['current_value_usd']:.2f}")
        if summary["unrealized_pl_usd"] is not None:
            sign = "+" if summary["unrealized_pl_usd"] >= 0 else ""
            print(f"  Unrealized P/L:       {sign}${summary['unrealized_pl_usd']:.2f} ({sign}{summary['unrealized_pl_pct']:.2f}%)")

        print()
        if current_price >= summary["target_sell_price_usd"]:
            print(f"  ✅ TARGET REACHED — selling now hits your {target_profit}% goal!")
        else:
            gap = summary["target_sell_price_usd"] - current_price
            pct_needed = (gap / current_price) * 100
            print(f"  ⏳ ${gap:.2f} below target ({pct_needed:.1f}% needed)")

    print(f"\n  → Open dashboard.html in your browser")
    print("=" * 55)
    print()


if __name__ == "__main__":
    main()
