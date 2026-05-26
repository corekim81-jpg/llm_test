# DB 슬로우 쿼리 / 장애 대응 Runbook

## 개요
DB 서버(db01, db02)에서 슬로우 쿼리 발생 또는 DB 연결 장애 시 대응 절차.

## 감지 조건
- 쿼리 응답시간 평균 1초 이상
- Prometheus Alert: `MySQLSlowQueries`, `MySQLConnectionHigh`
- WAS 로그에서 `HikariPool-1 - Connection is not available` 오류

## 현황 확인

### Prometheus 쿼리
```promql
# 슬로우 쿼리 증가율
rate(mysql_global_status_slow_queries[5m])

# DB 커넥션 현황
mysql_global_status_threads_connected
mysql_global_status_threads_running

# InnoDB 잠금 대기
mysql_global_status_innodb_row_lock_waits
```

### MySQL 직접 확인 (db01, db02)
```sql
-- 현재 실행 중인 쿼리
SHOW PROCESSLIST;
SHOW FULL PROCESSLIST;

-- 슬로우 쿼리 확인
SELECT * FROM information_schema.PROCESSLIST
WHERE TIME > 5
ORDER BY TIME DESC;

-- InnoDB 잠금 정보
SELECT * FROM information_schema.INNODB_TRX;
SELECT * FROM information_schema.INNODB_LOCK_WAITS;
```

## 원인 분류

### 원인 1: 풀 테이블 스캔 (인덱스 미사용)
대용량 테이블에 인덱스 없이 WHERE 조건으로 조회하는 쿼리.

**확인 방법:**
```sql
EXPLAIN SELECT ... ;  -- type=ALL 이면 풀 스캔
```

**조치:**
```sql
-- 인덱스 추가
ALTER TABLE 테이블명 ADD INDEX idx_컬럼명 (컬럼명);
-- 즉각 적용 (Online DDL)
ALTER TABLE 테이블명 ADD INDEX idx_컬럼명 (컬럼명), ALGORITHM=INPLACE, LOCK=NONE;
```

### 원인 2: 락 경합 (Lock Contention)
동일 row에 대한 UPDATE/DELETE 경합으로 트랜잭션 대기 발생.

**확인 방법:**
```sql
-- 잠금 보유 트랜잭션 확인
SELECT trx_id, trx_state, trx_started, trx_query
FROM information_schema.INNODB_TRX
WHERE trx_state = 'LOCK WAIT';
```

**조치:**
```sql
-- 블로킹 쿼리 강제 종료
KILL <thread_id>;
```

### 원인 3: 커넥션 풀 고갈
WAS의 DB 커넥션이 모두 사용 중이어서 신규 요청이 대기하는 상태.

**확인 방법:**
- Loki: `{job="was01"} |= "HikariPool" |= "Connection is not available"`
- Prometheus: `hikaricp_connections_active` vs `hikaricp_connections_max`

**조치:**
1. 슬로우 쿼리로 커넥션 점유 시 해당 쿼리 KILL
2. WAS 재시작으로 커넥션 풀 초기화
3. `application.yml` 커넥션 풀 크기 증가:
```yaml
spring:
  datasource:
    hikari:
      maximum-pool-size: 50
      connection-timeout: 30000
```

### 원인 4: DB 서버 자원 부족
db01 또는 db02의 CPU/메모리/디스크 I/O 포화.

**확인 방법:**
```promql
# DB 서버 CPU
100 - avg(rate(node_cpu_seconds_total{mode="idle", instance=~"db01.*"}[5m])) * 100

# 디스크 I/O 대기
rate(node_disk_io_time_seconds_total{instance=~"db01.*"}[5m])
```

**조치:**
- CPU 과부하 → 슬로우 쿼리 제거, 쿼리 캐시 활성화
- 디스크 I/O 포화 → innodb_buffer_pool_size 증가로 디스크 접근 감소
- 메모리 부족 → 불필요한 프로세스 정리

## HA 페일오버 (db01 완전 장애 시)

db01이 완전히 응답 불가 상태일 경우 db02(Replica)를 Primary로 승격.

```sql
-- db02에서 실행
STOP REPLICA;
RESET REPLICA ALL;

-- WAS 설정에서 DB 연결 대상을 db02로 변경 후 재시작
```

## 에스컬레이션 기준
- db01, db02 동시 장애 → 즉시 에스컬레이션 (DBA + 팀장)
- 데이터 손실 가능성 → 즉시 에스컬레이션
- 슬로우 쿼리 원인 불명으로 30분 이상 지속 → DBA 에스컬레이션
