"""
미국 주식 일봉/주봉/3분봉 대폭변동 종목 스캐너 (Twelve Data API)
----------------------------------------------------------------
사용법:
  pip install requests pandas
  export TWELVE_DATA_API_KEY=발급받은키
  python us_scan_twelvedata.py --out us_questions.json

무료 플랜 기준 분당 8회 / 일 800회 호출 제한이 있어, 호출 사이에 자동으로 대기합니다.
3분봉은 Twelve Data가 직접 제공하지 않아 1분봉을 받아 3분 단위로 리샘플링합니다.
1분봉은 무료 플랜에서 최근 데이터만 제공되므로(과거 전체가 아님), 분봉 문제는 최근 변동
위주로 쌓이고 주기적으로 스크립트를 다시 돌려 누적해가는 방식을 권장합니다(이미 만든
us_questions.json을 --merge 옵션으로 합칠 수 있습니다).
"""

import argparse
import json
import os
import sys
import time

import requests
import pandas as pd

API_BASE = "https://api.twelvedata.com/time_series"

DAILY_THRESHOLD = 10.0
WEEKLY_THRESHOLD = 30.0
MIN3_THRESHOLD = 5.0
LEAD_N = 30   # 선택 전 미리 보여줄 캔들 수
REVEAL_N = 5  # 선택 후 하나씩 공개할 캔들 수 (이 구간의 누적 변동이 정답)
MA_PERIODS = [5, 10, 20]  # 이동평균선 (타임프레임 단위 그대로)

# MVP용 기본 종목 목록 (유동성 높은 대형주 중심, 필요시 --symbols-file 로 교체 가능)
DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD", "NFLX", "AVGO",
    "INTC", "CRM", "ORCL", "ADBE", "CSCO", "QCOM", "TXN", "IBM", "UBER", "PYPL",
    "SHOP", "SQ", "PLTR", "SNOW", "COIN", "MARA", "RIOT", "SOFI", "RIVN", "LCID",
    "GME", "AMC", "BBBY", "F", "GM", "BA", "DIS", "NKE", "SBUX", "MCD",
    "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "PFE", "MRNA", "JNJ",
    "XOM", "CVX", "T", "VZ", "WMT", "TGT", "COST", "HD", "LOW", "PEP",
]


def fetch(symbol, interval, outputsize, api_key):
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": api_key,
        "order": "ASC",
    }
    for attempt in range(3):
        r = requests.get(API_BASE, params=params, timeout=20)
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "error":
            msg = data.get("message", "")
            if "limit" in msg.lower() or data.get("code") == 429:
                print(f"    rate limit, 60초 대기... ({msg})")
                time.sleep(60)
                continue
            print(f"    오류({symbol}, {interval}): {msg}")
            return None
        values = data.get("values")
        if not values:
            return None
        df = pd.DataFrame(values)
        df["datetime"] = pd.to_datetime(df["datetime"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    return None


def resample_3min(df):
    df = df.set_index("datetime")
    agg = df.resample("3min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    agg = agg.reset_index()
    return agg


def to_candle(row):
    candle = {
        "o": float(row["open"]),
        "h": float(row["high"]),
        "l": float(row["low"]),
        "c": float(row["close"]),
        "v": int(row["volume"]),
    }
    for p in MA_PERIODS:
        col = f"ma{p}"
        if col in row.index and pd.notna(row[col]):
            candle[col] = float(row[col])
    return candle


def add_moving_averages(df):
    if df is None or df.empty:
        return df
    for p in MA_PERIODS:
        df[f"ma{p}"] = df["close"].rolling(p).mean()
    return df


def scan(df, symbol, timeframe, threshold):
    questions = []
    ma_buffer = max(MA_PERIODS) - 1  # 첫 lead 캔들도 최장 이동평균을 온전히 갖도록
    if df is None or len(df) < LEAD_N + REVEAL_N + ma_buffer:
        return questions
    df = add_moving_averages(df)
    closes = df["close"].values
    i = LEAD_N + ma_buffer
    while i <= len(df) - REVEAL_N:
        prev_close = closes[i - 1]
        cur_close = closes[i + REVEAL_N - 1]
        if prev_close == 0:
            i += 1
            continue
        change_pct = (cur_close - prev_close) / prev_close * 100.0
        if abs(change_pct) < threshold:
            i += 1
            continue
        lead_rows = df.iloc[i - LEAD_N : i]
        reveal_rows = df.iloc[i : i + REVEAL_N]
        dt = df.iloc[i]["datetime"]
        date_str = str(dt)
        questions.append({
            "id": f"US_{timeframe}_{symbol}_{date_str.replace(' ', '_').replace(':', '').replace('-', '')}",
            "market": "US",
            "timeframe": timeframe,
            "direction": "up" if change_pct > 0 else "down",
            "change_pct": round(float(change_pct), 2),
            "lead_candles": [to_candle(lead_rows.iloc[j]) for j in range(LEAD_N)],
            "reveal_candles": [to_candle(reveal_rows.iloc[j]) for j in range(REVEAL_N)],
            "meta": {"symbol": symbol, "date": date_str, "source": "twelvedata"},
        })
        i += REVEAL_N
    return questions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="us_questions.json")
    parser.add_argument("--symbols-file", default=None, help="한 줄에 한 종목씩 적힌 텍스트 파일")
    parser.add_argument("--merge", default=None, help="기존 결과 파일과 합쳐서 저장")
    parser.add_argument("--api-key", default=os.environ.get("TWELVE_DATA_API_KEY"))
    args = parser.parse_args()

    if not args.api_key:
        print("API 키가 필요합니다. --api-key 또는 TWELVE_DATA_API_KEY 환경변수로 전달하세요.")
        sys.exit(1)

    symbols = DEFAULT_SYMBOLS
    if args.symbols_file:
        with open(args.symbols_file, encoding="utf-8") as f:
            symbols = [line.strip() for line in f if line.strip()]

    all_questions = []
    if args.merge and os.path.exists(args.merge):
        with open(args.merge, encoding="utf-8") as f:
            all_questions = json.load(f)
        print(f"기존 {len(all_questions)}건 불러옴 ({args.merge})")

    call_count = 0

    def paced_fetch(symbol, interval, outputsize):
        nonlocal call_count
        call_count += 1
        if call_count % 8 == 0:
            time.sleep(60)  # 분당 8회 제한 대비
        else:
            time.sleep(1)
        return fetch(symbol, interval, outputsize, args.api_key)

    for idx, symbol in enumerate(symbols, 1):
        print(f"[{idx}/{len(symbols)}] {symbol}")

        daily_df = paced_fetch(symbol, "1day", 5000)
        daily_qs = scan(daily_df, symbol, "1d", DAILY_THRESHOLD)

        weekly_df = paced_fetch(symbol, "1week", 5000)
        weekly_qs = scan(weekly_df, symbol, "1w", WEEKLY_THRESHOLD)

        min1_df = paced_fetch(symbol, "1min", 5000)
        min3_qs = []
        if min1_df is not None and not min1_df.empty:
            min3_df = resample_3min(min1_df)
            min3_qs = scan(min3_df, symbol, "3m", MIN3_THRESHOLD)

        print(f"  일봉 {len(daily_qs)} / 주봉 {len(weekly_qs)} / 3분봉 {len(min3_qs)}")
        all_questions.extend(daily_qs)
        all_questions.extend(weekly_qs)
        all_questions.extend(min3_qs)

        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(all_questions, f, ensure_ascii=False, indent=2)

    print(f"\n완료: 총 {len(all_questions)}건 -> {args.out}")


if __name__ == "__main__":
    main()
