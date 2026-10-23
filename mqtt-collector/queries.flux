// InfluxDB UI > Data Explorer > Script Editor 에 붙여 넣어 쓰는 쿼리 모음.
// "sensors" 는 실제 bucket 이름으로 바꾼다. 대시보드(Dashboards > Create)에 셀로 넣어 두면 한눈에 볼 수 있다.

// ── 1. 공간·항목별 건수 (최근 24시간) ─────────────────────────
from(bucket: "sensors")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "sensor" and r._field == "value")
  |> group(columns: ["space", "item"])
  |> count()
  |> group()
  |> sort(columns: ["space", "item"])

// ── 2. 공간·항목별 마지막 수신 시각, 지금부터 몇 초 전인지 ─────
from(bucket: "sensors")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "sensor" and r._field == "recv_ts_ms")
  |> group(columns: ["space", "item"])
  |> max()
  |> map(fn: (r) => ({space: r.space, item: r.item,
       last_recv: time(v: r._value * 1000000),
       age_s: (int(v: now()) - r._value * 1000000) / 1000000000}))
  |> group()
  |> sort(columns: ["age_s"], desc: true)

// ── 3. 공간·항목별 커버율 (최근 24시간, 1분 단위) ──────────────
// 빈 1분은 createEmpty 로 만들어 0 으로 세고, 데이터가 있는 분의 비율을 낸다.
import "date"
stop = date.truncate(t: now(), unit: 1m)
start = date.sub(d: 24h, from: stop)
from(bucket: "sensors")
  |> range(start: start, stop: stop)
  |> filter(fn: (r) => r._measurement == "sensor" and r._field == "value")
  |> group(columns: ["space", "item"])
  |> aggregateWindow(every: 1m, fn: count, createEmpty: true)
  |> map(fn: (r) => ({r with _value: if exists r._value and r._value > 0 then 1.0 else 0.0}))
  |> mean()
  |> map(fn: (r) => ({space: r.space, item: r.item, coverage_pct: r._value * 100.0}))
  |> group()
  |> sort(columns: ["coverage_pct"])

// ── 4. 커버율 시계열 (1시간마다 그 시간의 커버율, 그래프용) ───
from(bucket: "sensors")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "sensor" and r._field == "value")
  |> group(columns: ["space", "item"])
  |> aggregateWindow(every: 1m, fn: count, createEmpty: true)
  |> map(fn: (r) => ({r with _value: if exists r._value and r._value > 0 then 1.0 else 0.0}))
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: true)

// ── 5. 빈 1분 목록 (결측 위치 확인) ──────────────────────────
from(bucket: "sensors")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "sensor" and r._field == "value")
  |> group(columns: ["space", "item"])
  |> aggregateWindow(every: 1m, fn: count, createEmpty: true)
  |> filter(fn: (r) => not exists r._value or r._value == 0)

// ── 6. 수집기 상태 (연결, 초당 메시지, 스풀, 쓰기 실패, 중복) ──
from(bucket: "sensors")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "collector_health")
  |> filter(fn: (r) => r._field == "mqtt_connected" or r._field == "msgs" or r._field == "spool"
                    or r._field == "write_errors" or r._field == "duplicates" or r._field == "parse_errors")

// ── 7. 전송 지연 (센서 시각 → 수신 시각), 몰려서 도착한 구간 찾기 ─
from(bucket: "sensors")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "sensor" and r._field == "delay_ms")
  |> group(columns: ["space", "item"])
  |> aggregateWindow(every: 5m, fn: max, createEmpty: false)
