# BankSystem_16 관측성 스택 + Monitoring LLM 실행 가이드

## 전체 구성

```
┌─────────────────────────────────────────────────┐
│  BankSystem_16 서버들                            │
│                                                 │
│  web01/02 (Linux)   → node_exporter  :9100      │
│                     → apache_exporter :9117     │
│                     → Promtail (로그)           │
│                                                 │
│  was01/02 (Windows) → jmx_exporter   :9090      │
│                     → windows_exporter :9182    │
│                     → Grafana Alloy (로그)      │
│                                                 │
│  db01/02  (Windows) → mysqld_exporter :9104     │
│                     → windows_exporter :9182    │
│                     → Grafana Alloy (로그)      │
└─────────────────────────────────────────────────┘
         ↓ 메트릭 Pull         ↓ 로그 Push
┌─────────────────────────────────────────────────┐
│  docker-compose (관측성 서버 — 별도 서버 권장)    │
│                                                 │
│  Prometheus  :9090  ← 메트릭 수집               │
│  Loki        :3100  ← 로그 수집                 │
│  Jaeger      :16686 ← 트레이스 수집             │
│  Grafana     :3000  ← 시각화                   │
└─────────────────────────────────────────────────┘
         ↓ 쿼리
┌─────────────────────────────────────────────────┐
│  Monitoring LLM API  :8000                      │
│  (dev-ubuntu 서버 — Ollama 있는 서버)            │
└─────────────────────────────────────────────────┘
```

---

## Step 1: 관측성 스택 실행

```bash
cd monitoring_llm/docker

# 실행
docker compose up -d

# 상태 확인
docker compose ps

# 로그 확인
docker compose logs -f prometheus
docker compose logs -f loki
```

접속 확인:
- Prometheus: http://서버IP:9090
- Loki:       http://서버IP:3100/ready  ("ready" 출력되면 OK)
- Grafana:    http://서버IP:3000  (admin/admin)
- Jaeger:     http://서버IP:16686

---

## Step 2: 각 서버에 exporter 설치

### web01/02 (Linux) — node_exporter

```bash
# node_exporter 설치
wget https://github.com/prometheus/node_exporter/releases/download/v1.7.0/node_exporter-1.7.0.linux-amd64.tar.gz
tar xf node_exporter-*.tar.gz
sudo cp node_exporter-*/node_exporter /usr/local/bin/
sudo useradd -rs /bin/false node_exporter

# systemd 서비스
sudo tee /etc/systemd/system/node_exporter.service << 'EOF'
[Unit]
Description=Node Exporter
After=network.target

[Service]
User=node_exporter
ExecStart=/usr/local/bin/node_exporter
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now node_exporter

# 확인
curl http://localhost:9100/metrics | head -5
```

### web01/02 (Linux) — apache_exporter

```bash
wget https://github.com/Lusitaniae/apache_exporter/releases/download/v1.0.0/apache_exporter-1.0.0.linux-amd64.tar.gz
tar xf apache_exporter-*.tar.gz
sudo cp apache_exporter-*/apache_exporter /usr/local/bin/

# Apache mod_status 활성화 필요 (httpd.conf 또는 apache2.conf)
# <Location "/server-status">
#     SetHandler server-status
#     Require ip 127.0.0.1
# </Location>

sudo systemctl enable --now apache_exporter
curl http://localhost:9117/metrics | head -5
```

### web01/02 (Linux) — Promtail 로그 수집

```bash
wget https://github.com/grafana/loki/releases/download/v2.9.5/promtail-linux-amd64.zip
unzip promtail-linux-amd64.zip
sudo cp promtail-linux-amd64 /usr/local/bin/promtail

# 설정 파일 복사 (loki/promtail.yml 에서 host 값 수정)
sudo cp promtail.yml /etc/promtail/promtail.yml
# ← promtail.yml 에서 host: "web01-bank16" 또는 "web02-bank16" 으로 수정
# ← clients.url 을 Loki 서버 IP로 수정

sudo systemctl enable --now promtail
```

### was01/02, db01/02 (Windows) — Grafana Alloy

```
1. https://github.com/grafana/alloy/releases 에서 alloy-installer-windows-amd64.exe 다운로드
2. 설치 후 C:\Program Files\GrafanaLabs\Alloy\config.alloy 를
   loki/alloy-windows.river 내용으로 교체
3. host 값을 서버별로 수정 (was01-bank16, was02-bank16, db01-bank16, db02-bank16)
4. Loki URL을 관측성 서버 IP로 수정
5. Alloy 서비스 재시작
```

### was01/02 (Windows) — jmx_exporter (Tomcat JVM 메트릭)

```
1. https://github.com/prometheus/jmx_exporter/releases 에서 jmx_prometheus_javaagent.jar 다운로드
2. Tomcat 시작 옵션에 추가:
   JAVA_OPTS=-javaagent:C:\jmx_exporter\jmx_prometheus_javaagent-1.0.1.jar=9090:C:\jmx_exporter\tomcat.yml
3. Tomcat 재시작
4. 확인: curl http://was01:9090/metrics
```

### db01/02 (Windows) — mysqld_exporter

```
1. https://github.com/prometheus/mysqld_exporter/releases 에서 Windows 버전 다운로드
2. MySQL 계정 생성:
   CREATE USER 'exporter'@'localhost' IDENTIFIED BY 'ExporterPass123!';
   GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'localhost';
3. 실행: mysqld_exporter.exe --web.listen-address=:9104
4. 확인: curl http://db01:9104/metrics
```

---

## Step 3: Prometheus 수집 확인

```bash
# Prometheus 웹 UI에서 확인
http://서버IP:9090/targets

# 또는 curl
curl http://서버IP:9090/api/v1/targets | python -m json.tool | grep '"health"'
```

모든 target이 `"health": "up"` 이어야 함.

---

## Step 4: Loki 로그 수집 확인

```bash
# Loki에 로그가 들어오는지 확인
curl "http://서버IP:3100/loki/api/v1/labels"

# web01 로그 조회
curl "http://서버IP:3100/loki/api/v1/query_range?query={host=\"web01-bank16\"}&limit=5"
```

---

## Step 5: Monitoring LLM 실행

```bash
# dev-ubuntu 서버 (Ollama 있는 서버)
export PROMETHEUS_URL="http://관측성서버IP:9090"
export LOKI_URL="http://관측성서버IP:3100"
export JAEGER_URL="http://관측성서버IP:16686"
export OLLAMA_BASE_URL="http://localhost:11434"
export CMDB_DB_PATH="/path/to/cmdb.db"

cd /path/to/project
uvicorn monitoring_llm.api.main:app --host 0.0.0.0 --port 8000

# 헬스체크
curl http://localhost:8000/monitoring/health
```

---

## 자주 쓰는 명령어

```bash
# 전체 재시작
docker compose restart

# Prometheus 설정 리로드 (재시작 불필요)
curl -X POST http://localhost:9090/-/reload

# Loki 상태 확인
curl http://localhost:3100/ready
curl http://localhost:3100/metrics | grep loki_ingester

# 컨테이너 로그 실시간 확인
docker compose logs -f loki
docker compose logs -f prometheus

# 볼륨 포함 완전 삭제 (주의!)
docker compose down -v
```

---

## 트러블슈팅

**Prometheus target이 down인 경우**
- exporter 프로세스가 실행 중인지 확인
- 방화벽에서 exporter 포트 허용 여부 확인
- `curl http://서버IP:포트/metrics` 로 직접 접근 테스트

**Loki 로그가 안 들어오는 경우**
- Promtail/Alloy 에서 `host` 레이블 값 확인
- CMDB의 `loki_host` 값과 일치해야 함
- Loki URL이 정확한지 확인
- `curl -X POST http://로키IP:3100/loki/api/v1/push` 로 직접 테스트

**OOM 또는 Loki 응답 느린 경우**
- docker-compose.yml 에 메모리 제한 추가:
  ```yaml
  loki:
    mem_limit: 2g
  ```
