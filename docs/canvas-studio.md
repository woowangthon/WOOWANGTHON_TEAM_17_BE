# Shared canvas backend

## Routes

All routes require the existing device cookie and an ACTIVE membership in an OPEN trip. Writes require the configured Origin, `application/json`, and a canonical UUID `Idempotency-Key`. Successful responses use the established cookie refresh and `Cache-Control: no-store` behavior.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/v1/trips/{tripId}/canvas` | Shared document and hydrated place facts |
| PUT | `/api/v1/trips/{tripId}/canvas` | Full-document compare-and-swap save |
| GET | `/api/v1/trips/{tripId}/canvas/places/{contentid}` | Allowlisted place detail |
| POST | `/api/v1/trips/{tripId}/canvas/recommendations` | Gemini pick and up to five alternatives for user review |
| GET | `/api/v1/trips/{tripId}/canvas/route?sourceContentid=&targetContentid=&expectedConditionsVersion=` | Derived travel time using the existing generation router |

The document uses schema version 3. Validation rejects unknown keys, malformed UUIDs/dates, non-finite or out-of-range positions, comments outside 1–1000 characters, more than 31 days/100 nodes/100 edges/200 KiB, missing references, cross-day or comment edges, branching, cycles, and invalid place IDs. Place nodes are verified against the live catalog before saving. The client cannot supply place metadata.

Canvas documents and idempotency leases are in `trip_canvas_documents` and `trip_canvas_requests`. Install `migrations/004_canvas.up.sql` explicitly before enabling the routes; startup does not create or alter tables. The down migration removes only these two tables.

Every successful PUT increments the revision once. A stale revision returns `409 VERSION_CONFLICT` without changing the stored document. A completed same-key/body retry replays its stored response; a reused key with a different body returns `409 IDEMPOTENCY_KEY_REUSED`. Unfinished or expired request leases return `409 REQUEST_IN_PROGRESS`; recommendation work is not automatically charged again after an unknown outcome.

The recommendation provider sees the request prompt, selected day date, a small allowlist of common trip conditions, and at most 12 ranked, bounded public catalog candidates selected from the trip region. Personal preferences, review data, private evidence, comments, and member scores are not sent. Only the prompt fingerprint is stored. Provider rationale is neither persisted nor returned. Provider IDs must belong to the sent candidate pool or the route returns `502 AI_INVALID_RESPONSE`. Recommendations are limited to five requests per session per minute; a completed idempotent replay does not count again.

Place details are read from allowlisted columns in `places` and the sole matching `place_details` row. Ambiguous detail rows are omitted. Homepage links are exposed only for HTTP(S); descriptive text is returned as text data and must be rendered as text by clients. Unknown fees remain unknown.

The initial canvas is blank, with deterministic day IDs from the shared trip date range (or one undated day when no valid date range exists). No fake/sample locations are created. The initial state does not copy a generated plan; `sourcePlan` is null.

## Existing AI and routing reuse

The merged generation service already orchestrates full-itinerary selection, AI drafting, route validation, and explicit plan saving. These APIs and their review gates remain unchanged. A canvas single-place request uses a separate bounded picker because it does not generate a full itinerary; it reuses the adjacent AI package's Gemini settings/model/key configuration and the existing DB catalog rather than replacing the merged generation pipeline.

Canvas travel-time reads reuse `app.state.generation_router` and the existing route-fact validation. They resolve coordinates and transport from the DB, never client-supplied metadata. A condition-version mismatch, or a condition/coordinate change during the lookup, returns `409 STALE_ROUTE_INPUT`. Authorization is checked before and after work, including cache hits. External work holds no DB connection. A bounded short-lived cache, same-query merging, provider concurrency limit, and actor rate limit limit duplicate calls. Missing coordinates, unset/unsupported transport, and provider failures remain unknown; no straight-line time estimate is substituted. These facts do not approve a complete itinerary.

## Local checks

Run the canvas-only SQLite suite with `.venv/Scripts/python.exe -m pytest -q tests/test_canvas_core.py tests/test_canvas_api.py`. These checks use in-process SQLite and a fake recommendation provider. They do not connect to MySQL or call Gemini.

2026-11-11 최종 오프라인 회귀 검사: `.venv/Scripts/python.exe -m pytest -q -m 'not mysql'`에서 142개 통과, 전용 MySQL 대상 172개 제외. 신규 구간 조회는 `tests/test_canvas_routes.py`의 fake router로 권한 철회·조건/좌표 변경·동일 장소·캐시·동시 호출·응답 검증을 확인했다. 기존 라이브러리 deprecation 경고 46건은 별도 기록이며 실제 Gemini·Kakao·공용 DB API 쓰기나 서버 시작은 하지 않았다. 자동 검사에는 외부 provider 전송 차단을 적용한다.

## 작업 환경의 명시적 스키마 적용

2026-11-11, 코드·migration 검토와 SQLite 검사 후 대상 호스트 및 `SELECT DATABASE()`가 지정한 `chungbuk_trip1`과 일치함을 확인하고 migration 004의 CREATE TABLE 두 문장만 명시적으로 적용했다. `trip_canvas_documents`, `trip_canvas_requests`의 존재를 확인했으며 앱 기동 시 자동 DDL은 없다. 공용 DB에 검수용 여행방·댓글·추천 데이터를 넣거나 기존 데이터를 초기화하지 않았다. 다른 환경은 별도 적용이 필요하며 이미 존재하는 테이블에 up migration을 다시 실행하지 않는다.

공용 DB의 실제 API 저장·두 브라우저 동기화·실제 Gemini 호출·화면 검수는 수행하지 않았다. 서버는 사용자가 기존 `run.py`로 실행한다. 이미 실행 중인 프로세스는 사용자가 정지 후 다시 실행해야 새 route 등록이 적용된다.

동일 환경의 읽기 전용 준비 확인에서 카탈로그 1,857곳, 지역 전체/청주 후보 조회와 장소 상세 hydrate, 신규 테이블 두 개의 존재를 확인했다. Gemini 설정은 존재하지만 실제 provider 호출은 하지 않았다. 현재 BE `.env`와 이 작업 프로세스에는 `KAKAO_REST_API_KEY`가 없어 실제 구간 시간은 미확인으로 반환한다. 키를 개인 환경에 설정한 후 사용자가 재실행해야 한다. 실제 카카오 도보·대중교통 제공 범위도 별도 실호출 검수 대상이다.
