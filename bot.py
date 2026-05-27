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

current_up_token_id = None
current_down_token_id = None
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


def status_lines():
    return (
        f"W: {wins} | L: {losses}\n"
        f"WR: {win_rate():.1f}%\n"
        f"Total PnL: ${total_pnl:.2f}"
    )


def log_trade(result, direction, entry, exit_price, pnl, market_name):
    file_exists = os.path.exists(TRADES_FILE)

    with open(TRADES_FILE, "a", newline="") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "time",
                "market",
                "direction",
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
            direction,
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


def rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return (100 - (100 / (1 + rs))).fillna(50)


def get_bos_signal():
    df_1m = get_coinbase_candles(60)

    current_open = float(df_1m["open"].iloc[-1])
    current_close = float(df_1m["close"].iloc[-1])

    recent_high = float(
        df_1m["high"]
        .rolling(BOS_LOOKBACK_CANDLES)
        .max()
        .shift(1)
        .iloc[-1]
    )

    recent_low = float(
        df_1m["low"]
        .rolling(BOS_LOOKBACK_CANDLES)
        .min()
        .shift(1)
        .iloc[-1]
    )

    latest_rsi = float(rsi(df_1m["close"], RSI_PERIOD).iloc[-1])

    bullish_bos = current_close > recent_high
    bearish_bos = current_close < recent_low

    bullish_momentum = current_close > current_open
    bearish_momentum = current_close < current_open

    bullish_rsi = RSI_UP_MIN <= latest_rsi <= RSI_UP_MAX
    bearish_rsi = RSI_DOWN_MIN <= latest_rsi <= RSI_DOWN_MAX

    direction = None

    if bullish_bos and bullish_momentum and bullish_rsi:
        direction = "UP"

    elif bearish_bos and bearish_momentum and bearish_rsi:
        direction = "DOWN"

    return {
        "btc_price": current_close,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "rsi": latest_rsi,
        "direction": direction,
    }


def get_order_book(token_id):
    time.sleep(0.2)

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


def get_tokens_from_market(market):
    outcomes = market.get("outcomes")
    token_ids = market.get("clobTokenIds")

    try:
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
    except Exception:
        return None, None

    if not outcomes or not token_ids:
        return None, None

    up_token = None
    down_token = None

    for outcome, token_id in zip(outcomes, token_ids):
        outcome = str(outcome).lower()

        if outcome in ["up", "yes"]:
            up_token = str(token_id)

        if outcome in ["down", "no"]:
            down_token = str(token_id)

    return up_token, down_token


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
                up_token, down_token = get_tokens_from_market(market)

                if not up_token or not down_token:
                    continue

                try:
                    up_book = get_order_book(up_token)
                    down_book = get_order_book(down_token)

                    up_parsed = parse_book(up_book)
                    down_parsed = parse_book(down_book)

                    if (
                        up_parsed["best_ask"] > 0
                        and down_parsed["best_ask"] > 0
                    ):
                        name = market.get("question") or slug
                        return up_token, down_token, name

                except Exception:
                    continue

        except Exception:
            continue

    return None, None, None


def refresh_token():
    global current_up_token_id
    global current_down_token_id
    global current_market_name
    global last_token_refresh

    up_token, down_token, name = find_5m_market_by_timestamp()

    if up_token and down_token:
        current_up_token_id = up_token
        current_down_token_id = down_token
        current_market_name = name
        last_token_refresh = time.time()

        print(f"[INFO] Using market: {name}", flush=True)
        print(f"[INFO] UP Token ID: {up_token}", flush=True)
        print(f"[INFO] DOWN Token ID: {down_token}", flush=True)

        return True

    print("[WAITING] No valid BTC 5m contract found yet.", flush=True)
    return False


def get_current_tokens(force=False):
    expired = (
        current_up_token_id is None
        or current_down_token_id is None
        or time.time() - last_token_refresh >= TOKEN_REFRESH_SECONDS
    )

    if force or expired:
        refresh_token()

    return current_up_token_id, current_down_token_id


def close_trade(result, exit_price):
    global open_trade, wins, losses, total_pnl

    entry_price = open_trade["entry"]
    trade_pnl = (exit_price - entry_price) * open_trade["shares"]

    if result == "WIN":
        wins += 1
    else:
        losses += 1

    total_pnl += trade_pnl
    save_state()

    log_trade(
        result,
        open_trade["direction"],
        entry_price,
        exit_price,
        trade_pnl,
        open_trade["market"],
    )

    icon = "✅" if result == "WIN" else "❌"

    message = (
        f"{icon} 5M SCALP {result}\n"
        f"Direction: {open_trade['direction']}\n"
        f"Market: {open_trade['market']}\n"
        f"Entry: {entry_price:.3f}\n"
        f"Exit: {exit_price:.3f}\n"
        f"Trade PnL: ${trade_pnl:.2f}\n"
        f"{status_lines()}"
    )

    print(message, flush=True)
    discord_notify(message)

    traded_contracts.add(open_trade["token_id"])
    open_trade = None


def manage_trade(book_info):
    global open_trade

    if open_trade is None:
        return

    current_bid = float(book_info["best_bid"])

    trail_after_target = globals().get("TRAIL_AFTER_TARGET", True)
    trail_distance = globals().get("TRAIL_DISTANCE", 0.02)

    if trail_after_target:
        if not open_trade.get("trail_active", False):
            if current_bid >= open_trade["target"]:
                open_trade["trail_active"] = True
                open_trade["highest_bid"] = current_bid
                open_trade["trail_stop"] = max(
                    open_trade["entry"],
                    open_trade["target"] - trail_distance,
                )
        else:
            if current_bid > open_trade["highest_bid"]:
                open_trade["highest_bid"] = current_bid
                open_trade["trail_stop"] = max(
                    open_trade["trail_stop"],
                    current_bid - trail_distance,
                )

            if current_bid <= open_trade["trail_stop"]:
                close_trade("WIN", open_trade["trail_stop"])
                return

        if current_bid <= open_trade["stop"]:
            close_trade("LOSS", open_trade["stop"])
            return

    else:
        if current_bid >= open_trade["target"]:
            close_trade("WIN", open_trade["target"])
            return

        if current_bid <= open_trade["stop"]:
            close_trade("LOSS", open_trade["stop"])
            return


def maybe_enter_trade(signal, up_book_info, down_book_info):
    global open_trade

    if open_trade is not None:
        return

    direction = signal["direction"]

    if direction is None:
        return

    if direction == "UP":
        token_id = current_up_token_id
        book_info = up_book_info
    else:
        token_id = current_down_token_id
        book_info = down_book_info

    if token_id in traded_contracts:
        return

    ask = float(book_info["best_ask"])
    spread = float(book_info["spread"])

    if ask >= MAX_ENTRY_PRICE:
        return

    if ask <= MIN_ENTRY_PRICE:
        return

    if spread > MAX_SPREAD:
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
        "token_id": token_id,
        "direction": direction,
        "trail_active": False,
        "trail_stop": None,
        "highest_bid": ask,
    }

    message = (
        f"📈 NEW 5M SCALP\n"
        f"Direction: {direction}\n"
        f"Market: {market_name}\n"
        f"BUY {direction} @ {ask:.3f}\n"
        f"Target/Trail Activation: {target:.3f}\n"
        f"Stop: {stop:.3f}\n"
        f"Spread: {spread:.3f}\n"
        f"RSI: {signal['rsi']:.1f}\n"
        f"BOS + Momentum + RSI: TRUE\n"
        f"Trailing: ON after target\n"
        f"{status_lines()}"
    )

    print(message, flush=True)
    discord_notify(message)


def main():
    load_state()

    print("=======================================", flush=True)
    print(" Yapp Scalp Bot", flush=True)
    print(" BOTH DIRECTIONS MODE", flush=True)
    print(" BOS + Momentum + RSI + Spread", flush=True)
    print(" TRAILING STOP MODE", flush=True)
    print("=======================================", flush=True)

    discord_notify(
        f"🤖 Yapp Scalp Bot Started\n"
        f"{status_lines()}"
    )

    while True:
        try:
            up_token, down_token = get_current_tokens()

            if not up_token or not down_token:
                time.sleep(LOOP_SECONDS)
                continue

            signal = get_bos_signal()

            up_book = get_order_book(up_token)
            down_book = get_order_book(down_token)

            up_book_info = parse_book(up_book)
            down_book_info = parse_book(down_book)

            if open_trade is not None:
                if open_trade["direction"] == "UP":
                    manage_trade(up_book_info)
                else:
                    manage_trade(down_book_info)
            else:
                maybe_enter_trade(signal, up_book_info, down_book_info)

        except Exception as error:
            print(f"[ERROR] {error}", flush=True)
            discord_notify(
                f"⚠️ Yapp Scalp Bot Error\n"
                f"{status_lines()}"
            )

        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
