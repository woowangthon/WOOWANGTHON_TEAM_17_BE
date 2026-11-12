# Shared canvas backend

## Routes

All routes require the existing device cookie and an ACTIVE membership in an OPEN trip. Writes require the configured Origin, `application/json`, and a canonical UUID `Idempotency-Key`. Successful responses use the established cookie refresh and `Cache-Control: no-store` behavior.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/v1/trips/{tripId}/canvas` | Shared document and hydrated place facts |
| PUT | `/api/v1/trips/{tripId}/canvas` | Full-document compare-and-swap save |
| GET | `/api/v1/trips/{tripId}/canvas/places/{contentid}` | Allowlisted place detail |
| POST | `/api/v1/trips/{tripId}/canvas/recommendations` | Gemini pick and up to five alternatives for user review |
| GET | `/api/v1/trips/{tripId}/canvas/route?sourceContentid=&targetContentid=&expectedConditionsVersion=&mode=` | Derived travel time using the existing generation router; optional `mode` override and opt-in route geometry |

The backend reads and writes schema versions 3 and 4. Version 3 retains its legacy shape. Version 4 adds optional finite day positions bounded to ±100,000 and `dayLinks` (`id`, `source`, `target`), with at most 500 directed links to existing days. Self-links, duplicate IDs, and duplicate directed pairs are rejected. Ordinary edits and reopening the same imported version retain empty/custom/deleted links. The first successful import of a new plan version replaces the entire canvas with only the new plan dates, places, edges and default chronological day links. Previous manual places, candidates, comments, custom days and positions are discarded at that atomic import, including when shrinking, expanding or shifting the date range. Failure leaves the prior canvas intact. Reopening an already imported version never performs another reset or restores deleted cards. Version 4 saves are never downgraded by an older version 3 client: a matching-revision downgrade returns `409 SCHEMA_VERSION_CONFLICT` so the client can reload; stale revisions continue to return `409 VERSION_CONFLICT`.

Validation rejects unknown keys, malformed UUIDs/dates, non-finite or out-of-range positions, comments outside 1–1000 characters, more than 31 days/500 nodes/500 edges/1 MiB, missing references, cross-day or comment edges, branching, cycles, and invalid place IDs. Place nodes are verified against the live catalog before saving. The client cannot supply place metadata. Place hydration includes a safe HTTP(S) `imageUrl` from `places.firstimage` and uses the stored `place_details.feeText`. When that text is blank, v4 place details can include `priceLabel` from role-matched typed price tables, with 1-person, per-night, and lodging-basis labels stated explicitly; zero is retained. Missing optional typed tables yield no label. To retain exact response compatibility for v3 clients, clients opt into `imageUrl` and `priceLabel` by sending `X-Canvas-Version: 4`; absent or older values omit both fields, including for place detail and recommendation responses.

Canvas documents and idempotency leases are in `trip_canvas_documents` and `trip_canvas_requests`. Install `migrations/004_canvas.up.sql` and `migrations/005_canvas_v4.up.sql` explicitly before enabling v4 writes; startup does not create or alter tables. The down migration for 004 removes only these two tables.

PUT requires `{expectedRevision, expectedSourcePlan, document}`. `expectedSourcePlan` is exactly `{planId, versionId}` or null, captured from the acknowledged canvas. A different source or stale revision returns `409 VERSION_CONFLICT` without a write; a missing source field is a 400 validation error requiring an updated FE bundle. Every successful PUT increments the revision once. The source fence is part of the body fingerprint. A completed same-key/body retry replays its stored response; a reused key with a different body returns `409 IDEMPOTENCY_KEY_REUSED`. FE must ignore an old replay after it has observed a higher revision from a different source. Unfinished or expired request leases return `409 REQUEST_IN_PROGRESS`; recommendation work is not automatically charged again after an unknown outcome. The fence uses existing source columns and needs no migration.

The recommendation provider sees the request prompt, selected day date, a small allowlist of common trip conditions, and at most 12 ranked, bounded public catalog candidates selected from the trip region. Personal preferences, review data, private evidence, comments, and member scores are not sent. Only the prompt fingerprint is stored. Provider rationale is neither persisted nor returned. Provider IDs must belong to the sent candidate pool or the route returns `502 AI_INVALID_RESPONSE`. Recommendations are limited to five requests per session per minute; a completed idempotent replay does not count again.

Place details are read from allowlisted columns in `places` and the sole matching `place_details` row. Ambiguous detail rows are omitted. Homepage links are exposed only for HTTP(S); descriptive text is returned as text data and must be rendered as text by clients. Unknown fees remain unknown.

The initial canvas is blank, with deterministic day IDs from the shared trip date range (or one undated day when no valid date range exists). No fake/sample locations are created. The initial state does not copy a generated plan; `sourcePlan` is null.

## Existing AI and routing reuse

The merged generation service already orchestrates full-itinerary selection, AI drafting, route validation, and explicit plan saving. These APIs and their review gates remain unchanged. A canvas single-place request uses a separate bounded picker because it does not generate a full itinerary; it reuses the adjacent AI package's Gemini settings/model/key configuration and the existing DB catalog rather than replacing the merged generation pipeline.

Canvas travel-time reads reuse `app.state.generation_router` and the existing route-fact validation. An optional `mode` query selects `car`, `walk`, `transit`, or `charter`; when omitted, the shared trip transport remains in effect. The override is read-only and is never saved to trip conditions. To return the validated provider coordinates, clients opt in with `X-Canvas-Route-Geometry: 1`; absent or other header values preserve the exact legacy response shape. Opted-in responses add `geometry`, an array of at most 2,048 finite WGS84 `{latitude, longitude}` points. Geometry is exposed only when route facts are verified; unknown results return `geometry: []`. Provider geometry passes the existing route-fact validation and is copied into the bounded route cache. The configured Kakao router has car, walking, and public-transit adapters; provider coverage or unavailable routes remain unknown. Charter is not supported by that router and returns an unknown result without a provider call. Conditions version and the underlying shared transport are still checked before and after lookup, even with an override. A condition-version mismatch, or a condition/coordinate change during the lookup, returns `409 STALE_ROUTE_INPUT`. Authorization is checked before and after work, including cache hits. External work holds no DB connection. A bounded short-lived cache, same-query merging, provider concurrency limit, and actor rate limit limit duplicate calls. Missing coordinates, unset/unsupported transport, and provider failures remain unknown; no straight-line time estimate is substituted. These facts do not approve a complete itinerary.

## Local checks

현재 전체 교체 후속: non-MySQL 207개 통과, MySQL 177개 제외. 3→2→3일과 날짜 전체 이동을 연속 검증하고, 이전 수동 장소·댓글·후보·사용자 날짜·배치가 새 결과에 남지 않는지 확인했다. 같은 버전 재시도와 삭제 보존, 실패/CAS rollback, source fence·fingerprint·옛 탭 거부, 일반 장소 교체 및 undo의 메타데이터 규칙도 통과했다. 실제 Gemini/Kakao 호출·공용 DB 쓰기나 서버 기동은 수행하지 않았다. 이전 아래 검증 기록 중 수동 카드 보존은 과거 append 정책이며 현재 규칙이 우선한다.

Run the canvas-only SQLite suite with `.venv/Scripts/python.exe -m pytest -q tests/test_canvas_core.py tests/test_canvas_api.py`. These checks use in-process SQLite and a fake recommendation provider. They do not connect to MySQL or call Gemini.

2026-11-11 최종 오프라인 회귀 검사: `.venv/Scripts/python.exe -m pytest -q -m 'not mysql'`에서 142개 통과, 전용 MySQL 대상 172개 제외. 신규 구간 조회는 `tests/test_canvas_routes.py`의 fake router로 권한 철회·조건/좌표 변경·동일 장소·캐시·동시 호출·응답 검증을 확인했다. 기존 라이브러리 deprecation 경고 46건은 별도 기록이며 실제 Gemini·Kakao·공용 DB API 쓰기나 서버 시작은 하지 않았다. 자동 검사에는 외부 provider 전송 차단을 적용한다.

## 작업 환경의 명시적 스키마 적용

2026-11-12 기간 확대 후속은 non-MySQL 203개 통과, MySQL 177개 제외로 검증했다. 2일→3일 가져오기에서 이전 자동 카드 교체, 수동 장소·댓글·대안·남은 선 보존, 날짜별 새 source/schedule, 메타데이터 위조 거부, 정상 순차 날짜 연결 확장, 빈/사용자/끊긴 연결 보존과 같은 버전 재호출의 삭제 카드 복원 방지를 확인했다. in-process SQLite/fake provider 검사이며 새 migration이나 공용 DB 쓰기는 없다. 별도 Sol high 검토에서 조치할 finding은 없었다. 실제 서버는 사용자가 기존 `run.py`로 다시 실행한다.

2026-11-11, 코드·migration 검토와 SQLite 검사 후 대상 호스트 및 `SELECT DATABASE()`가 지정한 `chungbuk_trip1`과 일치함을 확인하고 migration 004의 CREATE TABLE 두 문장만 명시적으로 적용했다. `trip_canvas_documents`, `trip_canvas_requests`의 존재를 확인했으며 앱 기동 시 자동 DDL은 없다. 공용 DB에 검수용 여행방·댓글·추천 데이터를 넣거나 기존 데이터를 초기화하지 않았다. 다른 환경은 별도 적용이 필요하며 이미 존재하는 테이블에 up migration을 다시 실행하지 않는다.

공용 DB의 실제 API 저장·두 브라우저 동기화·실제 Gemini 호출·화면 검수는 수행하지 않았다. 서버는 사용자가 기존 `run.py`로 실행한다. 이미 실행 중인 프로세스는 사용자가 정지 후 다시 실행해야 새 route 등록이 적용된다.

동일 환경의 읽기 전용 준비 확인에서 카탈로그 1,857곳, 지역 전체/청주 후보 조회와 장소 상세 hydrate, 신규 테이블 두 개의 존재를 확인했다. Gemini 설정은 존재하지만 실제 provider 호출은 하지 않았다. 최초 검사에서는 카카오 REST 키가 없었으며, 사용자 추가 후 `KAKAO_REST_API_KEY` 설정의 존재를 확인했다. 개인 키의 값은 출력하거나 커밋하지 않았다. 키를 읽도록 사용자가 서버를 재실행해야 하며, 실제 카카오 도보·대중교통 제공 범위와 응답은 별도 실호출 검수 대상이다.

## 2026-11-12 경로 좌표 응답 검증

- 백엔드만 변경했다. `X-Canvas-Route-Geometry: 1` 요청만 geometry를 추가하며 헤더 없음/다른 값은 기존 응답 형식을 보존한다. FE 연결·화면 변경은 포함하지 않는다.
- `python -m pytest -q tests/test_canvas_routes.py tests/test_routing_validation.py`: 38개 통과. SQLite·fake provider로 응답 호환성, 캐시, 잘못된 geometry와 기존 권한/버전/좌표 fence를 검사했다.
- 격리 SQLite의 동일 endpoint에 실제 기존 KakaoRouter를 연결하고 공개 관광지 도담삼봉→단양 구경시장을 조회했다. 자동차 verified/79점/4,011m, 도보 verified/78점/3,516m를 반환했고 각 mode의 무헤더 재요청에는 geometry가 없었다. 거리·시간은 조회 시점 값이며 모든 구간의 지원을 보장하지 않는다. 키는 BE 개인 설정에서만 읽고 출력·복사하지 않았다. 공용 DB/실제 여행방은 사용하지 않았다.
- 전체 `python -m pytest -q -m 'not mysql'`: AI를 최신 main `282e6f8`로 fast-forward한 뒤 210개 통과·MySQL 177개 제외. 첫 실행의 AI 관련 9개 실패는 로컬 AI가 `7c2be6f`에 머물러 발생했으며 최신 AI 반영으로 모두 해소됐다. BE 가상환경은 인접 AI 소스를 editable로 불러오고 있어 재설치 없이 반영됐다. 초기 실패를 미해결 문제로 남기지 않는다.
- Sol high 독립 코드 검토: 미해결 P1/P2 없음. 별도 lint/typecheck 명령은 프로젝트에 등록되지 않아 실행하지 않았다. backend-only 응답 변경으로 브라우저 검수는 해당 없음이다.
- DB·migration·제공자·비밀키 설정은 변경하지 않았다. 기존 서버를 재시작하면 새 응답을 사용할 수 있으며 FE가 opt-in 헤더와 geometry 표시를 연결하는 작업은 별도 범위다.
