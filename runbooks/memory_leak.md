# 메모리 누수 / OOM 대응 Runbook

## 개요
WAS 서버(was01, was02)에서 JVM 힙 메모리가 지속적으로 증가하거나 OutOfMemoryError가 발생할 때의 대응 절차.

## 감지 조건
- JVM Heap 사용률 85% 이상 지속
- Prometheus Alert: `JvmHeapHighUsage`
- Full GC 이후에도 메모리가 회수되지 않는 패턴

## 메모리 현황 확인

### Prometheus 쿼리
```promql
# Heap 사용률
jvm_memory_used_bytes{area="heap", instance=~"was01.*"} / jvm_memory_max_bytes{area="heap", instance=~"was01.*"} * 100

# GC 빈도
rate(jvm_gc_pause_seconds_count[5m])

# GC 이후 메모리 회수량
jvm_memory_used_bytes{area="heap"} - jvm_memory_committed_bytes{area="heap"}
```

### Loki 로그 확인
```logql
{job=~"was01|was02"} |= "OutOfMemoryError"
{job=~"was01|was02"} |= "GC overhead limit exceeded"
```

## 긴급 조치 (서비스 영향 최소화)

### 1단계: 트래픽 전환
```
was01 장애 시 → 로드밸런서에서 was01 제거, was02로 트래픽 집중
was02 장애 시 → 로드밸런서에서 was02 제거, was01로 트래픽 집중
```

### 2단계: Heap Dump 수집 (원인 분석용)
JVM 재시작 전 반드시 수집한다.

```bash
# PID 확인
jcmd | grep java

# Heap Dump 생성 (was01, was02 - Windows)
jcmd <PID> GC.heap_dump C:\logs\heap_dump_was01.hprof

# 또는
jmap -dump:format=b,file=C:\logs\heap_dump.hprof <PID>
```

### 3단계: JVM 재시작
```powershell
# Windows Service 재시작 (was01, was02)
Restart-Service -Name "BankApp-WAS"

# 또는 직접 프로세스 재시작
Stop-Process -Name "java" -Force
Start-Process "C:\app\start.bat"
```

## Heap Dump 분석 절차

### Eclipse MAT(Memory Analyzer Tool) 사용
1. Heap Dump 파일(.hprof) 다운로드
2. Eclipse MAT 실행 → File → Open Heap Dump
3. Leak Suspects Report 확인
4. Dominator Tree에서 메모리 점유 객체 확인

### 일반적인 메모리 누수 패턴
| 패턴 | 원인 | 해결책 |
|------|------|--------|
| `HashMap` 무한 증가 | 캐시 TTL 미설정 | Caffeine/Guava Cache로 교체 |
| `HttpClient` 인스턴스 누적 | 매 요청마다 신규 생성 | 싱글톤 또는 풀 방식으로 변경 |
| `ThreadLocal` 미정리 | remove() 호출 누락 | finally 블록에서 remove() 추가 |
| Listener 등록 후 해제 안 함 | EventBus, Observer 패턴 | WeakReference 또는 명시적 해제 |

## JVM 메모리 설정 조정

### 임시 조치 (힙 증설)
```
-Xms2g -Xmx4g
-XX:+UseG1GC
-XX:MaxGCPauseMillis=200
```

### 메모리 덤프 자동 생성 설정
```
-XX:+HeapDumpOnOutOfMemoryError
-XX:HeapDumpPath=C:\logs\
```

## 에스컬레이션 기준
- was01, was02 동시 OOM → 즉시 에스컬레이션
- 재시작 후 2시간 내 재발 → 개발팀 긴급 투입
- Heap Dump 분석 후 코드 수정 필요 → 개발팀 핫픽스 요청
