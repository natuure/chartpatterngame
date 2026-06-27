"""
국내(KRX) 일봉/주봉 대폭변동 종목 스캐너
------------------------------------------
사용자 본인 PC에서 실행하는 스크립트입니다 (Claude 작업 환경은 KRX 접속이 막혀 있어 직접 실행할 수 없음).

사용법:
  pip install pykrx pandas
  python kr_scan_pykrx.py --start 20150101 --end 20260626 --out kr_questions.json

결과: data/SCHEMA.md 형식의 문제 JSON 배열을 --out 경로에 저장합니다.
- 일봉(1d): |직전종가→당일종가 변동률| >= 10%
- 주봉(1w): |직전주종가→당주종가 변동률| >= 30%

진행 상황은 콘솔에 종목 단위로 출력되며, 종목 수가 많아(코스피+코스닥 약 2,700개) 전체 실행에
시간이 꽤 걸립니다(수십 분~1시간 이상 가능). 중간에 멈춰도 그동안 모인 결과는 --out 파일로
저장되도록 일정 개수마다 체크포인트를 남깁니다.
"""

import argparse
import io
import json
import sys
import time

import pandas as pd
import requests

DAILY_THRESHOLD = 10.0   # %
WEEKLY_THRESHOLD = 30.0  # %
LEAD_N = 30               # 선택 전 미리 보여줄 캔들 수
REVEAL_N = 5              # 선택 후 0.7초 간격으로 하나씩 공개할 캔들 수 (이 구간의 누적 변동이 정답)
MA_PERIODS = [5, 10, 20]  # 이동평균선 (타임프레임 단위 그대로: 일봉=5/10/20일, 주봉=5/10/20주)
CHECKPOINT_EVERY = 50     # 종목 N개마다 중간 저장

KIND_MARKET_MAP = {"KOSPI": ["유가"], "KOSDAQ": ["코스닥"], "ALL": ["유가", "코스닥"]}


def get_kospi200_tickers():
    """코스피200 지수 실제 구성종목 코드를 가져온다 (인증 불필요, 네이버 금융 페이지 이용).
    pykrx의 지수 구성종목 조회(get_index_portfolio_deposit_file)도 로그인 인증으로 막혀 있어 대체."""
    import re

    codes = []
    for page in range(1, 25):
        r = requests.get(
            "https://finance.naver.com/sise/entryJongmok.naver",
            params={"type": "KPI200", "page": page},
            timeout=15,
        )
        r.encoding = "euc-kr"
        found = re.findall(r"code=(\d{6})", r.text)
        if not found:
            break
        codes.extend(found)
    return sorted(set(codes))


def get_ticker_list_via_kind(market):
    """KRX 전종목 조회(get_market_ticker_list)가 로그인 인증으로 막혀 있을 때의 대체 경로.
    KIND(상장공시시스템) 상장법인목록 다운로드는 인증 없이 접근 가능하다."""
    url = "https://kind.krx.co.kr/corpgeneral/corpList.do"
    params = {
        "method": "download",
        "searchType": "13",
        "orderMode": "3",
        "orderStat": "D",
        "fiscalYearEnd": "all",
        "location": "all",
    }
    r = requests.get(url, params=params, timeout=20)
    r.encoding = "euc-kr"
    df = pd.read_html(io.StringIO(r.text))[0]
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    df = df[df["종목코드"].str.fullmatch(r"\d{6}")]
    df = df[df["시장구분"].isin(KIND_MARKET_MAP[market])]
    return df["종목코드"].tolist()


def to_candle(row):
    o, h, l, c, v = row.iloc[0], row.iloc[1], row.iloc[2], row.iloc[3], row.iloc[4]
    candle = {
        "o": float(o),
        "h": float(h),
        "l": float(l),
        "c": float(c),
        "v": int(v),
    }
    for j, p in enumerate(MA_PERIODS):
        col_pos = 5 + j
        if col_pos < len(row):
            val = row.iloc[col_pos]
            if pd.notna(val):
                candle[f"ma{p}"] = float(val)
    return candle


def add_moving_averages(df):
    """종가(4번째 컬럼) 기준 5/10/20 이동평균을 뒤에 컬럼으로 덧붙인다."""
    close_col = df.columns[3]
    for p in MA_PERIODS:
        df[f"ma{p}"] = df[close_col].rolling(p).mean()
    return df


def resample_weekly(daily_df):
    """일봉(O/H/L/C/V, 5컬럼)을 주봉으로 리샘플링.
    pykrx get_market_ohlcv(freq="w")가 이 버전에서 d/m/y만 지원해 직접 집계한다."""
    if daily_df is None or daily_df.empty:
        return None
    cols = list(daily_df.columns)
    agg = daily_df.resample("W").agg({
        cols[0]: "first", cols[1]: "max", cols[2]: "min", cols[3]: "last", cols[4]: "sum",
    }).dropna()
    return agg


def scan_one_timeframe(df, ticker, timeframe, threshold, market, source):
    """df: 시가/고가/저가/종가/거래량 컬럼(순서 고정), 시간순 정렬된 DataFrame
    lead_candles(LEAD_N개)를 먼저 보여준 뒤, reveal_candles(REVEAL_N개)를 한 개씩 공개한다.
    direction/change_pct는 lead 마지막 종가 -> reveal 마지막 종가의 누적 변동 기준."""
    questions = []
    ma_buffer = max(MA_PERIODS) - 1  # 첫 lead 캔들도 최장 이동평균을 온전히 갖도록
    if len(df) < LEAD_N + REVEAL_N + ma_buffer:
        return questions

    closes = df.iloc[:, 3].astype(float).values
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
        date_str = str(df.index[i].date()) if hasattr(df.index[i], "date") else str(df.index[i])

        questions.append({
            "id": f"{market}_{timeframe}_{ticker}_{date_str.replace('-', '')}",
            "market": market,
            "timeframe": timeframe,
            "direction": "up" if change_pct > 0 else "down",
            "change_pct": round(float(change_pct), 2),
            "lead_candles": [to_candle(lead_rows.iloc[j]) for j in range(LEAD_N)],
            "reveal_candles": [to_candle(reveal_rows.iloc[j]) for j in range(REVEAL_N)],
            "meta": {
                "symbol": ticker,
                "date": date_str,
                "source": source,
            },
        })
        i += REVEAL_N  # 같은 구간이 겹쳐서 중복 문제로 쌓이지 않도록 다음 윈도우로 건너뜀
    return questions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="20150101")
    parser.add_argument("--end", default="20260626")
    parser.add_argument("--out", default="kr_questions.json")
    parser.add_argument("--market", default="ALL", choices=["ALL", "KOSPI", "KOSDAQ", "KOSPI200"])
    parser.add_argument("--limit", type=int, default=None, help="빠른 테스트용: 앞에서 N개 종목만 처리")
    args = parser.parse_args()

    from pykrx import stock

    print(f"[1/3] {args.end} 기준 종목 목록 조회 중...")
    if args.market == "KOSPI200":
        tickers = get_kospi200_tickers()
    else:
        try:
            tickers = stock.get_market_ticker_list(args.end, market=args.market)
        except Exception:
            tickers = []
        if not tickers:
            print("  -> pykrx 전종목 조회 실패(로그인 인증 요구), KIND 상장법인목록으로 대체")
            tickers = get_ticker_list_via_kind(args.market)
    if args.limit:
        tickers = tickers[: args.limit]
    print(f"  -> {len(tickers)}개 종목")

    all_questions = []
    for idx, ticker in enumerate(tickers, 1):
        name = ""
        try:
            name = stock.get_market_ticker_name(ticker)
        except Exception:
            pass
        print(f"[{idx}/{len(tickers)}] {ticker} {name} ...", end=" ")

        try:
            daily_df = stock.get_market_ohlcv(args.start, args.end, ticker)
            if daily_df is None or daily_df.empty:
                print("데이터 없음")
                continue
            daily_df = daily_df.iloc[:, :5]  # 시가,고가,저가,종가,거래량만 사용
            weekly_df = resample_weekly(daily_df)

            daily_df = add_moving_averages(daily_df)
            daily_qs = scan_one_timeframe(daily_df, ticker, "1d", DAILY_THRESHOLD, "KR", "pykrx")

            if weekly_df is not None and not weekly_df.empty:
                weekly_df = add_moving_averages(weekly_df)
                weekly_qs = scan_one_timeframe(weekly_df, ticker, "1w", WEEKLY_THRESHOLD, "KR", "pykrx")
            else:
                weekly_qs = []

            all_questions.extend(daily_qs)
            all_questions.extend(weekly_qs)
            print(f"일봉 {len(daily_qs)}건 / 주봉 {len(weekly_qs)}건 (누적 {len(all_questions)}건)")
        except Exception as e:
            print(f"오류: {e}")

        if idx % CHECKPOINT_EVERY == 0:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(all_questions, f, ensure_ascii=False, indent=2)
            print(f"  [체크포인트 저장: {len(all_questions)}건]")

        time.sleep(0.1)  # KRX 서버 부담을 줄이기 위한 짧은 대기

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(all_questions, f, ensure_ascii=False, indent=2)
    print(f"\n완료: 총 {len(all_questions)}건 -> {args.out}")


if __name__ == "__main__":
    main()
