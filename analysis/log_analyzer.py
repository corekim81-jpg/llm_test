"""
analysis/log_analyzer.py — 로그 분석기
────────────────────────────────────────
역할: Loki Tool이 반환한 raw 로그를 LLM에 넘기기 전에
      룰 기반으로 핵심만 추려 토큰을 절약하고,
      LLM으로 원인·패턴을 자연어 설명으로 변환.

흐름:
    Loki JSON (raw)
     → preprocess()      룰 기반: 중복제거·스택추출·패턴집계
     → to_prompt_text()  LLM 입력용 압축 텍스트 생성
     → explain()         LLM 자연어 해석 (선택)

독립 실행:
    python -m monitoring_llm.analysis.log_analyzer


변경 이력:
    [FIX] 정규식 모듈 레벨 사전 컴파일 (ERROR_PATTERNS_COMPILED)
    [FIX] preprocess() 루프 종료 후 미flush 스택트레이스 처리
    [FIX] 이중 포맷(key_logs) 진입 시 total_logs 이중집계 방지
          → 루프 내 레벨/TraceID 재집계 스킵, level_counts 덮어쓰기 방지
    [FIX] explain() 내 ChatOllama 인스턴스 싱글톤 캐싱
    [FIX] explain() 예외를 연결실패 / 모델오류로 구분 처리
    [개선] to_prompt_text() 피크 시각 TOP3 전달
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("monitoring_llm.analysis")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


# ── BankSystem_16 환경 기준 에러 패턴 사전 ───────────────────────────
ERROR_PATTERNS: dict[str, str] = {
    "OOM": r"OutOfMemoryError|java\.lang\.OutOfMemory|GC overhead|heap space",
    "NPE": r"NullPointerException|NullPointer|\.NPE",
    "AJP_Error": r"AJP.*(?:error|fail|timeout)|ajp.*(?:refused|timeout)",
    "DB_ConnFail": r"Can't connect to MySQL|MySQL.*timeout|Too many connections|JDBC.*fail",
    "DB_Deadlock": r"Deadlock found|deadlock|Lock wait timeout",
    "DB_SlowQuery": r"slow query|Query.*\d{4,}ms|long running",
    "HTTP_500": r"HTTP/\d\.\d\s+500|\bstatus[=:\s]+500\b|\" 500 ",
    "HTTP_503": r"HTTP/\d\.\d\s+503|\bstatus[=:\s]+503\b|Service Unavailable",
    "Disk_Full": r"No space left|disk.*full|ENOSPC",
    "ConnRefused": r"Connection refused|ECONNREFUSED|connect.*failed",
    "Timeout": r"(?:Read|Write|Socket|Connect)\s*timed?\s*out|ETIMEDOUT",
    "ThreadPoolExhaust": r"ThreadPoolExecutor|thread.*pool.*full|max.*threads.*reached",
    "ClassNotFound": r"ClassNotFoundException|NoClassDefFoundError",
    "PermissionDenied": r"Permission denied|Access denied.*file|EACCES",
}

# [FIX] 정규식을 모듈 로드 시점에 한 번만 컴파일 → 반복 호출 성능 개선
ERROR_PATTERNS_COMPILED: dict[str, re.Pattern] = {
    k: re.compile(v, re.IGNORECASE) for k, v in ERROR_PATTERNS.items()
}

# 스택트레이스 패턴 (Java 기준)
STACK_LINE_RE = re.compile(r"^\s+at\s+[\w.$<>]+\(")
CAUSED_BY_RE = re.compile(r"^Caused by:")
EXCEPTION_RE = re.compile(r"\b\w+Exception\b|\b\w+Error\b")
TRACE_ID_RE = re.compile(
    r"(?:traceId|trace_id|TraceID)[=:\s]+([0-9a-f]{16,32})", re.IGNORECASE
)
LEVEL_RE = re.compile(r"\b(ERROR|WARN|INFO|DEBUG|FATAL|TRACE)\b")

# ── LLM 싱글톤 캐시 ──────────────────────────────────────────────────
# [FIX] explain() 호출마다 ChatOllama 인스턴스를 새로 생성하던 문제 해결
_llm_instance: Optional[object] = None


def _get_llm():
    """ChatOllama 인스턴스를 최초 1회만 생성 후 재사용."""
    global _llm_instance
    if _llm_instance is None:
        from langchain_ollama import ChatOllama

        _llm_instance = ChatOllama(
            model=OLLAMA_MODEL,
            base_url=OLLAMA_BASE_URL,
            temperature=0.1,
            num_predict=1000,
            num_ctx=4096,
        )
    return _llm_instance


# ── 분석 결과 구조체 ──────────────────────────────────────────────────
@dataclass
class LogAnalysisResult:
    total_logs: int = 0
    unique_logs: int = 0
    level_counts: dict = field(default_factory=dict)
    patterns: dict = field(default_factory=dict)  # {패턴명: 발생횟수}
    key_logs: list = field(default_factory=list)  # [{time, level, log}]
    stack_traces: list = field(default_factory=list)  # [["at ...", ...]]
    trace_ids: list = field(default_factory=list)  # Jaeger TraceID 목록
    error_timeline: dict = field(default_factory=dict)  # {HH:MM: count}
    peak_minute: str = ""
    explanation: str = ""  # LLM 해석 (선택)

    @property
    def top_patterns(self) -> list[str]:
        return sorted(self.patterns, key=self.patterns.get, reverse=True)[:5]

    def is_empty(self) -> bool:
        return self.total_logs == 0

    def to_prompt_text(self, max_logs: int = 10) -> str:
        """LLM 프롬프트에 삽입할 압축 텍스트 (토큰 절약)"""
        parts = [
            f"로그 총 {self.total_logs}건 (중복제거 {self.unique_logs}건)",
            f"레벨별: {self.level_counts}",
        ]
        if self.patterns:
            pat_str = ", ".join(
                f"{k}×{v}"
                for k, v in sorted(self.patterns.items(), key=lambda x: -x[1])[:5]
            )
            parts.append(f"에러 패턴: {pat_str}")
        # if self.peak_minute:
        #     parts.append(f"피크 시각: {self.peak_minute} ({self.error_timeline.get(self.peak_minute, 0)}건)")

        # [개선] peak_minute 1개 → 상위 3개 시간대로 확장
        if self.error_timeline:
            top3 = sorted(self.error_timeline.items(), key=lambda x: -x[1])[:3]
            top3_str = ", ".join(f"{t}({c}건)" for t, c in top3)
            parts.append(f"피크 시각 TOP3: {top3_str}")

        if self.key_logs:
            parts.append("핵심 에러 로그:")
            for e in self.key_logs[:max_logs]:
                parts.append(f"  [{e['time']}] {e['log'][:150]}")
        if self.stack_traces:
            parts.append("스택트레이스 (상위 1개):")
            parts.extend(f"  {l}" for l in self.stack_traces[0][:6])
        if self.trace_ids:
            parts.append(f"TraceID: {self.trace_ids[:3]}")
        return "\n".join(parts)


# ── 핵심 전처리 함수 ──────────────────────────────────────────────────
def preprocess(loki_json: str | dict) -> LogAnalysisResult:
    """
    Loki Tool 결과 JSON → LogAnalysisResult.
    LLM 없이 순수 룰 기반 처리.
    """
    res = LogAnalysisResult()

    if isinstance(loki_json, str):
        try:
            data = json.loads(loki_json)
        except json.JSONDecodeError:
            log.warning("Loki JSON 파싱 실패")
            return res
    else:
        data = loki_json

    # 이미 전처리된 경우 (loki_tool.py preprocess_logs 출력)
    # [FIX] 이중 포맷 진입 시 total_logs·level_counts를 이미 확정된 값으로 설정하고
    #       루프 내 재집계를 스킵하는 플래그를 분리
    # if "key_logs" in data:
    is_preprocessed = "key_logs" in data
    if is_preprocessed:
        res.total_logs = data.get("total", 0)
        res.unique_logs = data.get("unique", res.total_logs)
        res.level_counts = data.get(
            "level_counts", {}
        )  # 이미 확정값 → 루프에서 덮어쓰지 않음
        res.key_logs = data.get("key_logs", [])
        res.trace_ids = data.get("trace_ids", [])
        raw_logs = data.get("key_logs", [])
    else:
        raw_logs = data.get("logs", [])

    if not raw_logs:
        return res

    seen_hashes: set[str] = set()
    current_stack: list[str] = []

    for entry in raw_logs:
        line = entry.get("log", "")
        t_str = entry.get("time", "")

        # # ── 레벨 집계 ────────────────────────────────────────────
        # m_lv = LEVEL_RE.search(line)
        # level = m_lv.group(1) if m_lv else "OTHER"
        # res.level_counts[level] = res.level_counts.get(level, 0) + 1

        # # ── TraceID 수집 ──────────────────────────────────────────
        # m_tid = TRACE_ID_RE.search(line)
        # if m_tid and m_tid.group(1) not in res.trace_ids:
        #     res.trace_ids.append(m_tid.group(1))

        # [FIX] 이미 전처리된 포맷은 레벨·TraceID 재집계 스킵 (이중집계 방지)
        if not is_preprocessed:
            # ── 레벨 집계 ──────────────────────────────────────────
            m_lv = LEVEL_RE.search(line)
            level = m_lv.group(1) if m_lv else "OTHER"
            res.level_counts[level] = res.level_counts.get(level, 0) + 1

            # ── TraceID 수집 ────────────────────────────────────────
            m_tid = TRACE_ID_RE.search(line)
            if m_tid and m_tid.group(1) not in res.trace_ids:
                res.trace_ids.append(m_tid.group(1))
        else:
            # 전처리 포맷: level 필드를 entry에서 직접 읽음
            level = entry.get("level", "OTHER")

        # ── 스택트레이스 감지 ──────────────────────────────────────
        if STACK_LINE_RE.match(line) or CAUSED_BY_RE.match(line):
            current_stack.append(line.strip())
        else:
            if len(current_stack) >= 2:
                res.stack_traces.append(current_stack[:8])
            current_stack = []

            # ── 중복 제거 ──────────────────────────────────────────
            norm = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "", line)
            h = hashlib.md5(norm.encode()).hexdigest()[:10]
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            # res.unique_logs += 1
            if not is_preprocessed:
                res.unique_logs += 1

            # # ── 에러 패턴 매칭 ────────────────────────────────────
            # for pat_name, pat_re in ERROR_PATTERNS.items():
            #     if re.search(pat_re, line, re.IGNORECASE):
            #         res.patterns[pat_name] = res.patterns.get(pat_name, 0) + 1

            # # ── 핵심 로그 (ERROR/FATAL) ───────────────────────────
            # if level in ("ERROR", "FATAL", "WARN"):
            #     res.key_logs.append({
            #         "time":  t_str,
            #         "level": level,
            #         "log":   line[:250],
            #     })

            if not is_preprocessed:
                res.unique_logs += 1

            # ── 에러 패턴 매칭 (컴파일된 패턴 사용) ──────────────
            # [FIX] ERROR_PATTERNS_COMPILED 사용 → re.search 재컴파일 제거
            for pat_name, pat_compiled in ERROR_PATTERNS_COMPILED.items():
                if pat_compiled.search(line):
                    res.patterns[pat_name] = res.patterns.get(pat_name, 0) + 1

            # ── 핵심 로그 (ERROR/FATAL/WARN) ──────────────────────
            # 전처리 포맷은 key_logs 이미 로드됨 → raw 포맷만 추가
            if not is_preprocessed and level in ("ERROR", "FATAL", "WARN"):
                res.key_logs.append(
                    {
                        "time": t_str,
                        "level": level,
                        "log": line[:250],
                    }
                )

            # ── 시간대별 분포 ──────────────────────────────────────
            if t_str and len(t_str) >= 5:
                minute = t_str[:5]  # "HH:MM"
                res.error_timeline[minute] = res.error_timeline.get(minute, 0) + 1

    # res.total_logs = res.total_logs or len(raw_logs)
    # if not res.unique_logs:
    #     res.unique_logs = len(seen_hashes)

    # [FIX] 루프 종료 후 미flush 스택트레이스 처리
    if len(current_stack) >= 2:
        res.stack_traces.append(current_stack[:8])

    # [FIX] total_logs: 이미 전처리된 경우 data["total"] 값 유지
    #       raw 포맷인 경우에만 len(raw_logs) 폴백
    if not is_preprocessed:
        res.total_logs = res.total_logs or len(raw_logs)
        if not res.unique_logs:
            res.unique_logs = len(seen_hashes)

    # 피크 시각
    if res.error_timeline:
        res.peak_minute = max(res.error_timeline, key=res.error_timeline.get)

    return res


# ── LLM 해석 ─────────────────────────────────────────────────────────
def explain(result: LogAnalysisResult, context: str = "") -> str:
    """
    전처리 결과를 LLM으로 해석.
    result.explanation 에 저장 후 반환.
    """
    if result.is_empty():
        return "분석할 로그가 없습니다."

    # from langchain_ollama import ChatOllama
    from langchain_core.messages import HumanMessage, SystemMessage

    # llm = ChatOllama(
    #     model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL,
    #     temperature=0.1, num_predict=1000, num_ctx=4096,
    # )
    # [FIX] 싱글톤 함수로 인스턴스 재사용
    llm = _get_llm()

    prompt = (
        f"{result.to_prompt_text()}"
        + (f"\n\n[추가 컨텍스트]\n{context}" if context else "")
        + "\n\n위 로그 데이터를 분석하여 다음을 한국어로 설명하세요:\n"
        "1. 가장 빈번한 에러 패턴과 발생 횟수\n"
        "2. 가장 심각한 로그 2~3건 (시간 포함)\n"
        "3. 스택트레이스가 있다면 핵심 원인 1줄 요약\n"
        "4. 에러가 집중된 시간대 (있는 경우)"
    )

    try:
        resp = llm.invoke(
            [
                SystemMessage(
                    content="/no_think\n당신은 IT 인프라 로그 분석 전문가입니다. 근거 데이터를 인용하며 한국어로 간결하게 답하세요."
                ),
                HumanMessage(content=prompt),
            ]
        )
        result.explanation = resp.content
    # except Exception as e:
    #     result.explanation = f"LLM 해석 실패: {e}"

    # [FIX] 예외 유형별 구분 처리
    except ConnectionError as e:
        result.explanation = f"LLM 연결 실패 (Ollama 서버 확인 필요): {e}"
        log.error("Ollama 연결 실패: %s", e)
    except TimeoutError as e:
        result.explanation = f"LLM 응답 시간 초과: {e}"
        log.error("Ollama 타임아웃: %s", e)
    except Exception as e:
        result.explanation = f"LLM 해석 실패 ({type(e).__name__}): {e}"
        log.error("LLM 해석 오류: %s", e)

    return result.explanation


# ── 편의 함수 ─────────────────────────────────────────────────────────
def analyze_loki_result(
    loki_json: str | dict, use_llm: bool = False
) -> LogAnalysisResult:
    """원스텝 분석. use_llm=True 이면 LLM 해석까지 포함."""
    result = preprocess(loki_json)
    if use_llm and not result.is_empty():
        explain(result)
    return result


# ── 독립 실행 테스트 ──────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    sys.path.insert(0, "../..")

    SAMPLE_LOKI = {
        "total": 12,
        "unique": 9,
        "level_counts": {"ERROR": 7, "WARN": 3, "INFO": 2},
        "key_logs": [
            {
                "time": "14:32:01",
                "level": "ERROR",
                "log": "[ERROR] java.lang.OutOfMemoryError: Java heap space",
            },
            {
                "time": "14:32:01",
                "level": "ERROR",
                "log": "  at java.util.Arrays.copyOf(Arrays.java:3210)",
            },
            {
                "time": "14:32:01",
                "level": "ERROR",
                "log": "  at com.bank.service.TransactionService.process(TransactionService.java:87)",
            },
            {
                "time": "14:32:03",
                "level": "ERROR",
                "log": '[ERROR] "POST /api/transfer HTTP/1.1" 500 1234',
            },
            {
                "time": "14:32:03",
                "level": "ERROR",
                "log": '[ERROR] "POST /api/transfer HTTP/1.1" 500 1234',
            },  # 중복
            {
                "time": "14:32:05",
                "level": "WARN",
                "log": "[WARN] DB connection timeout after 30000ms",
            },
            {
                "time": "14:32:10",
                "level": "ERROR",
                "log": "[ERROR] AJP error: connection refused to 192.168.16.20:8009",
            },
            {
                "time": "14:32:15",
                "level": "ERROR",
                "log": "[ERROR] GC overhead limit exceeded - heap usage 98%",
            },
            {
                "time": "14:33:01",
                "level": "ERROR",
                "log": "[ERROR] Read timed out after 5000ms",
            },
            {
                "time": "14:33:10",
                "level": "ERROR",
                "log": "[ERROR] traceId=abc123def456 status=500 /api/balance NullPointerException",
            },
        ],
        "trace_ids": ["abc123def456"],
    }

    result = preprocess(SAMPLE_LOKI)

    print("로그 분석 결과\n" + "=" * 50)
    print(f"총 {result.total_logs}건 (중복제거 {result.unique_logs}건)")
    print(f"레벨별: {result.level_counts}")
    print(f"에러 패턴:")
    for pat, cnt in sorted(result.patterns.items(), key=lambda x: -x[1]):
        print(f"  {pat:<22} {cnt}건")
    print(f"핵심 로그 {len(result.key_logs)}건:")
    for e in result.key_logs[:3]:
        print(f"  [{e['time']}] {e['log'][:80]}")
    print(f"스택트레이스 {len(result.stack_traces)}개 추출")
    print(f"TraceID: {result.trace_ids}")
    print(f"\n압축 프롬프트 텍스트 ({len(result.to_prompt_text())}자):")
    print(result.to_prompt_text())
