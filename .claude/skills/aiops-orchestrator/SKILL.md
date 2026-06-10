---
name: aiops-orchestrator
description: BankSystem_16 AIOps 모니터링 서비스의 메인 오케스트레이터. 새 인텐트/도구/기능 추가, 버그 수정, 파이프라인 디버깅, 레이어 간 변경 조율, 다시 실행/재실행/업데이트/보완 요청 시 반드시 이 스킬을 사용할 것. monitoring_llm 프로젝트의 NLP·Tools·Agent·API·RAG 레이어에 걸친 모든 개발·수정·디버깅 작업을 에이전트 팀 또는 서브 에이전트로 조율한다.
---

## 이 스킬의 역할

5개 전문 에이전트(nlp-engineer, tool-engineer, agent-engineer, api-engineer, rag-engineer)를 조율하여 AIOps 모니터링 서비스의 변경·수정·디버깅을 처리한다.

## Phase 0: 컨텍스트 확인

시작 시 기존 작업 상태를 확인하여 실행 모드를 결정한다.

1. `_workspace/` 디렉토리 존재 여부 확인
2. 사용자 요청 유형 파악:
   - `_workspace/` 없음 → **초기 실행**
   - `_workspace/` 있고 "부분 수정/다시/재실행" 요청 → **부분 재실행** (해당 에이전트만 재호출)
   - `_workspace/` 있고 새 요청 → 기존을 `_workspace_prev/`로 이동 후 **새 실행**
3. 요청 유형에 따라 Phase 1로 진행

## Phase 1: 요청 분석

사용자 요청을 분석하여 작업 유형을 결정한다.

### 작업 유형

| 유형 | 예시 | 실행 모드 |
|------|------|----------|
| **기능 개발** | 새 인텐트 추가, 새 도구 연동, 기능 확장 | 에이전트 팀 (파이프라인) |
| **버그 수정** | 특정 레이어 버그, NLP 오분류, 도구 에러 | 에이전트 팀 또는 단일 에이전트 |
| **파이프라인 디버깅** | "왜 이 쿼리가 틀리게 처리되지?", 흐름 추적 | 서브 에이전트 (팬아웃) |
| **성능 개선** | 쿼리 최적화, RAG 검색 품질 개선 | 영향 레이어 에이전트 |

### 영향 레이어 매핑

요청에서 영향받는 레이어를 식별한다:

- **NLP 변경** (인텐트, 엔티티, 시간, 파라미터 바인딩) → nlp-engineer (필수) + agent-engineer
- **도구 변경** (Prometheus/Loki/Jaeger/CMDB 쿼리) → tool-engineer (필수) + agent-engineer
- **그래프 변경** (노드 추가, 라우팅, 상태) → agent-engineer (필수)
- **API 변경** (새 엔드포인트, SSE, 세션) → api-engineer (필수)
- **RAG 변경** (새 소스, 인덱싱, 검색) → rag-engineer (필수) + agent-engineer

## Phase 2: 에이전트 팀 구성 (기능 개발 / 버그 수정)

**실행 모드: 에이전트 팀 (파이프라인)**

에이전트는 다음 순서로 순차 협업한다:

```
[nlp-engineer] → [tool-engineer] → [agent-engineer] → [api-engineer]
                                                      ↕
                                               [rag-engineer] (RAG 관련 시)
```

### 에이전트 팀 실행 순서

1. **nlp-engineer** 먼저 실행: NLP 변경이 없어도 파라미터 스펙 확인 필요 시 포함
2. **tool-engineer** 다음: NLP 출력 파라미터 기반 도구 구현
3. **agent-engineer** 다음: 새 노드·라우팅·상태 반영
4. **api-engineer** 마지막: API 레이어 변경 (없으면 스킵)
5. **rag-engineer** 독립적: RAG 관련 변경이 있을 때만 포함

각 에이전트 호출 시:
- `model: "opus"` 파라미터 명시
- `_workspace/{phase}_{agent}_{artifact}.md`에 결과 저장 지시
- 이전 단계 산출물 경로를 다음 에이전트에 전달

## Phase 3: 서브 에이전트 팬아웃 (파이프라인 디버깅)

**실행 모드: 서브 에이전트 (팬아웃)**

사용자 쿼리가 파이프라인 어느 단계에서 잘못 처리되는지 병렬 조사한다.

```
[오케스트레이터]
    ├── Agent(nlp-engineer, 인텐트/엔티티 분류 결과 확인, run_in_background=true)
    ├── Agent(tool-engineer, 쿼리 파라미터 및 백엔드 응답 확인, run_in_background=true)
    ├── Agent(agent-engineer, 노드 실행 흐름 및 상태 확인, run_in_background=true)
    └── 결과 수집 → 어느 레이어 문제인지 진단 보고
```

## Phase 4: 결과 통합 및 보고

1. 각 에이전트의 작업 완료 확인
2. 변경 사항 요약:
   - 수정된 파일 목록
   - 각 레이어의 변경 내용
   - 테스트 실행 결과
3. 사용자에게 변경 완료 보고

## 에러 핸들링

- 에이전트 실패 → 1회 재시도, 재실패 시 해당 결과 없이 계속 (누락 명시)
- 레이어 간 의존성 충돌 → 영향받는 에이전트 재호출
- 테스트 실패 → 실패 내용 분석 후 해당 에이전트에 수정 요청

## 테스트 시나리오

### 정상 흐름: 새 인텐트 추가

```
요청: "DISK_CHECK 인텐트 추가 - '디스크 용량 확인해줘' 쿼리 처리"
1. nlp-engineer: QueryIntent.DISK_CHECK enum, regex 패턴 추가
2. tool-engineer: DiskCheckTool 구현 (Prometheus node_filesystem 메트릭)
3. agent-engineer: _ROUTE_MAP, add_node("call_disk"), add_edge
4. 테스트: python -m monitoring_llm.nlp.test
```

### 에러 흐름: NLP 오분류 디버깅

```
요청: "'was01 디스크 꽉 찼어' 쿼리가 MULTI_MODAL로 잘못 분류됨"
1. nlp-engineer 서브 에이전트: intent_classifier.py regex 패턴 점검
2. agent-engineer 서브 에이전트: _ROUTE_MAP 매핑 확인
3. 진단 결과 → nlp-engineer에 regex 패턴 수정 요청
```

## 후속 작업 지원

- "다시", "재실행", "업데이트", "보완", "수정", "{레이어}만 다시" 요청 → Phase 0에서 부분 재실행으로 처리
- 이전 `_workspace/` 결과를 읽어 변경 범위를 최소화
