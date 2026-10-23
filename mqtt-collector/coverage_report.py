#!/usr/bin/env python3
"""수집 현황 보고서 생성: 공간·항목별 건수, 마지막 수신 시각, 1분 커버율, 결측 구간과 추정 원인.

사용 예 (젯슨에서, env 를 불러온 상태):
  venv/bin/python coverage_report.py --hours 24 --out reports/coverage_$(date +%F).md

추정 원인 규칙 (collector_health 와 다른 센서 상황을 같이 본다):
  - 그 시간에 collector_health 기록이 없음             -> 수집기 정지/보드 꺼짐·재부팅
  - health 는 있는데 mqtt_connected=0                  -> 브로커 또는 네트워크(VPN) 끊김
  - 연결은 정상인데 모든 공간·항목이 동시에 비어 있음   -> 브로커 상위/게이트웨이 쪽 미전송
  - 연결 정상, 이 센서만 비어 있음                     -> 해당 센서(또는 그 센서의 게이트웨이) 미전송
  - 그 시간에 Influx 쓰기 실패가 있었음                -> Influx 장애 (스풀 재전송 실패 시 남는 결측)
"""
import argparse
import collections
import datetime as dt
import os

from influxdb_client import InfluxDBClient

KST = dt.timezone(dt.timedelta(hours=9))


def q(api, flux):
    return api.query(flux)


def minute(t):
    return t.replace(second=0, microsecond=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--end", help="끝 시각 ISO (기본: 지금, 분 단위 내림)")
    ap.add_argument("--measurement", default="sensor")
    ap.add_argument("--min-gap", type=int, default=2, help="이 분 수 이상 연속으로 빈 구간만 결측으로 나열")
    ap.add_argument("--expected-period-s", type=float, default=None,
                    help="모든 센서의 전송 주기를 이 값으로 고정 (기본: 센서마다 자동 추정)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    url, token, org, bucket = (os.environ[k] for k in ("INFLUX_URL", "INFLUX_TOKEN", "INFLUX_ORG", "INFLUX_BUCKET"))
    end = dt.datetime.fromisoformat(a.end) if a.end else dt.datetime.now(dt.timezone.utc)
    end = minute(end.astimezone(dt.timezone.utc))
    start = end - dt.timedelta(hours=a.hours)
    rng = 'range(start: %s, stop: %s)' % (start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    total_min = int((end - start).total_seconds() // 60)

    with InfluxDBClient(url=url, token=token, org=org, timeout=60000) as cl:
        api = cl.query_api()
        # 1) 공간·항목별 1분 건수 (빈 분 제외)
        per_min = collections.defaultdict(dict)   # (space,item) -> {minute: count}
        for t in q(api, '''
from(bucket: "%s") |> %s
  |> filter(fn: (r) => r._measurement == "%s" and r._field == "value")
  |> keep(columns: ["_time", "_value", "_field", "space", "item"])
  |> group(columns: ["space", "item"])
  |> aggregateWindow(every: 1m, fn: count, createEmpty: false, timeSrc: "_start")
''' % (bucket, rng, a.measurement)):
            for r in t.records:
                key = (r.values.get("space", "-"), r.values.get("item", "-"))
                per_min[key][minute(r.get_time())] = int(r.get_value())
        # 2) 마지막 수신 시각, 평균 지연, 중복 의심 (전체 기간 무관 최근 7일)
        last = {}
        for t in q(api, '''
from(bucket: "%s") |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "%s" and r._field == "recv_ts_ms")
  |> keep(columns: ["_time", "_value", "_field", "space", "item"])
  |> group(columns: ["space", "item"]) |> max()
''' % (bucket, a.measurement)):
            for r in t.records:
                last[(r.values.get("space", "-"), r.values.get("item", "-"))] = \
                    dt.datetime.fromtimestamp(r.get_value() / 1000, KST)
        delay = {}
        for t in q(api, '''
from(bucket: "%s") |> %s
  |> filter(fn: (r) => r._measurement == "%s" and r._field == "delay_ms")
  |> keep(columns: ["_time", "_value", "_field", "space", "item"])
  |> group(columns: ["space", "item"]) |> quantile(q: 0.95)
''' % (bucket, rng, a.measurement)):
            for r in t.records:
                delay[(r.values.get("space", "-"), r.values.get("item", "-"))] = r.get_value()
        # 3) 수집기 헬스 (1분 단위)
        health = {}   # minute -> dict
        for t in q(api, '''
from(bucket: "%s") |> %s
  |> filter(fn: (r) => r._measurement == "collector_health")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
''' % (bucket, rng)):
            for r in t.records:
                health[minute(r.get_time())] = r.values
        dup_total = sum(int(h.get("duplicates") or 0) for h in health.values())

    minutes = [start + dt.timedelta(minutes=i) for i in range(total_min)]
    any_data = set()
    for d in per_min.values():
        any_data.update(d.keys())

    def health_near(m):
        # health 는 60초 주기라 그 분 또는 앞뒤 1분에 기록이 있으면 '살아 있음'
        for k in (m, m - dt.timedelta(minutes=1), m + dt.timedelta(minutes=1)):
            if k in health:
                return health[k]
        return None

    def cause(gap_start, gap_len, n_series):
        ms = [gap_start + dt.timedelta(minutes=i) for i in range(gap_len)]
        hs = [health_near(m) for m in ms]
        if all(h is None for h in hs):
            return "수집기 정지 또는 보드 꺼짐/재부팅 (health 기록 없음)"
        if any(h is not None and int(h.get("mqtt_connected") or 0) == 0 for h in hs):
            return "브로커 또는 네트워크/VPN 끊김 (mqtt_connected=0)"
        if any(h is not None and int(h.get("write_errors") or 0) > 0 for h in hs):
            return "InfluxDB 쓰기 실패 구간 (스풀 재전송 여부 확인)"
        if n_series > 1 and not any(m in any_data for m in ms):
            return "연결은 정상인데 모든 센서가 비어 있음 → 브로커 상위/게이트웨이 미전송"
        return "연결 정상, 이 센서만 미전송 → 센서/전원/센서측 네트워크 확인"

    def period_min(d):
        # 지정이 없으면 센서마다 실제 전송 주기(데이터 있는 분 사이 간격의 중앙값)를 쓴다. 회의실처럼 10분 주기인 센서용
        if a.expected_period_s:
            return max(1, int(round(a.expected_period_s / 60)))
        ts = sorted(d)
        gaps_ = sorted(int((b - x).total_seconds() // 60) for x, b in zip(ts, ts[1:]))
        return max(1, gaps_[len(gaps_) // 2]) if gaps_ else 1

    rows, gaps = [], []
    for key in sorted(per_min):
        d = per_min[key]
        step = period_min(d)
        have = sum(1 for m in minutes if m in d)
        expected = total_min / step
        cov = min(100.0, 100.0 * have / expected) if expected else 0
        rows.append((key, sum(d.values()), have, cov, last.get(key), delay.get(key), step))
        run = None
        for i, m in enumerate(minutes + [None]):
            empty = m is not None and m not in d
            if empty and run is None:
                run = i
            elif not empty and run is not None:
                ln = i - run
                if ln >= max(a.min_gap, step + 1):
                    gaps.append((key, minutes[run], ln, cause(minutes[run], ln, len(per_min))))
                run = None

    fmt = lambda t: t.astimezone(KST).strftime("%m-%d %H:%M") if t else "-"
    out = []
    out.append("# 수집 현황 보고")
    out.append("")
    out.append("- 기간: %s ~ %s (KST), %d분" % (fmt(start), fmt(end), total_min))
    out.append("- 커버율 = 데이터가 있는 1분 구간 수 / (전체 분 ÷ 전송 주기). 주기 1분이면 1분 단위 커버율 그대로")
    out.append("- 수집기 health 기록: %d/%d분, 재전송(중복)으로 건너뛴 건수: %d" % (len(health), total_min, dup_total))
    out.append("")
    out.append("| 공간 | 항목 | 주기 | 건수 | 데이터 있는 분 | 커버율 | 마지막 수신(KST) | 지연 p95 |")
    out.append("|---|---|---:|---:|---:|---:|---|---:|")
    for (sp, it), n, have, cov, ls, dl, st in rows:
        mark = "" if cov >= 95 else " ⚠"
        out.append("| %s | %s | %d분 | %d | %d | %.1f%%%s | %s | %s |" % (
            sp, it, st, n, have, cov, mark, ls.strftime("%m-%d %H:%M:%S") if ls else "-",
            ("%.1fs" % (dl / 1000)) if dl is not None else "-"))
    out.append("")
    out.append("## 결측 구간 (%d분 이상)" % a.min_gap)
    out.append("")
    if not gaps:
        out.append("없음")
    else:
        out.append("| 공간 | 항목 | 시작(KST) | 길이 | 추정 원인 |")
        out.append("|---|---|---|---:|---|")
        for (sp, it), gs, ln, c in sorted(gaps, key=lambda g: g[1]):
            out.append("| %s | %s | %s | %d분 | %s |" % (sp, it, fmt(gs), ln, c))
    text = "\n".join(out) + "\n"
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)


if __name__ == "__main__":
    main()
