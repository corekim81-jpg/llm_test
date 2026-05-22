"""
analysis/rca_bridge.py — MicroRCA ↔ LLM 브릿지
──────────────────────────────────────────────────
역할:
  1. 기존 MicroRCA 파이프라인을 실행 (또는 결과를 수신)
  2. PC Algorithm DAG + Granger score + Consensus 결과를
     LLM이 이해할 수 있는 자연어 설명으로 변환
  3. LangGraph State의 tool_results 에 저장될 dict 반환

MicroRCA 연동 방법:
    # 방법 A: 파이프라인 인스턴스 주입
    bridge = RCABridge(microrca=your_pipeline)
    result = bridge.run(metric_df, anomaly_intervals)

    # 방법 B: 이미 계산된 결과 dict 전달
    bridge = RCABridge()
    explanation = bridge.explain(rca_result_dict)

MicroRCA 결과 dict 예상 구조:
    {
      "root_cause": "db01_cpu_usage",
      "confidence": 0.87,
      "causal_chain": ["db01_cpu_usage","was01_response_time","web01_error_rate"],
      "granger_scores": {"db01→was01": 0.92, "was01→web01": 0.78},
      "reconstruction_errors": {"db01_cpu_usage": 0.34, "was01_heap": 0.12},
      "pc_dag_edges": [["db01_cpu","was01_resp"],["was01_resp","web01_err"]],
      "anomaly_interval": "2026-05-06 14:32~14:45",
      "metric_tier": {"db01_cpu_usage": 1, "was01_heap": 2},
    }

독립 실행:
    python -m monitoring_llm.analysis.rca_bridge
    
변경 이력:
    [FIX] METRIC_DESCRIPTIONS에 slow_queries, error_rate 누락 항목 추가
    [FIX] _humanize_metric() 매칭 순서 문제 — 긴 키 우선 정렬로 가로채기 방지
    [FIX] _humanize_metric() prefix 매칭을 숫자 포함 정확 매칭으로 개선
          (web01/web02 같은 멀티노드 확장 대비)
    [FIX] explain() Tier 라벨 중복 조건 정리
    [FIX] RCABridge.run() mock 경로에도 예외처리 추가
    [개선] _llm_explain() ChatOllama 싱글톤 캐싱 (log_analyzer.py와 일관성)
    [개선] _humanize_metric() 변환 실패 시 원본 메트릭명 + 경고 로그    
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional, Any

log = logging.getLogger("monitoring_llm.analysis")

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "../.."))
from monitoring_llm.llm_factory import build_chat_llm

# CMDB에서 메트릭명 → 서버명 변환용 접두어 매핑
# [FIX] prefix를 정확한 노드명(web01/web02)으로 확장 가능한 구조로 유지
#       현재 BankSystem_16은 노드별 1대이므로 prefix만으로 충분
METRIC_PREFIX_TO_SERVER = {
    "web": "web-bank16 (Apache/Linux)",
    # "web02": "web02-bank16 (Apache/Linux)",
    "was": "was-bank16 (Tomcat/Windows)",
    # "was02": "was02-bank16 (Tomcat/Windows)",
    "db":  "db-bank16 (MySQL Primary)",
    # "db02":  "db02-bank16 (MySQL Replica)",
}

# # 메트릭명 한국어 설명
# METRIC_DESCRIPTIONS = {
#     "cpu":              "CPU 사용률",
#     "cpu_usage":        "CPU 사용률",
#     "heap":             "JVM 힙 사용률",
#     "heap_used":        "JVM 힙 사용량",
#     "response_time":    "응답시간",
#     "req_per_sec":      "초당 요청수",
#     "error_rate":       "에러율",
#     "connections":      "DB 연결수",
#     "slow_queries":     "슬로우 쿼리수",
#     "gc_time":          "GC 시간",
#     "threads":          "활성 스레드수",
#     "disk":             "디스크 사용률",
# }

# [FIX] slow_queries, error_rate 누락 항목 추가
#       긴 키가 짧은 키보다 먼저 매칭되도록 길이 내림차순 정렬 유지
METRIC_DESCRIPTIONS: dict[str, str] = {
    "cpu_usage":        "CPU 사용률",       # cpu 보다 먼저 매칭돼야 함
    "heap_used":        "JVM 힙 사용량",    # heap 보다 먼저
    "response_time":    "응답시간",
    "req_per_sec":      "초당 요청수",
    "slow_queries":     "슬로우 쿼리수",    # [FIX] 추가
    "error_rate":       "에러율",           # [FIX] 추가
    "gc_time":          "GC 시간",
    "connections":      "DB 연결수",
    "threads":          "활성 스레드수",
    "cpu":              "CPU 사용률",
    "heap":             "JVM 힙 사용률",
    "disk":             "디스크 사용률",
}

# [FIX] 긴 키 우선 정렬 — 모듈 로드 시 1회 계산
#       "cpu"가 "cpu_usage"를 가로채는 문제 방지
_METRIC_DESC_SORTED: list[tuple[str, str]] = sorted(
    METRIC_DESCRIPTIONS.items(), key=lambda x: -len(x[0])
)

# [개선] prefix 정규식 사전 컴파일 (멀티노드 대비)
#        "web01_cpu" → prefix="web", node_id="01"
_PREFIX_RE = re.compile(
    r"^(" + "|".join(re.escape(p) for p in METRIC_PREFIX_TO_SERVER) + r")(\d*)(.*)",
    re.IGNORECASE,
)

# ── LLM 싱글톤 캐시 ──────────────────────────────────────────────────
# [개선] log_analyzer.py와 동일한 패턴으로 일관성 확보
_llm_instance: Optional[object] = None

def _get_llm():
    """LLM 인스턴스를 최초 1회만 생성 후 재사용 (llm_factory 위임)."""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = build_chat_llm(temperature=0.2, max_tokens=1200, num_ctx=4096)
    return _llm_instance


# def _humanize_metric(metric_name: str) -> str:
#     """'db01_cpu_usage' → 'db01-bank16 CPU 사용률'"""
#     for prefix, server in METRIC_PREFIX_TO_SERVER.items():
#         if metric_name.startswith(prefix):
#             suffix = metric_name[len(prefix):].lstrip("_")
#             for key, desc in METRIC_DESCRIPTIONS.items():
#                 if key in suffix:
#                     return f"{server} {desc}"
#             return f"{server} {suffix}"
#     return metric_name

def _humanize_metric(metric_name: str) -> str:
    """
    'db01_cpu_usage' → 'db-bank16 (MySQL Primary) CPU 사용률'

    [FIX] 개선사항:
    - prefix 정규식 매칭으로 web01/web02 같은 멀티노드도 대응 가능
    - 긴 키 우선 정렬로 "cpu"가 "cpu_usage"를 가로채는 문제 방지
    - 변환 실패 시 원본 반환 + 경고 로그
    """
    m = _PREFIX_RE.match(metric_name)
    if not m:
        log.debug("_humanize_metric: prefix 미매칭 — '%s' 원본 반환", metric_name)
        return metric_name

    prefix   = m.group(1).lower()   # "db"
    # node_id = m.group(2)           # "01" (현재 미사용, 멀티노드 확장 시 활용)
    suffix   = m.group(3).lstrip("_0123456789").lstrip("_")  # "cpu_usage"

    server = METRIC_PREFIX_TO_SERVER.get(prefix)
    if not server:
        log.debug("_humanize_metric: 서버 미등록 prefix='%s'", prefix)
        return metric_name

    # [FIX] 긴 키 우선 매칭 (_METRIC_DESC_SORTED 사용)
    for key, desc in _METRIC_DESC_SORTED:
        if key in suffix:
            return f"{server} {desc}"

    # suffix가 METRIC_DESCRIPTIONS에 없는 경우 원본 suffix 그대로
    log.debug("_humanize_metric: 설명 미등록 suffix='%s'", suffix)
    return f"{server} {suffix}"


def _format_causal_chain(chain: list[str]) -> str:
    """['db01_cpu', 'was01_resp', 'web01_err'] → 인과 연쇄 텍스트"""
    if not chain:
        return "(인과 연쇄 없음)"
    return " → ".join(_humanize_metric(m) for m in chain)


# ── RCA 결과 설명 생성 ────────────────────────────────────────────────
def explain(rca_result: dict, use_llm: bool = True) -> str:
    """
    MicroRCA 결과 dict → 자연어 설명 문자열.

    use_llm=False 이면 LLM 없이 템플릿 기반 설명 반환.
    """
    if not rca_result:
        return "RCA 결과가 없습니다. MicroRCA 파이프라인을 먼저 실행하세요."

    root_cause  = rca_result.get("root_cause", "미확인")
    confidence  = rca_result.get("confidence", 0.0)
    chain       = rca_result.get("causal_chain", [])
    granger     = rca_result.get("granger_scores", {})
    recon_err   = rca_result.get("reconstruction_errors", {})
    interval    = rca_result.get("anomaly_interval", "")
    tier        = rca_result.get("metric_tier", {})

    # 룰 기반 템플릿 설명 (LLM 없이도 동작)
    root_human  = _humanize_metric(root_cause)
    chain_human = _format_causal_chain(chain)
    conf_pct    = f"{confidence*100:.1f}%"

    template = (
        f"## RCA 분석 결과\n\n"
        f"**근본 원인 (Root Cause)**: {root_human}\n"
        f"**신뢰도**: {conf_pct} (Confidence)\n"
    )
    if interval:
        template += f"**이상 구간**: {interval}\n"

    template += f"\n**인과 연쇄 (Causal Chain)**:\n{chain_human}\n"

    if granger:
        template += "\n**Granger 인과 점수**:\n"
        for edge, score in sorted(granger.items(), key=lambda x: -x[1])[:5]:
            parts = edge.split("→")
            if len(parts) == 2:
                src = _humanize_metric(parts[0].strip())
                dst = _humanize_metric(parts[1].strip())
                template += f"  {src} → {dst}: {score:.3f}\n"
            else:
                template += f"  {edge}: {score:.3f}\n"

    if recon_err:
        template += "\n**재구성 오차** (높을수록 이상 강도 ↑):\n"
        for metric, err in sorted(recon_err.items(), key=lambda x: -x[1])[:5]:
            # t_label = f"[Tier{tier.get(metric,'?')}]" if metric in tier else ""
            # [FIX] 중복 조건 제거: tier.get(metric,'?') + if metric in tier → 단순화
            t_label = f"[Tier{tier[metric]}]" if metric in tier else ""
            template += f"  {_humanize_metric(metric)} {t_label}: {err:.4f}\n"

    if not use_llm:
        return template

    # LLM 으로 더 자연스러운 설명 생성
    return _llm_explain(rca_result, template)


# def _llm_explain(rca_result: dict, template: str) -> str:
#     try:
#         from langchain_ollama import ChatOllama
#         from langchain_core.messages import HumanMessage, SystemMessage

#         llm = ChatOllama(
#             model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL,
#             temperature=0.2, num_predict=1200, num_ctx=4096,
#         )
#         prompt = (
#             f"[MicroRCA 분석 요약]\n{template}\n\n"
#             "[원시 결과]\n"
#             f"{json.dumps(rca_result, ensure_ascii=False, indent=2)[:1500]}\n\n"
#             "위 RCA 결과를 IT 운영팀이 이해할 수 있는 한국어로 설명하세요.\n"
#             "다음 순서로 작성:\n"
#             "1. 근본 원인 한 줄 요약\n"
#             "2. 문제 전파 경로 설명 (인과 연쇄)\n"
#             "3. 신뢰도 및 핵심 지표 인용\n"
#             "4. 이 분석의 한계 또는 추가 확인 필요 사항"
#         )
#         resp = llm.invoke([
#             SystemMessage(content="/no_think\n당신은 MicroRCA 기반 AIOps 전문가입니다. 근거 데이터를 인용하며 한국어로 설명하세요."),
#             HumanMessage(content=prompt),
#         ])
#         return resp.content
#     except Exception as e:
#         log.warning(f"RCA LLM 설명 실패: {e}")
#         return template


def _llm_explain(rca_result: dict, template: str) -> str:
    """
    템플릿 기반 설명을 LLM으로 강화.

    [개선] ChatOllama 싱글톤 재사용.
    [개선] 예외 유형별 구분 처리.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    # [개선] 싱글톤 재사용
    llm = _get_llm()

    prompt = (
        f"[MicroRCA 분석 요약]\n{template}\n\n"
        "[원시 결과]\n"
        f"{json.dumps(rca_result, ensure_ascii=False, indent=2)[:1500]}\n\n"
        "위 RCA 결과를 IT 운영팀이 이해할 수 있는 한국어로 설명하세요.\n"
        "다음 순서로 작성:\n"
        "1. 근본 원인 한 줄 요약\n"
        "2. 문제 전파 경로 설명 (인과 연쇄)\n"
        "3. 신뢰도 및 핵심 지표 인용\n"
        "4. 이 분석의 한계 또는 추가 확인 필요 사항"
    )

    try:
        resp = llm.invoke([
            SystemMessage(content="/no_think\n당신은 MicroRCA 기반 AIOps 전문가입니다. 근거 데이터를 인용하며 한국어로 설명하세요."),
            HumanMessage(content=prompt),
        ])
        return resp.content

    # [개선] 예외 유형별 구분
    except ConnectionError as e:
        log.warning("RCA LLM 연결 실패 (Ollama 서버 확인): %s", e)
        return template
    except TimeoutError as e:
        log.warning("RCA LLM 응답 시간 초과: %s", e)
        return template
    except Exception as e:
        log.warning("RCA LLM 설명 실패 (%s): %s", type(e).__name__, e)
        return template


# ── MicroRCA 파이프라인 브릿지 클래스 ────────────────────────────────
class RCABridge:
    """
    MicroRCA 파이프라인과 LangGraph 에이전트를 연결하는 브릿지.

    사용 예시:
        # 파이프라인 인스턴스 주입
        from your_microrca import MicroRCAAnalyzer
        bridge = RCABridge(microrca=MicroRCAAnalyzer(config=cfg))
        result, explanation = bridge.run(metric_df, intervals)

        # 결과 dict만 전달
        bridge = RCABridge()
        explanation = bridge.explain(existing_result)
    """

    def __init__(self, microrca: Optional[Any] = None):
        self.microrca = microrca

    def run(
        self,
        metric_data: Any,
        anomaly_intervals: list,
        use_llm: bool = True,
    ) -> tuple[dict, str]:
        """
        MicroRCA 실행 → (결과 dict, 자연어 설명) 반환.
        microrca 미주입 시 mock 결과 반환.
        """
        if self.microrca is None:
            log.warning("MicroRCA 파이프라인 미주입 — mock 결과 반환")
            # [FIX] mock 경로에도 예외처리 추가 (일관성)
            try:
                rca_result = _mock_rca_result()
            except Exception as e:
                log.error("mock RCA 결과 생성 실패: %s", e)
                return {}, f"mock RCA 생성 오류: {e}"    
        else:
            # 실제 연동:
            # rca_result = self.microrca.analyze(metric_data, anomaly_intervals)
            try:
                rca_result = self.microrca.analyze(metric_data, anomaly_intervals)
            except Exception as e:
                # log.error(f"MicroRCA 실행 실패: {e}")
                log.error("MicroRCA 실행 실패: %s", e)
                return {}, f"MicroRCA 실행 오류: {e}"

        explanation = explain(rca_result, use_llm=use_llm)
        return rca_result, explanation

    def explain(self, rca_result: dict, use_llm: bool = True) -> str:
        """이미 계산된 결과 dict를 자연어로 변환."""
        return explain(rca_result, use_llm=use_llm)


# ── Mock 결과 (테스트용) ──────────────────────────────────────────────
def _mock_rca_result() -> dict:
    return {
        "root_cause":    "db01_slow_queries",
        "confidence":    0.87,
        "causal_chain":  [
            "db01_slow_queries",
            "was01_response_time",
            "web01_error_rate",
        ],
        "granger_scores": {
            "db01→was01": 0.92,
            "was01→web01": 0.78,
        },
        "reconstruction_errors": {
            "db01_slow_queries":  0.341,
            "was01_response_time": 0.189,
            "web01_error_rate":    0.097,
            "db01_cpu_usage":      0.052,
        },
        "pc_dag_edges": [
            ["db01_slow_queries", "was01_response_time"],
            ["was01_response_time", "web01_error_rate"],
        ],
        "anomaly_interval": "2026-05-06 14:32~14:45",
        "metric_tier": {
            "db01_slow_queries":   1,
            "was01_response_time": 1,
            "web01_error_rate":    1,
        },
    }


# ── 독립 실행 테스트 ──────────────────────────────────────────────────
if __name__ == "__main__":
    # print("RCA Bridge 테스트 (LLM 없이)\n" + "="*50)

    # result = _mock_rca_result()
    # explanation = explain(result, use_llm=False)
    # print(explanation)

    # print("\n" + "="*50)
    # print("RCABridge 클래스 (mock 모드):")
    # bridge = RCABridge()
    # rca_result, text = bridge.run(None, [], use_llm=False)
    # print(f"root_cause: {rca_result.get('root_cause')}")
    # print(f"confidence: {rca_result.get('confidence')}")
    # print(f"설명 길이: {len(text)}자")
    # print(text[:300])


    # _humanize_metric 변환 검증
    print("메트릭명 변환 테스트\n" + "="*50)
    test_metrics = [
        "db01_slow_queries",    # [FIX] 이전엔 변환 실패
        "was01_response_time",
        "web01_error_rate",     # [FIX] 이전엔 변환 실패
        "db01_cpu_usage",       # cpu vs cpu_usage 가로채기 방지 확인
        "db01_cpu",
        "was01_heap_used",      # heap vs heap_used 가로채기 방지 확인
        "was01_heap",
        "unknown_metric",       # prefix 미매칭 → 원본 반환
    ]
    for m in test_metrics:
        print(f"  {m:<30} → {_humanize_metric(m)}")

    print("\nRCA Bridge 테스트 (LLM 없이)\n" + "="*50)
    result = _mock_rca_result()
    explanation = explain(result, use_llm=False)
    print(explanation)

    print("\n" + "="*50)
    print("RCABridge 클래스 (mock 모드):")
    bridge = RCABridge()
    rca_result, text = bridge.run(None, [], use_llm=False)
    print(f"root_cause: {rca_result.get('root_cause')}")
    print(f"confidence: {rca_result.get('confidence')}")
    print(f"설명 길이: {len(text)}자")
    print(text[:300])