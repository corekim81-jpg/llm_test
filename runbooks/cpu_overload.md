# CPU 과부하 대응 Runbook

## 개요
WAS 서버(was01, was02) 또는 웹 서버(web01, web02)에서 CPU 사용률이 임계치를 초과할 때의 대응 절차.

## 감지 조건
- CPU 사용률 85% 이상 5분 이상 지속
- Prometheus Alert: `HighCPUUsage`

## 원인 분류

### 원인 1: 애플리케이션 스레드 폭증
JVM 스레드 덤프 또는 프로세스 목록을 통해 비정상적으로 CPU를 점유하는 프로세스를 확인한다.

**확인 명령 (Linux - web01, web02):**
```bash
top -bn1 | head -20
ps aux --sort=-%cpu | head -10
```

**확인 명령 (Windows - was01, was02):**
```powershell
Get-Process | Sort-Object CPU -Descending | Select-Object -First 10
```

### 원인 2: GC(Garbage Collection) 과부하
Java 힙 메모리 부족으로 Full GC가 빈번하게 발생할 때 CPU가 급등한다.

**확인 방법:**
- Prometheus 메트릭: `jvm_gc_pause_seconds_sum`
- GC 로그 위치: `/logs/gc.log` (was01, was02)

### 원인 3: DB 슬로우 쿼리 연쇄
DB 응답 지연으로 WAS 스레드가 대기 상태가 되고, 신규 요청이 계속 쌓여 CPU 폭증으로 이어지는 패턴.

**확인 방법:**
- Loki 쿼리: `{job="was01"} |= "slow query"`
- MySQL slow_query_log 확인 (db01, db02)

## 조치 절차

### 즉시 조치
1. 해당 서버의 트래픽을 다른 서버로 전환 (로드밸런서 가중치 조정)
2. CPU 점유 프로세스 특정 후 재시작 여부 결정
3. JVM Heap Dump 수집 (재현 불가 대비): `jcmd <pid> GC.heap_dump /tmp/heap.hprof`

### 근본 원인 조치
- 스레드 폭증 → 스레드 풀 크기 조정, 코드 리뷰
- GC 과부하 → `-Xmx` 힙 메모리 증설 또는 메모리 누수 분석
- 슬로우 쿼리 → 인덱스 추가, 쿼리 튜닝, DB 커넥션 풀 조정

## 에스컬레이션 기준
- 조치 후 15분 내 CPU가 85% 미만으로 내려오지 않으면 팀장 에스컬레이션
- 서비스 장애(HTTP 5xx 급증) 동반 시 즉시 에스컬레이션

## 관련 Alert
- `HighCPUUsage` (85% 이상 5분)
- `CriticalCPUUsage` (95% 이상 2분)
- `JvmGcHighPause`
