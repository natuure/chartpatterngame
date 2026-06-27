"""
국내(KRX) 3분봉 대폭변동 스캐너 (한국투자증권 KIS Developers API)
------------------------------------------------------------------
사전 준비:
  1. https://apiportal.koreainvestment.com 가입 (모의투자 계좌만 있어도 키 발급 가능)
  2. "APP KEY" / "APP SECRET" 발급
  3. pip install requests pandas

사용법:
  export KIS_APP_KEY=...
  export KIS_APP_SECRET=...
  python kr_scan_kis_minute.py --symbols-file kr_symbols.txt --out kr_questions_minute.json

주의:
- KIS 분봉조회 API는 기본적으로 "최근 영업일" 위주의 데이터를 반환합니다(과거 수년치를 한번에
  주지 않음). 그래서 이 스크립트는 "최근 분봉 중 큰 변동"을 찾는 용도이고, 문제 풀을 꾸준히
  늘리려면 주기적으로(예: 매일/매주) 재실행해서 --merge 로 누적하는 걸 권장합니다.
- 이 스크립트는 Claude 작업 환경의 네트워크 제한으로 실제로 실행/검증해보지 못했습니다.
  KIS API 응답 필드명이 문서와 약간 다를 수 있으니, 처음 실행 시 한 종목만 --symbols-file 에
  넣고 print(raw) 결과를 확인하며 보정하는 걸 추천합니다.
"""

import argparse
import json
import os
import time

import requests
import pandas as pd

BASE_URL = "https://openapi.koreainvestment.com:9443"
MIN3_THRESHOLD = 5.0
LEAD_N = 30   # 선택 전 미리 보여줄 캔들 수
REVEAL_N = 5  # 선택 후 하나씩 공개할 캔들 수 (이 구간의 누적 변동이 정답)
MA_PERIODS = [5, 10, 20]  # 이동평균선 (3분봉 단위 그대로: 5/10/20개 3분봉)


def get_access_token(app_key, app_secret):
    url = f"{BASE_URL}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    r = requests.post(url, json=body, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_minute_chart(token, app_key, app_secret, symbol):
    """주식분봉조회(국내) - 당일 분봉 데이터를 가져온다."""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST03010200",
        "custtype": "P",
    }
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_HOUR_1": "153000",
        "FID_PW_DATA_INCU_YN": "Y",
    }
    r = requests.get(url, headers=headers, params=params, timeout=10)
    data = r.json()
    rows = data.get("output2", [])
    if not rows:
        return None
    df = pd.DataFrame(rows)
    # 실제 응답 필드명: stck_bsop_date, stck_cntg_hour, stck_oprc, stck_hgpr, stck_lwpr, stck_prpr, cntg_vol
    rename = {
        "stck_oprc": "open", "stck_hgpr": "high", "stck_lwpr": "low",
        "stck_prpr": "close", "cntg_vol": "volume",
    }
    df = df.rename(columns=rename)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = df[col].astype(float)
    df["datetime"] = pd.to_datetime(df["stck_bsop_date"] + df["stck_cntg_hour"], format="%Y%m%d%H%M%S", errors="coerce")
    df = df.sort_values("datetime").reset_index(drop=True)
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def resample_3min(df):
    df = df.set_index("datetime")
    agg = df.resample("3min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna().reset_index()
    return agg


def to_candle(row):
    candle = {"o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]),
              "c": float(row["close"]), "v": int(row["volume"])}
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


def scan(df, symbol):
    questions = []
    ma_buffer = max(MA_PERIODS) - 1  # 첫 lead 캔들도 최장 이동평균을 온전히 갖도록
    if df is None or len(df) < LEAD_N + REVEAL_N + ma_buffer:
        return questions
    df = add_moving_averages(df)
    closes = df["close"].values
    i = LEAD_N + ma_buffer
    while i <= len(df) - REVEAL_N:
        prev_close, cur_close = closes[i - 1], closes[i + REVEAL_N - 1]
        if prev_close == 0:
            i += 1
            continue
        change_pct = (cur_close - prev_close) / prev_close * 100.0
        if abs(change_pct) < MIN3_THRESHOLD:
            i += 1
            continue
        lead_rows = df.iloc[i - LEAD_N : i]
        reveal_rows = df.iloc[i : i + REVEAL_N]
        date_str = str(df.iloc[i]["datetime"])
        questions.append({
            "id": f"KR_3m_{symbol}_{date_str.replace(' ', '_').replace(':', '').replace('-', '')}",
            "market": "KR",
            "timeframe": "3m",
            "direction": "up" if change_pct > 0 else "down",
            "change_pct": round(float(change_pct), 2),
            "lead_candles": [to_candle(lead_rows.iloc[j]) for j in range(LEAD_N)],
            "reveal_candles": [to_candle(reveal_rows.iloc[j]) for j in range(REVEAL_N)],
            "meta": {"symbol": symbol, "date": date_str, "source": "kis"},
        })
        i += REVEAL_N
    return questions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols-file", required=True, help="한 줄에 종목코드 하나씩 (예: 005930)")
    parser.add_argument("--out", default="kr_questions_minute.json")
    parser.add_argument("--merge", default=None)
    args = parser.parse_args()

    app_key = os.environ["KIS_APP_KEY"]
    app_secret = os.environ["KIS_APP_SECRET"]
    token = get_access_token(app_key, app_secret)

    with open(args.symbols_file, encoding="utf-8") as f:
        symbols = [line.strip() for line in f if line.strip()]

    all_questions = []
    if args.merge and os.path.exists(args.merge):
        with open(args.merge, encoding="utf-8") as f:
            all_questions = json.load(f)

    for idx, symbol in enumerate(symbols, 1):
        print(f"[{idx}/{len(symbols)}] {symbol}")
        try:
            df = fetch_minute_chart(token, app_key, app_secret, symbol)
            df3 = resample_3min(df) if df is not None else None
            qs = scan(df3, symbol)
            print(f"  3분봉 {len(qs)}건")
            all_questions.extend(qs)
        except Exception as e:
            print(f"  오류: {e}")
        time.sleep(0.3)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(all_questions, f, ensure_ascii=False, indent=2)
    print(f"완료: 총 {len(all_questions)}건 -> {args.out}")


if __name__ == "__main__":
    main()
