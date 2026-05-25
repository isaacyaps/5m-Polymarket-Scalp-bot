import csv
import json
import os
import time
from datetime import datetime

import pandas as pd
import requests

from config import *

COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{product}/candles"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

STATE_FILE = "bot_state.json"
TRADES_FILE = "trades.csv"

open_trade = None
wins = 0
losses = 0
total_pnl = 0.0

current_token_id = None
current_market_name = None
last_token_refresh = 0

traded_contracts = set()


def discord_notify(message):
    webhook = os.getenv("DISCORD_WEBHOOK_URL") or DISCORD_WEBHOOK_URL

    if not webhook:
        return

    try:
        requests.post(webhook, json={"content": message}, timeout=10)
    except Exception:
        pass


def load_state():
    global wins, losses, total_pnl

    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        wins = state.get("wins", 0)
        losses = state.get("losses", 0)
        total_pnl = state.get("total_pnl", 0.0)

    except Exception:
        wins = 0
        losses = 0
        total_pnl = 0.0


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "wins": wins,
                "losses": losses,
                "total_pnl": total_pnl,
            },
            f,
            indent=4,
        )


def win_rate():
    total = wins + losses
    return wins / total * 100 if total else 0.0


def log_trade(result, entry, exit_price, pnl, market_name):
    file_exists = os.path.exists(TRADES_FILE)

    with open(TRADES_FILE, "a", newline="") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "time",
                "market",
                "result",
                "entry",
                "exit",
                "pnl",
                "total_pnl",
                "win_rate",
            ])

        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            market_name,
            result,
            round(entry, 4),
            round(exit_price, 4),
            round(pnl, 2),
            round(total_pnl, 2),
            round(win_rate(), 2),
        ])


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def get_coinbase_candles(granularity):
    url = COINBASE_CANDLES.format(product=BTC_PRODUCT)

    response = requests.get(
        url,
        params={"granularity": granularity},
        headers={"User-Agent": "btc-polymarket-5m-scalper"},
        timeout=10,
    )

    response.raise_for_status()

    data = response.json()

    df = pd.DataFrame(
        data,
        columns=["time", "low", "high", "open", "close", "volume"],
    )

    df = df.sort_values("time").tail(CANDLE_LIMIT)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    return df.reset_index(drop=True)


def get_bos_signal():
    df_1m = get_coinbase_candles(60)

    current_close = float(df_1m["close"].iloc[-1])

    recent_high = float(
        df_1m["high"]
        .rolling(BOS_LOOKBACK_CANDLES)
        .max()
        .shift(1)
        .iloc[-1]
    )

    bos_ok = current_close > recent_high

    return {
        "btc_price": current_close,
        "recent_high": recent_high,
        "bos_ok": bos_ok,
    }


def get_order_book(token_id):
    response = requests.get(
        f"{CLOB_API}/book",
        params={"token_id": token_id},
        timeout=10,
    )

    response.raise_for_status()

    return response.json()


def parse_book(book):
    bids = book.get("bids") or []
    asks = book.get("asks") or []

    best_bid = 0.0
    best_ask = 1.0

    if bids:
        best_bid = max(safe_float(x.get("price")) for x in bids)

    if asks:
        best_ask = min(safe_float(x.get("price")) for x in asks)

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": best_ask - best_bid,
    }


def get_up_token_from_market(market):
    outcomes = market.get("outcomes")
    token_ids = market.get("clobTokenIds")

    try:
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
    except Exception:
        return None

    if not outcomes or not token_ids:
        return None

    for outcome, token_id in zip(outcomes, token_ids):
        if str(outcome).lower() in ["up", "yes"]:
            return str(token_id)

    return None


def find_5m_market_by_timestamp():
    now = int(time.time())
    interval = 300
    base = now - (now % interval)

    possible_times = [
        base,
        base + interval,
        base + interval * 2,
    ]

    for timestamp in possible_times:
        slug = f"btc-updown-5m-{timestamp}"

        try:
            response = requests.get(
                f"{GAMMA_API}/events/slug/{slug}",
                timeout=10,
            )

            if response.status_code != 200:
                continue

            event = response.json()
            markets = event.get("markets") or []

            for market in markets:
                token = get_up_token_from_market(market)

                if not token:
                    continue

                try:
                    book = get_order_book(token)
                    parsed = parse_book(book)

                    if parsed["best_ask"] > 0 and parsed["best_bid"] >= 0:
                        name = market.get("question") or slug
                        return token, name

                except Exception:
                    continue

        except Exception:
            continue

    return None, None


def refresh_token():
    global current_token_id, current_market_name, last_token_refresh

    token, name = find_5m_market_by_timestamp()

    if token:
        current_token_id = token
        current_market_name = name
        last_token_refresh = time.time()

        print(f"[INFO] Using market: {name}", flush=True)
        print(f"[INFO] Token ID: {token}", flush=True)

        return True

    print("[WAITING] No valid BTC 5m contract found yet.", flush=True)
    return False


def get_current_token(force=False):
    expired = (
        current_token_id is None
        or time.time() - last_token_refresh >= TOKEN_REFRESH_SECONDS
    )

    if force or expired:
        refresh_token()

    return current_token_id


def manage_trade(book_info):
    global open_trade, wins, losses, total_pnl

    if open_trade is None:
        return

    current_bid = book_info["best_bid"]

    if current_bid >= open_trade["target"]:
        pnl = (current_bid - open_trade["entry"]) * open_trade["shares"]

        if pnl <= 0:
            return

        wins += 1
        total_pnl += pnl

        save_state()

        log_trade(
            "WIN",
            open_trade["entry"],
            current_bid,
            pnl,
            open_trade["market"],
        )

        message = (
            f"✅ 5M SCALP WIN\n"
            f"Market: {open_trade['market']}\n"
            f"Entry: {open_trade['entry']:.3f}\n"
            f"Exit: {current_bid:.3f}\n"
            f"PnL: ${pnl:.2f}\n"
            f"Total PnL: ${total_pnl:.2f}\n"
            f"WR: {win_rate():.1f}%\n"
            f"Wins: {wins} | Losses: {losses}"
        )

        print(message, flush=True)
        discord_notify(message)

        traded_contracts.add(open_trade["token_id"])
        open_trade = None

    elif current_bid <= open_trade["stop"]:
        pnl = (current_bid - open_trade["entry"]) * open_trade["shares"]

        losses += 1
        total_pnl += pnl

        save_state()

        log_trade(
            "LOSS",
            open_trade["entry"],
            current_bid,
            pnl,
            open_trade["market"],
        )

        message = (
            f"❌ 5M SCALP LOSS\n"
            f"Market: {open_trade['market']}\n"
            f"Entry: {open_trade['entry']:.3f}\n"
            f"Exit: {current_bid:.3f}\n"
            f"PnL: ${pnl:.2f}\n"
            f"Total PnL: ${total_pnl:.2f}\n"
            f"WR: {win_rate():.1f}%\n"
            f"Wins: {wins} | Losses: {losses}"
        )

        print(message, flush=True)
        discord_notify(message)

        traded_contracts.add(open_trade["token_id"])
        open_trade = None


def maybe_enter_trade(signal, book_info):
    global open_trade

    if open_trade is not None:
        return

    if current_token_id in traded_contracts:
        return

    ask = float(book_info["best_ask"])
    spread = float(book_info["spread"])

    bos_ok = signal["bos_ok"]
    spread_ok = spread <= MAX_SPREAD

    if ask >= MAX_ENTRY_PRICE:
        return

    if ask <= MIN_ENTRY_PRICE:
        return

    if not bos_ok or not spread_ok:
        return

    risk_per_share = 0.06

    stop = max(0.01, ask - risk_per_share)
    target = ask + (risk_per_share * TAKE_PROFIT_RR)

    if target >= 0.99:
        return

    if target <= ask:
        return

    shares = RISK_PER_TRADE_USD / risk_per_share

    market_name = current_market_name or "BTC 5m Up/Down"

    open_trade = {
        "entry": ask,
        "stop": stop,
        "target": target,
        "shares": shares,
        "market": market_name,
        "token_id": current_token_id,
    }

    message = (
        f"📈 NEW 5M SCALP\n"
        f"Market: {market_name}\n"
        f"BUY UP @ {ask:.3f}\n"
        f"Target: {target:.3f}\n"
        f"Stop: {stop:.3f}\n"
        f"Spread: {spread:.3f}\n"
        f"BOS: TRUE\n"
        f"Risk: ${RISK_PER_TRADE_USD:.2f}\n"
        f"Current WR: {win_rate():.1f}%\n"
        f"Total PnL: ${total_pnl:.2f}"
    )

    print(message, flush=True)
    discord_notify(message)


def main():
    load_state()

    print("=======================================", flush=True)
    print(" BTC 5M Polymarket Paper Scalper", flush=True)
    print(" BOS + Spread", flush=True)
    print(" FIXED RR MODE", flush=True)
    print("=======================================", flush=True)

    discord_notify(
        f"🤖 5M SCALPER STARTED\n"
        f"BOS + Spread\n"
        f"FIXED RR MODE\n"
        f"Loaded WR: {win_rate():.1f}%\n"
        f"Loaded Total PnL: ${total_pnl:.2f}"
    )

    while True:
        try:
            token_id = get_current_token()

            if not token_id:
                time.sleep(LOOP_SECONDS)
                continue

            signal = get_bos_signal()

            book = get_order_book(token_id)
            book_info = parse_book(book)

            manage_trade(book_info)
            maybe_enter_trade(signal, book_info)

        except Exception as error:
            print(f"[ERROR] {error}", flush=True)

        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
