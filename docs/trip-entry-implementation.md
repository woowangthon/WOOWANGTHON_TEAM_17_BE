# 여행방 시작 구현 범위

기준은 [여행방 시작](https://app.notion.com/p/40ee977e49088105a9b4ce4df0aef6f8)과 [공통 규칙](https://app.notion.com/p/40ee977e49088189b512e6e817984d48)입니다. API 규격 원본은 Notion이며 이 문서는 구현과 실행 방법을 설명합니다.

생성 입력은 방 이름과 닉네임입니다. 인원·출발지·출발 시간·여행 날짜·예산·이동수단은 생성 이후의 별도 기능에서 설정합니다. 생성 시 공통 조건 행은 미정 값(NULL)을 유지합니다. 개인 취향·AI 일정 생성·방 내부 화면은 이번 범위에 포함하지 않습니다.

## API

| Method | Path | 동작 |
| --- | --- | --- |
| POST | `/api/v1/device-session` | 기기 쿠키 세션 시작/재사용 |
| POST | `/api/v1/trips` | 여행방과 방장을 함께 생성 |
| POST | `/api/v1/trip-memberships` | 초대 코드로 참여/같은 기기 기존 본인 유지 |
| GET | `/api/v1/device-session/trips` | 본인 기기의 최근 여행 조회 |
| POST | `/api/v1/trips/{tripId}/reconnect` | 본인의 기존 여행방 재접속 |

최근 여행 API는 기존 서버 계약을 지원하기 위한 것입니다. 시작 화면에 최근 목록 UI를 추가하는 작업은 포함하지 않습니다.

## 데이터와 운영 적용

기존 `trips`, `trip_members`, `trip_conditions`의 구조를 유지합니다. 추가되는 `device_sessions`, `device_session_memberships`, `api_idempotency`는 각각 기기 자격의 해시, 기기별 회원 연결, 재시도 결과를 영속 보관합니다.

앱 import/실행은 DB 테이블을 생성하거나 바꾸지 않습니다. 운영 적용 전에 기존 스키마와 추가 migration을 검토하고 담당자가 `migrations/001_device_entry.up.sql`을 명시적으로 적용해야 합니다. `down.sql`은 추가 인증·매핑·중복 방지 기록을 삭제하므로 기존 기기 재접속과 과거 요청 재시도 보장이 소실됩니다. 원본 덤프 복원이나 기존 테이블 재생성은 하지 않습니다.

사용자의 후속 연결 요청에 따라 공용 개발 DB `chungbuk_trip1`을 확인하고 위 up migration을 한 번 명시적으로 적용했습니다. 기존 26개 테이블의 SHOW CREATE TABLE 해시가 유지됐고 3개 추가 후 29개입니다. 기존 여행방 관련 세 테이블의 제약과 호환성을 확인했습니다. 실제 앱 연결로 DB 이름과 UTC 세션을 확인했으며, 공용 DB에 자동 테스트 데이터 생성·초기화·down migration을 실행하지 않았습니다. 개인 `.env`만 연결 대상으로 갱신했고 접속 정보는 저장소에 포함하지 않습니다. 이 적용을 다른 환경의 적용 완료로 간주하지 않습니다.

동일한 생성·참여 요청의 재시도는 기기 세션, method, path, Idempotency-Key로 구분합니다. MySQL 연결별 named lock으로 같은 요청의 동시 처리를 차단하고, 도메인 변경과 응답 기록을 하나의 트랜잭션으로 확정합니다. 커밋 전 실패는 전체 rollback, 커밋 후 응답 유실은 저장된 결과 재사용으로 처리합니다. 재사용 전에도 현재 세션/참여/방 상태를 다시 확인합니다.

요청 제한기는 한 프로세스 안에서 동작합니다. 초기 실행은 Uvicorn worker 1개를 기준으로 하며 여러 프로세스나 서버로 확대하기 전에 공유 제한 저장소가 필요합니다. 검증 전 프록시 헤더를 신뢰하지 않습니다.

## FE 연결 조건

FE는 같은 출처 API 프록시 또는 HTTPS 동일 출처 환경에서 쿠키를 포함해 호출해야 합니다. `file://`로 직접 연 HTML은 운영 쿠키 인증용 실행 환경이 아닙니다.

첫 호출은 기기 세션 bootstrap입니다. 같은 브라우저의 여러 탭에서 초기 bootstrap이 겹치지 않도록 FE가 Web Locks 등의 탭 간 잠금을 적용해야 합니다. 생성/참여의 네트워크 재시도에서는 같은 입력과 Idempotency-Key를 유지합니다. 새로운 작업이나 입력 변경은 새 키를 사용합니다.

이 서버의 HTTP/DB 검사만으로 FE 탭 간 처리나 실제 화면 연동을 완료했다고 간주하지 않습니다. 검수한 시작·방 진입 화면은 인접 FE 저장소의 HTML/JavaScript로 관리하며 Vite가 빌드합니다. BE 루트 `run.py`는 FE의 `dist/`를 같은 출처로 제공합니다. 예전 임시 데모 폴더는 실행에 필요하지 않습니다. 실행과 수동 검수 항목은 [로컬 검수 안내](local-review.md)를 따릅니다.

## 테스트 환경

일반 테스트는 `.env`와 실제 DB를 사용하지 않습니다. MySQL 통합 검사는 `TEST_DATABASE_URL`에 명시된 `127.0.0.1:3317/osami_entry_test`만 허용합니다. 실행 중인 일반 MySQL 서비스와 별도로, 새 임시 데이터 디렉터리를 초기화한 격리 인스턴스를 사용합니다. 서버 실행 전 포트 충돌을 확인하고, 종료할 때는 해당 격리 인스턴스만 종료합니다.

전용 인스턴스가 준비된 뒤 아래 명령으로 문서의 세 기존 테이블 DDL과 추가 migration을 명시적으로 적용합니다. `--reset`은 이 전용 스키마의 여섯 테스트 테이블과 테스트 데이터만 재생성합니다.

```powershell
$env:TEST_DATABASE_URL = 'mysql+pymysql://root@127.0.0.1:3317/osami_entry_test'
.\.venv\Scripts\python.exe scripts/provision_test_db.py
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe -m compileall -q app tests scripts
```

현재 실행 결과와 독립 검토 결과는 PR에 기록합니다. 공용 DB 연결 확인과 격리 DB의 CRUD 검증을 구분하며, 정식 FE 통합 검증은 별도 절차입니다.

## 이번 구현의 검증 결과

- 격리 MySQL 8.0.41에서 `pytest -q tests`: 기존 API/DB 54개와 실행 파일 15개를 합쳐 69 passed. 외부 Starlette/HTTPX 전환 권고 경고 1개가 있으며 실패는 없습니다.
- `compileall -q app tests scripts`, `pip check`, `git diff --check` 통과.
- 실제 Uvicorn HTTP 서버에서 `/docs` 200, API 경로 5개, 생성 201, 다른 기기 초대 참여 201, 같은 생성 요청 재시도 201 및 동일 결과, 방장 재접속 200, 실제 참여자 수 2명 확인.
- 독립 검토에서 발견한 이벤트 루프의 동기 DB 호출, DB 연결의 UTC 설정 누락, 테스트 DB URL 쿼리 우회 문제 3건을 수정하고 재검토했습니다. DB 작업은 연결·잠금·트랜잭션 단위로 worker thread에서 실행합니다.
- 생성 후 공통 조건 미정, 중복/동시 요청, 재시작 후 재시도, 실패 중 rollback, 쿠키/권한, 목록 cursor, migration down/up 시 기존 세 테이블 DDL 보존을 확인했습니다.
- 자동 검사와 Uvicorn CRUD 검증은 격리 테스트 DB에서 수행했습니다. 사용자 요청으로 공용 `chungbuk_trip1` 연결과 추가 테이블 적용도 완료했으며, 공용 DB에는 동일 테스트를 실행하지 않았습니다.
- 후속 FE 빌드를 `run.py`의 기본 실행 경로로 연결했습니다. 격리 DB에서 실제 두 탭 방 생성·재접속과 다른 브라우저 초대 참여·기존 본인 유지·코드 복사를 확인했습니다. 생성 방 2개와 회원 3개, 두 방장의 동일 기기 세션 연결, 조건 NULL을 DB에서 대조했습니다. 이 검증은 전용 쿠키 이름을 사용해 공용 DB용 브라우저 기기 자격을 덮어쓰지 않았습니다. FE 최근 목록 UI와 여행 조건 설정은 이번 범위 밖입니다.
