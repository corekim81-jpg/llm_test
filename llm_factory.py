"""
llm_factory.py — LLM 프로바이더 팩토리
────────────────────────────────────────
LLM_PROVIDER 환경변수로 백엔드 선택:
  - "ollama"    (기본) : ChatOllama  — 로컬 Ollama 서버
  - "anthropic"        : ChatAnthropic — Anthropic API (Claude)

모든 반환값은 LangChain BaseChatModel 인터페이스를 구현하므로
nodes.py / intent_classifier.py 등에서 동일하게 사용 가능.
"""

import os
import logging

log = logging.getLogger("monitoring_llm.llm")

# ── 공통 설정 ──────────────────────────────────────────────────────
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()  # "ollama" | "anthropic"

# ── Ollama 설정 ────────────────────────────────────────────────────
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "qwen3:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# ── Anthropic 설정 ─────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL",   "claude-sonnet-4-6")


# ── 공개 팩토리 함수 ───────────────────────────────────────────────
def build_chat_llm(
    temperature: float = 0.2,
    max_tokens:  int   = 2048,
    num_ctx:     int   = 8192,   # Ollama 전용 컨텍스트 크기
):
    """
    LLM_PROVIDER 에 따라 적절한 Chat LLM 인스턴스를 반환한다.
    실패 시 None 반환 (호출부에서 None 체크 필요).
    """
    if LLM_PROVIDER == "anthropic":
        return _build_anthropic(temperature=temperature, max_tokens=max_tokens)
    return _build_ollama(temperature=temperature, max_tokens=max_tokens, num_ctx=num_ctx)


def no_think_prefix() -> str:
    """
    Ollama Qwen3의 think 모드 비활성화 지시어.
    Anthropic Claude 등 다른 모델에는 불필요하므로 빈 문자열 반환.
    """
    return "/no_think\n" if LLM_PROVIDER == "ollama" else ""


def provider_info() -> str:
    """현재 프로바이더/모델 식별 문자열 (로그·헬스체크용)"""
    if LLM_PROVIDER == "anthropic":
        return f"anthropic/{ANTHROPIC_MODEL}"
    return f"ollama/{OLLAMA_MODEL} @ {OLLAMA_BASE_URL}"


# ── 내부 빌더 ─────────────────────────────────────────────────────
def _build_ollama(temperature: float, max_tokens: int, num_ctx: int):
    try:
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=OLLAMA_MODEL,
            base_url=OLLAMA_BASE_URL,
            temperature=temperature,
            num_predict=max_tokens,
            num_ctx=num_ctx,
            extra_body={"think": False},  # Qwen3 thinking 비활성화
        )
    except Exception as e:
        log.warning("[llm_factory] Ollama 초기화 실패: %s", e)
        return None


def _build_anthropic(temperature: float, max_tokens: int):
    try:
        from langchain_anthropic import ChatAnthropic
        if not ANTHROPIC_API_KEY:
            raise ValueError(
                "ANTHROPIC_API_KEY 환경변수 미설정. "
                ".env 에 ANTHROPIC_API_KEY=sk-ant-... 추가 필요."
            )
        return ChatAnthropic(
            model=ANTHROPIC_MODEL,
            api_key=ANTHROPIC_API_KEY,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:
        log.warning("[llm_factory] Anthropic 초기화 실패: %s", e)
        return None
