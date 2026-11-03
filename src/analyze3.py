#!/usr/bin/env python3
"""과제 3 결과 정리: 비교표, 공간·시간대별 오차, 일정 정보 효과, 오차가 큰 순간, 그림.

입력: out/forecast/pred_*.parquet (forecast.py), out/baseline_mae.csv (과제 2)
출력: out/forecast/table_*.csv, out/forecast/*.png
최종 모델 = 검증 기간 실습실 CO2 60분 MAE가 가장 작은 설정(GRU, 과거 2시간, 일정 포함)의 시드 3개 평균.
"""
import glob
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from evaluate import points, busy  # noqa: E402

D = os.path.join(os.path.dirname(__file__), "..", "out")
F = os.path.join(D, "forecast")
plt.rcParams.update({"font.family": "WenQuanYi Zen Hei", "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.color": "#e4e3df", "grid.linewidth": 0.6, "axes.edgecolor": "#8a8984",
                     "axes.labelcolor": "#52514e", "xtick.color": "#52514e", "ytick.color": "#52514e"})
SERIES = ["실습실_co2", "실습실_temperature", "회의실_co2", "회의실_temperature"]
SEEDS = ["", "_seed1", "_seed2"]
FINAL = [f"gru_L24_s1{s}" for s in SEEDS]
NOSCHED = [f"gru_L24_s0{s}" for s in SEEDS]


def preds(runs):
    """여러 run 예측의 평균 -> (series, h, time, pred)"""
    acc = None
    for r in runs:
        p = pd.read_parquet(os.path.join(F, f"pred_{r}.parquet"))
        p = p.set_index(["space", "split", "time", "h"])[["co2", "temperature"]]
        acc = p if acc is None else acc + p
    acc = (acc / len(runs)).reset_index()
    long = []
    for item in ["co2", "temperature"]:
        d = acc[["time", "h", "space", item]].rename(columns={item: "pred"})
        d["series"] = d["space"] + "_" + item
        long.append(d.drop(columns="space"))
    return pd.concat(long)


def errors(pts, runs):
    m = pts.merge(preds(runs), on=["series", "h", "time"], how="left")
    m["pred"] = m["pred"].fillna(m["persist"])
    m["ae"] = (m["pred"] - m["y"]).abs()
    m["ae_p"] = (m["persist"] - m["y"]).abs()
    return m


def mae(m, by=("split", "series", "h")):
    return m.groupby(list(by))["ae"].mean()


def main():
    g = pd.read_parquet(os.path.join(D, "grid5.parquet"))
    pts = points(g)
    runs = sorted(os.path.basename(f)[5:-8] for f in glob.glob(os.path.join(F, "pred_*.parquet")))
    meta = {r: json.load(open(os.path.join(F, f"meta_{r}.json"))) for r in runs}

    # 1) 모든 run, 60분 뒤, 검증/시험 (선택은 검증 실습실 CO2 60분으로만)
    rows = []
    for r in runs:
        e = mae(errors(pts, [r]))
        row = {"run": r, "best_epoch": meta[r]["best_epoch"], "train_min": round(meta[r]["train_sec"] / 60, 1),
               "params": meta[r]["n_params"]}
        for part in ["val", "test"]:
            for s in SERIES:
                for h in [30, 60]:
                    row[f"{part}|{s}|{h}"] = e[(part, s, h)]
        rows.append(row)
    allruns = pd.DataFrame(rows)
    allruns.to_csv(os.path.join(F, "table_all_runs.csv"), index=False)

    # 2) 최종 비교표: 기준선 3종 + 모델(3종 대표: 과거 2시간·일정 포함, 시드 0) + 최종(시드 3개 평균)
    base = pd.read_csv(os.path.join(D, "baseline_mae.csv"))
    base = base[base["분할"] == "test"]
    base["series"] = base["공간"] + "_" + base["항목"]
    comp = base.set_index(["series", "h_분"])[["현재값 유지", "최근 1시간 평균", "어제 같은 시각"]]
    comp.index.names = ["series", "h"]
    for name, rs in [("LSTM", ["lstm_L24_s1"]), ("TCN", ["tcn_L24_s1"]), ("GRU", ["gru_L24_s1"]),
                     ("GRU 최종(시드 3개 평균)", FINAL), ("GRU 일정 제외(시드 3개 평균)", NOSCHED)]:
        if all(r in runs for r in rs):
            comp[name] = mae(errors(pts, rs)).loc["test"]
    comp["최종 개선율 %"] = 100 * (1 - comp["GRU 최종(시드 3개 평균)"] / comp["현재값 유지"])
    comp.reset_index().to_csv(os.path.join(F, "table_compare_test.csv"), index=False)
    print(comp.round(3).to_string())

    # 검증 기간 같은 표 (선택 근거)
    vb = pd.read_csv(os.path.join(D, "baseline_mae.csv")); vb = vb[vb["분할"] == "val"]
    vb["series"] = vb["공간"] + "_" + vb["항목"]
    vcomp = vb.set_index(["series", "h_분"])[["현재값 유지"]]; vcomp.index.names = ["series", "h"]
    vcomp["GRU 최종"] = mae(errors(pts, FINAL)).loc["val"]
    vcomp["GRU 일정 제외"] = mae(errors(pts, NOSCHED)).loc["val"]
    vcomp.reset_index().to_csv(os.path.join(F, "table_compare_val.csv"), index=False)

    # 3) 일정 정보 효과: 시드별, 시간대별
    ef, e0 = errors(pts, FINAL), errors(pts, NOSCHED)
    sched = []
    for part in ["val", "test"]:
        for s in SERIES:
            for h in [15, 30, 60]:
                a = ef[(ef.split == part) & (ef.series == s) & (ef.h == h)]
                b = e0[(e0.split == part) & (e0.series == s) & (e0.h == h)]
                r = {"split": part, "series": s, "h": h, "일정 포함": a.ae.mean(), "일정 제외": b.ae.mean()}
                bz = busy(a.time)
                r["일정 포함|평일08-19"] = a.ae[bz].mean(); r["일정 제외|평일08-19"] = b.ae[busy(b.time)].mean()
                # 시드별 값(편차 확인)
                for k, (x, y) in enumerate(zip(FINAL, NOSCHED)):
                    r[f"seed{k}|포함"] = mae(errors(pts, [x])).loc[(part, s, h)]
                    r[f"seed{k}|제외"] = mae(errors(pts, [y])).loc[(part, s, h)]
                sched.append(r)
    sched = pd.DataFrame(sched)
    sched["차이(제외-포함)"] = sched["일정 제외"] - sched["일정 포함"]
    sched.to_csv(os.path.join(F, "table_schedule.csv"), index=False)
    print(sched.round(3).to_string())

    # 일정 효과를 날짜 종류별로: 시험 기간 실습실 CO2 60분
    t = ef[(ef.split == "test") & (ef.series == "실습실_co2") & (ef.h == 60)].copy()
    t0 = e0[(e0.split == "test") & (e0.series == "실습실_co2") & (e0.h == 60)].set_index("time").ae
    hol = pd.to_datetime(__import__("forecast").HOLIDAYS)
    day = t.time.dt.tz_localize(None).dt.normalize()
    t["daytype"] = np.select([day.isin(hol), t.time.dt.dayofweek >= 5], ["공휴일", "주말"], "평일")
    t["ae0"] = t.time.map(t0)
    dt = t.groupby("daytype").agg(n=("ae", "size"), 현재값유지=("ae_p", "mean"), 일정포함=("ae", "mean"), 일정제외=("ae0", "mean"))
    dt.to_csv(os.path.join(F, "table_schedule_daytype.csv")); print(dt.round(1))

    # 4) 공간·시간대별 (최종 모델, 시험)
    sp = ef[ef.split == "test"].copy()
    sp["시간대"] = np.where(busy(sp.time), "평일 08~19시", "그 밖")
    st = sp.groupby(["series", "h", "시간대"]).agg(n=("ae", "size"), 현재값유지=("ae_p", "mean"), 모델=("ae", "mean")).reset_index()
    st["개선율 %"] = 100 * (1 - st["모델"] / st["현재값유지"])
    st.to_csv(os.path.join(F, "table_space_time.csv"), index=False); print(st.round(2).to_string())

    # 5) 오차가 큰 순간: 실습실·회의실 CO2 60분, 목표 시각(t+h)의 시각대별
    rows = []
    for s in ["실습실_co2", "회의실_co2"]:
        x = sp[(sp.series == s) & (sp.h == 60)].copy()
        tgt = x.time + pd.Timedelta(minutes=60)
        wd = (tgt.dt.dayofweek < 5)
        hm = tgt.dt.hour * 60 + tgt.dt.minute
        x["구간"] = np.select([wd & (hm >= 480) & (hm < 570), wd & (hm >= 690) & (hm < 750), wd & (hm >= 780) & (hm < 840),
                               wd & (hm >= 1080) & (hm < 1140), wd & (hm >= 570) & (hm < 1080)],
                              ["출근·수업 시작(8:00~9:30)", "점심 시작(11:30~12:30)", "오후 수업(13:00~14:00)", "퇴실(18:00~19:00)",
                               "수업 중 나머지"], "밤·주말")
        for k, v in x.groupby("구간"):
            rows.append({"series": s, "구간(목표 시각)": k, "n": len(v), "현재값유지": v.ae_p.mean(), "모델": v.ae.mean(),
                         "오차 상위 5% 비중 %": 100 * (v.ae > x.ae.quantile(.95)).sum() / (x.ae > x.ae.quantile(.95)).sum()})
    big = pd.DataFrame(rows); big.to_csv(os.path.join(F, "table_big_errors.csv"), index=False); print(big.round(1).to_string())
    worst = sp[(sp.series == "실습실_co2") & (sp.h == 60)].nlargest(15, "ae")[["time", "y", "pred", "persist", "ae"]]
    worst.to_csv(os.path.join(F, "table_worst_lab.csv"), index=False); print(worst.round(0).to_string())
    worstm = sp[(sp.series == "회의실_co2") & (sp.h == 60)].nlargest(15, "ae")[["time", "y", "pred", "persist", "ae"]]
    worstm.to_csv(os.path.join(F, "table_worst_meeting.csv"), index=False)

    # 6) 회의실 특성: 60분 동안의 실제 변화 크기 분포 (예측 난이도)
    var = []
    for s in ["실습실_co2", "회의실_co2"]:
        x = sp[(sp.series == s) & (sp.h == 60)]
        ch = (x.y - x.persist).abs()
        var.append({"series": s, "60분 변화 중앙값": ch.median(), "60분 변화 90%": ch.quantile(.9),
                    "200ppm 넘게 변한 비율 %": 100 * (ch > 200).mean(), "평일 낮 비율 %": 100 * busy(x.time).mean()})
    pd.DataFrame(var).to_csv(os.path.join(F, "table_space_change.csv"), index=False); print(pd.DataFrame(var).round(1))

    # 그림 1: 예측 시간별 MAE (시험)
    fig, ax = plt.subplots(1, 4, figsize=(13, 3.4))
    lines = [("현재값 유지", "#8a8984", "--"), ("최근 1시간 평균", "#c9c7c1", ":"), ("LSTM", "#eb6834", "-"), ("TCN", "#1baf7a", "-"),
             ("GRU 일정 제외(시드 3개 평균)", "#9b6fd6", "-"), ("GRU 최종(시드 3개 평균)", "#2a78d6", "-")]
    for i, s in enumerate(SERIES):
        c = comp.loc[s]
        for n, col, ls in lines:
            if n in c:
                ax[i].plot(c.index, c[n], marker="o", ms=4, lw=2, color=col, ls=ls,
                           label=n.replace("(시드 3개 평균)", ""))
        sp_, it = s.split("_")
        ax[i].set_title(f"{sp_} {'CO₂ (ppm)' if it == 'co2' else '온도 (℃)'}", loc="left", fontsize=10)
        ax[i].set_xticks([5, 15, 30, 60]); ax[i].set_xlabel("몇 분 뒤"); ax[i].set_ylim(bottom=0)
    ax[0].legend(frameon=False, fontsize=7)
    fig.text(0.01, 0.01, "시험 기간 2026-09-01 ~ 10-22, MAE, 실데이터", fontsize=8, color="#8a8984")
    fig.tight_layout(rect=(0, 0.04, 1, 1)); fig.savefig(os.path.join(F, "mae_by_horizon.png"), dpi=150); plt.close(fig)

    # 그림 2: 예시 날짜 — 실습실 CO2, 60분 앞 예측 vs 실측 (10월 평일 이틀)
    fig, ax = plt.subplots(1, 2, figsize=(12, 3.4), sharey=True)
    for i, d in enumerate(["2026-10-14", "2026-10-21"]):
        x = ef[(ef.series == "실습실_co2") & (ef.h == 60) & (ef.time.dt.strftime("%Y-%m-%d") == d)]
        tt = x.time + pd.Timedelta(minutes=60)
        ax[i].plot(tt, x.y, color="#2b2a27", lw=2, label="실측")
        ax[i].plot(tt, x.pred, color="#2a78d6", lw=1.6, label="GRU 60분 전 예측")
        ax[i].plot(tt, x.persist, color="#8a8984", lw=1.2, ls="--", label="현재값 유지(60분 전 값)")
        ax[i].set_title(f"실습실 CO₂ — {d}", loc="left", fontsize=10)
        ax[i].xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H", tz="Asia/Seoul"))
        ax[i].set_xlabel("시각")
    ax[0].set_ylabel("ppm"); ax[0].legend(frameon=False, fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(F, "example_days.png"), dpi=150); plt.close(fig)


if __name__ == "__main__":
    main()
