"""
nlp/pipeline.py — NL 처리 통합 파이프라인
──────────────────────────────────────────
역할: entity_extractor → time_parser → intent_classifier → param_binder
      를 순서대로 호출하는 오케스트레이터.
      LangGraph agent/graph.py 에서 import해서 사용.

사용 예시:
    processor = NLPipeline()
    result = processor.run("어제 was01 서버 500 에러가 왜 발생했어?")
    print(result.intent, result.servers, result.time_range)

    # 멀티턴 (이전 state 전달)
    result2 = processor.run("그 서버 로그도 보여줘", state=current_state)

개선 사항:
  1. _text_needs_context 중복 제거 — entity_extractor.needs_context() 재사용
  2. bind_params()에 cmdb 전달 — NLPipeline.__init__에서 cmdb 보유
  3. run_batch()에 states 파라미터 추가 — 멀티턴 배치 처리 지원
  4. get_pipeline() use_llm 변경 가능 — 인스턴스 재생성 조건 추가
"""

import logging
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from typing import Optional

# from monitoring_llm.nlp.entity_extractor import extract_entities, needs_context, ExtractedEntities
from monitoring_llm.nlp.entity_extractor import (
    needs_context,  # [개선 1] 중복 정의 제거 — 여기서 import해서 재사용
)
from monitoring_llm.nlp.entity_extractor import (
    ExtractedEntities,
    extract_entities,
)
from monitoring_llm.nlp.intent_classifier import (
    ClassifyResult,
    IntentClassifier,
    QueryIntent,
)
from monitoring_llm.nlp.param_binder import BoundParams, bind_params
from monitoring_llm.nlp.time_parser import (
    TimeRange,
    default_range,
    parse_time_expression,
)

log = logging.getLogger("monitoring_llm.nlp")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
CMDB_DB_PATH = os.getenv("CMDB_DB_PATH", "cmdb.db")


# # ── 컨텍스트 대명사 텍스트 포함 감지 ──────────────────────────────
# import re
# _CONTEXT_RE = re.compile(
#     r'\b(그|이|해당|그\s*서버|해당\s*서버|방금|아까|'
#     r'위에서|위의|앞서|앞에서|그\s*문제|이\s*문제)\b'
# )

# def _text_needs_context(text: str, entities: ExtractedEntities) -> bool:
#     return bool(_CONTEXT_RE.search(text)) and not entities.all_servers


class NLPipeline:
    """
    Phase 3 통합 파이프라인.
    agent/graph.py의 node_nlp_parse() 에서 호출.
    """

    def __init__(
        self,
        ollama_model: str = OLLAMA_MODEL,
        ollama_base_url: str = OLLAMA_BASE_URL,
        use_llm: bool = True,
        cmdb=None,  # [개선 2] CMDB 인스턴스 외부 주입
    ):
        self.classifier = IntentClassifier(
            model=ollama_model,
            base_url=ollama_base_url,
            use_llm=use_llm,
        )

        # [개선 2] cmdb 미전달 시 내부에서 자동 생성
        if cmdb is not None:
            self.cmdb = cmdb
        else:
            try:
                from monitoring_llm.cmdb.database import CMDB

                self.cmdb = CMDB(CMDB_DB_PATH)
            except Exception as e:
                log.warning(f"[NLPipeline] CMDB 초기화 실패: {e}")
                self.cmdb = None

    def run(
        self,
        text: str,
        state: Optional[dict] = None,
        conversation_history: str = "",
    ) -> BoundParams:
        """
        자연어 입력 → BoundParams 반환.

        state: LangGraph State dict (current_servers, last_time_range 참조)
        conversation_history: 최근 대화 요약 (LLM 컨텍스트용)
        """
        state = state or {}

        # ── 1. 엔티티 추출 ─────────────────────────────────────────
        entities = extract_entities(text)
        log.debug(f"[NLP] entities: {entities.to_dict()}")

        # ── 2. 시간 파싱 ───────────────────────────────────────────
        time_range: Optional[TimeRange] = parse_time_expression(text)

        # 시간 없으면 state에서 가져오기
        if time_range is None:
            time_range = state.get("last_time_range")

        # ── 3. 인텐트 분류 ─────────────────────────────────────────
        classify_result: ClassifyResult = self.classifier.classify(
            text, conversation_history
        )
        intent = classify_result.intent
        log.info(
            f"[NLP] intent={intent.value} "
            f"conf={classify_result.confidence:.2f} "
            f"method={classify_result.method}"
        )

        # action_recommend는 시간 범위 불필요
        if intent == QueryIntent.ACTION_RECOMMEND:
            time_range = None

        # ── 4. 컨텍스트 대명사 감지 ───────────────────────────────
        # from_context = _text_needs_context(text, entities)

        # [개선 1] 중복 정의 제거 — entity_extractor.needs_context() 사용
        from_context = needs_context(text, entities)

        # ── 5. 파라미터 바인딩 ─────────────────────────────────────
        # bp = bind_params(intent, entities, time_range, state)
        # [개선 2] self.cmdb 를 bind_params 에 전달
        bp = bind_params(intent, entities, time_range, state, cmdb=self.cmdb)
        bp.from_context = from_context

        # 서버 없고 컨텍스트 필요 → state에서 복원
        if from_context and not bp.servers and state.get("current_servers"):
            bp.servers = state["current_servers"]
            log.info(
                f"[NLP] 컨텍스트 서버 참조: "
                f"{[s.get('hostname') for s in bp.servers]}"
            )

        return bp

    # def run_batch(self, texts: list[str]) -> list[BoundParams]:
    #     """여러 쿼리 일괄 처리 (테스트/벤치마크용)"""
    #     return [self.run(t) for t in texts]
    # [개선 3] run_batch에 states 파라미터 추가 — 멀티턴 배치 처리 지원
    def run_batch(
        self,
        texts: list[str],
        states: Optional[list[dict]] = None,
    ) -> list[BoundParams]:
        """
        여러 쿼리 일괄 처리 (테스트/벤치마크용).

        states: 각 쿼리에 대응하는 state 리스트.
                None 이면 모든 쿼리에 빈 state 적용.
                texts 와 길이가 다르면 빈 state로 채움.
        """
        if states is None:
            states = [{} for _ in texts]
        elif len(states) < len(texts):
            # 길이 부족분은 빈 state로 채움
            states = states + [{} for _ in range(len(texts) - len(states))]

        return [self.run(t, state=s) for t, s in zip(texts, states)]


# ── 전역 싱글톤 ────────────────────────────────────────────────────
# _pipeline: Optional[NLPipeline] = None
# def get_pipeline(use_llm: bool = True) -> NLPipeline:
#     global _pipeline
#     if _pipeline is None:
#         _pipeline = NLPipeline(use_llm=use_llm)
#     return _pipeline

_pipeline: Optional[NLPipeline] = None
_pipeline_use_llm: Optional[bool] = None  # [개선 4] 현재 인스턴스의 use_llm 기록


def get_pipeline(use_llm: bool = True, cmdb=None) -> NLPipeline:
    """
    [개선 4] use_llm 이 변경됐을 때 인스턴스를 재생성.
                cmdb 인스턴스도 외부에서 주입 가능.
    """
    global _pipeline, _pipeline_use_llm

    if _pipeline is None or _pipeline_use_llm != use_llm:
        if _pipeline is not None:
            log.info(
                f"[NLPipeline] use_llm 변경 감지 "
                f"({_pipeline_use_llm} → {use_llm}), 인스턴스 재생성"
            )
        _pipeline = NLPipeline(use_llm=use_llm, cmdb=cmdb)
        _pipeline_use_llm = use_llm
    return _pipeline


if __name__ == "__main__":
    import sys

    sys.path.insert(0, "../..")
    from monitoring_llm.cmdb.database import CMDB, seed_banksystem_16

    # cmdb = CMDB("/tmp/test_pipeline.db")
    cmdb = CMDB("cmbdb.db")
    seed_banksystem_16(cmdb)

    # pipeline = NLPipeline(use_llm=False)  # 룰만 사용
    
    # [개선 2] cmdb 주입
    pipeline = NLPipeline(use_llm=False, cmdb=cmdb)

    # 4턴 연속 대화 시뮬레이션
    CONVERSATION = [
        ("어제 어떤 서버에 문제가 있었어?", {}),
        ("was 서버 메트릭 보여줘", {}),
        (
            "그 서버 로그도 같이 확인해줘",
            {
                "current_servers": [
                    {
                        "hostname": "was01-bank16",
                        "ip": "192.168.16.20",
                        "role": "was",
                        "prometheus_instance": "192.168.16.20:9090",
                        "loki_host": "was01-bank16",
                        "prometheus_job": "jmx_exporter",
                    }
                ]
            },
        ),
        (
            "이 상황에서 어떤 조치를 취해야 해?",
            {
                "current_servers": [
                    {
                        "hostname": "was01-bank16",
                        "ip": "192.168.16.20",
                        "role": "was",
                        "prometheus_instance": "192.168.16.20:9090",
                        "loki_host": "was01-bank16",
                        "prometheus_job": "jmx_exporter",
                    }
                ]
            },
        ),
    ]

    print("4턴 연속 대화 시뮬레이션\n" + "=" * 60)
    for i, (text, state) in enumerate(CONVERSATION, 1):
        bp = pipeline.run(text, state=state)
        servers_str = [s.get("hostname", "?") for s in bp.servers]
        print(f"\n[{i}턴] {text}")
        print(f"  intent     = {bp.intent}")
        print(f"  servers    = {servers_str}")
        print(f"  time_range = {bp.time_range}")
        print(f"  from_ctx   = {bp.from_context}")
        if bp.loki_keyword or bp.loki_status_code:
            print(
                f"  loki       = keyword={bp.loki_keyword}, code={bp.loki_status_code}"
            )

    # [개선 3] run_batch states 지원 테스트
    print("\n\nrun_batch 멀티턴 테스트\n" + "="*60)
    batch_texts  = [t for t, _ in CONVERSATION]
    batch_states = [s for _, s in CONVERSATION]
    results = pipeline.run_batch(batch_texts, states=batch_states)
    for i, bp in enumerate(results, 1):
        print(f"[{i}] intent={bp.intent}, servers={[s.get('hostname') for s in bp.servers]}")

    # [개선 4] get_pipeline use_llm 변경 테스트
    print("\n\nget_pipeline use_llm 변경 테스트\n" + "="*60)
    p1 = get_pipeline(use_llm=False)
    p2 = get_pipeline(use_llm=False)   # 동일 → 재사용
    p3 = get_pipeline(use_llm=True)    # 변경 → 재생성
    print(f"p1 is p2: {p1 is p2}")     # True
    print(f"p2 is p3: {p2 is p3}")     # False