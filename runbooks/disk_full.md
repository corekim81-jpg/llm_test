# 디스크 용량 부족 대응 Runbook

## 개요
서버 디스크 사용률이 임계치를 초과할 때의 대응 절차. 로그 누적이 주요 원인.

## 감지 조건
- 디스크 사용률 80% 이상
- Prometheus Alert: `DiskSpaceWarning` (80%), `DiskSpaceCritical` (90%)

## 현황 확인

### Prometheus 쿼리
```promql
# 디스크 사용률 (전체 서버)
(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100

# 남은 용량 (GB)
node_filesystem_avail_bytes{mountpoint="/"} / 1024 / 1024 / 1024
```

### 서버 직접 확인

**Linux (web01, web02):**
```bash
df -h
du -sh /logs/* | sort -rh | head -20
du -sh /var/log/* | sort -rh | head -20
```

**Windows (was01, was02, db01, db02):**
```powershell
Get-PSDrive -PSProvider FileSystem
Get-ChildItem C:\logs -Recurse | Sort-Object Length -Descending | Select-Object -First 20 FullName, Length
```

## 즉시 조치

### 로그 파일 정리

**WAS 로그 (was01, was02 - Windows):**
```powershell
# 30일 이상 된 로그 삭제
Get-ChildItem "C:\logs" -Filter "*.log" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force

# GC 로그 압축
Compress-Archive -Path "C:\logs\gc*.log" -DestinationPath "C:\logs\archive\gc_$(Get-Date -Format yyyyMMdd).zip"
```

**웹 로그 (web01, web02 - Linux):**
```bash
# 30일 이상 된 로그 삭제
find /logs -name "*.log" -mtime +30 -delete

# 로그 압축
gzip /logs/access_$(date -d '7 days ago' +%Y%m%d).log
```

**DB 바이너리 로그 (db01, db02):**
```sql
-- 바이너리 로그 현황
SHOW BINARY LOGS;

-- 7일 이상 된 바이너리 로그 삭제
PURGE BINARY LOGS BEFORE DATE_SUB(NOW(), INTERVAL 7 DAY);
```

### Heap Dump / Thread Dump 정리
장애 분석 후 남은 대용량 덤프 파일 삭제.
```powershell
# was01, was02
Get-ChildItem "C:\logs" -Filter "*.hprof" | Remove-Item -Force
Get-ChildItem "C:\logs" -Filter "threaddump*.txt" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Force
```

## 로그 로테이션 설정 확인

### Logback 설정 확인 (WAS)
`logback-spring.xml`에 RollingFileAppender와 보관 정책이 설정되어 있는지 확인.

```xml
<rollingPolicy class="ch.qos.logback.core.rolling.TimeBasedRollingPolicy">
    <fileNamePattern>/logs/app-%d{yyyy-MM-dd}.log</fileNamePattern>
    <maxHistory>14</maxHistory>       <!-- 14일 보관 -->
    <totalSizeCap>5GB</totalSizeCap>  <!-- 전체 로그 최대 5GB -->
</rollingPolicy>
```

### Promtail / Alloy 로그 설정 확인
수집 완료된 로그를 주기적으로 삭제하도록 logrotate 설정 확인.

```bash
# web01, web02 - logrotate 설정 확인
cat /etc/logrotate.d/app
logrotate -dv /etc/logrotate.d/app  # 드라이런 테스트
```

## 에스컬레이션 기준
- 90% 초과 → 즉각 팀장 보고 + 모든 서비스 로그 레벨 WARN으로 임시 변경
- 95% 초과 → 서비스 로깅 일시 중단 검토
- DB 서버 디스크 부족 → 즉시 DBA 에스컬레이션 (데이터 손실 위험)
