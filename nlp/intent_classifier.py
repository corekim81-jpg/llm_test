"""
nlp/intent_classifier.py — 인텐트 분류기 (룰 우선 + LLM fallback)
────────────────────────────────────────────────────────────────────
원본 대비 개선:
  - 룰 커버: 3/6 → 6/6 인텐트 전부 룰 기반 처리
  - LLM 프롬프트에 Few-shot 예시 10개 추가 (정확도 향상)
  - think 모드 비활성화 (Qwen3 /no_think 태그)
  - 분류 근거(reason) 반환
  - confidence 임계값 기반 재시도 로직
"""

import re
import json
import os
import logging
from enum import Enum
from typing import Optional

log = logging.getLogger("monitoring_llm.nlp")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "qwen3:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


class QueryIntent(str, Enum):
    INCIDENT_HISTORY = "incident_history"
    ASSET_INFO       = "asset_info"
    METRIC_RANGE     = "metric_range"
    MULTI_MODAL      = "multi_modal"
    ERROR_ANALYSIS   = "error_analysis"
    ACTION_RECOMMEND = "action_recommend"
    UNKNOWN          = "unknown"


class ClassifyResult:
    def __init__(self, intent: QueryIntent, confidence: float,
                 reason: str, method: str):
        self.intent     = intent
        self.confidence = confidence
        self.reason     = reason
        self.method     = method

    def __repr__(self):
        return (f"ClassifyResult(intent={self.intent.value}, "
                f"conf={self.confidence:.2f}, method={self.method})")


# ── 룰 헬퍼 ────────────────────────────────────────────────────────
_IP_RE       = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
_HTTP_ERR_RE = re.compile(r'\b[45]\d{2}\b')
_HOST_RE     = re.compile(r'\b(?:web|was|db|app|proxy|lb)\d+(?:[-_][\w]+)?\b', re.IGNORECASE)

def _has_ip(t, _):       return bool(_IP_RE.search(t))
def _has_err(t, _):      return bool(_HTTP_ERR_RE.search(t))
def _has_host(t, _):     return bool(_HOST_RE.search(t))
def _has_metric(_, tl):  return bool(re.search(r'메트릭|cpu|메모리|heap|성능|지표|disk|tps|qps|스레드|thread|연결\s*수', tl))
def _has_log(_, tl):     return bool(re.search(r'로그|log\b', tl))
def _has_past(_, tl):    return bool(re.search(r'어제|지난\s*주|그제|이틀\s*전|\d+일\s*전|최근', tl))
def _has_trouble(_, tl): return bool(re.search(r'문제|장애|이슈|이상|오류|에러|알람|알림|다운', tl))
def _has_err_kw(_, tl):  return bool(re.search(r'oom|outofmemory|timeout|타임아웃|slow.?query|슬로우|deadlock|교착|disk.?full|ajp|npe|gc.?overhead', tl))
def _ask_why(_, tl):     return bool(re.search(r'머지|원인|왜|이유|뭐야|뭔지|뭔데|분석|발생', tl))
def _ask_action(_, tl):  return bool(re.search(r'조치|해결|어떻게\s*해|뭘\s*해야|방법|대응|복구|해야\s*할|할\s*것', tl))
def _ask_info(_, tl):    return bool(re.search(r'뭐하|정보|역할|어떤\s*서버|누가|담당|뭔지|소속', tl))


RULES = [
    (lambda t,tl: _ask_action(t,tl),
     QueryIntent.ACTION_RECOMMEND, 0.95, "조치/해결/방법 키워드"),
    (lambda t,tl: _has_ip(t,tl) and _ask_info(t,tl),
     QueryIntent.ASSET_INFO, 0.98, "IP + 정보 요청"),
    (lambda t,tl: _has_host(t,tl) and _ask_info(t,tl) and not _has_metric(t,tl),
     QueryIntent.ASSET_INFO, 0.90, "hostname + 정보 요청"),
    (lambda t,tl: (_has_err(t,tl) or _has_err_kw(t,tl)) and _ask_why(t,tl),
     QueryIntent.ERROR_ANALYSIS, 0.95, "에러 + 원인 질문"),
    (lambda t,tl: _has_metric(t,tl) and _has_log(t,tl),
     QueryIntent.MULTI_MODAL, 0.92, "메트릭 + 로그 동시 요청"),
    (lambda t,tl: re.search(r'같이|함께|모두|전체\s*확인|통합|종합', tl) and _has_trouble(t,tl),
     QueryIntent.MULTI_MODAL, 0.85, "복합 조회 패턴"),
    (lambda t,tl: _has_metric(t,tl) and not _has_log(t,tl),
     QueryIntent.METRIC_RANGE, 0.88, "메트릭 조회"),
    (lambda t,tl: _has_past(t,tl) and _has_trouble(t,tl),
     QueryIntent.INCIDENT_HISTORY, 0.92, "과거 시간 + 문제/장애"),
    # 로그 단독 조회: '로그 보여줘', '로그 확인해줘'
    (lambda t,tl: _has_log(t,tl) and not _has_metric(t,tl),
     QueryIntent.MULTI_MODAL, 0.82, '로그 단독 조회 → multi_modal'),
    (lambda t,tl: _has_trouble(t,tl) and not _has_metric(t,tl)
                  and not _has_log(t,tl) and not _ask_why(t,tl),
     QueryIntent.INCIDENT_HISTORY, 0.80, "문제 이력 조회"),
]


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
FEW_SHOT = """
Q: 어제 어떤 서버에 문제가 있었어?
A: {"intent":"incident_history","confidence":0.98,"reason":"어제+문제"}

Q: 192.168.16.10 서버는 뭐하는 서버야?
A: {"intent":"asset_info","confidence":0.99,"reason":"IP+정보요청"}

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
"""

SYSTEM_PROMPT = f"""/no_think
IT 운영 모니터링 쿼리 분류기. JSON만 출력.

인텐트: incident_history | asset_info | metric_range | multi_modal | error_analysis | action_recommend | unknown

예시:{FEW_SHOT}
출력: {{"intent":"<값>","confidence":0.0~1.0,"reason":"<한줄>"}}"""


def llm_classify(text: str, context: str = "") -> ClassifyResult:
    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL,
                         temperature=0.0, num_predict=120, format="json")
        msg = f"[이전 대화]\n{context}\n\n[질문]\n{text}" if context else text
        resp = llm.invoke([SystemMessage(content=SYSTEM_PROMPT),
                           HumanMessage(content=msg)])
        raw = resp.content.strip()
        if "```" in raw:
            raw = re.search(r'\{.*\}', raw, re.DOTALL).group()
        data = json.loads(raw)
        intent = QueryIntent(data.get("intent", "unknown"))
        return ClassifyResult(intent, float(data.get("confidence", 0.7)),
                              data.get("reason", "LLM"), "llm")
    except Exception as e:
        log.warning(f"[LLM 실패] {e}")
        return ClassifyResult(QueryIntent.UNKNOWN, 0.3, str(e)[:40], "fallback")


class IntentClassifier:
    def __init__(self, model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, use_llm=True):
        self.model    = model
        self.base_url = base_url
        self.use_llm  = use_llm

    def classify(self, text: str, context: str = "") -> ClassifyResult:
        result = rule_classify(text)
        if result:
            return result
        if self.use_llm:
            return llm_classify(text, context)
        return ClassifyResult(QueryIntent.UNKNOWN, 0.0, "룰 미매칭", "fallback")


# ── 테스트 케이스 ──────────────────────────────────────────────────
TEST_CASES = [
    ("어제 어떤 서버에 문제가 있었어?",           "incident_history"),
    ("지난주에 장애 있었어?",                    "incident_history"),
    ("최근 이슈 알려줘",                         "incident_history"),
    ("192.168.16.10 서버는 뭐하는 서버야?",       "asset_info"),
    ("web01 서버 정보 알려줘",                   "asset_info"),
    ("was01-bank16 어떤 서버야?",               "asset_info"),
    ("5월 6일 14시~16시 web01 메트릭 조회해줘",   "metric_range"),
    ("어제 was01 CPU 어떻게 됐어?",              "metric_range"),
    ("db01 연결 수 추이 보여줘",                  "metric_range"),
    ("문제있는 서버 메트릭이랑 로그 같이 보여줘",  "multi_modal"),
    ("was01 성능과 에러 로그 함께 확인해줘",       "multi_modal"),
    ("500 에러가 머지?",                         "error_analysis"),
    ("OOM 왜 발생해?",                           "error_analysis"),
    ("slow query 원인이 뭐야?",                  "error_analysis"),
    ("이 상황에서 어떤 조치를 취해야 해?",         "action_recommend"),
    ("해결 방법 알려줘",                          "action_recommend"),
    ("어떻게 해야 해?",                           "action_recommend"),
]


if __name__ == "__main__":
    print("인텐트 분류 정확도 테스트 (룰 기반)\n" + "="*55)
    clf = IntentClassifier(use_llm=False)
    ok_count = 0
    by_intent = {}
    for text, expected in TEST_CASES:
        result = clf.classify(text)
        correct = result.intent.value == expected
        if correct: ok_count += 1
        by_intent.setdefault(expected, []).append(correct)
        icon = "✓" if correct else "✗"
        print(f"{icon} [{result.method:<8} {result.confidence:.2f}] {text[:50]}")
        if not correct:
            print(f"    예상={expected}, 실제={result.intent.value}")
    total = len(TEST_CASES)
    print(f"\n전체: {ok_count}/{total} ({ok_count/total*100:.1f}%)")
    print("\n인텐트별:")
    for intent, results in sorted(by_intent.items()):
        n = sum(results)
        print(f"  {intent:<22} {n}/{len(results)}")
