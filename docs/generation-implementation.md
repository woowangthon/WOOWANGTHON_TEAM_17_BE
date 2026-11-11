# 취향 확정부터 일정 초안 생성·저장 (5·6단계)

2026-11-11 후속 기준: 최초 분석해 저장·준비 완료한 취향을 confirmed-v1로 바로 사용하며 별도 태그 정규화·수동 재검토를 새 생성 gate로 사용하지 않습니다. 출발 시각과 본인 일정 항목은 기존 준비 JSON에 저장하고, 실제 생성 성공 후 [공동 캔버스 가져오기](plan-canvas-import.md)로 연결합니다. 숙소는 당일 활동 뒤로 배치하고 퇴실일을 추가 숙박으로 만들지 않으며, 예약·약속의 누락/초과 범위를 조용히 버리지 않습니다. 현재 로컬 검증은 non-MySQL 178개 통과/172개 제외, AI 51개·FE 122개 통과입니다. 별도 Sol high 독립 검토 지적 5건을 수정·재검토했습니다. 실제 서버·브라우저·Gemini/Kakao·MySQL 쓰기는 이번 작업에서 실행하지 않았으며 아래 과거 검증 기록과 구분합니다. 사용자가 기존 run.py로 검수합니다. 상세 실행 검수는 인접 FE의 docs/preparation-generation-validation.md를 따릅니다.

[기능 계약](https://app.notion.com/p/413e977e4908817a9af9ca9c7fa0ddee)과 [공통 규칙](https://app.notion.com/p/40ee977e49088189b512e6e817984d48)을 구현합니다. 후보 선별·AI 날짜별 방문 초안은 기존 AI 패키지 0.4.0을 사용합니다. 이번 작업은 실제 일정 생성, 검증, 초안 조회·저장과 인접 FE 연결입니다. 변경 이력·복원·자동 재생성은 포함하지 않습니다.

## API

아래 경로의 공통 접두사는 `/api/v1/trips/{tripId}`입니다. 기존 기기 쿠키로 인증하며 ACTIVE 참여자와 OPEN 여행방을 확인합니다. 쓰기 요청에는 허용된 Origin과 JSON이 필요하고, 모든 POST에는 UUID `Idempotency-Key`가 필요합니다.

| Method | Path | 권한·동작 |
| --- | --- | --- |
| GET | `/generation` | 공유 준비 상태·설정·최신 작업/일정 참조 |
| GET | `/generation/preferences/me` | 본인 저장 취향의 지원 여부와 일정 항목 확인 |
| POST | `/generation/preferences/me` | 레거시 정규화 제안 API (새 생성 흐름은 호출하지 않음) |
| PUT | `/generation/preferences/me/review` | 레거시 리뷰 API (새 생성 준비 조건에 영향 없음) |
| PUT | `/generation/settings` | 방장만 활동 시간·출발/복귀 좌표·숙박/고정 기준점 저장 |
| GET | `/generation/places` | 검증된 카탈로그 좌표를 가진 기준점 검색 |
| POST | `/generation/jobs` | 방장만 현재 입력 토큰으로 생성 요청, 202 |
| GET | `/generation/jobs/{jobId}` | 같은 방의 생성 진행 상태 조회 |
| GET | `/plans/{planId}` | 같은 방의 생성 초안·저장 일정 조회 |
| POST | `/plans/{planId}/save` | 방장만 현재 입력·잠금 버전을 확인하고 명시적 저장 |

방장도 다른 참여자의 원문·태그·개인 일정·개인 점수에 접근할 수 없습니다. 공유 준비 응답에는 ACTIVE 참여자의 `preparationMembers` 확정 상태만 포함합니다. 이전 리뷰 API와 테이블은 호환성을 위해 남아 있지만 신규 생성 readiness, input token, 생성 job은 이를 읽거나 요구하지 않습니다.

## 사용자 흐름과 버전

1. 각자 취향 원문을 저장하고 AI 해석 제안 중 본인이 쓸 내용을 확인·수정한 뒤 직접 확정합니다. 서버는 이때 `likes`, `avoid`, `food`, 중요도, `walking`, `scheduleItems`를 저장합니다. AI 제안은 자동 저장·확정되지 않습니다.
2. 초안 생성은 저장된 확정 취향을 그대로 사용합니다. 두 번째 정규화 호출이나 `generation_member_reviews`의 사람 승인 행을 만들지 않습니다. `confirmed-v1`의 제한된 명시 표현만 태그로 바꾸고, 알 수 없는 선호는 점수에 넣지 않습니다. 지원하지 않는 required 조건은 `REQUIRED_PREFERENCE_UNSUPPORTED`로 막습니다. 태그 부재를 알레르기·의료 안전의 근거로 쓰지 않습니다.
3. `scheduleItems`는 장소 질의와 확정된 DB `contentid`, 일정 종류/범위/시작·종료 날짜·시각을 보존합니다. 모호하거나 빠진 장소/필드는 본인 응답의 `clarifications`에서 수정하도록 보여 줍니다. OWNER가 `scope:'group'`으로 확정한 항목만 공통 고정 방문이 됩니다. `scope:'personal'` 일정은 본인 자료에 남고 공통 일정에 고정되지 않습니다.
4. 방장은 활동 시간(기본 한국 시각 09:00~20:00), `departureTime`(기존 입력은 09:00), 선택한 `departureContentid`, 필요한 숙박·고정 일정을 정합니다. 출발 시각은 첫날 시작에 적용됩니다. 출발 장소/좌표가 모호하면 route는 unknown으로 남으며 소요 시간을 만들어내지 않습니다.
5. 방장의 현재 확정 상태, 일정 항목 보정, required 취향 지원 여부, 공통 고정 기준점이 준비되면 후보 선별 → AI 날짜별 방문 초안 → 실제 이동·시간 검증을 실행합니다. 참여자는 결과를 조회하고 방장이 확인 후 저장합니다. 확인되지 않은 사실은 별도 동의가 필요하며, 알려진 위반은 동의해도 저장할 수 없습니다.

공통 조건 변경은 전원 재확인, 개인 변경은 본인 재확인을 요구합니다. 생성 입력 토큰에는 `confirmed-v1`, 공통 조건 버전, 현재 확정된 회원 집합·개인 버전·저장 문서 digest, 설정 버전과 장소 카탈로그 digest를 반영합니다. 레거시 정규화/리뷰 ID는 토큰에서 제외합니다. AI/이동 조회 동안 DB 트랜잭션을 열어 두지 않으며 최종 저장 트랜잭션에서 권한·현재 입력·카탈로그·작업 lease를 다시 확인합니다. 오래된 결과는 STALE 처리하고 계획을 만들지 않습니다. 초안이 만들어진 후 입력이 바뀌면 조회는 가능하지만 `stale=true`이며 저장할 수 없습니다.

작업 상태는 DB에 기록됩니다. 중단 후 만료된 QUEUED/RUNNING 작업은 `PROCESS_INTERRUPTED`로 끝내며 유료 AI 호출을 자동 재시도하지 않습니다. 단일 프로세스 요청 제한을 사용하므로 다중 worker 배포는 이번 범위가 아닙니다.

## 좌표·이동·시간·요금

- DB의 `mapx`는 경도, `mapy`는 위도입니다. 공개 장소 좌표는 `{latitude,longitude,crs:'WGS84'}`이고 Kakao `LatLng`에는 위도, 경도 순으로 전달합니다. 경로 geometry는 서버가 제공하는 `{latitude,longitude}` 점 목록입니다. 목록·지도·상세는 동일한 일정 노드 UUID를 사용합니다.
- 카카오 자동차·도보·대중교통 API의 실제 결과만 경로와 소요 시간으로 사용합니다. 미지원 전세버스는 자동차로 대체하지 않으며, 실패·미설정·경로 없음은 unknown으로 남깁니다. 출발·복귀 좌표 누락도 0분으로 계산하지 않습니다. 실제 geometry가 없는 구간에는 임의 직선을 그리지 않습니다.
- 도보 20분/40분은 한 장소 간 연속 도보 구간 기준입니다. 대중교통의 연속 도보 step은 합산하며, 중간 버스/지하철 이후 도보는 별도 구간입니다. 일정 추정은 한국 시각으로 계산하고 DB DATETIME(3)은 UTC로 기록합니다.
- 명확하게 해석할 수 있는 체류 시간·요금·영업시간만 검증합니다. 자유 문장의 임의 숫자를 요금으로 취급하지 않습니다. 영업시간 원문이 없으면 화면에 표시하지 않고, 요금 원문이 없으면 `요금 확인 필요`로 표시합니다.
- 확인된 장소 요금과 교통비 소계만 제공합니다. 식비·유류비 등 전체 비용이 보장되지 않으므로 총액은 unknown일 수 있습니다. 확인된 소계만으로 예산을 초과하면 위반입니다. 영업시간/이동 등 미확인은 무료·운영 중·이동 가능의 뜻이 아닙니다.

## DB와 명시적 migration

현재 DB에서 읽기 전용으로 확인한 `group_snapshots`, `plans`, `plan_versions`, `itinerary_nodes`, `itinerary_edges`, `ai_requests` DDL과 signed INT 장소 키를 기준으로 작성했습니다. 테스트 fixture의 해당 DDL은 메타데이터만 포함하며 실제 장소 행은 포함하지 않습니다.

기존 여행방 migration 001·준비 migration 002와 계획 관련 기존 테이블이 준비된 환경에만 [003 up](../migrations/003_generation.up.sql)을 관리자가 명시적으로 적용합니다. 이번 작업은 공용 DB에 003을 적용하지 않습니다. 앱 실행·import·일반 검사에서 DDL을 실행하지 않습니다.

추가 테이블은 `generation_member_reviews`, `generation_settings`, `generation_jobs` 3개입니다. 개인 정규화·검토, 생성 설정, 멱등 요청·lease·진행 상태를 저장합니다. 계획은 기존 스키마에 DRAFT/GENERATED 버전으로 저장하고 최종 명시적 저장 시 SAVED, version 번호, UTC 저장 시각과 계획 포인터를 같은 트랜잭션에서 갱신합니다. 생성 컬럼 `active_order_index`에는 쓰지 않습니다. 기존 이동 ENUM/CHECK에 없는 전세버스 논리 구간은 버전 JSON에 보존하고 CAR로 변환해 행을 만들지 않습니다.

[003 down](../migrations/003_generation.down.sql)은 지원 테이블 3개만 제거합니다. 개인 검토·설정과 과거 요청 재시도/진행 기록이 사라집니다. 기존 생성 계획·버전·노드·구간은 남으므로 down은 계획 데이터 삭제나 기능 전체 복원이 아닙니다. 실행 중인 작업이 없는 점검 시간에 적용해야 합니다. 원본 dump 실행·공용 DB 초기화는 필요하지 않습니다.

## 로컬 실행

기존 BE requirements와 인접 AI 패키지 0.4.0이 필요하며 이번 변경의 추가 런타임 의존성은 없습니다. 비밀값은 Git에서 제외된 개인 파일에만 설정합니다.

- BE `.env`: 기존 DB/쿠키/Origin 설정과 `KAKAO_REST_API_KEY`
- AI `.env`: `GEMINI_API_KEY`, 선택적으로 `GEMINI_MODEL=gemini-3.5-flash-lite`
- FE `.env.local`: 공개 브라우저 키 `VITE_KAKAO_JAVASCRIPT_KEY`. REST 키·Gemini 키는 FE에 넣지 않습니다. 카카오 앱의 JavaScript 도메인에 실제 접속 출처를 등록해야 합니다.

```powershell
# BE 폴더
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\WOOWANGTHON_TEAM_17_AI

# FE 폴더 — .env.local 변경 시 다시 빌드
npm ci
npm run build

# BE 폴더 — 화면과 API 함께 http://127.0.0.1:8017
.\.venv\Scripts\python.exe run.py
```

AI는 BE가 호출하는 Python 패키지이므로 별도 AI HTTP 서버를 켤 필요는 없습니다. FE 개발 서버를 사용한다면 기존 Vite 프록시와 BE 허용 Origin 설정을 확인합니다. 지도 SDK를 불러오지 못해도 일정 목록과 상세는 계속 사용할 수 있습니다.

## 검증 환경

자동 검사는 `127.0.0.1:3317/osami_entry_test`만 허용하고 실제 HTTPS AI/이동 호출을 차단합니다. 공용 DB나 실제 사용자 방으로 테스트하지 않습니다. `scripts/provision_test_db.py --reset`은 이 격리 대상에서만 동작하며 앱 migration 실행기가 아닙니다. 이번 사용자는 최종 전달 범위를 로컬 Git 커밋으로 제한했습니다. push·PR 생성·추가 Notion 수정은 수행하지 않습니다.

로컬 검증(2026-11-09): BE 전체 pytest 270개, compileall, pip check, git diff --check를 실행했습니다. AI 별도 패키지 회귀는 41 tests / 54 subtests가 통과했습니다. 인접 FE는 npm run check(typecheck/lint/build)와 37 tests가 통과했으며 React effect/ref/dependency lint 경고 9건이 있습니다. BE Starlette/HTTPX 전환 권고 경고 1건은 별도로 남습니다.

실제 Chrome과 별도 인앱 브라우저를 전용 쿠키·격리 MySQL·합성 AI/경로로 검수했습니다. 원문만 저장·nullable AI 제안·명시적 적용/별도 저장·본인 확정·정규화 검토·설정·생성·미확인 동의 저장·새로고침 복원을 확인했습니다. 다른 기기는 저장 일정을 조회하되 방장 원문·생성/저장 버튼이 보이지 않았고, 회원 비활성 후 개인 원문이 DOM에서 제거됐습니다. 모바일 breakpoint의 목록/지도 대체 표시와 가로 넘침 없음도 확인했습니다.

실제 Gemini 모델 목록 조회는 성공했고 설정 모델이 존재함을 확인했습니다. 카카오 자동차·도보·대중교통 경로 응답도 직접 확인했습니다. 이는 전체 일정의 실서비스 정확도·전체 예산 보장 검사가 아닙니다. 실제 브라우저 지도 SDK는 도메인 등록 안내 후에도 `AccessDeniedError: domain mismatched` 401이 남아 지도/마커 표시를 검증하지 못했습니다. JavaScript 키의 **앱 → 플랫폼 키 → JavaScript 키 → JavaScript SDK 도메인**에서 `http://127.0.0.1:8017`을 확인해야 합니다. 제품 링크 관리의 웹 도메인과 별도이며 [공식 지도 가이드](https://apis.map.kakao.com/web/guide/)를 따릅니다. 지도 실패 시 목록·상세는 동작했습니다.

별도 Sol high planner 계획과 Luna high bounded 구현 뒤 독립 Sol high 검토를 실행했습니다. 중복 AI 실행, lease/catalog 최신성, 빈 어휘, UI 버전 갱신·늦은 해석·개인정보 정리·새 공통 버전 자동확정 문제를 수정하고 재검토에서 남은 중요 지적이 없음을 확인했습니다.
