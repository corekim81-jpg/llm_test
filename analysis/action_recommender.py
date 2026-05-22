"""
analysis/action_recommender.py — 조치 추천 엔진
────────────────────────────────────────────────
역할: 에러 분류 결과 + RCA 결과를 바탕으로
      단계별 조치 플랜을 생성하고 위험도를 평가.

조치 플랜 구조:
    - immediate  : 지금 당장 할 것 (서비스 복구 최우선)
    - short_term : 금일~금주 안에 할 것 (재발 방지)
    - monitor    : 조치 후 관찰할 메트릭/로그
    - risk       : "low" | "medium" | "high"
    - approval   : Human 승인 필요 여부

독립 실행:
    python -m monitoring_llm.analysis.action_recommender
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("monitoring_llm.analysis")

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "../.."))
from monitoring_llm.llm_factory import build_chat_llm


# ── Runbook 사전 ──────────────────────────────────────────────────────
# 키: 에러 패턴명 (error_classifier.py의 ErrorItem.code 와 매핑)
RUNBOOKS: dict[str, dict] = {

    "OutOfMemoryError": {
        "trigger":    "JVM Heap 부족 (OOM)",
        "immediate":  [
            "jstack -l <pid> 으로 스레드 덤프 수집 (증거 보전)",
            "jmap -heap <pid> 으로 힙 현황 확인",
            "트래픽 일부를 was02로 전환 후 WAS 재시작",
        ],
        "short_term": [
            "jmap -dump:format=b,file=heap.hprof <pid> 후 Eclipse MAT 분석",
            "JVM -Xmx 힙 설정 증설 (현재 값 + 50% 권장)",
            "메모리 누수 코드 리뷰 (static 컬렉션, 캐시 TTL 확인)",
            "GC 로그 활성화 (-Xlog:gc:file=/var/log/gc.log)",
        ],
        "monitor":    [
            "jvm_memory_bytes_used{area='heap'}",
            "jvm_gc_collection_seconds_sum",
            "jvm_threads_current",
        ],
        "risk":       "medium",
        "approval":   True,   # 재시작은 Human 승인 필요
    },

    "GC overhead": {
        "trigger":    "GC 과부하 (CPU 98% 이상 GC 소모)",
        "immediate":  [
            "GC 로그 확인: grep 'GC overhead' <gc_log>",
            "힙 덤프 수집 후 WAS 재시작",
        ],
        "short_term": [
            "G1GC 튜닝 (-XX:MaxGCPauseMillis=200 조정)",
            "힙 증설 또는 메모리 누수 수정",
            "객체 생성이 많은 코드 경로 프로파일링",
        ],
        "monitor":    ["jvm_gc_collection_seconds_sum", "jvm_memory_bytes_used"],
        "risk":       "medium",
        "approval":   True,
    },

    "500": {
        "trigger":    "HTTP 500 Internal Server Error",
        "immediate":  [
            "WAS 로그에서 스택트레이스 확인: grep -A20 'ERROR' catalina.out",
            "최근 배포 이력 확인 (롤백 여부 검토)",
            "DB 연결 풀 상태 확인: SHOW STATUS LIKE 'Threads_connected'",
        ],
        "short_term": [
            "에러 발생 코드 경로 수정 및 재배포",
            "DB 연결 풀 설정 최적화 (maxActive, minIdle)",
            "에러 핸들링 강화 (적절한 예외 처리 추가)",
        ],
        "monitor":    [
            "rate(tomcat_errorcount_total[1m])",
            "mysql_global_status_threads_connected",
        ],
        "risk":       "medium",
        "approval":   False,
    },

    "502": {
        "trigger":    "HTTP 502 Bad Gateway (Web→WAS AJP 실패)",
        "immediate":  [
            "web → was AJP 포트(8009) telnet 연결 테스트",
            "Tomcat 프로세스 상태 확인: ps -ef | grep tomcat",
            "Apache mod_proxy_ajp 오류 로그 확인",
        ],
        "short_term": [
            "AJP Secret 설정 통일 (Tomcat 9+ 필수)",
            "Apache worker 설정 검토 (ProxyPass retry=0)",
            "L4 헬스체크 임계값 조정",
        ],
        "monitor":    ["apache_workers{state='busy'}", "tomcat_threads_current"],
        "risk":       "high",
        "approval":   True,
    },

    "503": {
        "trigger":    "HTTP 503 Service Unavailable",
        "immediate":  [
            "WAS 스레드 풀 현황 확인: jstack | grep -c 'http-'",
            "디스크 여유 공간 확인: df -h",
            "WAS 연결 대기 큐 크기 확인 (Tomcat acceptCount)",
        ],
        "short_term": [
            "maxThreads 증설 (현재 + 50, 단 CPU 과부하 확인 선행)",
            "디스크 풀이면 logrotate 강제 실행",
            "slow 요청 타임아웃 설정으로 스레드 점유 해소",
        ],
        "monitor":    [
            "jvm_threads_current",
            "node_filesystem_avail_bytes{mountpoint='/'}",
        ],
        "risk":       "high",
        "approval":   True,
    },

    "1213": {
        "trigger":    "MySQL Deadlock",
        "immediate":  [
            "SHOW ENGINE INNODB STATUS 로 deadlock 상세 확인",
            "연관 트랜잭션 KILL: KILL QUERY <thread_id>",
            "deadlock 발생 테이블 확인",
        ],
        "short_term": [
            "트랜잭션 내 테이블 접근 순서 통일",
            "불필요한 트랜잭션 범위 축소",
            "innodb_deadlock_detect 활성화 확인",
        ],
        "monitor":    [
            "rate(mysql_global_status_innodb_deadlocks[1m])",
            "mysql_global_status_threads_running",
        ],
        "risk":       "medium",
        "approval":   False,
    },

    "DB_SlowQuery": {
        "trigger":    "DB Slow Query",
        "immediate":  [
            "SHOW PROCESSLIST 로 현재 실행 중인 쿼리 확인",
            "장시간 쿼리 강제 종료: KILL QUERY <thread_id>",
        ],
        "short_term": [
            "slow_query_log 분석: mysqldumpslow -s t /var/log/mysql/slow.log",
            "EXPLAIN 으로 문제 쿼리 실행 계획 확인",
            "누락 인덱스 추가",
            "InnoDB buffer pool 히트율 점검 (목표 95% 이상)",
        ],
        "monitor":    [
            "rate(mysql_global_status_slow_queries[1m])",
            "mysql_global_status_innodb_buffer_pool_reads",
        ],
        "risk":       "low",
        "approval":   False,
    },

    "No space left": {
        "trigger":    "디스크 풀",
        "immediate":  [
            "df -h 로 전체 파티션 확인",
            "logrotate -f 강제 실행으로 로그 압축",
            "du -sh /var/log/* | sort -rh | head -20 으로 대용량 파일 탐색",
            "오래된 힙 덤프 파일 삭제: find / -name '*.hprof' -mtime +7 -delete",
        ],
        "short_term": [
            "logrotate 설정 rotate 주기 단축 (weekly → daily)",
            "모니터링 알람 임계값 조정 (80% 경고, 90% 위험)",
            "디스크 증설 요청",
        ],
        "monitor":    [
            "node_filesystem_avail_bytes{mountpoint='/'}",
            "node_filesystem_avail_bytes{mountpoint='/var'}",
        ],
        "risk":       "high",
        "approval":   True,
    },

    "Connection refused": {
        "trigger":    "연결 거부 (AJP / DB / 외부 API)",
        "immediate":  [
            "대상 서비스 프로세스 상태 확인",
            "포트 개방 여부 확인: netstat -tlnp | grep <port>",
            "방화벽 규칙 확인: iptables -L -n | grep <port>",
        ],
        "short_term": [
            "서비스 자동 재시작 설정 (systemd Restart=always)",
            "헬스체크 엔드포인트 추가",
            "네트워크 방화벽 규칙 정비",
        ],
        "monitor":    ["up{job='<target_job>'}"],
        "risk":       "high",
        "approval":   True,
    },

    "Read timed out": {
        "trigger":    "읽기 타임아웃",
        "immediate":  [
            "네트워크 지연 측정: ping + traceroute <target>",
            "대상 서비스 응답시간 모니터링",
        ],
        "short_term": [
            "타임아웃 임계값 재검토 (너무 짧은 경우 증설)",
            "서킷브레이커 패턴 도입 검토",
            "커넥션 풀 및 타임아웃 설정 최적화",
        ],
        "monitor":    ["rate(http_server_requests_seconds_bucket[1m])"],
        "risk":       "medium",
        "approval":   False,
    },
}

# 트리거 키워드 → Runbook 키 매핑 (에러 분류기 패턴명과 연결)
PATTERN_TO_RUNBOOK: dict[str, str] = {
    "OOM":          "OutOfMemoryError",
    "GC_OVERHEAD":  "GC overhead",
    "HTTP_500":     "500",
    "HTTP_502":     "502",          # ← 이 한 줄만 추가
    "HTTP_503":     "503",
    "DB_ConnFail":  "Connection refused",
    "DB_Deadlock":  "1213",
    "DB_SlowQuery": "DB_SlowQuery",
    "AJP_Error":    "502",
    "Disk_Full":    "No space left",
    "ConnRefused":  "Connection refused",
    "Timeout":      "Read timed out",
}


# ── 조치 플랜 구조체 ──────────────────────────────────────────────────
@dataclass
class ActionPlan:
    trigger: str
    immediate: list[str]    = field(default_factory=list)
    short_term: list[str]   = field(default_factory=list)
    monitor: list[str]      = field(default_factory=list)
    risk: str               = "medium"   # "low" | "medium" | "high"
    approval_required: bool = False

    def to_markdown(self) -> str:
        risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(self.risk, "⚪")
        lines = [
            f"### {risk_icon} {self.trigger}",
            f"위험도: **{self.risk.upper()}**"
            + (" ⚠️ **Human 승인 필요**" if self.approval_required else ""),
            "",
            "**즉시 조치:**",
        ]
        for i, a in enumerate(self.immediate, 1):
            lines.append(f"  {i}. {a}")
        lines += ["", "**단기 개선:**"]
        for i, a in enumerate(self.short_term, 1):
            lines.append(f"  {i}. {a}")
        lines += ["", "**모니터링 지표:**"]
        for m in self.monitor:
            lines.append(f"  - `{m}`")
        return "\n".join(lines)


# ── 핵심 추천 함수 ────────────────────────────────────────────────────
def recommend(
    error_patterns: list[str],
    rca_result: Optional[dict] = None,
    use_llm: bool = False,
) -> list[ActionPlan]:
    """
    에러 패턴 목록 → 우선순위 정렬된 ActionPlan 목록.

    use_llm=True 이면 LLM으로 상황에 맞게 조치 내용을 조정.
    """
    plans: list[ActionPlan] = []
    matched_keys: set[str] = set()

    # 패턴 → Runbook 매칭
    for pat in error_patterns:
        rb_key = PATTERN_TO_RUNBOOK.get(pat)
        if not rb_key:
            # 직접 코드로 매칭 시도 (예: "500", "1213")
            rb_key = pat if pat in RUNBOOKS else None
        if rb_key and rb_key not in matched_keys:
            matched_keys.add(rb_key)
            rb = RUNBOOKS[rb_key]
            plans.append(ActionPlan(
                trigger           = rb["trigger"],
                immediate         = list(rb["immediate"]),
                short_term        = list(rb["short_term"]),
                monitor           = list(rb["monitor"]),
                risk              = rb["risk"],
                approval_required = rb.get("approval", False),
            ))

    if not plans:
        plans.append(ActionPlan(
            trigger   = "일반 장애 대응",
            immediate = [
                "서비스 상태 확인: curl -I http://<endpoint>/health",
                "담당팀 에스컬레이션",
                "최근 배포·변경 이력 점검",
            ],
            short_term= ["모니터링 대시보드 강화", "알람 임계값 검토"],
            monitor   = ["up", "http_server_requests_seconds_count"],
            risk      = "medium",
        ))

    # 위험도 높은 순으로 정렬
    risk_order = {"high": 0, "medium": 1, "low": 2}
    plans.sort(key=lambda p: risk_order.get(p.risk, 1))

    # if use_llm and plans:
    if use_llm and matched_keys:
        plans = _llm_refine(plans, error_patterns, rca_result)

    return plans


def to_markdown_report(plans: list[ActionPlan]) -> str:
    """ActionPlan 목록 → 마크다운 보고서"""
    lines = ["## 조치 플랜\n"]
    for plan in plans:
        lines.append(plan.to_markdown())
        lines.append("")
    approval_items = [p.trigger for p in plans if p.approval_required]
    if approval_items:
        lines += [
            "---",
            "⚠️ **운영자 승인 필요 항목:**",
            *[f"  - {item}" for item in approval_items],
        ]
    return "\n".join(lines)


# ── LLM 조정 ─────────────────────────────────────────────────────────
# def _llm_refine(
#     plans: list[ActionPlan],
#     patterns: list[str],
#     rca_result: Optional[dict],
# ) -> list[ActionPlan]:
#     try:
#         from langchain_ollama import ChatOllama
#         from langchain_core.messages import HumanMessage, SystemMessage

#         context = to_markdown_report(plans)
#         rca_ctx = json.dumps(rca_result, ensure_ascii=False) if rca_result else "없음"

#         llm = ChatOllama(
#             model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL,
#             temperature=0.2, num_predict=1500, num_ctx=4096,
#         )
#         prompt = (
#             f"[현재 장애 상황]\n에러 패턴: {patterns}\nRCA 결과: {rca_ctx}\n\n"
#             f"[기본 조치 플랜]\n{context}\n\n"
#             "위 정보를 바탕으로 BankSystem_16 환경에 맞게 조치 플랜을 보완하세요.\n"
#             "특히 즉시 조치 항목을 구체적인 명령어 수준으로 상세화하고,\n"
#             "현재 상황과 무관한 항목은 제거하세요.\n"
#             "형식은 기존 마크다운을 유지하세요."
#         )
#         resp = llm.invoke([
#             SystemMessage(content="/no_think\n당신은 BankSystem_16 운영 전문가입니다. 실행 가능한 구체적 조치를 한국어로 제시하세요."),
#             HumanMessage(content=prompt),
#         ])
#         # LLM 응답을 첫 번째 플랜의 즉시 조치에 추가 (원본 유지)
#         plans[0].immediate.insert(0, f"[LLM 보완] {resp.content[:200]}")
#     except Exception as e:
#         log.warning(f"조치 LLM 보완 실패: {e}")
#     return plans

def _llm_refine(
    plans: list[ActionPlan],
    patterns: list[str],
    rca_result: Optional[dict],
) -> list[ActionPlan]:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        context = to_markdown_report(plans)
        rca_ctx = json.dumps(rca_result, ensure_ascii=False) if rca_result else "없음"

        llm = build_chat_llm(temperature=0.2, max_tokens=1500, num_ctx=4096)

        prompt = (
            f"[현재 장애 상황]\n에러 패턴: {patterns}\nRCA 결과: {rca_ctx}\n\n"
            f"[기본 조치 플랜]\n{context}\n\n"
            "위 정보를 바탕으로 BankSystem_16 환경에 맞게 조치 플랜을 보완하세요.\n"
            "즉시 조치 항목을 구체적인 명령어 수준으로 상세화하고,\n"
            "현재 상황과 무관한 항목은 제거하세요.\n\n"
            "반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.\n"
            "[\n"
            "  {\n"
            '    "trigger": "트리거 설명",\n'
            '    "immediate": ["즉시조치1", "즉시조치2"],\n'
            '    "short_term": ["단기개선1", "단기개선2"],\n'
            '    "monitor": ["지표1", "지표2"],\n'
            '    "risk": "high|medium|low",\n'
            '    "approval_required": true\n'
            "  }\n"
            "]"
        )

        resp = llm.invoke([
            SystemMessage(content="/no_think\n당신은 BankSystem_16 운영 전문가입니다. 실행 가능한 구체적 조치를 한국어로 제시하세요."),
            HumanMessage(content=prompt),
        ])

        # JSON 파싱 후 ActionPlan 전체 업데이트
        raw = resp.content.strip().removeprefix("```json").removesuffix("```").strip()
        refined = json.loads(raw)

        updated: list[ActionPlan] = []
        for i, item in enumerate(refined):
            # LLM 응답 수가 기존 plans보다 적을 수 있으니 원본으로 fallback
            base = plans[i] if i < len(plans) else plans[-1]
            updated.append(ActionPlan(
                trigger           = item.get("trigger",           base.trigger),
                immediate         = item.get("immediate",         base.immediate),
                short_term        = item.get("short_term",        base.short_term),
                monitor           = item.get("monitor",           base.monitor),
                risk              = item.get("risk",              base.risk),
                approval_required = item.get("approval_required", base.approval_required),
            ))
        return updated if updated else plans

    except json.JSONDecodeError as e:
        log.warning(f"LLM 응답 JSON 파싱 실패: {e}\n원본 응답: {resp.content[:300]}")
        return plans  # 파싱 실패 시 원본 플랜 반환
    except Exception as e:
        log.warning(f"조치 LLM 보완 실패: {e}")
        return plans


# ── 독립 실행 테스트 ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("조치 추천 테스트 (LLM 없이)\n" + "="*50)

    scenarios = [
        ["OOM", "HTTP_500"],
        ["DB_Deadlock", "DB_SlowQuery"],
        ["Disk_Full"],
        ["AJP_Error", "HTTP_503"],
    ]

    for patterns in scenarios:
        print(f"\n패턴: {patterns}")
        plans = recommend(patterns, use_llm=True)
        for plan in plans:
            risk_icon = {"low":"🟢","medium":"🟡","high":"🔴"}.get(plan.risk,"⚪")
            appr = "⚠️ 승인필요" if plan.approval_required else ""
            print(f"  {risk_icon} {plan.trigger} [{plan.risk}] {appr}")
            print(f"    즉시: {plan.immediate[0]}")

    print("\n" + "="*50)
    print("마크다운 보고서 샘플 (OOM + 500):")
    plans = recommend(["OOM", "HTTP_500"], use_llm=False)
    print(to_markdown_report(plans)[:800])
