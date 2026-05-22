"""
llm_factory.py — LLM 프로바이더 팩토리
────────────────────────────────────────
LLM_PROVIDER 환경변수로 백엔드 선택:
  - "ollama"     (기본) : ChatOllama       — 로컬 Ollama 서버
  - "anthropic"         : ChatAnthropic    — Anthropic API (sk-ant-... 키 필요)
  - "claude-cli"        : Claude Code CLI  — `claude -p` subprocess (현재 로그인 세션 사용)

모든 반환값은 LangChain BaseChatModel 인터페이스를 구현하므로
nodes.py / intent_classifier.py 등에서 동일하게 사용 가능.
"""

from __future__ import annotations

import os
import logging
import subprocess
import shutil
from typing import Any, Iterator, List, Optional

log = logging.getLogger("monitoring_llm.llm")

# ── 공통 설정 ──────────────────────────────────────────────────────
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
# "ollama" | "anthropic" | "claude-cli"

# ── Ollama 설정 ────────────────────────────────────────────────────
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "qwen3:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# ── Anthropic API 설정 ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.getenv("ANTHROPIC_MODEL",   "claude-sonnet-4-6")

# ── Claude CLI 설정 ────────────────────────────────────────────────
CLAUDE_CLI_TIMEOUT = int(os.getenv("CLAUDE_CLI_TIMEOUT", "120").split("#")[0].strip())
CLAUDE_CLI_MODEL   = os.getenv("CLAUDE_CLI_MODEL", "")  # 빈 문자열 = CLI 기본 모델


# ── 공개 팩토리 함수 ───────────────────────────────────────────────
def build_chat_llm(
    temperature: float = 0.2,
    max_tokens:  int   = 2048,
    num_ctx:     int   = 8192,   # Ollama 전용
) -> Any:
    """
    LLM_PROVIDER 에 따라 적절한 Chat LLM 인스턴스를 반환한다.
    실패 시 None 반환 (호출부에서 None 체크 필요).
    """
    if LLM_PROVIDER == "anthropic":
        return _build_anthropic(temperature=temperature, max_tokens=max_tokens)
    if LLM_PROVIDER == "claude-cli":
        return _build_claude_cli()
    return _build_ollama(temperature=temperature, max_tokens=max_tokens, num_ctx=num_ctx)


def no_think_prefix() -> str:
    """
    Ollama Qwen3의 thinking 모드 비활성화 지시어.
    Anthropic / claude-cli 에는 불필요하므로 빈 문자열 반환.
    """
    return "/no_think\n" if LLM_PROVIDER == "ollama" else ""


def provider_info() -> str:
    """현재 프로바이더/모델 식별 문자열 (로그·헬스체크용)"""
    if LLM_PROVIDER == "anthropic":
        return f"anthropic/{ANTHROPIC_MODEL}"
    if LLM_PROVIDER == "claude-cli":
        model_hint = CLAUDE_CLI_MODEL or "default"
        return f"claude-cli/{model_hint} (subprocess)"
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


def _build_claude_cli():
    try:
        if not shutil.which("claude"):
            raise RuntimeError(
                "claude CLI 를 찾을 수 없음. Claude Code 설치 확인 필요."
            )
        return _ClaudeCLIModel(timeout=CLAUDE_CLI_TIMEOUT, cli_model=CLAUDE_CLI_MODEL)
    except Exception as e:
        log.warning("[llm_factory] Claude CLI 초기화 실패: %s", e)
        return None


# ── Claude CLI LangChain 호환 모델 ─────────────────────────────────
class _ClaudeCLIModel:
    """
    `claude -p` subprocess 를 LangChain BaseChatModel 인터페이스로 래핑.

    사용 흐름:
        prompt_msgs → _extract() → claude -p <human> --system-prompt <sys>
                    → AIMessage(content=stdout)

    LangChain 파이프라인 (`prompt | llm | parser`) 지원을 위해
    invoke / ainvoke / stream / astream / __or__ 를 구현.
    """

    def __init__(self, timeout: int = 120, cli_model: str = ""):
        self.timeout  = timeout
        self.cli_model = cli_model

    # ── LangChain Runnable 인터페이스 ──────────────────────────────
    def invoke(self, messages, config=None, **kwargs):
        from langchain_core.messages import AIMessage
        system, human = self._extract(messages)
        content = self._run_cli(system, human)
        return AIMessage(content=content)

    async def ainvoke(self, messages, config=None, **kwargs):
        import asyncio
        from langchain_core.messages import AIMessage
        system, human = self._extract(messages)
        content = await asyncio.to_thread(self._run_cli, system, human)
        return AIMessage(content=content)

    def stream(self, messages, config=None, **kwargs) -> Iterator:
        """CLI 는 스트리밍 미지원 — 전체 응답을 단일 청크로 반환."""
        yield self.invoke(messages, **kwargs)

    async def astream(self, messages, config=None, **kwargs):
        result = await self.ainvoke(messages, **kwargs)
        yield result

    def bind(self, **kwargs):
        """LangChain bind() 호환 — 파라미터 무시하고 self 반환."""
        return self

    def with_config(self, config=None, **kwargs):
        return self

    def __or__(self, other):
        """prompt | llm | parser 파이프라인 지원."""
        from langchain_core.runnables import RunnableLambda

        outer = self

        def _pipe(messages, **kw):
            result = outer.invoke(messages, **kw)
            return other.invoke(result)

        return RunnableLambda(_pipe)

    def __ror__(self, other):
        from langchain_core.runnables import RunnableLambda

        outer = self

        def _pipe(inp, **kw):
            messages = other.invoke(inp, **kw) if hasattr(other, "invoke") else inp
            return outer.invoke(messages, **kw)

        return RunnableLambda(_pipe)

    # ── 내부 구현 ──────────────────────────────────────────────────
    def _run_cli(self, system: str, human: str) -> str:
        if not human.strip():
            raise ValueError("claude CLI: 빈 사용자 메시지")

        cmd = [
            "claude", "-p", human,
            "--output-format", "text",
            "--no-session-persistence",
            "--tools", "",              # 모든 도구 비활성화 (순수 LLM 응답)
        ]
        if system:
            cmd += ["--system-prompt", system]
        if self.cli_model:
            cmd += ["--model", self.cli_model]

        log.debug("[claude-cli] cmd=%s", cmd[:4])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"claude CLI 응답 시간 초과 ({self.timeout}초)")
        except FileNotFoundError:
            raise RuntimeError("claude 명령어를 찾을 수 없음")

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI 오류 (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout)[:300]}"
            )

        return proc.stdout.strip()

    @staticmethod
    def _extract(messages) -> tuple[str, str]:
        """LangChain 메시지 리스트에서 (system, human) 텍스트 추출."""
        system_parts: list[str] = []
        human_parts:  list[str] = []

        for msg in messages:
            t = getattr(msg, "type", "")
            c = str(msg.content) if hasattr(msg, "content") else str(msg)
            if t == "system":
                system_parts.append(c)
            elif t == "human":
                human_parts.append(c)
            elif t == "ai":
                # 멀티턴 컨텍스트를 사용자 메시지에 포함
                human_parts.append(f"[이전 답변]: {c}")

        return "\n".join(system_parts), "\n".join(human_parts)
