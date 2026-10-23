#!/usr/bin/env bash
# 젯슨에서 실행: 이 폴더(예: ~/mqtt-collector 또는 /mnt/ssd/mqtt-collector) 안에서  bash install.sh
# 폴더 위치는 어디든 상관없다. 이 스크립트가 있는 곳을 기준으로 서비스를 등록한다.
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP"
echo ">> 설치 위치: $APP"

# 패키지 설치와 서비스 등록에 관리자 권한이 필요하다
if ! sudo -v; then
  echo "!! 이 계정($(whoami))에 sudo 권한이 없습니다. sudo 권한이 있는 계정에서 'sudo usermod -aG sudo $(whoami)' 실행 후 다시 접속하세요."
  exit 1
fi

# python3 가 3.8 이상이면 그대로 쓰고 (Ubuntu 22.04/24.04 계열 JetPack),
# 3.6 인 옛 JetPack 4.x 면 python3.8 을 설치해서 쓴다 (paho-mqtt 2.x 는 3.7 이상 필요)
PY=python3
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  command -v python3.8 >/dev/null || { sudo apt-get update && sudo apt-get install -y python3.8 python3.8-venv python3.8-dev; }
  PY=python3.8
fi
if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
  sudo apt-get update && sudo apt-get install -y "$("$PY" -c 'import sys; print("python%d.%d-venv" % sys.version_info[:2])')"
fi
[ -x venv/bin/pip ] || { rm -rf venv; "$PY" -m venv venv; }   # 반쯤 만들어진 venv 는 지우고 다시
venv/bin/pip install -U pip wheel
venv/bin/pip install -r requirements.txt

[ -f config.yaml ] || cp config.example.yaml config.yaml
sudo mkdir -p /etc/mqtt-collector
if [ ! -f /etc/mqtt-collector/env ]; then
  sudo cp env.example /etc/mqtt-collector/env
  echo ">> /etc/mqtt-collector/env 에 실제 접속 정보를 넣으세요"
fi
sudo chmod 600 /etc/mqtt-collector/env

sed -e "s#__USER__#$(whoami)#g" -e "s#__APP__#$APP#g" mqtt-collector.service \
  | sudo tee /etc/systemd/system/mqtt-collector.service >/dev/null
sudo systemctl daemon-reload
echo ">> config.yaml 과 env 를 채운 뒤:  sudo systemctl enable --now mqtt-collector"
