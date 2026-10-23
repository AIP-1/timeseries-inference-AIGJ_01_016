#!/usr/bin/env python3
"""MQTT -> InfluxDB 수집기.

- 토픽/메시지 형식 -> measurement/tag/field 매핑은 전부 config.yaml 에서 정한다.
  새 센서나 형식 변경은 설정만 고치고 `systemctl restart mqtt-collector` 하면 된다.
- 브로커가 끊기면 paho 가 자동 재접속(최대 reconnect_max_s 간격)하고, on_connect 에서 다시 구독한다.
- InfluxDB 쓰기가 실패하면 SSD 의 SQLite 스풀에 line protocol 로 쌓아 두었다가 복구되면 다시 보낸다.
- 60초마다 collector_health 를 기록해서 결측 원인(수집기 정지 / 브로커 끊김 / 센서 미전송)을 구분할 수 있게 한다.

Python 3.7 이상, paho-mqtt 2.x, influxdb-client, PyYAML.
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import logging
import os
import queue
import re
import signal
import socket
import sqlite3
import threading
import time

import paho.mqtt.client as mqtt
import yaml
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

log = logging.getLogger("collector")


# ---------------------------------------------------------------- 설정
def load_config(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    # ${VAR} 는 환경변수로 치환 (비밀값은 /etc/mqtt-collector/env 에 둔다)
    raw = "\n".join(l for l in raw.splitlines() if not l.lstrip().startswith("#"))
    missing = [v for v in re.findall(r"\$\{(\w+)\}", raw) if v not in os.environ]
    if missing:
        raise SystemExit("환경변수가 없습니다: %s" % ", ".join(sorted(set(missing))))
    return yaml.safe_load(os.path.expandvars(raw))


def topic_matches(pattern, topic):
    """MQTT 와일드카드(+, #) 매칭."""
    p, t = pattern.split("/"), topic.split("/")
    for i, seg in enumerate(p):
        if seg == "#":
            return True
        if i >= len(t) or (seg != "+" and seg != t[i]):
            return False
    return len(p) == len(t)


# ---------------------------------------------------------------- 파싱
def get_path(obj, path):
    """'a.b.0.c' 형태 경로로 값을 꺼낸다. 없으면 None."""
    if path in (None, "", "."):
        return obj
    cur = obj
    for key in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and key.lstrip("-").isdigit():
            idx = int(key)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def first_path(obj, paths):
    """paths 가 리스트면 처음으로 값이 있는 경로를 쓴다 (키 이름 변경 대응용)."""
    if not isinstance(paths, list):
        paths = [paths]
    for p in paths:
        v = get_path(obj, p)
        if v is not None:
            return v
    return None


_TMPL = re.compile(r"\{(t-?\d+|p:[^}]+|topic)\}")


def render(template, topic_parts, payload):
    """'{t1}' = 토픽 1번째 조각(0부터), '{p:device.id}' = payload 경로, '{topic}' = 전체 토픽."""
    def sub(m):
        key = m.group(1)
        if key == "topic":
            return "/".join(topic_parts)
        if key.startswith("t"):
            i = int(key[1:])
            return topic_parts[i] if -len(topic_parts) <= i < len(topic_parts) else ""
        v = get_path(payload, key[2:]) if isinstance(payload, (dict, list)) else None
        return "" if v is None else str(v)
    return _TMPL.sub(sub, str(template))


def decode_payload(raw, rule):
    fmt = rule.get("format", "json")
    text = raw.decode(rule.get("encoding", "utf-8"), errors="replace").strip()
    if fmt == "json":
        return json.loads(text)
    if fmt == "plain":  # 숫자 하나만 오는 경우
        return {"value": text}
    if fmt == "csv":  # "23.1,45.0,1729380000"
        cols = rule["columns"]
        vals = [v.strip() for v in text.split(rule.get("delimiter", ","))]
        return dict(zip(cols, vals))
    if fmt == "kv":  # "temp=23.1;hum=45"
        out = {}
        for part in re.split(rule.get("delimiter", r"[;,&\s]+"), text):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    raise ValueError("알 수 없는 format: %s" % fmt)


def to_number(v, typ="float"):
    if isinstance(v, bool):
        return float(v) if typ == "float" else int(v)
    if typ == "int":
        return int(float(v))
    if typ == "bool":
        return str(v).lower() in ("1", "true", "on", "yes")
    if typ == "string":
        return str(v)
    return float(v)


def parse_time(v, unit="auto"):
    """센서 시각 -> epoch ns. 실패하면 None."""
    if v is None:
        return None
    if unit == "iso" or (unit == "auto" and isinstance(v, str) and not _is_num(v)):
        s = str(v).replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # ChirpStack nsTime 처럼 소수점 9자리는 6자리로 자른다
        t = dt.datetime.fromisoformat(s)
        if t.tzinfo is None:  # 시간대 없는 문자열은 설정의 tz 로 해석 (기본 Asia/Seoul +09:00)
            t = t.replace(tzinfo=_DEFAULT_TZ)
        return int(t.replace(microsecond=0).timestamp()) * 10**9 + t.microsecond * 1000
    x = float(v)
    if unit == "auto":  # 자릿수로 단위 추정
        unit = "s" if x < 1e11 else "ms" if x < 1e14 else "us" if x < 1e17 else "ns"
    mul = {"s": 10**9, "ms": 10**6, "us": 10**3, "ns": 1}[unit]
    if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
        return int(v) * mul  # 정수는 float 를 거치지 않아 ns 오차가 없다
    return int(round(x * mul))


def _is_num(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


_DEFAULT_TZ = dt.timezone(dt.timedelta(hours=9))


class Mapper:
    """config 의 sensors 규칙으로 MQTT 메시지를 Point 목록으로 바꾼다."""

    def __init__(self, cfg):
        self.rules = cfg["sensors"]
        self.default_measurement = cfg.get("defaults", {}).get("measurement", "sensor")
        self.max_skew_ns = int(cfg.get("defaults", {}).get("max_clock_skew_s", 300) * 1e9)

    def match(self, topic):
        for r in self.rules:
            if topic_matches(r["topic"], topic):
                return r
        return None

    def to_points(self, topic, raw, recv_ns):
        rule = self.match(topic)
        if rule is None:
            return None, "unmatched"
        payload = decode_payload(raw, rule)
        parts = topic.split("/")
        if rule.get("foreach"):
            # {"values": {"회의실": {...}, "사무실": {...}}} 처럼 키 이름이 바뀌는 묶음을 하나씩 펼친다.
            # 각 묶음 안에서 {p:_key} = 키 이름, _item = 그 안의 값들
            container = get_path(payload, rule["foreach"])
            if not isinstance(container, dict):
                return [], "no_items"
            sub_rule = {k: v for k, v in rule.items() if k != "foreach"}
            points = []
            for key, sub in container.items():
                p2 = dict(payload, _key=key, _item=sub)
                points.extend(self._build(sub_rule, parts, p2, recv_ns))
            return points, ("ok" if points else "no_items")
        points = self._build(rule, parts, payload, recv_ns)
        return points, ("ok" if points else "no_items")

    def _build(self, rule, parts, payload, recv_ns):
        # 1) 시각: 센서 시각을 우선, 없거나 수신 시각과 너무 다르면 수신 시각
        t_cfg = rule.get("time") or {}
        ts, src = None, "recv"
        if t_cfg.get("path"):
            try:
                ts = parse_time(first_path(payload, t_cfg["path"]), t_cfg.get("unit", "auto"))
            except (ValueError, TypeError, OverflowError):
                ts = None
            if ts is not None and abs(ts - recv_ns) <= self.max_skew_ns:
                src = "sensor"
            else:
                ts = None
        if ts is None:
            ts = recv_ns

        # 2) 태그
        tags = {k: render(v, parts, payload) for k, v in (rule.get("tags") or {}).items()}
        tags = {k: v for k, v in tags.items() if v != ""}
        # 필터: only_tags 에 적은 값이 아닌 메시지는 저장하지 않는다 (예: space: [실습실, 회의실])
        for k, allowed in (rule.get("only_tags") or {}).items():
            if tags.get(k) not in allowed:
                return []

        # 3) 항목(item) 목록: 명시한 items + auto_items(숫자인 키 전부)
        items = []
        for it in rule.get("items") or []:
            v = first_path(payload, it.get("path", it["name"]))
            if v is not None:
                # name 에도 템플릿 사용 가능: "{t2}" = 토픽 2번째 조각을 항목 이름으로
                items.append((render(it["name"], parts, payload), v, it.get("type", "float"), it.get("scale")))
        auto = rule.get("auto_items")
        if auto and isinstance(payload, dict):
            base = get_path(payload, auto.get("path", "")) if isinstance(auto, dict) else payload
            exclude = set((auto.get("exclude") if isinstance(auto, dict) else None) or [])
            named = {i[0] for i in items}
            for k, v in (base or {}).items():
                if k in named or k in exclude or isinstance(v, (dict, list)):
                    continue
                if isinstance(v, (int, float)) or (isinstance(v, str) and _is_num(v)):
                    items.append((k, v, "float", None))

        only = rule.get("only_items")  # 필터: 이 항목들만 저장 (예: [co2, temperature, humidity])
        if only:
            items = [i for i in items if i[0] in only]

        measurement = render(rule.get("measurement", self.default_measurement), parts, payload)
        points = []
        for name, v, typ, scale in items:
            try:
                val = to_number(v, typ)
            except (ValueError, TypeError):
                continue
            if scale is not None and typ in ("float", "int"):
                val = val * scale
            p = Point(measurement).time(ts, WritePrecision.NS).tag("item", name)
            for k, tv in tags.items():
                p = p.tag(k, tv)
            p = (p.field("value", val)
                  .field("recv_ts_ms", recv_ns // 1_000_000)
                  .field("delay_ms", (recv_ns - ts) // 1_000_000)
                  .field("time_src", src))
            points.append((p, (measurement, tuple(sorted(tags.items())), name, ts)))
        return points


# ---------------------------------------------------------------- 스풀 (Influx 장애 시 SSD 버퍼)
class Spool:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS spool (id INTEGER PRIMARY KEY, line TEXT)")
        self.db.commit()
        self.lock = threading.Lock()

    def put(self, lines):
        with self.lock:
            self.db.executemany("INSERT INTO spool(line) VALUES (?)", [(l,) for l in lines])
            self.db.commit()

    def take(self, n):
        with self.lock:
            return self.db.execute("SELECT id, line FROM spool ORDER BY id LIMIT ?", (n,)).fetchall()

    def delete_upto(self, max_id):
        with self.lock:
            self.db.execute("DELETE FROM spool WHERE id <= ?", (max_id,))
            self.db.commit()

    def size(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM spool").fetchone()[0]


# ---------------------------------------------------------------- 수집기
class Collector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mapper = Mapper(cfg)
        self.q = queue.Queue(maxsize=cfg.get("queue_max", 100000))
        self.stop = threading.Event()
        self.stats = collections.Counter()
        self.mqtt_connected = False
        self.host = socket.gethostname()

        d = cfg.get("dedup", {})
        self.dedup_on = d.get("enabled", True)
        self.dedup_keys = collections.OrderedDict()
        self.dedup_max = d.get("window", 50000)

        ic = cfg["influx"]
        self.bucket = ic["bucket"]
        self.org = ic["org"]
        self.influx = InfluxDBClient(url=ic["url"], token=ic["token"], org=ic["org"],
                                     timeout=ic.get("timeout_ms", 10000),
                                     verify_ssl=ic.get("verify_ssl", True))
        self.write_api = self.influx.write_api(write_options=SYNCHRONOUS)
        self.spool = Spool(cfg.get("spool_path", "./spool.db"))
        self.batch_size = ic.get("batch_size", 500)
        self.flush_s = ic.get("flush_interval_s", 2)

        mc = cfg["mqtt"]
        self.mc = mc
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=mc.get("client_id", "collector-" + self.host),
                                  clean_session=mc.get("clean_session", False))
        if mc.get("username"):
            self.client.username_pw_set(mc["username"], mc.get("password"))
        if mc.get("tls"):
            self.client.tls_set(ca_certs=mc.get("ca_certs"))
        self.client.reconnect_delay_set(min_delay=1, max_delay=mc.get("reconnect_max_s", 10))
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    # --- MQTT 콜백
    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT 접속 거부: %s", reason_code)
            return
        self.mqtt_connected = True
        self.stats["mqtt_connects"] += 1
        subs = self.mc.get("subscribe") or sorted({r["topic"] for r in self.cfg["sensors"]})
        qos = self.mc.get("qos", 1)
        client.subscribe([(t, qos) for t in subs])
        log.info("MQTT 접속 완료, 구독: %s", subs)

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.mqtt_connected = False
        self.stats["mqtt_disconnects"] += 1
        log.warning("MQTT 끊김(%s), 자동 재접속 대기", reason_code)

    def on_message(self, client, userdata, msg):
        recv_ns = time.time_ns()
        self.stats["msgs"] += 1
        try:
            points, status = self.mapper.to_points(msg.topic, msg.payload, recv_ns)
        except Exception as e:  # 형식이 깨진 메시지 하나 때문에 수집기가 죽지 않게
            self.stats["parse_errors"] += 1
            if self.stats["parse_errors"] % 100 == 1:
                log.warning("파싱 실패 %s: %r (%s)", msg.topic, msg.payload[:200], e)
            return
        if points is None:
            self.stats["unmatched"] += 1
            if self.stats["unmatched"] % 100 == 1:
                log.info("매핑 규칙 없는 토픽: %s  payload=%r", msg.topic, msg.payload[:200])
            return
        if not points:
            self.stats["no_items"] += 1
            return
        for p, key in points:
            if self.dedup_on and self._is_dup(key):
                self.stats["duplicates"] += 1
                continue
            try:
                self.q.put_nowait(p.to_line_protocol())
            except queue.Full:
                self.stats["queue_drops"] += 1

    def _is_dup(self, key):
        # 같은 (series, item, 센서시각) 이 이미 들어왔으면 재전송으로 본다.
        # (Influx 도 같은 series+time 이면 덮어쓰므로 저장은 원래 멱등이고, 여기서는 '세기'가 목적)
        h = hashlib.blake2b(repr(key).encode(), digest_size=12).digest()
        if h in self.dedup_keys:
            self.dedup_keys.move_to_end(h)
            return True
        self.dedup_keys[h] = None
        if len(self.dedup_keys) > self.dedup_max:
            self.dedup_keys.popitem(last=False)
        return False

    # --- 쓰기 스레드
    def _write(self, lines):
        try:
            self.write_api.write(bucket=self.bucket, org=self.org, record=lines,
                                 write_precision=WritePrecision.NS)
            self.stats["points_written"] += len(lines)
            return True
        except Exception as e:
            self.stats["write_errors"] += 1
            log.warning("Influx 쓰기 실패(%d건 스풀로): %s", len(lines), str(e)[:200])
            return False

    def writer_loop(self):
        buf, last = [], time.monotonic()
        down_until = 0.0  # Influx 가 안 될 때는 30초 동안 바로 스풀로 (매번 타임아웃 기다리지 않게)
        while not self.stop.is_set() or not self.q.empty():
            try:
                buf.append(self.q.get(timeout=0.5))
                while len(buf) < self.batch_size * 10:  # 밀린 것은 한 번에 꺼낸다
                    buf.append(self.q.get_nowait())
            except queue.Empty:
                pass
            if buf and (len(buf) >= self.batch_size or time.monotonic() - last >= self.flush_s):
                if time.monotonic() < down_until:
                    self.spool.put(buf)
                elif not self._write(buf):
                    self.spool.put(buf)
                    down_until = time.monotonic() + 30
                else:
                    self._drain_spool()
                buf, last = [], time.monotonic()
        if buf and not self._write(buf):  # 종료 직전 남은 것
            self.spool.put(buf)

    def _drain_spool(self, n=5000, rounds=5):
        for _ in range(rounds):
            rows = self.spool.take(n)
            if not rows:
                return
            if not self._write([r[1] for r in rows]):
                return
            self.spool.delete_upto(rows[-1][0])
            log.info("스풀 재전송 %d건", len(rows))

    # --- 헬스 기록
    def health_loop(self):
        every = self.cfg.get("health_interval_s", 60)
        prev = collections.Counter()
        while not self.stop.wait(every):
            cur = self.stats.copy()
            d = {k: cur[k] - prev[k] for k in ("msgs", "points_written", "parse_errors",
                                                "unmatched", "no_items", "duplicates", "write_errors",
                                                "queue_drops", "mqtt_disconnects")}
            prev = cur
            spool_n = self.spool.size()
            if spool_n and self.mqtt_connected:
                self._drain_spool()
            p = Point("collector_health").tag("host", self.host).time(time.time_ns(), WritePrecision.NS)
            p = p.field("mqtt_connected", int(self.mqtt_connected)).field("spool", spool_n)
            p = p.field("queue", self.q.qsize())
            for k, v in d.items():
                p = p.field(k, v)
            line = p.to_line_protocol()
            if not self._write([line]):
                self.spool.put([line])
            log.info("health %s spool=%d connected=%s", dict(d), spool_n, self.mqtt_connected)

    def run(self):
        writer = threading.Thread(target=self.writer_loop, name="writer", daemon=True)
        writer.start()
        threading.Thread(target=self.health_loop, name="health", daemon=True).start()
        mc = self.mc
        # 첫 접속도 실패하면 계속 재시도 (부팅 직후 네트워크/VPN 이 늦게 올라오는 경우)
        while not self.stop.is_set():
            try:
                self.client.connect(mc["host"], int(mc.get("port", 1883)), keepalive=mc.get("keepalive", 30))
                break
            except OSError as e:
                log.warning("MQTT 첫 접속 실패(%s), 5초 뒤 재시도", e)
                self.stop.wait(5)
        self.client.loop_start()
        self.stop.wait()
        self.client.disconnect()
        self.client.loop_stop()
        writer.join(timeout=15)

    def shutdown(self, *_):
        log.info("종료 신호 수신")
        self.stop.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="Influx 에 쓰지 않고, 변환 결과(line protocol)를 화면에 찍는다")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    global _DEFAULT_TZ
    _DEFAULT_TZ = dt.timezone(dt.timedelta(hours=cfg.get("defaults", {}).get("tz_offset_h", 9)))
    c = Collector(cfg)
    if args.dry_run:
        def show(lines):
            for l in lines:
                print(l)
            return True
        c._write = show
    signal.signal(signal.SIGTERM, c.shutdown)
    signal.signal(signal.SIGINT, c.shutdown)
    c.run()


if __name__ == "__main__":
    main()
