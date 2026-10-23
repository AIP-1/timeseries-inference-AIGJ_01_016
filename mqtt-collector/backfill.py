#!/usr/bin/env python3
"""학교 InfluxDB(읽기 전용)에서 과거 데이터를 읽어, 수집기와 같은 형식으로 젯슨 InfluxDB 에 넣는다.

학교 DB 구조: measurement = 항목(co2, temperature ...), field = value,
              tag = device_id, location(공간), spot, point, gateway_id(일부)
젯슨 형식:    measurement = sensor, field = value, time_src="backfill",
              tag = space, spot, device, item

기간: --start 부터 수집기가 처음 받은 시각(젯슨 DB 의 첫 recv_ts_ms)까지. 겹치지 않으니 중복이 없다.
backfill 은 recv_ts_ms 를 쓰지 않으므로 여러 번 돌려도 끝 시각이 바뀌지 않고, 같은 점은 덮어써진다.

사용 예 (젯슨에서):
  set -a; . <(sudo cat /etc/mqtt-collector/env); set +a
  export SRC_TOKEN='학교토큰'
  venv/bin/python backfill.py --check            # 학교 DB 시각이 수집기 시각과 얼마나 다른지 먼저 확인
  venv/bin/python backfill.py --dry-run          # 날짜별 건수만 출력
  venv/bin/python backfill.py                    # 실제로 넣기
"""
import argparse
import collections
import datetime as dt
import os
import statistics

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

KST = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def flux_or(col, values):
    return " or ".join('r.%s == "%s"' % (col, v) for v in values)


def src_query(bucket, start, stop, items, spaces, devices=None):
    dev = (" and (%s)" % flux_or("device_id", devices)) if devices else ""
    return '''
from(bucket: "%s") |> range(start: %s, stop: %s)
  |> filter(fn: (r) => (%s) and r._field == "value" and (%s)%s)
  |> keep(columns: ["_time", "_value", "_measurement", "device_id", "location", "spot", "point"])
''' % (bucket, iso(start), iso(stop), flux_or("_measurement", items), flux_or("location", spaces), dev)


def src_rows(api, flux):
    seen = set()
    for r in api.query_stream(flux):
        v = r.values
        key = (v.get("device_id"), v.get("_measurement"), r.get_time())
        if key in seen:              # 게이트웨이별로 같은 값이 두 번 들어간 경우
            continue
        seen.add(key)
        yield v, r.get_time()


def collector_start(local_api, bucket):
    # 수집기가 쓴 점에만 recv_ts_ms 가 있다. 그중 가장 이른 시각 = 수집기 시작
    for t in local_api.query('''
from(bucket: "%s") |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "sensor" and r._field == "recv_ts_ms")
  |> keep(columns: ["_time", "_value"]) |> group() |> min(column: "_time") |> limit(n: 1)
''' % bucket):
        for r in t.records:
            return r.get_time()
    return None


def check(src_api, local_api, a, local_bucket, start):
    """겹치는 구간에서 같은 센서·항목·값을 짝지어 (학교 시각 - 수집기 시각) 을 본다."""
    end = min(start + dt.timedelta(hours=a.check_hours), dt.datetime.now(UTC))
    local = collections.defaultdict(list)     # (device,item) -> [(t, value)]
    for t in local_api.query('''
from(bucket: "%s") |> range(start: %s, stop: %s)
  |> filter(fn: (r) => r._measurement == "sensor" and r._field == "value")
  |> keep(columns: ["_time", "_value", "device", "item"])
''' % (local_bucket, iso(start), iso(end))):
        for r in t.records:
            local[(r.values.get("device"), r.values.get("item"))].append((r.get_time(), r.get_value()))
    if not local:
        print("겹치는 구간에 수집기 데이터가 없다.")
        return
    devices = sorted({d for d, _ in local})
    diffs = collections.defaultdict(list)
    unmatched = collections.Counter()
    for v, t in src_rows(src_api, src_query(a.src_bucket, start - dt.timedelta(minutes=10),
                                            end + dt.timedelta(minutes=10), a.items, a.spaces, devices)):
        cands = [lt for lt, lv in local.get((v["device_id"], v["_measurement"]), [])
                 if abs((lt - t).total_seconds()) <= 600 and abs(lv - float(v["_value"])) < 1e-6]
        if cands:
            lt = min(cands, key=lambda x: abs((x - t).total_seconds()))
            diffs[v["device_id"]].append((t - lt).total_seconds())
        else:
            unmatched[v["device_id"]] += 1
    print("구간 %s ~ %s (KST)" % (start.astimezone(KST).strftime("%m-%d %H:%M"), end.astimezone(KST).strftime("%m-%d %H:%M")))
    print("%-18s %6s %10s %10s %10s %8s" % ("device", "짝지음", "중앙값(s)", "최소(s)", "최대(s)", "5분차"))
    for d in devices:
        ds = diffs.get(d, [])
        if not ds:
            print("%-18s %6d %10s %10s %10s %8s  (짝 없음 %d)" % (d, 0, "-", "-", "-", "-", unmatched[d]))
            continue
        far = sum(1 for x in ds if abs(x) > 60)
        print("%-18s %6d %10.1f %10.1f %10.1f %8d" % (d, len(ds), statistics.median(ds), min(ds), max(ds), far))
    print("\n5분차 = 학교 시각이 수집기보다 60초 넘게 다른 건수. 0 이면 학교 DB 시각을 그대로 써도 된다.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-url", default=os.environ.get("SRC_URL", "http://10.116.64.13:8086"))
    ap.add_argument("--src-org", default=os.environ.get("SRC_ORG", "33e75366a5081f13"))
    ap.add_argument("--src-bucket", default=os.environ.get("SRC_BUCKET", "iot-bucket"))
    ap.add_argument("--start", default="2025-08-01", help="이 날짜(KST)부터 가져온다")
    ap.add_argument("--stop", default=None, help="이 시각(ISO)까지. 기본: 수집기가 처음 받은 시각")
    ap.add_argument("--spaces", default="실습실,회의실")
    ap.add_argument("--items", default="co2,temperature,humidity")
    ap.add_argument("--chunk-days", type=float, default=1)
    ap.add_argument("--check", action="store_true", help="겹치는 구간에서 학교 시각과 수집기 시각 차이만 확인")
    ap.add_argument("--check-hours", type=float, default=6)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    a.spaces = [s.strip() for s in a.spaces.split(",") if s.strip()]
    a.items = [s.strip() for s in a.items.split(",") if s.strip()]

    src = InfluxDBClient(url=a.src_url, token=os.environ["SRC_TOKEN"], org=a.src_org, timeout=120000)
    dst = InfluxDBClient(url=os.environ["INFLUX_URL"], token=os.environ["INFLUX_TOKEN"],
                         org=os.environ["INFLUX_ORG"], timeout=60000)
    bucket = os.environ["INFLUX_BUCKET"]
    src_api, dst_api = src.query_api(), dst.query_api()

    cstart = collector_start(dst_api, bucket)
    if a.check:
        if cstart is None:
            print("수집기 데이터가 아직 없다.")
            return
        check(src_api, dst_api, a, bucket, cstart)
        return

    stop = dt.datetime.fromisoformat(a.stop) if a.stop else cstart
    if stop is None:
        raise SystemExit("수집기 데이터가 없어 끝 시각을 정할 수 없다. --stop 을 지정한다.")
    if stop.tzinfo is None:
        stop = stop.replace(tzinfo=KST)
    start = dt.datetime.fromisoformat(a.start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=KST)
    print("기간 %s ~ %s (KST), 공간 %s, 항목 %s" % (
        start.astimezone(KST).strftime("%Y-%m-%d %H:%M"), stop.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
        a.spaces, a.items))

    writer = None if a.dry_run else dst.write_api(write_options=SYNCHRONOUS)
    total = 0
    t0 = start
    step = dt.timedelta(days=a.chunk_days)
    while t0 < stop:
        t1 = min(t0 + step, stop)
        n = 0
        per_item = collections.Counter()
        batch = []
        for v, t in src_rows(src_api, src_query(a.src_bucket, t0, t1, a.items, a.spaces)):
            p = (Point("sensor").tag("space", v["location"]).tag("item", v["_measurement"]).field("value", float(v["_value"]))
                 .field("time_src", "backfill").time(t))
            if v.get("device_id"):       # 2025년 초기 데이터에는 device_id 가 없다
                p.tag("device", v["device_id"])
            spot = v.get("spot") or v.get("point")
            if spot:
                p.tag("spot", spot)
            batch.append(p)
            n += 1
            per_item["%s/%s" % (v["location"], v["_measurement"])] += 1
        if writer and batch:
            for i in range(0, len(batch), 5000):
                writer.write(bucket=bucket, record=batch[i:i + 5000])
        if n:
            print("%s  %6d건  %s" % (t0.astimezone(KST).strftime("%Y-%m-%d"), n,
                                    ", ".join("%s %d" % kv for kv in sorted(per_item.items()))))
        total += n
        t0 = t1
    if writer:
        writer.close()
    print("합계 %d건%s" % (total, " (dry-run, 쓰지 않음)" if a.dry_run else " 저장"))
    src.close()
    dst.close()


if __name__ == "__main__":
    main()
