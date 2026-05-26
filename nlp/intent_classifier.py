"""
nlp/intent_classifier.py — 인텐트 분류기 (룰 우선 + LLM fallback)
────────────────────────────────────────────────────────────────────
원본 대비 개선:
  - 룰 커버: 3/6 → 6/6 인텐트 전부 룰 기반 처리
  - LLM 프롬프트에 Few-shot 예시 10개 추가 (정확도 향상)
  - think 모드 비활성화 (Qwen3 /no_think 태그)
  - 분류 근거(reason) 반환
  - confidence 임계값 기반 재시도 로직

More 개선:
개선 사항:
  1. 룰 우선순위 충돌 수정 — ERROR_ANALYSIS를 ACTION_RECOMMEND보다 앞에 배치
  2. _ask_action 조건 강화 — 에러/원인 키워드와 함께 올 때는 ERROR_ANALYSIS 우선
  3. ChatOllama 인스턴스 재사용 — IntentClassifier.__init__ 에서 한 번만 생성
  4. confidence 임계값 기반 재시도 로직 구현 — 룰 conf < threshold 시 LLM 재확인


변경 요약:
  - _has_role() 추가 — 숫자 없는 역할명 (web, was, db) 매칭
  - _ask_info() — "알려줘", "뭐야" 패턴 추가
  - ASSET_INFO 룰 — _has_host OR _has_role 로 확장
    "web 서버 정보 알려줘", "was 서버 정보 알려줘" 매칭
  - LLM format="json" 제거 — Qwen3 /no_think 와 충돌해 빈 응답 유발
  - LLM 응답 파싱 강화 — think 태그 제거 후 JSON 추출
  - TEST_CASES에 숫자 없는 role 질문 추가

"""

import json
import logging
import os
import re
from enum import Enum
from typing import Optional
import sys

# nlp/bert/ → nlp/ → monitoring_llm/ → llm_test/  (3단계 위 = 패키지 루트)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "../../.."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

log = logging.getLogger("monitoring_llm.nlp")

import sys, os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "../.."))
from monitoring_llm.llm_factory import build_chat_llm, no_think_prefix

# [개선 4] confidence 임계값 — 룰 결과가 이 값 미만이면 LLM 재확인
CONFIDENCE_THRESHOLD = 0.85

# BERT confidence 임계값 — 이 값 미만이면 Rule 로 fallback
BERT_CONFIDENCE_THRESHOLD = float(os.getenv("BERT_CONFIDENCE_THRESHOLD", "0.85"))

# BERT 출력 레이블 → QueryIntent 매핑 (Fine-tuning 시 사용한 레이블 이름에 맞게 수정)
BERT_LABEL_MAP: dict[str, "QueryIntent"] = {}  # QueryIntent 정의 후 아래에서 채움


class QueryIntent(str, Enum):
    INCIDENT_HISTORY = "incident_history"
    ASSET_INFO = "asset_info"
    METRIC_RANGE = "metric_range"
    MULTI_MODAL = "multi_modal"
    ERROR_ANALYSIS = "error_analysis"
    ACTION_RECOMMEND = "action_recommend"
    UNKNOWN = "unknown"


# QueryIntent 정의 이후에 매핑 초기화
BERT_LABEL_MAP = {
    "incident_history": QueryIntent.INCIDENT_HISTORY,
    "asset_info":       QueryIntent.ASSET_INFO,
    "metric_range":     QueryIntent.METRIC_RANGE,
    "multi_modal":      QueryIntent.MULTI_MODAL,
    "error_analysis":   QueryIntent.ERROR_ANALYSIS,
    "action_recommend": QueryIntent.ACTION_RECOMMEND,
    "unknown":          QueryIntent.UNKNOWN,
}


class ClassifyResult:
    def __init__(
        self, intent: QueryIntent, confidence: float, reason: str, method: str
    ):
        self.intent = intent
        self.confidence = confidence
        self.reason = reason
        self.method = method

    def __repr__(self):
        return (
            f"ClassifyResult(intent={self.intent.value}, "
            f"conf={self.confidence:.2f}, method={self.method})"
        )


# ── 룰 헬퍼 ────────────────────────────────────────────────────────
# 대소문자 구분이 필요하면 t, 필요 없으면 tl을 사용하는 구조입니다.
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_HTTP_ERR_RE = re.compile(r"\b[45]\d{2}\b")
_HOST_RE = re.compile(
    r"\b(?:web|was|db|app|proxy|lb)\d+(?:[-_][\w]+)?\b", re.IGNORECASE
)
_ROLE_RE = re.compile(
    r"\b(web|was|db)\s*서버\b", re.IGNORECASE
)  # ← 추가: "web 서버", "was 서버"


def _has_ip(t, _):
    return bool(_IP_RE.search(t))


def _has_err(t, _):
    return bool(_HTTP_ERR_RE.search(t))


def _has_host(t, _):
    return bool(_HOST_RE.search(t))


def _has_role(_, tl):
    return bool(_ROLE_RE.search(tl))  # ← 추가


# def _has_metric(_, tl):
#     return bool(
#         re.search(
#             # r"메트릭|cpu|메모리|heap|성능|지표|disk|tps|qps|스레드|thread|연결\s*수", tl
#             r"메트릭|cpu|메모리|heap|성능|지표|disk|디스크|tps|qps|스레드|thread|연결\s*수|사용률|사용량|상태",
#             tl,
#         )
#     )
def _has_metric(_, tl): return bool(re.search(
    r'메트릭|cpu|메모리|heap|성능|지표|disk|디스크|tps|qps'
    r'|스레드|thread|연결\s*수|사용률|사용량|상태'
    r'|요청|request|응답\s*시간|처리량|트래픽',  # ← 추가
    tl))

def _has_log(_, tl):
    return bool(re.search(r"로그|log\b", tl))


def _has_past(_, tl):
    return bool(re.search(r"어제|지난\s*주|그제|이틀\s*전|\d+일\s*전|최근", tl))


def _has_trouble(_, tl):
    return bool(re.search(r"문제|장애|이슈|이상|오류|에러|알람|알림|다운", tl))


# def _has_err_kw(_, tl):
#     return bool(
#         re.search(
#             r"oom|outofmemory|timeout|타임아웃|slow.?query|슬로우|deadlock|교착|disk.?full|ajp|npe|gc.?overhead",
#             tl,
#         )
#     )
def _has_err_kw(_, tl):
    return bool(
        re.search(
            r"oom|outofmemory|timeout|타임아웃|slow.?query|슬로우|deadlock|교착"
            r"|disk.?full|ajp|npe|gc.?overhead"
            r"|느린|느려|지연|늦어|응답\s*시간",  # ← 추가
            tl,
        )
    )


def _ask_why(_, tl):
    return bool(re.search(r"머지|원인|왜|이유|뭐야|뭔지|뭔데|분석|발생", tl))


def _ask_action(_, tl):
    return bool(
        re.search(
            r"조치|해결|어떻게\s*해|뭘\s*해야|방법|대응|복구|해야\s*할|할\s*것", tl
        )
    )


# def _ask_info(_, tl):    return bool(re.search(r'뭐하|정보|역할|어떤\s*서버|누가|담당|뭔지|소속', tl))
def _ask_info(_, tl):
    return bool(
        re.search(
            r"뭐하|정보|역할|어떤\s*서버|누가|담당|뭔지|소속|알려줘|뭐야|어떤\s*역할",
            tl,  # ← 알려줘, 뭐야 추가
        )
    )


def _has_err_natural(_, tl):
    """한국어 에러/오류 자연어 표현 (HTTP 코드 아닌 단어 기반)"""
    return bool(re.search(r"에러|오류|error\b", tl))


# ── [개선 1, 2] 룰 우선순위 재조정 ─────────────────────────────────
# 변경 전: ACTION_RECOMMEND 가 최우선 → "OOM 해결 방법" 이 ERROR_ANALYSIS 대신 ACTION_RECOMMEND 로 분류
# 변경 후:
#   - 에러/원인 키워드가 함께 있으면 ERROR_ANALYSIS 를 먼저 판단
#   - 에러/원인 없이 순수 조치 요청일 때만 ACTION_RECOMMEND

RULES = [
    # ← 여기에 추가 (기존 첫 번째 룰보다 앞에)
    # HTTP 에러코드 또는 자연어 에러 + 존재/발생 질문 → error_analysis
    # "에러가 발생하고 있어?", "500 에러 있어?", "어떤 에러가 발생하고 있어?" 등
    # 단, 메트릭+로그 동시 요청(multi_modal)은 제외
    (
        lambda t, tl: (_has_err(t, tl) or _has_err_natural(t, tl))
        and re.search(r"있었어|있어|발생|나왔어|떴어|확인|어떤\s*에러", tl)
        and not (_has_metric(t, tl) and _has_log(t, tl)),
        QueryIntent.ERROR_ANALYSIS,
        0.93,
        "에러(코드/자연어) + 존재/발생 질문",
    ),
    # [개선 2] 에러+원인+조치가 동시에 있으면 ERROR_ANALYSIS 우선
    # "OOM 해결 방법 알려줘", "500 에러 원인이랑 조치 방법"
    # 에러 + 원인 질문 → ERROR_ANALYSIS 최우선
    (
        lambda t, tl: (_has_err(t, tl) or _has_err_kw(t, tl)) and _ask_why(t, tl),
        QueryIntent.ERROR_ANALYSIS,
        0.95,
        "에러 + 원인 질문",
    ),
    # (lambda t,tl: _ask_action(t,tl),
    #  QueryIntent.ACTION_RECOMMEND, 0.95, "조치/해결/방법 키워드"),
    # [개선 2] 순수 조치 요청 (에러/원인 키워드 없음)
    # 순수 조치 요청 (에러/원인 없음)
    (
        lambda t, tl: _ask_action(t, tl)
        and not (_has_err(t, tl) or _has_err_kw(t, tl))
        and not _ask_why(t, tl),
        QueryIntent.ACTION_RECOMMEND,
        0.95,
        "순수 조치/해결/방법 요청",
    ),
    # 전체 서버 상태 점검 → multi_modal
    (
        lambda t, tl: re.search(r"전체|모든|BankSystem", tl)
        and re.search(r"점검|상태|확인", tl),
        QueryIntent.MULTI_MODAL,
        0.90,
        "전체 서버 점검",
    ),
    # (lambda t,tl: (_has_err(t,tl) or _has_err_kw(t,tl)) and _ask_why(t,tl),
    #  QueryIntent.ERROR_ANALYSIS, 0.95, "에러 + 원인 질문"),
    # [개선 2] 에러+조치 (원인 질문 없음) → ACTION_RECOMMEND
    # "OOM 해결 방법 알려줘" (왜? 없음)
    # 에러 + 조치 (원인 없음) → ACTION_RECOMMEND
    (
        lambda t, tl: (_has_err(t, tl) or _has_err_kw(t, tl))
        and _ask_action(t, tl)
        and not _ask_why(t, tl),
        QueryIntent.ACTION_RECOMMEND,
        0.90,
        "에러 + 조치 요청",
    ),
    # IP + 정보 요청
    (
        lambda t, tl: _has_ip(t, tl) and _ask_info(t, tl),
        QueryIntent.ASSET_INFO,
        0.98,
        "IP + 정보 요청",
    ),
    # (lambda t, tl: _has_host(t, tl) and _ask_info(t, tl) and not _has_metric(t, tl),
    #  QueryIntent.ASSET_INFO, 0.90, "hostname + 정보 요청"),
    # hostname(번호 있음) 또는 role(번호 없음) + 정보 요청  ← 수정
    (
        lambda t, tl: (_has_host(t, tl) or _has_role(t, tl))
        and _ask_info(t, tl)
        and not _has_metric(t, tl),
        QueryIntent.ASSET_INFO,
        0.90,
        "hostname/role + 정보 요청",
    ),
    # 메트릭 + 로그 동시 → MULTI_MODAL
    (
        lambda t, tl: _has_metric(t, tl) and _has_log(t, tl),
        QueryIntent.MULTI_MODAL,
        0.92,
        "메트릭 + 로그 동시 요청",
    ),
    # 복합 조회 패턴
    (
        lambda t, tl: re.search(r"같이|함께|모두|전체\s*확인|통합|종합", tl)
        and _has_trouble(t, tl),
        QueryIntent.MULTI_MODAL,
        0.85,
        "복합 조회 패턴",
    ),
    # 메트릭만
    (
        lambda t, tl: _has_metric(t, tl) and not _has_log(t, tl),
        QueryIntent.METRIC_RANGE,
        0.88,
        "메트릭 조회",
    ),
    # 에러/오류 키워드 + 로그 → ERROR_ANALYSIS (INCIDENT_HISTORY보다 앞에)
    # "bank-was-app 에서 에러가 나는데 최근 1시간 로그 확인해줘" 같은 패턴
    (
        lambda t, tl: _has_err_natural(t, tl) and _has_log(t, tl),
        QueryIntent.ERROR_ANALYSIS,
        0.90,
        "에러/오류 + 로그 조회",
    ),
    # [개선 1] "어제 OOM 왜 발생했어?" 는 ERROR_ANALYSIS(위)에서 먼저 잡힘
    # 과거 시간 + 문제 (에러 원인 질문 아님, 로그 요청 아님)
    (
        lambda t, tl: _has_past(t, tl) and _has_trouble(t, tl)
                      and not _ask_why(t, tl) and not _has_log(t, tl),
        QueryIntent.INCIDENT_HISTORY,
        0.92,
        "과거 시간 + 문제/장애",
    ),
    # 로그 단독
    (
        lambda t, tl: _has_log(t, tl) and not _has_metric(t, tl),
        QueryIntent.MULTI_MODAL,
        0.82,
        "로그 단독 조회 → multi_modal",
    ),
    # 문제 이력 조회
    (
        lambda t, tl: _has_trouble(t, tl)
        and not _has_metric(t, tl)
        and not _has_log(t, tl)
        and not _ask_why(t, tl),
        # (lambda t,tl: _has_ip(t,tl) and _ask_info(t,tl),
        #  QueryIntent.ASSET_INFO, 0.98, "IP + 정보 요청"),
        # (lambda t,tl: _has_host(t,tl) and _ask_info(t,tl) and not _has_metric(t,tl),
        #  QueryIntent.ASSET_INFO, 0.90, "hostname + 정보 요청"),
        # (lambda t,tl: _has_metric(t,tl) and _has_log(t,tl),
        #  QueryIntent.MULTI_MODAL, 0.92, "메트릭 + 로그 동시 요청"),
        # (lambda t,tl: re.search(r'같이|함께|모두|전체\s*확인|통합|종합', tl) and _has_trouble(t,tl),
        #  QueryIntent.MULTI_MODAL, 0.85, "복합 조회 패턴"),
        # (lambda t,tl: _has_metric(t,tl) and not _has_log(t,tl),
        #  QueryIntent.METRIC_RANGE, 0.88, "메트릭 조회"),
        # (lambda t,tl: _has_past(t,tl) and _has_trouble(t,tl),
        #  QueryIntent.INCIDENT_HISTORY, 0.92, "과거 시간 + 문제/장애"),
        # # 로그 단독 조회: '로그 보여줘', '로그 확인해줘'
        # (lambda t,tl: _has_log(t,tl) and not _has_metric(t,tl),
        #  QueryIntent.MULTI_MODAL, 0.82, '로그 단독 조회 → multi_modal'),
        # (lambda t,tl: _has_trouble(t,tl) and not _has_metric(t,tl)
        #               and not _has_log(t,tl) and not _ask_why(t,tl),
        QueryIntent.INCIDENT_HISTORY,
        0.80,
        "문제 이력 조회",
    ),
]


# ── BERT Fine-tuning 분류기 ──────────────────────────────────────────
_bert_pipeline = None  # 지연 로딩 (최초 bert_classify 호출 시 초기화)


def _load_bert_pipeline():
    """BERT_MODEL_PATH 환경변수가 설정된 경우에만 transformers pipeline 로드."""
    global _bert_pipeline
    if _bert_pipeline is not None:
        return _bert_pipeline
    model_path = os.getenv("BERT_MODEL_PATH", "")
    if not model_path:
        return None
    try:
        from transformers import pipeline as hf_pipeline
        _bert_pipeline = hf_pipeline(
            "text-classification",
            model=model_path,
            device=-1,  # CPU. GPU 사용 시 device=0
            # top_k 미설정 → 단일 결과를 dict로 반환 (top_k=1이면 list of list)
        )
        log.info("[BERT] 모델 로드 완료: %s", model_path)
    except Exception as e:
        log.warning("[BERT] 모델 로드 실패: %s", e)
        _bert_pipeline = None
    return _bert_pipeline


def bert_classify(text: str) -> Optional[ClassifyResult]:
    """
    Fine-tuned BERT 인텐트 분류기.

    반환:
      - ClassifyResult : BERT가 확신하는 결과 (confidence >= BERT_CONFIDENCE_THRESHOLD)
      - None           : BERT 미설정이거나 UNKNOWN / 낮은 신뢰도 → Rule 로 자동 fallback

    사용자 구현 가이드:
      1. transformers Trainer 로 6-class 분류 모델 Fine-tuning 후 저장
         (저장 레이블이 BERT_LABEL_MAP 키와 일치해야 함)
      2. .env 에 BERT_MODEL_PATH=./models/intent-bert 설정
      3. BERT_LABEL_MAP 을 모델 출력 레이블에 맞게 수정
      4. 모델이 LABEL_0 형식을 출력하는 경우 아래 label 파싱 부분을 수정

    Fine-tuning 예시 (별도 스크립트):
        from transformers import AutoTokenizer, AutoModelForSequenceClassification, Trainer, TrainingArguments
        # id2label = {0:"incident_history", 1:"asset_info", ...}
        # ...
    """
    pipe = _load_bert_pipeline()
    if pipe is None:
        return None  # BERT 미설정 → Rule 로 fallback

    try:
        raw = pipe(text[:512])
        # top_k 없음 → [{"label":..., "score":...}]
        # top_k=1    → [[{"label":..., "score":...}]]  (중첩 리스트)
        output = raw[0][0] if isinstance(raw[0], list) else raw[0]
        label      = str(output["label"]).lower()
        confidence = float(output["score"])

        intent = BERT_LABEL_MAP.get(label, QueryIntent.UNKNOWN)

        if intent == QueryIntent.UNKNOWN or confidence < BERT_CONFIDENCE_THRESHOLD:
            log.debug("[BERT] UNKNOWN/낮은conf(%.2f) → fallback: %s", confidence, text[:40])
            return None

        log.debug("[BERT] %s (%.2f): %s", intent.value, confidence, text[:40])
        return ClassifyResult(intent, confidence, f"BERT:{label}", "bert")

    except Exception as e:
        log.warning("[BERT] 추론 실패: %s", e)
        return None


# ── Rule-based 분류기 ────────────────────────────────────────────────
def rule_classify(text: str) -> Optional[ClassifyResult]:
    tl = text.lower()
    for cond, intent, conf, reason in RULES:
        try:
            if cond(text, tl):
                return ClassifyResult(intent, conf, reason, "rule")
        except Exception:
            continue
    return None


# ── LLM Few-shot 프롬프트 ─────────────────────────────────────────
# 두 질문 추가함.
# Q: OOM 해결 방법 알려줘
# A: {"intent":"action_recommend","confidence":0.93,"reason":"OOM+해결방법"}
# Q: 어제 OOM 왜 발생했어?
# A: {"intent":"error_analysis","confidence":0.96,"reason":"과거+OOM+원인질문"}

FEW_SHOT = """
Q: 어제 어떤 서버에 문제가 있었어?
A: {"intent":"incident_history","confidence":0.98,"reason":"어제+문제"}

Q: 192.168.16.10 서버는 뭐하는 서버야?
A: {"intent":"asset_info","confidence":0.99,"reason":"IP+정보요청"}

Q: web 서버 정보 알려줘
A: {"intent":"asset_info","confidence":0.97,"reason":"role+정보요청"}

Q: was 서버 정보 알려줘
A: {"intent":"asset_info","confidence":0.97,"reason":"role+정보요청"}

Q: 5월 6일 14시~16시 was01 메트릭 조회해줘
A: {"intent":"metric_range","confidence":0.97,"reason":"시간범위+메트릭"}

Q: 문제있는 서버 성능 메트릭이랑 로그 같이 보여줘
A: {"intent":"multi_modal","confidence":0.96,"reason":"메트릭+로그"}

Q: 500 에러가 머지?
A: {"intent":"error_analysis","confidence":0.98,"reason":"500+원인질문"}

Q: OOM 왜 발생해?
A: {"intent":"error_analysis","confidence":0.97,"reason":"OOM+원인질문"}

Q: 이 상황에서 어떤 조치를 취해야 해?
A: {"intent":"action_recommend","confidence":0.99,"reason":"조치키워드"}

Q: OOM 해결 방법 알려줘
A: {"intent":"action_recommend","confidence":0.93,"reason":"OOM+해결방법"}

Q: 어제 OOM 왜 발생했어?
A: {"intent":"error_analysis","confidence":0.96,"reason":"과거+OOM+원인질문"}
"""

# SYSTEM_PROMPT = f"""/no_think
# IT 운영 모니터링 쿼리 분류기. JSON만 출력.

# 인텐트: incident_history | asset_info | metric_range | multi_modal | error_analysis | action_recommend | unknown

# 예시:{FEW_SHOT}
# 출력: {{"intent":"<값>","confidence":0.0~1.0,"reason":"<한줄>"}}"""


_CLASSIFIER_PROMPT_BODY = f"""IT 운영 모니터링 쿼리 분류기. JSON만 출력. 마크다운 없이.

인텐트: incident_history | asset_info | metric_range | multi_modal | error_analysis | action_recommend | unknown

예시:{FEW_SHOT}
출력형식(JSON만): {{"intent":"<값>","confidence":0.0~1.0,"reason":"<한줄>"}}"""

def _classifier_system_prompt() -> str:
    return no_think_prefix() + _CLASSIFIER_PROMPT_BODY


# ── [개선 3] LLM 인스턴스를 함수 내부가 아닌 클래스에서 관리 ──────
def _build_llm(model: str = "", base_url: str = ""):
    """llm_factory 경유 LLM 인스턴스 생성. 실패 시 None 반환."""
    # model/base_url 인자는 하위 호환 유지를 위해 받되 무시 (factory가 env 읽음)
    return build_chat_llm(temperature=0.0, max_tokens=512, num_ctx=2048)


# def llm_classify(text: str, context: str = "") -> ClassifyResult:
def llm_classify(text: str, context: str = "", llm=None) -> ClassifyResult:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        # from langchain_ollama import ChatOllama
        # llm = ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL,
        #                  temperature=0.0, num_predict=120, format="json")

        # [개선 3] 외부 주입 llm 없으면 임시 생성 (하위 호환)
        _llm = llm or _build_llm()
        if _llm is None:
            raise RuntimeError("LLM 인스턴스 없음")

        msg = f"[이전 대화]\n{context}\n\n[질문]\n{text}" if context else text

        # resp = llm.invoke([SystemMessage(content=SYSTEM_PROMPT),
        #                    HumanMessage(content=msg)])
        resp = _llm.invoke(
            [
                SystemMessage(content=_classifier_system_prompt()),
                HumanMessage(content=msg),
            ]
        )

        raw = resp.content.strip()
        log.debug(f"[LLM raw] {raw[:100]}")  # ← 임시 추가

        # Qwen3 think 태그 제거
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        # if "```" in raw:
        #     raw = re.search(r'\{.*\}', raw, re.DOTALL).group()

        # 마크다운 코드블록 제거
        if "```" in raw:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            raw = m.group() if m else raw

        # JSON 추출 (앞뒤 텍스트가 있을 경우)
        m = re.search(r"\{[^{}]+\}", raw)
        if m:
            raw = m.group()

        data = json.loads(raw)
        intent = QueryIntent(data.get("intent", "unknown"))

        # return ClassifyResult(intent, float(data.get("confidence", 0.7)),
        #                       data.get("reason", "LLM"), "llm")
        return ClassifyResult(
            intent,
            float(data.get("confidence", 0.7)),
            data.get("reason", "LLM"),
            "llm",
        )
    except Exception as e:
        log.warning(f"[LLM 실패] {e}")
        return ClassifyResult(QueryIntent.UNKNOWN, 0.3, str(e)[:40], "fallback")


# class IntentClassifier:
#     def __init__(self, model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, use_llm=True):
#         self.model    = model
#         self.base_url = base_url
#         self.use_llm  = use_llm

#     def classify(self, text: str, context: str = "") -> ClassifyResult:
#         result = rule_classify(text)
#         if result:
#             return result
#         if self.use_llm:
#             return llm_classify(text, context)
#         return ClassifyResult(QueryIntent.UNKNOWN, 0.0, "룰 미매칭", "fallback")


class IntentClassifier:
    """
    인텐트 분류기 — 3단계 체인:
      1. BERT Fine-tuning  (BERT_MODEL_PATH 설정 시 활성화)
      2. Rule-based        (항상 활성화)
      3. LLM classify      (use_llm=True 시 활성화)

    우선순위 변경: use_bert / use_llm 플래그로 각 단계 ON/OFF 가능.
    """

    def __init__(
        self,
        model="",        # 하위 호환 — factory가 env에서 읽으므로 무시됨
        base_url="",     # 하위 호환 — factory가 env에서 읽으므로 무시됨
        use_llm=True,
        use_bert=True,
        confidence_threshold=CONFIDENCE_THRESHOLD,
        bert_threshold=BERT_CONFIDENCE_THRESHOLD,
    ):
        self.use_llm = use_llm
        self.use_bert = use_bert
        self.threshold = confidence_threshold
        self.bert_threshold = bert_threshold
        self._llm = _build_llm() if use_llm else None

    def classify(self, text: str, context: str = "") -> ClassifyResult:
        # ── 1단계: BERT Fine-tuning ──────────────────────────────
        if self.use_bert:
            bert_result = bert_classify(text)
            if bert_result:
                return bert_result
            # bert_result=None 이면 (미설정·UNKNOWN·낮은conf) 다음 단계로

        # ── 2단계: Rule-based ────────────────────────────────────
        rule_result = rule_classify(text)
        if rule_result:
            if rule_result.confidence >= self.threshold or not self.use_llm:
            # if rule_result.confidence >= 0.99 or not self.use_llm:
                return rule_result
            log.debug("[룰 conf 낮음 %.2f] LLM 재확인: %s", rule_result.confidence, text[:40])

        # ── 3단계: LLM classify ──────────────────────────────────
        if self.use_llm:
            llm_result = llm_classify(text, context, llm=self._llm)
            if rule_result and rule_result.confidence > llm_result.confidence:
                return rule_result
            return llm_result

        return rule_result or ClassifyResult(QueryIntent.UNKNOWN, 0.0, "모든 분류기 미매칭", "fallback")


# ── 테스트 케이스 ──────────────────────────────────────────────────
# TEST_CASES = [
#     ("어제 어떤 서버에 문제가 있었어?",           "incident_history"),
#     ("지난주에 장애 있었어?",                    "incident_history"),
#     ("최근 이슈 알려줘",                         "incident_history"),
#     ("192.168.16.10 서버는 뭐하는 서버야?",       "asset_info"),
#     ("web01 서버 정보 알려줘",                   "asset_info"),
#     ("was01-bank16 어떤 서버야?",               "asset_info"),
#     ("5월 6일 14시~16시 web01 메트릭 조회해줘",   "metric_range"),
#     ("어제 was01 CPU 어떻게 됐어?",              "metric_range"),
#     ("db01 연결 수 추이 보여줘",                  "metric_range"),
#     ("문제있는 서버 메트릭이랑 로그 같이 보여줘",  "multi_modal"),
#     ("was01 성능과 에러 로그 함께 확인해줘",       "multi_modal"),
#     ("500 에러가 머지?",                         "error_analysis"),
#     ("OOM 왜 발생해?",                           "error_analysis"),
#     ("slow query 원인이 뭐야?",                  "error_analysis"),
#     # [개선 1] 과거+에러+원인 → ERROR_ANALYSIS (기존엔 INCIDENT_HISTORY 오분류 가능)
#     ("어제 OOM 왜 발생했어?",                    "error_analysis"),
#     ("이 상황에서 어떤 조치를 취해야 해?",         "action_recommend"),
#     ("해결 방법 알려줘",                          "action_recommend"),
#     ("어떻게 해야 해?",                           "action_recommend"),
#     # [개선 2] 에러+조치 (원인 없음) → ACTION_RECOMMEND
#     ("OOM 해결 방법 알려줘",                      "action_recommend"),
# ]

TEST_CASES = [
    ("어제 어떤 서버에 문제가 있었어?", "incident_history"),
    ("지난주에 장애 있었어?", "incident_history"),
    ("최근 이슈 알려줘", "incident_history"),
    ("192.168.16.10 서버는 뭐하는 서버야?", "asset_info"),
    ("web01 서버 정보 알려줘", "asset_info"),
    ("was01-bank16 어떤 서버야?", "asset_info"),
    ("web 서버 정보 알려줘", "asset_info"),  # ← 추가
    ("was 서버 정보 알려줘", "asset_info"),  # ← 추가
    ("db 서버 담당팀이 어디야?", "asset_info"),  # ← 추가
    ("192.168.0.63 서버는 뭐하는 서버야?", "asset_info"),  # ← 추가
    ("5월 6일 14시~16시 web01 메트릭 조회해줘", "metric_range"),
    ("어제 was01 CPU 어떻게 됐어?", "metric_range"),
    ("db01 연결 수 추이 보여줘", "metric_range"),
    ("지금 WAS 서버 CPU랑 힙 메모리 상태 어때?", "metric_range"),  # ← 추가
    ("문제있는 서버 메트릭이랑 로그 같이 보여줘", "multi_modal"),
    ("was01 성능과 에러 로그 함께 확인해줘", "multi_modal"),
    ("500 에러가 머지?", "error_analysis"),
    ("OOM 왜 발생해?", "error_analysis"),
    ("slow query 원인이 뭐야?", "error_analysis"),
    ("어제 OOM 왜 발생했어?", "error_analysis"),
    ("어제 was에서 500 에러가 왜 발생했어?", "error_analysis"),  # ← 추가
    ("이 상황에서 어떤 조치를 취해야 해?", "action_recommend"),
    ("해결 방법 알려줘", "action_recommend"),
    ("OOM 해결 방법 알려줘", "action_recommend"),
    ("web 서버 디스크가 꽉 찼는데 어떻게 해야 해?", "action_recommend"),  # ← 추가
    ("bank-was-app 에서 에러가 나는데 최근 1시간 로그 확인해줘", "error_analysis"),  # ← 추가
    ("was 서버에서 일주일 내에 어떤 에러가 발생하고 있어?", "error_analysis"),      # ← 추가
    ("최근 이슈 알려줘", "incident_history"),  # 최근 + 로그 없음 → incident_history 유지
]


if __name__ == "__main__":
    # print("인텐트 분류 정확도 테스트 (룰 기반)\n" + "="*55)
    print("인텐트 분류 정확도 테스트 (룰 기반)\n" + "=" * 55)
    clf = IntentClassifier(use_llm=False)
    ok_count = 0
    by_intent = {}
    for text, expected in TEST_CASES:
        result = clf.classify(text)
        correct = result.intent.value == expected
        if correct:
            ok_count += 1
        by_intent.setdefault(expected, []).append(correct)
        icon = "✓" if correct else "✗"
        print(f"{icon} [{result.method:<8} {result.confidence:.2f}] {text[:50]}")
        if not correct:
            # print(f"    예상={expected}, 실제={result.intent.value}")
            print(
                f"    예상={expected}, 실제={result.intent.value}, 이유={result.reason}"
            )
    total = len(TEST_CASES)
    print(f"\n전체: {ok_count}/{total} ({ok_count / total * 100:.1f}%)")
    print("\n인텐트별:")
    for intent, results in sorted(by_intent.items()):
        n = sum(results)
        print(f"  {intent:<22} {n}/{len(results)}")
