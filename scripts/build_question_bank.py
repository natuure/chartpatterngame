"""
data/kr_questions.json, data/kr_questions_minute.json, data/us_questions.json 을 합쳐서
app/questions-data.js (window.QUESTION_BANK = [...]; 형태) 로 변환합니다.

사용법:
  python build_question_bank.py
"""
import json
import os
import random
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(ROOT, "app", "questions-data.js")

FILES = ["kr_questions.json", "kr_questions_minute.json", "us_questions.json"]

# 캔들마다 30(lead)+5(reveal)개 * 이동평균 등 여러 필드가 붙어 문제 1개가 꽤 무겁다(약 4KB).
# questions-data.js를 모바일에서 가볍게 받을 수 있도록 (시장,타임프레임)별 최대 개수를 제한한다.
MAX_PER_BUCKET = 250  # up/down 각각 최대 개수 (버킷당 최대 500문제)


def balance_up_down(questions):
    """(시장,타임프레임)별로 up/down 개수가 같아지도록 다수 쪽을 줄이고, 전체 용량을 위해
    버킷당 개수도 MAX_PER_BUCKET으로 제한한다. 실제 시장 데이터는 상승 변동이 더 많이 잡히는
    경향이 있어, 정답이 한쪽으로 쏠리지 않도록 게임 데이터 빌드 시점에 비율을 맞춘다."""
    groups = defaultdict(lambda: {"up": [], "down": []})
    for q in questions:
        groups[(q["market"], q["timeframe"])][q["direction"]].append(q)

    balanced = []
    for (market, timeframe), by_dir in groups.items():
        ups, downs = by_dir["up"], by_dir["down"]
        random.shuffle(ups)
        random.shuffle(downs)
        n = min(len(ups), len(downs), MAX_PER_BUCKET)
        balanced.extend(ups[:n])
        balanced.extend(downs[:n])
        print(f"  {market}/{timeframe}: up {len(ups)} / down {len(downs)} -> 각 {n}개로 균형/제한")
    return balanced


def main():
    merged = []
    for fname in FILES:
        path = os.path.join(DATA_DIR, fname)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
            merged.extend(items)
            print(f"{fname}: {len(items)}건")
        else:
            print(f"{fname}: 없음 (건너뜀)")

    print("\nup/down 균형 조정:")
    balanced = balance_up_down(merged)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("window.QUESTION_BANK = ")
        json.dump(balanced, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    print(f"\n병합 {len(merged)}건 -> 균형 조정 후 {len(balanced)}건 -> {OUT_PATH}")


if __name__ == "__main__":
    main()
