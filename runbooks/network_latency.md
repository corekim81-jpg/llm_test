# 네트워크 지연 / 연결 장애 대응 Runbook

## 개요
서버 간 네트워크 지연 또는 연결 실패가 발생할 때의 대응 절차.
주로 web → was, was → db 구간에서 발생.

## 감지 조건
- HTTP 요청 응답시간 P95 기준 3초 이상
- Prometheus Alert: `HighRequestLatency`
- Jaeger/Tempo: 특정 span 구간 지연 확인

## 구간별 지연 확인

### web → was 구간
```promql
# WAS HTTP 응답시간 P95
histogram_quantile(0.95, rate(http_server_requests_seconds_bucket{instance=~"was01.*"}[5m]))
```

```logql
# Nginx Access Log에서 응답시간 확인
{job="web01"} | pattern `<ip> - - [<_>] "<method> <uri> <_>" <status> <bytes> "<_>" "<_>" <rt>`
| rt > 1
```

### was → db 구간
```promql
# DB 쿼리 응답시간
histogram_quantile(0.95, rate(jdbc_connections_pending_bucket{instance=~"was01.*"}[5m]))
```

### Jaeger/Tempo 트레이스 분석
특정 traceID로 전체 호출 구간의 span 지연을 시각화해 병목 구간 특정.

## 원인 분류

### 원인 1: 특정 API 엔드포인트 지연
일부 API만 느리고 나머지는 정상인 경우.

**확인 방법:**
```promql
# URI별 응답시간 비교
histogram_quantile(0.95, sum(rate(http_server_requests_seconds_bucket[5m])) by (uri, le))
```

**조치:**
- 해당 API의 DB 쿼리 실행계획 확인 (EXPLAIN)
- 외부 API 호출 타임아웃 설정 확인

### 원인 2: 전체적인 응답 지연 (트래픽 폭증)
모든 API가 느려지고 CPU/메모리도 함께 상승하는 경우.

**확인 방법:**
```promql
# 초당 요청 수 추이
sum(rate(http_server_requests_seconds_count[5m])) by (instance)
```

**조치:**
- 로드밸런서에서 트래픽 조절
- was01/was02 인스턴스 추가 (스케일 아웃)
- 불필요한 배치 작업 일시 중단

### 원인 3: WAS-DB 구간 지연
was → db 구간만 느린 경우.

**확인 방법:**
- Jaeger: was01에서 db01으로의 JDBC span 확인
- db01 서버 직접 확인 (슬로우 쿼리 Runbook 참조)

**조치:**
- DB 슬로우 쿼리 대응 Runbook 참조
- 커넥션 풀 설정 확인

### 원인 4: 네트워크 장비 문제
```bash
# web01에서 was01로 ping 테스트
ping was01 -c 10

# 경로 추적
traceroute was01

# 패킷 손실 확인
mtr was01 --report
```

**조치:**
- 네트워크 담당자에게 스위치/라우터 확인 요청
- 임시 조치: 영향 받는 서버 트래픽 우회

## 응답시간 SLO 기준

| 구간 | 정상 | 경고 | 위험 |
|------|------|------|------|
| web → was | < 500ms | 500ms~2s | > 2s |
| was → db | < 100ms | 100ms~500ms | > 500ms |
| 전체 E2E | < 1s | 1s~3s | > 3s |

## 에스컬레이션 기준
- E2E 응답시간 5초 이상 10분 지속 → 팀장 에스컬레이션
- 네트워크 장비 이상 의심 → 인프라팀 즉시 연락
- 외부 서비스(결제망 등) 연동 지연 → 해당 업체 연락
