#!/usr/bin/env python3
"""과제 2용 실데이터 내보내기: InfluxDB 의 센서 원본값을 월별 CSV(gzip)로 저장한다.

사용 예 (젯슨에서, env 를 불러온 상태):
  venv/bin/python export_data.py --start 2025-08-01 --out export
  -> export/sensors_2025-08.csv.gz ... 와 export/devices.csv

열: time(UTC, ISO), device, item, value
devices.csv: device, space, spot, model (센서 ID 로 공간·위치·모델을 찾는 표)
원본을 그대로 내보낸다. 정제(이상값·결측·주기 맞추기)는 분석 쪽에서 한다.
"""
import argparse
import csv
import datetime as dt
import gzip
import os

from influxdb_client import InfluxDBClient


def month_starts(start, end):
    m = dt.datetime(start.year, start.month, 1, tzinfo=dt.timezone.utc)
    while m < end:
        nxt = dt.datetime(m.year + (m.month == 12), m.month % 12 + 1, 1, tzinfo=dt.timezone.utc)
        yield max(m, start), min(nxt, end), m.strftime("%Y-%m")
        m = nxt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-08-01")
    ap.add_argument("--end", help="기본: 지금")
    ap.add_argument("--measurement", default="sensor")
    ap.add_argument("--out", default="export")
    a = ap.parse_args()

    url, token, org, bucket = (os.environ[k] for k in ("INFLUX_URL", "INFLUX_TOKEN", "INFLUX_ORG", "INFLUX_BUCKET"))
    start = dt.datetime.fromisoformat(a.start).replace(tzinfo=dt.timezone.utc)
    end = dt.datetime.fromisoformat(a.end).replace(tzinfo=dt.timezone.utc) if a.end else dt.datetime.now(dt.timezone.utc)
    os.makedirs(a.out, exist_ok=True)
    devices = {}

    with InfluxDBClient(url=url, token=token, org=org, timeout=600000) as cl:
        api = cl.query_api()
        for s, e, tag in month_starts(start, end):
            path = os.path.join(a.out, "sensors_%s.csv.gz" % tag)
            n = 0
            with gzip.open(path, "wt", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "device", "item", "value"])
                # 센서마다 태그 구성(spot 유무 등)이 달라서 keep 으로 열을 맞춘다.
                flux = '''
from(bucket: "%s")
  |> range(start: %s, stop: %s)
  |> filter(fn: (r) => r._measurement == "%s" and r._field == "value")
  |> keep(columns: ["_time", "_value", "space", "spot", "device", "model", "item"])
''' % (bucket, s.strftime("%Y-%m-%dT%H:%M:%SZ"), e.strftime("%Y-%m-%dT%H:%M:%SZ"), a.measurement)
                for r in api.query_stream(flux):
                    v = r.values
                    dev = v.get("device") or "%s/%s" % (v.get("space"), v.get("spot") or "")
                    if dev not in devices:
                        devices[dev] = (v.get("space") or "", v.get("spot") or "", v.get("model") or "")
                    w.writerow([r.get_time().strftime("%Y-%m-%dT%H:%M:%SZ"), dev, v.get("item"), r.get_value()])
                    n += 1
            print("%s: %d rows -> %s" % (tag, n, path), flush=True)

    with open(os.path.join(a.out, "devices.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["device", "space", "spot", "model"])
        for d, (sp, spot, model) in sorted(devices.items()):
            w.writerow([d, sp, spot, model])
    print("devices: %d" % len(devices))


if __name__ == "__main__":
    main()
