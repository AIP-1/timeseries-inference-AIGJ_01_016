#!/usr/bin/env python3
"""데이터 진단 + 기준선 평가.

입력: out/grid5.parquet (prep.py)
출력: out/ 아래 표(csv/json)와 그림(png)
  split.json, norm_stats.json, missing.csv, profile_weekday.png, profile_weekend.png,
  response.csv, response_days.csv, baseline_mae.csv, baseline_mae.png, eval_index.parquet
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
plt.rcParams.update({"font.family": "WenQuanYi Zen Hei", "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.color": "#e4e3df", "grid.linewidth": 0.6, "axes.edgecolor": "#8a8984",
                     "axes.labelcolor": "#52514e", "xtick.color": "#52514e", "ytick.color": "#52514e"})
C = {"실습실": "#2a78d6", "회의실": "#eb6834", "x": "#1baf7a"}
SERIES = ["실습실_co2", "실습실_temperature", "회의실_co2", "회의실_temperature"]
UNIT = {"co2": "ppm", "temperature": "℃"}
# 분할: 시간 순서대로. 경계는 날짜 단위.
SPLIT = {"train": ("2025-09-03 16:00", "2026-07-01"), "val": ("2026-07-01", "2026-09-01"), "test": ("2026-09-01", "2026-10-23")}
# 짧은 결측 = 메시지 2건까지 빠진 정도. 그 안에서는 마지막 실측값을 유지(인과적), 넘으면 결측으로 둔다.
HOLD = {"실습실": 3, "회의실": 4}   # 5분 칸 수: 15분, 20분
# 일정: 2026-10-01 부터 확인된 실습실 평일 일정 (수업 09:00~18:00, 점심 11:30~13:00).
# 그 이전과 회의실은 일정 자료가 없다. 이전 기간은 같은 일정이었다고 가정하고 따로 집계한다.
EVENTS = {"수업 시작": ("09:00", +1), "점심 시작": ("11:30", -1), "오후 수업": ("13:00", +1), "퇴실": ("18:00", -1)}
CONFIRMED_FROM = "2026-10-01"
HORIZONS = [5, 15, 30, 60]


def split_of(idx):
    s = pd.Series("", index=idx)
    for k, (a, b) in SPLIT.items():
        s[(idx >= pd.Timestamp(a, tz="Asia/Seoul")) & (idx < pd.Timestamp(b, tz="Asia/Seoul"))] = k
    return s


def missing_table(g):
    rows = []
    sp = split_of(g.index)
    for col in SERIES:
        space, item = col.split("_")
        # 회의실은 10분 주기라 10분 칸 기준으로 센다
        s = g[col].resample("10min").count() > 0 if space == "회의실" else g[col].notna()
        ssp = split_of(s.index) if space == "회의실" else sp
        r = {"공간": space, "항목": item, "칸": "10분" if space == "회의실" else "5분"}
        for k in ["전체", "train", "val", "test"]:
            m = s if k == "전체" else s[ssp == k]
            r[f"결측률_{k}"] = round(100 * (1 - m.mean()), 2)
        # 1시간 넘는 결측 구간
        miss = ~s
        runs = (miss != miss.shift()).cumsum()
        L = miss.groupby(runs).agg(["first", "size"])
        step = 10 if space == "회의실" else 5
        L = L[L["first"]]
        r["짧은결측_구간수(≤20분)"] = int((L["size"] * step <= 20).sum())
        r["긴결측_구간수(>1시간)"] = int((L["size"] * step > 60).sum())
        r["긴결측_시간합(h)"] = round(float(L.loc[L["size"] * step > 60, "size"].sum() * step / 60), 1)
        rows.append(r)
    return pd.DataFrame(rows)


def profile_plot(g, weekday, path):
    d = g[SERIES].copy()
    d = d[(d.index.dayofweek < 5) == weekday]
    # 10분 단위 하루 시각으로 묶는다. 회의실 센서는 10분 주기이고 전송 시각이 날마다 조금씩 밀려서,
    # 5분 칸으로 묶으면 칸마다 다른 날들이 섞여 톱니 모양이 생긴다.
    tod = d.index.hour * 60 + d.index.minute // 10 * 10
    prof = d.groupby(tod).mean()
    q = d.groupby(tod).quantile([0.25, 0.75]).unstack()
    fig, ax = plt.subplots(1, 2, figsize=(11, 3.6))
    for i, item in enumerate(["co2", "temperature"]):
        for space in ["실습실", "회의실"]:
            col = f"{space}_{item}"
            p = prof[col].dropna()
            ax[i].plot(p.index / 60, p.values, color=C[space], lw=2, label=space)
            lo, hi = q[(col, 0.25)].dropna(), q[(col, 0.75)].dropna()
            ax[i].fill_between(lo.index / 60, lo.values, hi.reindex(lo.index).values, color=C[space], alpha=0.12, lw=0)
        if weekday:
            for j, (name, (t, _)) in enumerate(EVENTS.items()):
                h = int(t[:2]) + int(t[3:]) / 60
                ax[i].axvline(h, color="#8a8984", lw=0.8, ls="--")
                if i == 0:
                    ax[i].annotate(name, (h, 1), xycoords=("data", "axes fraction"), xytext=(3, -4 - 11 * (j % 2)),
                                   textcoords="offset points", fontsize=8, color="#52514e", va="top")
        ax[i].set_xlim(0, 24); ax[i].set_xticks(range(0, 25, 3))
        ax[i].set_xlabel("시각"); ax[i].set_ylabel(UNIT[item])
        ax[i].set_title(("CO₂" if item == "co2" else "온도") + (" — 평일" if weekday else " — 주말"), loc="left", fontsize=11)
    ax[0].legend(frameon=False, loc="upper left")
    fig.text(0.01, 0.01, "선: 평균, 띠: 25~75% 범위. 기간 2025-09-03 ~ 2026-10-22", fontsize=8, color="#8a8984")
    fig.tight_layout(rect=(0, 0.04, 1, 1)); fig.savefig(path, dpi=150); plt.close(fig)
    return prof


def response(s):
    """일정 시각 E 전후로 실습실 CO2가 언제 움직이기 시작하는지 날마다 잰다 (1분 격자).
    반응 시작 = E-90분 ~ E+60분에서, 5분 중앙값으로 다듬은 CO2가 그 시각부터 10분 동안 30 ppm,
               20분 동안 60 ppm 넘게 예상 방향으로 변하는 첫 시각 (계속 이어지는 변화만 잡는다.
               빈 방의 10분 변화는 다듬은 뒤 90%가 ±11 ppm 안쪽이라 우연히 걸리지 않는다).
    반응 지연 = 반응 시작 - E (분). 음수면 일정보다 먼저 움직였다는 뜻.
    느린 반응 = 위 기준에는 안 걸리지만 E 후 60분 안에 E 시각 값보다 50 ppm 넘게 변한 경우. 50 ppm 도달 시각을 반응 시작으로 둔다.
    어긋난 날 = 평일인데 그 창 안에서 반응이 없거나(반응 없음),
               반응 지연이 같은 기간 그 일정의 중앙값과 30분 넘게 다른 날(일찍/늦게).
    """
    rows = []
    for day in pd.unique(s.index.normalize()):
        if day.dayofweek >= 5:
            continue
        for name, (t, sign) in EVENTS.items():
            E = day + pd.Timedelta(hours=int(t[:2]), minutes=int(t[3:]))
            w = s[E - pd.Timedelta("90min"):E + pd.Timedelta("85min")]
            if w.count() < 150 or w.index[-1] < E + pd.Timedelta("80min"):
                continue
            sm = w.rolling(5, center=True, min_periods=3).median()
            d10, d20 = sign * (sm.shift(-10) - sm), sign * (sm.shift(-20) - sm)
            ok = ((d10 > 30) & (d20 > 60))[:E + pd.Timedelta("60min")]
            hit = ok[ok]
            onset = hit.index[0] if len(hit) else None
            weak = False
            if onset is None and pd.notna(s[E]):
                # 빠른 변화는 없지만 60분 안에 50 ppm 넘게 서서히 변한 경우: 50 ppm 도달 시각을 반응으로 본다
                ch = sign * (sm[E:E + pd.Timedelta("60min")] - s[E])
                if (ch > 50).any():
                    onset, weak = ch[ch > 50].index[0], True
            rows.append({"date": day.strftime("%Y-%m-%d"), "요일": "월화수목금"[day.dayofweek], "일정": name,
                         "반응시작": onset.strftime("%H:%M") if onset is not None else "", "느린반응": weak,
                         "반응지연_분": (onset - E).total_seconds() / 60 if onset is not None else None,
                         "E시각값": round(float(s[E])) if pd.notna(s[E]) else None,
                         "E후60분_변화": round(float(sign * (s[E:E + pd.Timedelta("60min")].agg("max" if sign > 0 else "min") - s[E])))
                         if pd.notna(s[E]) else None})
    r = pd.DataFrame(rows)
    r.insert(0, "기간", np.where(r["date"] >= CONFIRMED_FROM, "확인된 일정(10/1~)", "가정(~9/30)"))
    med = r.groupby(["기간", "일정"])["반응지연_분"].transform("median")
    off = r["반응지연_분"] - med
    r["어긋남"] = np.select([r["반응지연_분"].isna(), off < -30, off > 30], ["반응 없음", "일찍", "늦게"], "")
    return r


def baselines(g):
    rows, idx_rows = [], []
    sp = split_of(g.index)
    for col in SERIES:
        space = col.split("_")[0]
        y = g[col]                                    # 실측(5분 평균). 보간 없음
        cur = y.ffill(limit=HOLD[space])              # 지금 알고 있는 마지막 값 (짧은 결측만 유지)
        need = 6 if space == "실습실" else 3          # 최근 1시간 실측 칸 최소 개수
        m1h = y.rolling(12, min_periods=need).mean()  # (t-60분, t] 실측 평균
        for h in HORIZONS:
            k = h // 5
            tgt = y.shift(-k)                         # t+h 의 실측
            preds = {"현재값 유지": cur, "최근 1시간 평균": m1h, "어제 같은 시각": cur.shift(288 - k)}
            ok = tgt.notna()
            for p in preds.values():
                ok &= p.notna()
            # 참고용(세 기준선 비교 대상 아님): 지난주 같은 요일 같은 시각
            ref = cur.shift(288 * 7 - k)
            # 시점 t 와 목표 t+h 가 같은 분할 안에 있어야 한다
            same = sp == sp.shift(-k)
            for part in ["val", "test"]:
                m = ok & same & (sp == part)
                r = {"공간": space, "항목": col.split("_")[1], "분할": part, "h_분": h, "평가칸수": int(m.sum())}
                for name, p in preds.items():
                    r[name] = float((p[m] - tgt[m]).abs().mean())
                mr = m & ref.notna()
                r["(참고) 지난주 같은 시각"] = float((ref[mr] - tgt[mr]).abs().mean())
                # 시간대별: 평일 08~19시(사람이 드나드는 시간) / 그 밖
                busy = pd.Series((g.index.dayofweek < 5) & (g.index.hour >= 8) & (g.index.hour < 19), index=g.index)
                for seg, sm in [("평일08-19시", m & busy), ("그밖", m & ~busy)]:
                    for name, p in preds.items():
                        r[f"{name}|{seg}"] = float((p[sm] - tgt[sm]).abs().mean())
                rows.append(r)
                if part == "test":
                    idx_rows.append(pd.DataFrame({"series": col, "h": h, "time": g.index[m]}))
    return pd.DataFrame(rows), pd.concat(idx_rows, ignore_index=True)


def mae_plot(t, path):
    fig, ax = plt.subplots(1, 4, figsize=(13, 3.4))
    names = ["현재값 유지", "최근 1시간 평균", "어제 같은 시각"]
    cols = ["#2a78d6", "#eb6834", "#1baf7a"]
    for i, col in enumerate(SERIES):
        space, item = col.split("_")
        d = t[(t["공간"] == space) & (t["항목"] == item) & (t["분할"] == "test")]
        for n, c in zip(names, cols):
            ax[i].plot(d["h_분"], d[n], marker="o", ms=5, lw=2, color=c, label=n)
        ax[i].set_xticks(HORIZONS); ax[i].set_xlabel("몇 분 뒤")
        ax[i].set_title(f"{space} {'CO₂' if item == 'co2' else '온도'} MAE ({UNIT[item]})", loc="left", fontsize=10)
        ax[i].set_ylim(bottom=0)
    ax[0].legend(frameon=False, fontsize=8)
    fig.tight_layout(); fig.savefig(path, dpi=150); plt.close(fig)


def main():
    g = pd.read_parquet(os.path.join(OUT, "grid5.parquet"))
    sp = split_of(g.index)
    json.dump({k: {"start": a, "end_exclusive": b, "steps_5min": int((sp == k).sum())} for k, (a, b) in SPLIT.items()},
              open(os.path.join(OUT, "split.json"), "w"), ensure_ascii=False, indent=1)

    # 정규화 값: 학습 기간 실측만으로 계산
    norm = {c: {"mean": float(g.loc[sp == "train", c].mean()), "std": float(g.loc[sp == "train", c].std())} for c in SERIES}
    allp = {c: {"mean": float(g[c].mean()), "std": float(g[c].std())} for c in SERIES}
    json.dump({"computed_on": "train", "split": SPLIT["train"], "stats": norm, "for_reference_all_period": allp},
              open(os.path.join(OUT, "norm_stats.json"), "w"), ensure_ascii=False, indent=1)

    miss = missing_table(g); miss.to_csv(os.path.join(OUT, "missing.csv"), index=False)
    print(miss.to_string())
    profile_plot(g, True, os.path.join(OUT, "profile_weekday.png"))
    profile_plot(g, False, os.path.join(OUT, "profile_weekend.png"))

    s1 = pd.read_parquet(os.path.join(OUT, "grid1_lab_co2.parquet"))["실습실_co2"]
    r = response(s1)
    r.to_csv(os.path.join(OUT, "response_days.csv"), index=False)
    summ = r.groupby(["기간", "일정"]).agg(평일수=("date", "size"), 반응한날=("반응지연_분", "count"),
                                반응지연_중앙값=("반응지연_분", "median"),
                                반응지연_p25=("반응지연_분", lambda x: x.quantile(.25)),
                                반응지연_p75=("반응지연_분", lambda x: x.quantile(.75)),
                                E후60분_변화_중앙값=("E후60분_변화", "median"),
                                느린반응=("느린반응", "sum"),
                                어긋난날=("어긋남", lambda x: (x != "").sum()))
    summ.to_csv(os.path.join(OUT, "response.csv")); print(summ)

    t, ev = baselines(g)
    t.to_csv(os.path.join(OUT, "baseline_mae.csv"), index=False)
    ev.to_parquet(os.path.join(OUT, "eval_index.parquet"))
    print(t.round(2).to_string())
    mae_plot(t, os.path.join(OUT, "baseline_mae.png"))


if __name__ == "__main__":
    main()
