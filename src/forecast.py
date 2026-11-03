#!/usr/bin/env python3
"""과제 3: 60분 예측 (5분 간격 12칸, CO2·온도).

입력: out/grid5.parquet, out/eval_index.parquet (과제 2)
출력: out/forecast/ 아래 run별 예측·MAE, 모델 가중치

설계
  - 샘플 = (공간, 시점 t). 실습실·회의실을 한 모델로 학습하고 공간은 원-핫으로 넣는다.
  - 과거 입력(L칸): CO2·온도·습도(학습 기간 평균·표준편차로 정규화, 짧은 결측만 마지막 값 유지),
    결측 표시, 시간대(sin/cos)·요일(sin/cos)·주말, [일정: 공휴일, 수업 중, 점심].
  - 미래 입력(t+5 ~ t+60분, 12칸): 미리 아는 정보만. 시간대·요일·주말, [일정].
  - 출력: 12칸 × (CO2, 온도)의 '지금 값에서의 변화량'을 한 번에 낸다(직접 다중 출력).
  - 손실: 정규화 단위 MAE, 정답이 실측인 칸만.
  - 학습은 학습 기간, 에폭·설정 선택은 검증 기간 MAE로만. 시험 기간은 마지막에 한 번 예측만.
  - 평가: 과제 2 eval_index 의 같은 (시리즈, h, 시점)에서 원래 단위 MAE.

일정 정보 (사용 가능한 것만)
  - 실습실: 평일(공휴일 제외) 수업 09:00~11:30, 13:00~18:00, 점심 11:30~13:00.
    2026-10 부터 확인된 시간표이고, 과제 2에서 그 이전에도 같은 시각에 반응하는 것을 확인해 전 기간에 적용.
  - 회의실: 예약 자료가 없어 수업·점심 표시는 0. 공휴일 표시만 공통.
  - 공휴일은 공식 달력(미리 알 수 있음). 데이터로 찾은 휴강일은 쓰지 않는다(정답 누수).
"""
import argparse
import json
import os
import time

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

D = os.path.join(os.path.dirname(__file__), "..", "out")
SPACES = ["실습실", "회의실"]
SPLIT = {"train": ("2025-09-03 16:00", "2026-07-01"), "val": ("2026-07-01", "2026-09-01"), "test": ("2026-09-01", "2026-10-23")}
HOLD = {"실습실": 3, "회의실": 4}
H = 12
HOLIDAYS = [
    "2025-10-03", "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08", "2025-10-09", "2025-12-25",
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-02", "2026-05-01", "2026-05-05",
    "2026-05-25", "2026-06-03", "2026-07-17", "2026-08-17", "2026-09-24", "2026-09-25", "2026-09-26",
    "2026-10-05", "2026-10-09",
]


def split_of(idx):
    s = np.full(len(idx), "", dtype=object)
    for k, (a, b) in SPLIT.items():
        s[(idx >= pd.Timestamp(a, tz="Asia/Seoul")) & (idx < pd.Timestamp(b, tz="Asia/Seoul"))] = k
    return s


def calendar(idx, space, sched):
    """시각만으로 미리 알 수 있는 특징. sched=False 면 일정(공휴일·수업·점심) 제외."""
    mins = idx.hour * 60 + idx.minute
    dow = idx.dayofweek
    f = [np.sin(2 * np.pi * mins / 1440), np.cos(2 * np.pi * mins / 1440),
         np.sin(2 * np.pi * dow / 7), np.cos(2 * np.pi * dow / 7), (dow >= 5).astype(float)]
    if sched:
        hol = idx.normalize().tz_localize(None).isin(pd.to_datetime(HOLIDAYS)).astype(float)
        work = (dow < 5) & (hol == 0)
        lab = space == "실습실"
        cls = work & (((mins >= 540) & (mins < 690)) | ((mins >= 780) & (mins < 1080))) & lab
        lunch = work & (mins >= 690) & (mins < 780) & lab
        f += [hol, cls.astype(float), lunch.astype(float)]
    return np.stack(f, 1).astype(np.float32)


def build(g, norm, L, sched):
    """공간별 배열을 만든다. 반환: dict(space -> 배열들)."""
    out = {}
    sp = split_of(g.index)
    for s_i, space in enumerate(SPACES):
        cols = [f"{space}_co2", f"{space}_temperature", f"{space}_humidity"]
        raw = g[cols].to_numpy(np.float64)                         # 실측(정답용, 보간 없음)
        cur = g[cols].ffill(limit=HOLD[space]).to_numpy(np.float64)  # t 에 알고 있는 값
        z = np.stack([(cur[:, j] - norm[c]["mean"]) / norm[c]["std"] for j, c in enumerate(cols)], 1)
        miss = np.isnan(z).astype(np.float32)
        z = np.nan_to_num(z).astype(np.float32)
        cal = calendar(g.index, space, sched)
        onehot = np.zeros((len(g), len(SPACES)), np.float32); onehot[:, s_i] = 1
        past = np.concatenate([z, miss, cal, onehot], 1)            # [T, Fp]
        out[space] = dict(past=past, cal=cal, cur=cur[:, :2], raw=raw[:, :2], split=sp,
                          std=np.array([norm[cols[0]]["std"], norm[cols[1]]["std"]], np.float32),
                          mean=np.array([norm[cols[0]]["mean"], norm[cols[1]]["mean"]], np.float32))
    return out


def samples(arr, L, part, need_target=True):
    """part 구간에서 쓸 시점 t 의 인덱스. t 에 현재 CO2·온도를 알고 있어야 하고, 과거 L칸이 격자 안에 있어야 한다."""
    T = len(arr["split"])
    t = np.arange(L - 1, T - H)   # 격자를 H칸 늘려 두었으므로 실제 마지막 시점까지 포함
    ok = (arr["split"][t] == part) & ~np.isnan(arr["cur"][t]).any(1)
    if need_target:
        # 미래 12칸 중 같은 구간에서 정답이 하나라도 있어야
        fut = np.stack([arr["raw"][t + k, 0] for k in range(1, H + 1)], 1)
        ok &= ~np.isnan(fut).all(1)
    return t[ok]


def gather(arr, t, L, delta=True):
    """시점 배열 t -> 입력·정답 텐서."""
    win = t[:, None] + np.arange(-L + 1, 1)[None]
    past = arr["past"][win]                                        # [N, L, Fp]
    fut = arr["cal"][t[:, None] + np.arange(1, H + 1)[None]]       # [N, H, Fc]
    cur = arr["cur"][t]                                            # [N, 2]
    tgt = np.stack([arr["raw"][t + k] for k in range(1, H + 1)], 1)  # [N, H, 2]
    same = arr["split"][t + np.arange(1, H + 1)[:, None]].T == arr["split"][t][:, None]  # [N, H]
    tgt[~same] = np.nan
    base = cur[:, None] if delta else arr["mean"]                  # 변화량 또는 값 자체
    dz = (tgt - base) / arr["std"]
    return past, fut, (cur if delta else np.broadcast_to(arr["mean"], cur.shape)), dz


class RNN(nn.Module):
    def __init__(self, fp, fc, kind, hid=64):
        super().__init__()
        cell = nn.LSTM if kind == "lstm" else nn.GRU
        self.rnn = cell(fp, hid, num_layers=2, batch_first=True, dropout=0.1)
        self.head = Head(hid, fc)

    def forward(self, past, fut):
        o, _ = self.rnn(past)
        return self.head(o[:, -1], fut)


class TCN(nn.Module):
    def __init__(self, fp, fc, hid=64, dil=(1, 2, 4, 8, 16)):
        super().__init__()
        self.inp = nn.Conv1d(fp, hid, 1)
        self.blocks = nn.ModuleList()
        for d in dil:
            self.blocks.append(nn.Sequential(nn.ConstantPad1d((2 * d, 0), 0.), nn.Conv1d(hid, hid, 3, dilation=d), nn.ReLU(),
                                             nn.Dropout(0.1)))
        self.head = Head(hid, fc)

    def forward(self, past, fut):
        x = self.inp(past.transpose(1, 2))
        for b in self.blocks:
            x = x + b(x)
        return self.head(x[:, :, -1], fut)


class Head(nn.Module):
    """과거 요약 + 미래 12칸의 달력·일정 -> 12칸 × 2 변화량."""
    def __init__(self, hid, fc):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(hid + H * fc, 128), nn.ReLU(), nn.Linear(128, H * 2))

    def forward(self, h, fut):
        return self.net(torch.cat([h, fut.flatten(1)], 1)).view(-1, H, 2)


def masked_l1(p, y):
    m = ~torch.isnan(y)
    return (p - torch.nan_to_num(y)).abs()[m].mean()


def to_t(*a):
    return [torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32)) for x in a]


def predict(model, past, fut, bs=4096):
    model.eval(); out = []
    with torch.no_grad():
        for i in range(0, len(past), bs):
            out.append(model(past[i:i + bs], fut[i:i + bs]))
    return torch.cat(out).numpy()


def run(args):
    torch.manual_seed(args.seed); np.random.seed(args.seed)
    torch.set_num_threads(os.cpu_count())
    g = pd.read_parquet(os.path.join(D, "grid5.parquet"))
    # 마지막 시점에서도 12칸 뒤 달력을 만들 수 있게 격자 끝을 12칸 늘린다(값은 결측)
    g = g.reindex(pd.date_range(g.index[0], g.index[-1] + pd.Timedelta(minutes=5 * H), freq="5min", name="time"))
    sp = split_of(g.index)
    norm = json.load(open(os.path.join(D, "norm_stats.json")))["stats"]
    for space in SPACES:   # 습도 정규화 값도 학습 기간으로만
        c = f"{space}_humidity"
        v = g.loc[sp == "train", c]
        norm[c] = {"mean": float(v.mean()), "std": float(v.std())}
    A = build(g, norm, args.L, args.sched)

    data = {}
    for part in ["train", "val"]:
        P, F, C, Y, S = [], [], [], [], []
        for space in SPACES:
            t = samples(A[space], args.L, part)
            p, f, c, y = gather(A[space], t, args.L, args.delta)
            P.append(p); F.append(f); C.append(c); Y.append(y); S.append(np.full(len(t), space))
        data[part] = to_t(np.concatenate(P), np.concatenate(F), np.concatenate(Y)) + [np.concatenate(C), np.concatenate(S)]
        if args.train_spaces:
            keep = np.isin(data[part][4], args.train_spaces)
            data[part] = [x[keep] if isinstance(x, np.ndarray) else x[torch.from_numpy(keep)] for x in data[part]]
    fp, fc = data["train"][0].shape[2], data["train"][1].shape[2]
    model = TCN(fp, fc) if args.model == "tcn" else RNN(fp, fc, args.model)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    Ptr, Ftr, Ytr = data["train"][:3]
    Pva, Fva, Yva = data["val"][:3]
    best, best_ep, bad, hist = 1e9, -1, 0, []
    n = len(Ptr); t0 = time.time()
    for ep in range(args.epochs):
        model.train(); perm = torch.randperm(n); tl = 0
        for i in range(0, n, args.bs):
            b = perm[i:i + args.bs]
            loss = masked_l1(model(Ptr[b], Ftr[b]), Ytr[b])
            opt.zero_grad(); loss.backward(); nn.utils.clip_grad_norm_(model.parameters(), 1.0); opt.step()
            tl += loss.item() * len(b)
        vl = masked_l1(torch.from_numpy(predict(model, Pva, Fva)), Yva).item()
        hist.append((ep, tl / n, vl))
        print(f"ep{ep} train {tl / n:.4f} val {vl:.4f} {time.time() - t0:.0f}s", flush=True)
        if vl < best - 1e-4:
            best, best_ep, bad = vl, ep, 0
            state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= args.patience:
                break
    model.load_state_dict(state)
    train_sec = time.time() - t0

    # 평가: 검증(선택용)과 시험(보고용) 모두 과제 2 와 같은 규칙의 시점에서
    ev = pd.read_parquet(os.path.join(D, "eval_index.parquet"))
    rows, preds = [], []
    for space in SPACES:
        arr = A[space]
        for part in ["val", "test"]:
            t = samples(arr, args.L, part, need_target=False)
            p, f, c, y = gather(arr, t, args.L, args.delta)
            dz = predict(model, *to_t(p, f))
            yhat = c[:, None] + dz * arr["std"]                       # [N, H, 2] 원래 단위
            df = pd.DataFrame({"time": np.repeat(g.index[t], H), "h": np.tile(np.arange(1, H + 1) * 5, len(t)),
                               "co2": yhat[:, :, 0].ravel(), "temperature": yhat[:, :, 1].ravel()})
            df["space"] = space; df["split"] = part
            preds.append(df)
    pr = pd.concat(preds, ignore_index=True)
    os.makedirs(args.out, exist_ok=True)
    name = args.name
    pr.to_parquet(os.path.join(args.out, f"pred_{name}.parquet"))
    torch.save(model.state_dict(), os.path.join(args.out, f"model_{name}.pt"))
    json.dump({"args": vars(args), "best_epoch": best_ep, "best_val_loss": best, "train_sec": train_sec,
               "n_train": int(n), "n_params": sum(p.numel() for p in model.parameters()), "hist": hist},
              open(os.path.join(args.out, f"meta_{name}.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps({"name": name, "best_epoch": best_ep, "val": best, "sec": round(train_sec)}, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gru", choices=["gru", "lstm", "tcn"])
    ap.add_argument("--L", type=int, default=24, help="과거 칸 수 (5분 단위). 12=1시간, 24=2시간, 48=4시간")
    ap.add_argument("--sched", type=int, default=1)
    ap.add_argument("--delta", type=int, default=1)
    ap.add_argument("--epochs", type=int, default=25)
    ap.add_argument("--patience", type=int, default=5)
    ap.add_argument("--bs", type=int, default=512)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--train_spaces", nargs="*", default=None)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", default=os.path.join(D, "forecast"))
    run(ap.parse_args())
