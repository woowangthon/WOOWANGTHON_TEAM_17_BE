# 여행 준비·개인 조건 백엔드

구현 기준은 [여행 준비·개인 조건 계약](https://app.notion.com/p/412e977e49088197b345d73603c6a27f)과 [공통 규칙](https://app.notion.com/p/40ee977e49088189b512e6e817984d48)입니다. 관련 작업은 BE #10, AI #2, FE PR #16입니다.

## HTTP 기능

| Method | Path | 권한·동작 |
| --- | --- | --- |
| GET | `/api/v1/trips/{tripId}/preparation` | 본인 ACTIVE 참여 방의 공통 조건·공개 준비 상태 |
| PUT | `/api/v1/trips/{tripId}/conditions` | OWNER만 공통 조건 전체 저장 |
| GET | `/api/v1/trips/{tripId}/preferences/me` | 본인 초안·현재 확정 상태 |
| PUT | `/api/v1/trips/{tripId}/preferences/me` | 두 버전 일치 시 본인 초안 전체 저장 |
| POST | `/api/v1/trips/{tripId}/preferences/me/interpret` | 저장된 본인 원문의 AI 제안 |
| POST | `/api/v1/trips/{tripId}/preferences/me/confirm` | 본인이 검토한 현재 두 버전 직접 확정 |

기존 HttpOnly 기기 쿠키와 세션·참여 매핑을 재사용합니다. 모든 요청은 ACTIVE 본인 참여와 OPEN 방을 확인하며, 쓰기 요청은 Origin과 JSON 형식을 검사합니다. 응답은 `Cache-Control: no-store`이며 성공 시 기기 쿠키를 갱신합니다. OWNER에게도 다른 참여자의 개인 원문이나 정리 조건을 반환하지 않습니다.

## 문서 저장과 버전

이번 v1의 지역·출발/복귀 장소·숙박·고정 일정은 사용자 텍스트입니다. `charter`, `walk`도 그대로 보존합니다. 옛 구조화 테이블의 지역 코드·좌표·숙박 boolean·고정 일정 배열·CAR/PUBLIC_TRANSIT에 변환하지 않습니다.

| 새 테이블 | 저장 내용 |
| --- | --- |
| `trip_preparation_documents` | 공통 JSON 문서와 조건 버전 |
| `member_preparation_documents` | 본인 JSON 초안, 개인 버전, 확정한 두 버전과 UTC 시각 |
| `preparation_interpret_requests` | 세션·실제 HTTP 경로·key별 AI 실행 lease와 24시간 성공 제안 |

최초 조회는 문서 버전 0과 계약 기본값을 반환하며 문서를 생성하지 않습니다. 저장은 현재 버전을 비교해 1씩 증가시킵니다. 충돌은 409이며 원래 값과 확정을 유지합니다. 공통 저장은 전원 확정을 해제하고, 개인 저장은 본인 확정만 해제합니다. 동일한 현재 두 버전의 확정 재요청은 기존 확정 시각을 유지합니다.

세션→여행방→참여 매핑→회원→문서 순으로 잠급니다. 여행방 행은 공통 수정·개인 수정·확정·준비 조회 및 기존 가입과의 일관성 경계입니다. 공개 readiness에는 ACTIVE 참여자의 작성/확정 상태만 포함됩니다. 예정 인원, 실제 ACTIVE 인원, 필수 조건, 현재 두 버전의 전원 확정을 함께 검사합니다. 이 readiness는 일정 생성 지원 여부와 별개입니다.

## AI 제안

AI 저장소 `chungbuk-trip-ai` 0.2.0의 공개 함수 `interpret_personal_draft(original_text, conditions)`를 adapter로 호출합니다. 기존 미리보기 CLI도 유지됩니다. 정확한 함수 구현은 AI #2에서 별도로 검토합니다.

BE 개발환경에서 AI 저장소의 해당 버전을 설치합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ..\WOOWANGTHON_TEAM_17_AI
```

AI 제안 요청에는 UUID `Idempotency-Key`와 원본의 두 버전이 필요합니다. 권한·입력·버전·회원/세션 각각 분당 5회 제한을 확인한 후 짧은 트랜잭션으로 실행을 예약하고 연결을 닫습니다. 외부 처리 중 DB 잠금과 named lock을 유지하지 않습니다.

provider 처리에는 전체 60초 deadline을 적용합니다. 응답 전 별도 트랜잭션에서 권한·두 버전·실행 token을 다시 확인합니다. 원문·수동 정리·공통 조건이 바뀌면 `STALE_AI_RESULT`로 제안을 반환하지 않습니다. 성공 replay도 현재 권한과 버전을 검사합니다. 실행 lease와 token으로 장애 후 재시도와 늦은 실행 결과를 구분합니다. timeout·실패·잘못된 응답은 성공 결과로 고정하지 않습니다.

제안은 원문을 제외한 허용된 여섯 필드와 `needsWalkingAnswer`만 반환하며, 원문·초안·확정 상태를 자동 변경하지 않습니다. 사용자가 검토한 값을 PUT으로 저장하고 현재 두 버전으로 직접 확정해야 합니다. 실제 Google 비용을 발생시키는 검사는 이 기능의 자동 검사에 포함하지 않습니다.

## 명시적 migration과 rollback

`migrations/002_preparation.up.sql`은 위 세 테이블만 추가합니다. 기존 여행방 시작 migration 001과 호환되는 여행방·회원·기기 세션이 선행되어야 합니다. 앱 시작 시 자동 적용하지 않으며, 공용 `chungbuk_trip1`에는 이번 작업에서 적용하지 않습니다. 담당자가 대상 DB와 기존 구조를 확인한 후 별도 적용해야 합니다.

`002_preparation.down.sql`은 새 세 테이블을 역순 제거합니다. v1 공통·개인 문서와 확정·AI 재시도 기록이 소실됩니다. 기존 구조화 데이터와 여행방·회원·기기 세션은 유지합니다. MySQL DDL은 암묵적 commit이 있으므로 up 실행 전체가 일반 트랜잭션 rollback으로 복구된다고 가정하지 않습니다.

## 격리 검증

프로젝트 검사·provision 도구는 `127.0.0.1:3317/osami_entry_test`만 허용합니다. `.env`와 공용 DB URL을 사용하지 않습니다. 테스트 서버를 별도로 준비한 뒤 다음을 실행합니다. `--reset`은 이 전용 스키마의 명시된 테스트 테이블만 재생성합니다.

```powershell
$env:TEST_DATABASE_URL = 'mysql+pymysql://root@127.0.0.1:3317/osami_entry_test'
.\.venv\Scripts\python.exe scripts/provision_test_db.py --reset
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe -m compileall -q app tests scripts run.py
.\.venv\Scripts\python.exe -m pip check
```

AI는 모든 HTTP 검사에서 stub으로 대체합니다. 정상·실패·timeout·형식 오류, 처리 중 원문/정리/공통 조건 변경, LEFT/CLOSED/폐기, replay·동시 요청·서버 재시작·만료 lease를 검사합니다. 조건/확정 경합, 전원·본인 재확인, 개인정보 비노출, 기존 구조화 필드 미오염과 migration up/down의 기존 행·DDL 보존도 검사합니다. 최종 명령·검사 수·검토 결과는 BE PR에 기록합니다.

## FE 후속 연결

FE PR #16의 화면은 아직 신규 API에 연결되지 않은 초안입니다. 후속 FE 작업에서 아래를 연결합니다.

- 준비 화면 진입과 재접속 뒤 GET preparation / preferences 복원
- 정수·null 정규화와 OWNER 저장 버튼, 서버의 개인정보 접근 범위 준수
- 버전 기반 저장·409 충돌 안내·재조회와 사용자 재확인
- 원문 저장 후 UUID key로 AI 제안 요청, 제안 검토 후 별도 PUT
- 직접 확정과 전원 준비 상태 갱신
- 늦은 응답·화면 이동·개인 입력 변경 시 오래된 제안 폐기
- 실제 FE·격리 BE에서 두 기기의 편집·충돌·확정·새로고침 검증

실제 일정 생성·지도·변경 이력은 이번 범위에 포함하지 않습니다.
