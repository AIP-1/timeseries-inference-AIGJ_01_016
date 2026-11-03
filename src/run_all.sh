#!/bin/bash
# 과제 3 실험 전체. 순서대로 돌린다. 로그는 out/forecast/logs/
cd "$(dirname "$0")/.."
mkdir -p out/forecast/logs
run() { name=$1; shift; [ -f out/forecast/meta_$name.json ] && { echo "skip $name"; return; }
        python3 src/forecast.py --name $name "$@" > out/forecast/logs/$name.log 2>&1 && echo "done $name" || echo "FAIL $name"; }
# 기본: 3종 × 일정 포함/제외, 과거 2시간
for m in gru lstm tcn; do
  run ${m}_L24_s1 --model $m --L 24 --sched 1
  run ${m}_L24_s0 --model $m --L 24 --sched 0
done
# 심화: 과거 길이 1시간·4시간
for m in gru lstm tcn; do
  run ${m}_L12_s1 --model $m --L 12 --sched 1
  run ${m}_L48_s1 --model $m --L 48 --sched 1
done
# 변화량 대신 값 자체를 예측
run gru_L24_s1_abs --model gru --L 24 --sched 1 --delta 0
# 일반화: 실습실만으로 학습 -> 회의실(처음 보는 공간)에서 평가
run gru_L24_s1_labonly --model gru --L 24 --sched 1 --train_spaces 실습실
# 반복 편차 확인: 시드만 바꿔 일정 포함/제외 한 번 더
run gru_L24_s1_seed1 --model gru --L 24 --sched 1 --seed 1
run gru_L24_s0_seed1 --model gru --L 24 --sched 0 --seed 1
run gru_L24_s1_seed2 --model gru --L 24 --sched 1 --seed 2
run gru_L24_s0_seed2 --model gru --L 24 --sched 0 --seed 2
echo ALLDONE
