// 데모/오프라인용 샘플 문제 데이터.
// 실제 수집 데이터(data/kr_questions.json 등)를 scripts/build_question_bank.py 로 변환해서
// questions-data.js 를 만들면 이 샘플 대신 그 데이터가 사용됩니다.
(function () {
  var LEAD_N = 30;
  var REVEAL_N = 5;

  function rngFactory(seed) {
    let s = seed;
    return function () {
      s = (s * 9301 + 49297) % 233280;
      return s / 233280;
    };
  }

  function genCandles(rng, startPrice, n, drift, vol) {
    let price = startPrice;
    const out = [];
    for (let i = 0; i < n; i++) {
      const o = price;
      const change = (rng() - 0.5) * vol + drift;
      const c = Math.max(1, o * (1 + change));
      const h = Math.max(o, c) * (1 + rng() * vol * 0.4);
      const l = Math.min(o, c) * (1 - rng() * vol * 0.4);
      const v = Math.round(500000 + rng() * 1500000);
      out.push({ o, h, l, c, v });
      price = c;
    }
    return out;
  }

  function computeMovingAverages(candles) {
    var periods = [5, 10, 20];
    var closes = candles.map(function (c) { return c.c; });
    candles.forEach(function (c, i) {
      periods.forEach(function (p) {
        if (i + 1 >= p) {
          var slice = closes.slice(i + 1 - p, i + 1);
          c["ma" + p] = slice.reduce(function (a, b) { return a + b; }, 0) / p;
        }
      });
    });
  }

  function makeQuestion(id, market, timeframe, dirSign, magnitudePct, seed) {
    const rng = rngFactory(seed);
    const lead = genCandles(rng, 10000 + rng() * 50000, LEAD_N, 0, 0.012);
    const lastClose = lead[LEAD_N - 1].c;
    const targetClose = lastClose * (1 + (dirSign * magnitudePct) / 100);
    const totalLogReturn = Math.log(targetClose / lastClose);

    const reveal = [];
    let price = lastClose;
    for (let i = 0; i < REVEAL_N; i++) {
      const isLast = i === REVEAL_N - 1;
      const o = price;
      const c = isLast
        ? targetClose
        : Math.max(1, o * Math.exp(totalLogReturn / REVEAL_N + (rng() - 0.5) * Math.abs(totalLogReturn) * 0.3));
      const hi = Math.max(o, c) * (1 + rng() * 0.01);
      const lo = Math.min(o, c) * (1 - rng() * 0.01);
      const v = Math.round(2000000 + rng() * 6000000);
      reveal.push({ o, h: hi, l: lo, c, v });
      price = c;
    }

    computeMovingAverages(lead.concat(reveal));

    return {
      id,
      market,
      timeframe,
      direction: dirSign > 0 ? "up" : "down",
      change_pct: +(dirSign * magnitudePct).toFixed(2),
      lead_candles: lead,
      reveal_candles: reveal,
      meta: { symbol: "SAMPLE", date: "2026-01-01", source: "sample" },
    };
  }

  const thresholds = { "1d": 10, "1w": 30, "3m": 5 };
  const markets = ["KR", "US"];
  const timeframes = ["1d", "1w", "3m"];
  const out = [];
  let seed = 1;
  let n = 0;
  markets.forEach((m) => {
    timeframes.forEach((tf) => {
      for (let i = 0; i < 6; i++) {
        n++;
        const dirSign = i % 2 === 0 ? 1 : -1;
        const mag = thresholds[tf] + Math.abs(Math.sin(seed)) * thresholds[tf] * 1.5;
        out.push(makeQuestion(`SAMPLE_${m}_${tf}_${i}`, m, tf, dirSign, mag, seed * 13 + n));
        seed++;
      }
    });
  });

  window.SAMPLE_QUESTIONS = out;
})();
