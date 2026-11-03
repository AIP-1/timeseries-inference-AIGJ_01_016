#!/usr/bin/env python3
"""학습 모델 예측을 과제 2 기준선과 같은 시점·조건으로 채점한다.

시험: out/eval_index.parquet 의 (시리즈, h, 시점) 그대로.
검증: 과제 2 와 같은 규칙(세 기준선이 모두 값을 내고, t+h 실측이 있고, t·t+h 가 같은 구간)으로 다시 만든다.
사용: python evaluate.py RUN_NAME [RUN_NAME ...]  -> out/forecast/score_<run>.csv
"""
import os
import sys

import numpy as np
import pandas as pd

D = os.path.join(os.path.dirname(__file__), "..", "out")
F = os.path.join(D, "forecast")
SERIES = ["실습실_co2", "실습실_temperature", "회의실_co2", "회의실_temperature"]
SPLIT = {"val": ("2026-07-01", "2026-09-01"), "test": ("2026-09-01", "2026-10-23")}
HOLD = {"실습실": 3, "회의실": 4}


def points(g):
    """과제 2 baselines() 와 같은 규칙으로 (series, h, split, time, y, persist) 표를 만든다."""
    rows = []
    sp = pd.Series("", index=g.index)
    for k, (a, b) in SPLIT.items():
        sp[(g.index >= pd.Timestamp(a, tz="Asia/Seoul")) & (g.index < pd.Timestamp(b, tz="Asia/Seoul"))] = k
    sp[g.index < pd.Timestamp("2026-07-01", tz="Asia/Seoul")] = "train"
    for col in SERIES:
        space = col.split("_")[0]
        y = g[col]; cur = y.ffill(limit=HOLD[space])
        m1h = y.rolling(12, min_periods=6 if space == "실습실" else 3).mean()
        for h in [5, 15, 30, 60]:
            k = h // 5
            tgt = y.shift(-k)
            ok = tgt.notna() & cur.notna() & m1h.notna() & cur.shift(288 - k).notna() & (sp == sp.shift(-k))
            for part in ["val", "test"]:
                m = ok & (sp == part)
                rows.append(pd.DataFrame({"series": col, "h": h, "split": part, "time": g.index[m],
                                          "y": tgt[m].values, "persist": cur[m].values}))
    return pd.concat(rows, ignore_index=True)


def busy(t):
    return (t.dt.dayofweek < 5) & (t.dt.hour >= 8) & (t.dt.hour < 19)


def score(pts, run):
    pr = pd.read_parquet(os.path.join(F, f"pred_{run}.parquet"))
    long = []
    for item in ["co2", "temperature"]:
        d = pr[["time", "h", "space", item]].rename(columns={item: "pred"})
        d["series"] = d["space"] + "_" + item
        long.append(d.drop(columns="space"))
    pr = pd.concat(long)
    m = pts.merge(pr, on=["series", "h", "time"], how="left")
    miss = m["pred"].isna()
    if miss.any():   # 모델이 입력을 못 만든 시점(드묾)은 현재값 유지로 채우고 개수를 남긴다
        m.loc[miss, "pred"] = m.loc[miss, "persist"]
    m["ae"] = (m["pred"] - m["y"]).abs(); m["ae_p"] = (m["persist"] - m["y"]).abs()
    m["busy"] = busy(m["time"])
    out = m.groupby(["split", "series", "h"]).agg(n=("ae", "size"), filled=("pred", lambda x: int(miss[x.index].sum())),
                                                  model=("ae", "mean"), persist=("ae_p", "mean")).reset_index()
    b = m[m.busy].groupby(["split", "series", "h"]).agg(model_busy=("ae", "mean"), persist_busy=("ae_p", "mean")).reset_index()
    nb = m[~m.busy].groupby(["split", "series", "h"]).agg(model_rest=("ae", "mean"), persist_rest=("ae_p", "mean")).reset_index()
    out = out.merge(b).merge(nb)
    out["gain_%"] = 100 * (1 - out["model"] / out["persist"])
    out.insert(0, "run", run)
    out.to_csv(os.path.join(F, f"score_{run}.csv"), index=False)
    m[m.split == "test"].to_parquet(os.path.join(F, f"errors_{run}.parquet"))
    return out


if __name__ == "__main__":
    g = pd.read_parquet(os.path.join(D, "grid5.parquet"))
    pts = points(g)
    ev = pd.read_parquet(os.path.join(D, "eval_index.parquet"))
    t = pts[pts.split == "test"]
    assert len(t) == len(ev), (len(t), len(ev))   # 과제 2 시험 시점과 정확히 같은지
    if len(sys.argv) > 1 and sys.argv[1].startswith("--dir="):
        F = sys.argv.pop(1)[6:]
    for run in sys.argv[1:]:
        s = score(pts, run)
        print(s[["split", "series", "h", "n", "filled", "model", "persist", "gain_%"]].round(3).to_string(index=False))
