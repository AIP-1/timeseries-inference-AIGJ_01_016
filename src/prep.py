#!/usr/bin/env python3
"""정제 + 5분 격자 만들기.

입력: export/sensors_*.csv.gz, export/devices.csv (export_data.py 결과)
출력: out/grid5.parquet   공간·항목별 5분 값 (실측 칸만 값이 있음, 보간 없음)
      out/clean_log.json  규칙별 제거 건수

정제 규칙 (자세한 이유는 진단서 참고)
  R0 중복: (센서, 항목, 시각)이 같은 행은 하나만 남긴다.
  R1 물리 범위: CO2 300~9999 ppm, 온도 -10~50 ℃, 습도 0~100 % 밖은 제거.
     65535(센서 오류 코드), 0, 10000(측정 상한 포화)이 여기서 걸린다.
  R2 순간 튐: 1~2분 주기 실습실 CO2 센서에서 앞뒤 5개 중앙값과 500 ppm 넘게 다른 값.
  R3 센서 간 불일치: 실습실 온도가 같은 시각 다른 센서들 중앙값과 5 ℃ 넘게 다른 값.
  R4 분석 기간: 2025-09-03 16:00 이전(옛 서버에서 옮겨온 6일치, 뒤에 20일 공백)은 쓰지 않는다.
  센서 구성: 실습실 = 실내 장기 센서 4대(전방 좌·우, 후방 오른쪽, 후면 오른쪽)의 중앙값.
     복도 센서(실외)와 2026-08 에 추가된 중앙 센서 2대·무태그 센서 1대는 기간 내내 있지 않아서 뺀다.
  주기 맞추기: 센서마다 5분 평균 -> 자기 전송 주기만큼만 앞 값 유지(10분 센서는 1칸) -> 공간 중앙값.
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

SRC = sys.argv[1] if len(sys.argv) > 1 else "export"
OUT = sys.argv[2] if len(sys.argv) > 2 else "out"
START = pd.Timestamp("2025-09-03 16:00", tz="Asia/Seoul")
RANGE = {"co2": (300, 9999), "temperature": (-10, 50), "humidity": (0, 100)}
LAB_SPOTS = ["전방 좌측", "전방 우측", "후방 오른쪽", "후면 오른쪽"]
STEP = "5min"


def load():
    df = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(SRC, "sensors_*.csv.gz")))], ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert("Asia/Seoul")
    dev = pd.read_csv(os.path.join(SRC, "devices.csv")).fillna("")
    return df.merge(dev, on="device", how="left")


def main():
    os.makedirs(OUT, exist_ok=True)
    log = {}
    df = load()
    log["rows_raw"] = len(df)

    n = len(df); df = df.drop_duplicates(["device", "item", "time"]); log["R0_duplicates"] = n - len(df)
    n = len(df); df = df[df.time >= START]; log["R4_before_start"] = n - len(df)
    # 분석 대상 센서만
    lab = (df.space == "실습실") & df.spot.isin(LAB_SPOTS) & df.device.str.startswith("24e")
    meet = (df.space == "회의실") & df.device.str.startswith("24e")
    log["excluded_sensors_rows"] = int((~(lab | meet)).sum())
    df = df[lab | meet].copy()

    # R1 물리 범위
    lo = df.item.map(lambda i: RANGE[i][0]); hi = df.item.map(lambda i: RANGE[i][1])
    bad = (df.value < lo) | (df.value > hi)
    log["R1_physical"] = df[bad].groupby(["space", "item"]).size().rename(lambda x: x).to_dict()
    log["R1_physical_values"] = {str(k): int(v) for k, v in df[bad].groupby("value").size().items()}
    df = df[~bad]

    # R2 순간 튐 (실습실 CO2, 1~2분 센서)
    drop = []
    for d, g in df[(df.item == "co2") & (df.space == "실습실")].groupby("device"):
        g = g.sort_values("time")
        if g.time.diff().dt.total_seconds().median() > 180:
            continue
        med = g.value.rolling(5, center=True, min_periods=3).median()
        drop += list(g.index[(g.value - med).abs() > 500])
    log["R2_spike"] = len(drop)
    df = df.drop(drop)

    # R3 실습실 온도 센서 간 불일치 (1분 단위로 비교)
    t = df[(df.item == "temperature") & (df.space == "실습실")].copy()
    t["m"] = t.time.dt.floor("10min")
    med = t.groupby("m").value.median()
    dev_ = (t.value - t.m.map(med)).abs()
    drop = t.index[dev_ > 5]
    log["R3_cross_sensor"] = len(drop)
    df = df.drop(drop)

    # 5분 격자: 센서별 평균 -> 주기만큼 유지 -> 공간 중앙값
    end = df.time.max().floor(STEP)
    idx = pd.date_range(START, end, freq=STEP)
    cols = {}
    for (sp, it), g in df.groupby(["space", "item"]):
        per = []
        for d, gd in g.groupby("device"):
            period = gd.time.sort_values().diff().dt.total_seconds().median()
            s = gd.set_index("time").value.resample(STEP).mean().reindex(idx)
            hold = max(int(round(period / 300)) - 1, 0)   # 10분 센서 -> 1칸, 1~2분 센서 -> 0칸
            if hold and sp == "실습실":
                s = s.ffill(limit=hold)
            per.append(s)
        cols[f"{sp}_{it}"] = pd.concat(per, axis=1).median(axis=1, skipna=True)
        cols[f"{sp}_{it}_nsensor"] = pd.concat(per, axis=1).notna().sum(axis=1)
    grid = pd.DataFrame(cols, index=idx)
    # 반응 분석용 1분 격자 (실습실 CO2만, 같은 방식: 센서별 1분 평균 -> 주기만큼 유지 -> 중앙값)
    idx1 = pd.date_range(START, end, freq="1min")
    per = []
    for d, gd in df[(df.space == "실습실") & (df.item == "co2")].groupby("device"):
        period = gd.time.sort_values().diff().dt.total_seconds().median()
        s = gd.set_index("time").value.resample("1min").mean().reindex(idx1)
        hold = int(round(period / 60)) - 1
        per.append(s.ffill(limit=hold) if hold > 0 else s)
    pd.DataFrame({"실습실_co2": pd.concat(per, axis=1).median(axis=1)}, index=idx1).rename_axis("time") \
        .to_parquet(os.path.join(OUT, "grid1_lab_co2.parquet"))
    grid.index.name = "time"
    grid.to_parquet(os.path.join(OUT, "grid5.parquet"))
    log["grid"] = {"start": str(idx[0]), "end": str(idx[-1]), "steps": len(idx)}
    log["R1_physical"] = {f"{k[0]}_{k[1]}": int(v) for k, v in log["R1_physical"].items()}
    json.dump(log, open(os.path.join(OUT, "clean_log.json"), "w"), ensure_ascii=False, indent=1, default=int)
    print(json.dumps(log, ensure_ascii=False, indent=1, default=int))


if __name__ == "__main__":
    main()
