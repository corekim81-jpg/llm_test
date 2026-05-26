# HTTP 500 에러 급증 대응 Runbook

## 개요
WAS 서버(was01, was02)에서 HTTP 500 Internal Server Error가 급증할 때의 분석 및 대응 절차.

## 감지 조건
- 5분 내 500 에러 비율 5% 이상
- Prometheus Alert: `Http5xxErrorRateHigh`
- Loki Alert: 500 에러 로그 분당 50건 이상

## 초기 확인 사항

### Loki에서 에러 로그 확인
```logql
{job=~"was01|was02"} |= "500" | json | level="ERROR"
{job=~"was01|was02"} |~ "Exception|Error" | json
```

### Prometheus에서 에러율 확인
```promql
sum(rate(http_server_requests_seconds_count{status="500"}[5m])) by (uri, instance)
```

## 원인 분류

### 원인 1: NullPointerException / 애플리케이션 버그
최근 배포 이후 특정 API 엔드포인트에서 반복적으로 동일한 스택 트레이스 발생.

**확인 방법:**
- Loki: `{job="was01"} |= "NullPointerException"`
- Jaeger/Tempo: 에러 트레이스에서 스택 트레이스 확인
- 배포 이력 확인: `git log --oneline -10`

**조치:**
- 직전 버전으로 롤백
- 핫픽스 적용 후 재배포

### 원인 2: DB 연결 실패 / 커넥션 풀 고갈
DB 서버(db01, db02) 장애 또는 커넥션 풀 부족으로 전체 API가 500 반환.

**확인 방법:**
- Loki: `{job="was01"} |= "Connection refused"` 또는 `|= "HikariPool"`
- Prometheus: `hikaricp_connections_active` / `hikaricp_connections_max`
- db01/db02 상태 직접 확인

**조치:**
- DB 서버 상태 확인 및 복구
- 커넥션 풀 크기 임시 증가: `spring.datasource.hikari.maximum-pool-size=50`
- DB 슬로우 쿼리로 인한 커넥션 점유 시 슬로우 쿼리 kill

### 원인 3: 외부 API 연동 장애
결제, 인증 등 외부 시스템 타임아웃으로 인한 연쇄 장애.

**확인 방법:**
- Loki: `{job="was01"} |= "SocketTimeoutException"` 또는 `|= "ConnectTimeoutException"`
- Jaeger: 외부 호출 span의 duration이 타임아웃 값에 근접하는지 확인

**조치:**
- 해당 외부 API 담당팀 연락
- Circuit Breaker 동작 여부 확인
- 외부 API 의존 기능 임시 비활성화 (Fallback 처리)

### 원인 4: 메모리 부족 / OOM
```
java.lang.OutOfMemoryError: Java heap space
```

**확인 방법:**
- Prometheus: `jvm_memory_used_bytes{area="heap"}` 추이
- Loki: `{job="was01"} |= "OutOfMemoryError"`

**조치:**
- JVM 재시작 (트래픽 전환 후)
- Heap Dump 분석으로 메모리 누수 특정

## 조치 우선순위
1. 영향 받는 서버 트래픽 격리 (로드밸런서에서 제외)
2. 에러 로그 패턴으로 원인 분류 (1~2분 내)
3. 원인에 맞는 조치 적용
4. 모니터링 후 트래픽 복구

## 에스컬레이션 기준
- 전체 서버(was01, was02) 동시 장애 → 즉시 에스컬레이션
- 원인 불명 상태로 10분 경과 → 팀장 에스컬레이션
- 결제/계좌이체 API 포함 시 → 즉시 비즈니스 담당 통보
