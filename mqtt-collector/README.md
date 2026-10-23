# 과제 1. MQTT 센서 데이터 수집 체계: 실행 가이드

젯슨 나노에서 MQTT 메시지를 받아 InfluxDB에 공간·항목·시각별로 저장하는 수집기를 상시 가동하기까지의 순서입니다.
이 폴더의 코드를 그대로 젯슨에 복사해서 쓰면 됩니다.

| 파일 | 역할 |
|---|---|
| `collector.py` | 수집기 본체 (MQTT 구독 → 변환 → InfluxDB 쓰기, 자동 재접속, 장애 시 SSD 스풀) |
| `config.example.yaml` | 토픽 → 공간/항목/시각 매핑 설정. **센서 추가·형식 변경은 이 파일만 고친다** |
| `env.example` | 접속 정보 자리표시자 (실제 값은 젯슨의 `/etc/mqtt-collector/env`에만 둔다) |
| `mqtt-collector.service` | systemd 서비스 (부팅 시 자동 시작, 죽으면 5초 뒤 재시작) |
| `install.sh` | venv 생성, 패키지 설치, 서비스 등록 |
| `queries.flux` | InfluxDB 화면에서 쓰는 건수·마지막 수신·커버율·결측 쿼리 |
| `coverage_report.py` | 산출물용 수집 현황 보고서(커버율 표 + 결측 구간 + 추정 원인) 생성 |

## 0. 가정

- **InfluxDB는 원격 서버(받은 URL + token)라고 가정**했습니다. token을 받았다는 점에서 InfluxDB 2.x로 봅니다.
  만약 "젯슨에 InfluxDB를 직접 설치하라"는 뜻이었다면 URL만 `http://localhost:8086`으로 바꾸고, InfluxDB 데이터 폴더를 SSD에 두면 됩니다(10장).
- InfluxDB 3.x라면 쓰기는 그대로 되지만 Flux 쿼리 대신 SQL을 써야 합니다.
- 설치 폴더는 어디든 됩니다(`install.sh`가 있는 폴더 기준으로 서비스를 등록). InfluxDB가 원격이라 젯슨에는 코드·로그·장애 시 스풀만 쓰이므로 microSD의 홈 폴더(`~/mqtt-collector`)로 충분합니다. SSD는 ext4로 마운트한 뒤 나중에 학습 데이터·모델용으로 쓰면 됩니다(1장).
- 받은 정보 중 **InfluxDB org 이름과 bucket 이름**이 없으면 담당자에게 먼저 물어보세요. token만으로는 어디에 쓸지 정할 수 없습니다.

## 0-1. 이 교육장에 맞춘 포인트

- **저장될 시리즈는 12개**예요: 공간 4곳(강의실 2, 회의실 1, 라운지 1) × 항목 3개(co2, temperature, humidity). 1분 주기라 시리즈마다 하루 1,440건이 정상이고, 커버율 표도 12줄이 나와야 합니다. 12줄보다 적으면 그 공간·항목은 아예 안 들어오고 있는 거예요.
- **공간 이름은 처음에 고정**하세요(예: `classroom1`, `classroom2`, `meeting1`, `lounge1`). 이 이름이 과제 2~8과 일정 자료(시간표·예약)를 잇는 열쇠라서, 중간에 바뀌면 데이터가 두 갈래로 갈라집니다. 토픽 이름이 일정 자료의 공간 이름과 다르면 일정 쪽에 대응표를 하나 두세요.
- **1분 주기의 함정.** 센서가 08:59:59.8에 보내고 다음을 09:01:00.2에 보내면, 데이터는 하나도 안 빠졌는데 09:00 칸이 비어 커버율이 깎입니다. 그래서 `_time`을 센서 시각으로 두는 것이 중요하고, 보고서에는 2분 이상 연속으로 빈 구간만 결측으로 나열합니다. 이런 경계 흔들림은 결측 원인 설명에 "주기 흔들림"으로 적으면 됩니다.
- **0ppm 같은 깨진 값은 그대로 저장**합니다. 수집할 때 걸러 내면 과제 4(이상 탐지)가 찾아야 할 고장의 흔적이 사라집니다. 정제는 과제 2에서 읽을 때 합니다.
- **합성 데이터는 실데이터와 섞지 마세요.** 별도 bucket(예: `aip_synth`)에 넣으면 커버율 보고서와 학습 데이터가 오염되지 않습니다.
- **일정 자료는 수집기 대상이 아닙니다.** MQTT로 오지 않고 미래 값도 있으므로 CSV 파일로 두거나 별도 measurement(`schedule`)로 한 번에 넣어 두면 됩니다.
- **과제 8에서 같은 보드가 추론 서비스도 돌립니다.** 그래서 9장의 "같은 저장소 동시 사용" 문제가 이 과제에서는 실제 상황입니다. 추론은 최근 2~3시간만 읽고, 학습은 내보낸 파일로 하는 구성을 미리 정해 두면 좋습니다.

## 1. 젯슨 기본 준비 (한 번만)

**SSD 자동 마운트.** 재부팅 뒤에도 같은 경로에 붙어야 서비스가 뜹니다.

```bash
lsblk -f                                   # SSD 파티션(예: /dev/sda1)과 UUID 확인
sudo mkdir -p /mnt/ssd
echo 'UUID=<SSD-UUID>  /mnt/ssd  ext4  defaults,nofail,noatime  0  2' | sudo tee -a /etc/fstab
sudo mount -a && df -h /mnt/ssd
sudo chown -R $USER:$USER /mnt/ssd
```

**시간 동기화.** 수신 시각을 저장하므로 젯슨 시계가 맞아야 합니다. 나노는 RTC 배터리가 없으면 재부팅 직후 시계가 틀릴 수 있습니다.

```bash
timedatectl                                # "System clock synchronized: yes" 인지 확인
sudo timedatectl set-timezone Asia/Seoul
# 외부 NTP가 막혀 있으면 /etc/systemd/timesyncd.conf 의 NTP= 에 내부/VPN 쪽 NTP 서버를 넣는다
```

**접속 도구.**

```bash
sudo apt-get update && sudo apt-get install -y mosquitto-clients curl
```

## 2. 받은 정보로 연결부터 확인

수집기를 만들기 전에 "무엇이 오는지"를 먼저 기록해야 매핑을 정할 수 있습니다.

**MQTT 브로커.** MQTT Explorer에서 받은 host/port/ID/PW로 접속하고 `#`을 구독해서 토픽 트리를 봅니다. 터미널에서는:

```bash
mosquitto_sub -h <브로커> -p 1883 -u '<ID>' -P '<PW>' -t '#' -v -C 30     # 30개만 받고 종료
```

아래 표를 채워 두세요. 이 표가 곧 `config.yaml`의 근거이고 보고서에도 들어갑니다.

| 토픽 예 | payload 예 | 공간은 어디서? | 항목(키) | 센서 시각 키/단위 | 주기 |
|---|---|---|---|---|---|
| `site/room1/th01/env` | `{"ts":1729380000123,"temp":23.1,"hum":45.2}` | 토픽 2번째 | temp, hum | ts / ms | 10초 |
| … | … | … | … | … | … |

**InfluxDB.**

```bash
curl -s <INFLUX_URL>/health                                         # "status":"pass"
curl -s -H "Authorization: Token <TOKEN>" "<INFLUX_URL>/api/v2/buckets?org=<ORG>" | grep '"name"'
# 쓰기 권한 확인 (test 측정값 한 줄)
curl -s -w '%{http_code}\n' -XPOST "<INFLUX_URL>/api/v2/write?org=<ORG>&bucket=<BUCKET>&precision=s" \
  -H "Authorization: Token <TOKEN>" --data-binary "conn_test value=1 $(date +%s)"   # 204 면 성공
```

## 3. 설치

노트북에서 이 폴더를 젯슨으로 복사하고(VPN 연결 상태), 젯슨에서 설치 스크립트를 실행합니다.

```bash
scp -r mqtt-collector <user>@<젯슨-VPN-IP>:~/   # 또는 VS Code 탐색기로 끌어다 놓기
ssh <user>@<젯슨-VPN-IP>
cd ~/mqtt-collector && bash install.sh
sudo nano /etc/mqtt-collector/env           # 받은 ID/PW/URL/token/org/bucket 입력 (chmod 600 으로 보호됨)
```

`install.sh`는 python3가 3.8 이상이면 그대로 쓰고, 옛 JetPack 4.x(Python 3.6)이면 python3.8을 설치해서 가상환경을 만듭니다.

## 4. 매핑 설정 (`config.yaml`)

2장에서 채운 표대로 `config.yaml`의 `sensors:`를 고칩니다. 규칙 하나가 "이 토픽 패턴의 메시지를 어떻게 읽을지"입니다.

```yaml
- topic: "site/+/+/env"            # MQTT 와일드카드
  format: json                     # json | plain(숫자만) | csv | kv(a=1;b=2)
  tags:
    space: "{t1}"                  # 토픽 1번째 조각(0부터) → 공간
    device: "{t2}"                 # 또는 "{p:device.id}" 처럼 payload 에서
  time: {path: ts, unit: ms}       # 센서 시각. s|ms|us|ns|iso|auto
  items:                           # 항목 = 저장할 값
    - {name: temperature, path: [temp, temperature]}   # 리스트면 먼저 있는 키를 쓴다
  # auto_items: true               # 숫자인 키를 전부 항목으로 (새 키가 생겨도 설정 변경 불필요)
```

저장 구조는 모든 센서가 같습니다. 그래서 커버율 쿼리 하나로 모든 센서를 볼 수 있습니다.

```
measurement = sensor
tags        = space, item, device(선택)
fields      = value(측정값), recv_ts_ms(젯슨 수신 시각), delay_ms(수신-센서 시각), time_src("sensor"|"recv")
_time       = 센서 시각 (없거나 5분 넘게 어긋나면 수신 시각)
```

**dry-run으로 변환 결과를 먼저 확인합니다.** InfluxDB에 쓰지 않고 화면에 line protocol만 찍습니다.

```bash
cd ~/mqtt-collector
set -a; source <(sudo cat /etc/mqtt-collector/env); set +a
venv/bin/python collector.py --config config.yaml --dry-run
# 로그의 "매핑 규칙 없는 토픽" / "파싱 실패" 가 0 이 되도록 config 를 고친다. Ctrl+C 로 종료
```

## 5. 상시 가동

```bash
sudo systemctl enable --now mqtt-collector     # 지금 시작 + 부팅 시 자동 시작
systemctl status mqtt-collector
journalctl -u mqtt-collector -f                # 실시간 로그. 60초마다 health 줄이 찍힌다
```

VPN을 거쳐야만 브로커/Influx에 닿는다면 `mqtt-collector.service`의 `After=` 주석을 VPN 서비스 이름으로 풀어 주세요.
설정을 바꾼 뒤에는 `sudo systemctl restart mqtt-collector`만 하면 됩니다. 고정 client_id + QoS1 + clean_session=false라서 재시작하는 몇 초 사이의 메시지도 브로커가 보관했다가 다시 줍니다(브로커가 세션을 유지하는 경우).

수집기가 복구를 위해 하는 일은 다음과 같습니다.

| 상황 | 동작 |
|---|---|
| 브로커 재시작·네트워크 끊김 | keepalive 30초로 끊김 감지 → 1~10초 간격 자동 재접속 → 접속 시 다시 구독 |
| 부팅 직후 네트워크/VPN이 늦게 뜸 | 첫 접속을 5초마다 계속 재시도 |
| InfluxDB 쓰기 실패 | SSD의 `spool.db`에 쌓아 두고, 쓰기가 다시 되면 순서대로 재전송 |
| 수집기 프로세스가 죽음 | systemd `Restart=always`로 5초 뒤 재시작 |
| 형식이 깨진 메시지 | 그 메시지만 버리고 `parse_errors`로 셈 (수집기는 계속 돎) |

## 6. "지금 잘 쌓이고 있나" 보기

**InfluxDB 화면(가장 간단).** `queries.flux`의 쿼리를 Data Explorer > Script Editor에 붙여 넣고, Dashboards에 셀로 저장해 두면 한 화면에서 봅니다.

1. 공간·항목별 건수
2. 마지막 수신 시각과 경과 초 (경과가 주기의 몇 배를 넘으면 이상)
3. 공간·항목별 24시간 커버율
4. 시간대별 커버율 그래프
5. 빈 1분 목록
6. 수집기 상태 (`collector_health`: 연결 여부, 분당 메시지, 스풀, 쓰기 실패, 중복)
7. 전송 지연

Grafana를 쓰고 싶다면 같은 Flux 쿼리를 Grafana InfluxDB 데이터소스(Flux 모드)에 넣으면 됩니다. 필수는 아닙니다.

**보고서(산출물용).**

```bash
set -a; source <(sudo cat /etc/mqtt-collector/env); set +a
venv/bin/python coverage_report.py --hours 24 --out reports/coverage_$(date +%F).md
```

공간·항목별 건수, 데이터 있는 분, 커버율(95% 미만은 ⚠), 마지막 수신, 지연 p95 표와 결측 구간 + 추정 원인 표가 나옵니다.
추정 원인은 `collector_health` 기록으로 가립니다: health 기록이 없으면 수집기·보드 중단, `mqtt_connected=0`이면 브로커·네트워크 끊김, 연결은 정상인데 그 센서만 비면 센서 쪽 미전송입니다.
센서 주기가 1분보다 길면(예: 2분) `--expected-period-s 120`을 주세요. 그렇지 않으면 커버율이 구조적으로 50%가 됩니다.

## 7. 확인 기준 테스트

각 테스트의 시작·종료 시각을 적어 두면 보고서의 결측 구간과 맞춰 설명할 수 있습니다.

**① 브로커를 껐다 켜도 1분 안에 재개.**
브로커를 직접 재시작할 수 있으면 재시작하고, 공용 브로커라 못 건드리면 젯슨에서 브로커 포트만 막았다 푸는 것으로 같은 상황을 만듭니다(SSH는 끊기지 않음).

```bash
date; sudo iptables -I OUTPUT -p tcp --dport 1883 -j REJECT     # 끊기
sleep 60
date; sudo iptables -D OUTPUT -p tcp --dport 1883 -j REJECT     # 복구
journalctl -u mqtt-collector --since "-3min" | grep -E "끊김|접속 완료"
```

"접속 완료" 시각과 복구 시각의 차이가 재개 시간입니다(로컬 mosquitto로 시험했을 때 브로커가 뜬 뒤 약 1초 만에 재접속).
연결이 끊긴 사이 센서가 보낸 메시지는 브로커 세션이 유지되면 재접속 직후 한꺼번에 들어오고, `delay_ms`가 크게 찍힙니다.
(포트가 1883이 아니면 바꾸고, TLS면 8883입니다.)

**② 보드 재부팅 뒤 자동 재개.**

```bash
date; sudo reboot
# 다시 접속해서
systemctl status mqtt-collector            # active (running), 시작 시각 확인
journalctl -u mqtt-collector -b | head     # 이번 부팅 로그에서 "접속 완료" 시각
```

**③ InfluxDB 끊김(추가 확인).** `--dport 8086`(또는 Influx 포트)을 위와 같이 막았다 풀면 로그에 "스풀로" → "스풀 재전송 N건"이 찍히고, 그 구간에 결측이 남지 않아야 합니다.

**④ 24시간 커버율 95% 이상.** 테스트를 다 끝낸 뒤 24시간 동안 손대지 않고 둔 다음 `coverage_report.py --hours 24`를 돌립니다.

## 8. 산출물

**① 수집 현황 보고.** `coverage_report.py` 결과 표에, 결측 구간마다 "추정 원인"이 실제와 맞는지(테스트로 일부러 만든 것인지, 센서 문제인지) 한 줄씩 덧붙이면 됩니다.

**② 메시지 형식 변경 대응 기록.** 예시 하나를 들면:

> **상황:** 온습도 센서 펌웨어가 바뀌어 payload가 `{"ts":1729380000123,"temp":23.1,"hum":45.2}`에서
> `{"time":"2026-10-20T09:00:00+09:00","sensor":{"temperature":23.1,"humidity":45.2,"battery":87}}`로 바뀜.
> 키 이름, 중첩 구조, 시각 형식(ms → ISO)이 모두 달라짐.
>
> **바꾼 것 (`config.yaml`만):**
> ```yaml
> time: {path: [time, ts], unit: auto}        # ISO 와 ms 둘 다 받음
> items:
>   - {name: temperature, path: [sensor.temperature, temp]}
>   - {name: humidity,    path: [sensor.humidity, hum]}
>   - {name: battery,     path: sensor.battery}   # 새 항목 추가
> ```
> → `sudo systemctl restart mqtt-collector`. 옛 형식과 새 형식이 섞여 와도 둘 다 같은 `temperature`/`humidity` 항목으로 저장됨. 코드 수정 없음.
>
> **확인:** dry-run 출력, 재시작 전후 `parse_errors`/`no_items`가 0, 커버율 쿼리에 끊김 없음.

직접 해 보려면 `mosquitto_pub`으로 새 형식 메시지를 테스트 토픽에 보내고 위 순서대로 기록하면 됩니다.

## 9. 생각해 볼 점

**센서 시각과 수신 시각 중 무엇을 저장하나.**
둘 다 저장하고, 시계열의 기준 시각(`_time`)은 센서 시각으로 했습니다. 측정이 일어난 시점이 학습에 의미 있는 시각이기 때문입니다.
네트워크가 30초 밀렸다가 한꺼번에 도착해도 각 점은 원래 측정 시각 자리에 들어가므로 1분 커버율이 깎이지 않고, 대신 `delay_ms`가 30000 근처로 찍혀서 "밀렸다"는 사실이 남습니다.
수신 시각을 기준으로 저장했다면 30초 동안 빈 구간이 생기고 다음 순간 값이 몰려서 간격이 왜곡됩니다.
다만 센서 시계가 틀리면 엉뚱한 자리에 저장되므로, 수신 시각과 5분(`max_clock_skew_s`) 넘게 차이 나면 수신 시각을 쓰고 `time_src="recv"`로 표시합니다. 센서가 시각을 안 보내면 수신 시각밖에 쓸 수 없고, 이때는 몰림이 그대로 보입니다.

**수집과 학습·추론이 같은 저장소를 동시에 쓰면.**
- 학습용 대량 조회(몇 달치 원시 데이터)가 InfluxDB CPU·메모리·디스크 I/O를 잡아먹으면 쓰기가 느려지거나 타임아웃이 나서 수집이 밀립니다. 수집기는 스풀로 버티지만, 저장소 서버가 젯슨 같은 작은 장비라면 더 위험합니다.
- 최근 몇 분은 아직 늦게 도착할 데이터가 있어서 "덜 찬" 상태입니다. 학습 데이터셋을 이 구간까지 잘라 가면 결측처럼 보이거나 재현이 안 됩니다. 학습은 `now() - 10m` 같은 기준선 이전 데이터만 읽는 것이 안전합니다.
- 같은 쿼리를 다시 돌렸을 때 결과가 달라지면(늦게 온 데이터, 재전송 덮어쓰기) 실험 재현성이 깨집니다. 학습 시점의 데이터를 파일(Parquet/CSV)로 스냅숏해 두고 그 파일로 학습하는 방식을 권합니다.
- 추론은 최근 데이터만 짧게 읽으니 부담이 작지만, 쿼리 빈도를 정해 두고 다운샘플 버킷을 읽게 하면 더 가볍습니다.
- 권한도 나눕니다: 수집기 token은 해당 bucket 쓰기만, 학습·추론 token은 읽기만.

## 10. 심화(B) 방향

**1년 뒤 저장소 크기 추정.**
`크기 ≈ 시리즈 수 × (86400 / 주기초) × 365 × 필드 수 × 점당 바이트`.
InfluxDB(TSM)는 규칙적인 시계열을 잘 압축해서 값 하나에 대략 2~10바이트가 듭니다. 실제 값은 직접 재서 정하는 게 맞습니다: 하루 쌓은 뒤 저장소가 로컬이면 `du -sh` 차이로, 원격이면 하루 점 수(쿼리 1번 결과 합)와 관리자에게 받은 디스크 증가량으로 점당 바이트를 계산합니다.
이 교육장 기준: 12 시리즈 × 1,440건/일 × 365일 ≈ 630만 점, 필드 4개(value, recv_ts_ms, delay_ms, time_src) × 점당 약 6바이트로 잡으면 **약 150MB/년**이고, `collector_health`(1분마다 1건)를 더해도 수백 MB 안쪽입니다. 지금은 작지만 센서 종류나 공간이 늘면 시리즈 수에 비례해 커지므로, 보관 정책은 "센서가 10배로 늘어도 버티는가"를 기준으로 잡으세요. `recv_ts_ms`·`delay_ms`는 압축이 덜 되는 편이라 크기가 문제가 되면 원시 버킷 보관 기간을 줄이는 쪽으로 대응합니다.

**다운샘플링·보관 정책 예.**

| 버킷 | 내용 | 보관 |
|---|---|---|
| `sensors_raw` | 원시 데이터 (지금 쓰는 버킷) | 90일 |
| `sensors_1m` | 1분 mean/min/max/count | 2년 |
| `sensors_1h` | 1시간 mean/min/max | 영구 |

InfluxDB Task로 1분 다운샘플을 주기적으로 만들되, 늦게 오는 데이터를 놓치지 않게 **몇 분 늦게(offset) 돌고 이전 구간도 다시 계산**하게 합니다. `count`를 같이 남겨 두면 원시 데이터가 지워진 뒤에도 커버율을 계산할 수 있습니다.

```flux
option task = {name: "downsample_1m", every: 5m, offset: 2m}
from(bucket: "sensors_raw") |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "sensor" and r._field == "value")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> to(bucket: "sensors_1m")
```

**같은 메시지 두 번(재전송) 찾기와 처리.**
- QoS1은 "최소 한 번" 전달이라 재접속 직후 같은 메시지가 다시 올 수 있습니다.
- 저장은 이미 멱등입니다: InfluxDB는 같은 measurement·태그·`_time`의 점을 덮어쓰므로, 센서 시각을 `_time`으로 쓰는 한 재전송이 중복 행을 만들지 않습니다.
- 수집기는 최근 5만 개의 (시리즈, 항목, 센서 시각) 키를 기억해서 재전송을 세고 건너뛰며, 그 수를 `collector_health.duplicates`로 남깁니다. 보고서의 "재전송 건수"가 이 값입니다.
- 센서 시각이 없어서 수신 시각을 쓰는 센서는 재전송이 다른 시각의 새 점으로 들어갑니다. 이런 센서는 payload에 일련번호(seq)가 있으면 그것을 키로 쓰도록 확장하는 것이 다음 단계입니다.

## 11. 자주 막히는 곳

| 증상 | 확인 |
|---|---|
| `MQTT 접속 거부: Not authorized` | ID/PW, 그 계정에 해당 토픽 구독 권한이 있는지 |
| 접속은 되는데 데이터가 안 들어옴 | 로그의 "매핑 규칙 없는 토픽" 줄 → `config.yaml`의 topic 패턴 |
| `Influx 쓰기 실패 ... 401/403` | token 권한(해당 bucket write), org/bucket 이름 |
| `Influx 쓰기 실패 ... timeout` | VPN 연결, URL·포트. 스풀이 쌓이는지 `collector_health.spool` 확인 |
| 재부팅 뒤 서비스가 안 뜸 | `journalctl -u mqtt-collector -b`, SSD 마운트(`df -h /mnt/ssd`) |
| 시각이 9시간 어긋남 | 시간대 없는 ISO 문자열 → `defaults.tz_offset_h` 확인 |
