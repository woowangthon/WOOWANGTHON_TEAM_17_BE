# WOOWANGTHON_TEAM_17_BE

충북 단체여행 서비스 FastAPI/MySQL 백엔드입니다. 현재 구현 범위는 기기 세션, 여행방 생성·초대 참여·최근 여행 조회·재접속입니다. 인원과 여행 조건은 방 생성 이후 설정하며 시작 API에서 임의 확정하지 않습니다.

Python 3.12 이상과 MySQL 8.0.16 이상이 필요합니다. 설치와 실행:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

현재 여행방 화면과 API를 함께 보려면 인접한 FE 저장소에서 `npm ci`, `npm run build`를 먼저 실행합니다. 이후 파이참에서 BE의 `run.py`를 프로젝트 `.venv` 인터프리터로 실행합니다. 터미널에서는 `.\.venv\Scripts\python.exe run.py`입니다. 접속 주소는 `http://127.0.0.1:8017/`이며 프로젝트 루트의 개인 `.env`를 읽습니다. 화면은 기본적으로 인접한 `../WOOWANGTHON_TEAM_17_FE/dist` 빌드를 사용합니다. 다른 위치는 `FRONTEND_DIR`로 지정할 수 있습니다. [실행·검수 안내](docs/local-review.md)를 확인하세요.

## 데이터베이스 설정

`.env`에서 `DATABASE_URL`을 설정하거나, 다음 `DB_*` 변수를 모두 설정할 수 있습니다. 같은 설정 원본에서 `DATABASE_URL`이 있으면 이를 우선 사용합니다. SQLAlchemy URL 문법으로 만들기 때문에 비밀번호에 `@`, `:`, `/`, `%` 등이 있으면 percent-encoding이 필요합니다. 단순히 설정하려면 `DB_*` 방식을 사용하세요.

```dotenv
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USERNAME=your_user
DB_PASSWORD=your_password
DB_NAME=chungbuk_trip1
```

`run.py`와 DB 연결 점검 CLI의 설정 우선순위는 OS 환경변수 `DATABASE_URL`, 완전한 OS `DB_*`, `.env`의 `DATABASE_URL`, `.env`의 `DB_*` 순입니다. 일부 OS `DB_*`만 설정하면 설정 오류를 표시합니다. Uvicorn의 `--env-file`로 직접 실행하면 먼저 병합된 환경에서 `DATABASE_URL`이 우선하므로 DB 설정 방식 하나를 선택하세요. 설정은 비밀번호를 출력하지 않으며, API는 테이블을 자동 생성·변경하지 않습니다.

여행방 API는 명시적 SQL migration을 사용합니다. 사용자 지정 공용 개발 DB `chungbuk_trip1`에는 지원 테이블 3개가 추가됐습니다. 테스트 준비 및 API 호출 조건은 [여행방 시작 구현 문서](docs/trip-entry-implementation.md)를 확인하세요. DB 스키마의 최신 확인 범위는 [DB 구현 기준](docs/database-schema.md)을 따릅니다. 원본 dump를 실행하거나 공용 DB를 자동 초기화하지 마세요.

API 입력·응답·인증·오류는 [Notion 여행방 시작 명세](https://app.notion.com/p/40ee977e49088105a9b4ce4df0aef6f8)를 따릅니다. 실행 후 `/docs`에서 API 문서를 볼 수 있습니다. 회원가입 대신 기기 쿠키를 사용하며, 일반 실행은 worker 1개를 기준으로 합니다.

## 여행 선호 미리보기 (내부 CLI)

여행 선호 추출 코드는 별도 [AI 저장소](https://github.com/woowangthon/WOOWANGTHON_TEAM_17_AI.git)에서 관리합니다. 로컬 개발 환경을 준비한 뒤 AI 저장소를 인접 디렉터리에 clone하고 같은 가상환경에 설치합니다.

```powershell
git clone https://github.com/woowangthon/WOOWANGTHON_TEAM_17_AI.git ..\WOOWANGTHON_TEAM_17_AI
.\.venv\Scripts\python.exe -m pip install -e ..\WOOWANGTHON_TEAM_17_AI
```

AI 프로젝트 루트의 개인 `.env`에 `GEMINI_API_KEY`를 설정합니다. 선택 가능한 `GEMINI_MODEL` 기본값은 `gemini-3.5-flash-lite`입니다. AI 패키지는 OS 환경변수를 우선하고 해당 AI 프로젝트의 `.env`만 읽습니다. 이 저장소의 `.env`에 Gemini 키를 두지 마세요.

`app.trip_preview`는 날짜, 교통, 전체 예산(원), 출발 시간/지역 및 방 정보의 인원수를 고정한 채 자유 메모에서 지역·필수 조건·제외·선호·확인 질문을 구조화합니다. 고정값과 메모가 충돌하면 확인이 필요한 미리보기 상태로 반환합니다. 이 CLI는 공용 API endpoint를 만들거나 결과를 DB에 저장하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m app.trip_preview ..\WOOWANGTHON_TEAM_17_AI\examples\trip_input.json
```

출력은 `pending_review` 미리보기입니다. 사람이 확인하기 전에는 확정 일정으로 사용하지 마세요. 오류 응답을 자동 재시도하지 않습니다. 검사는 Gemini를 호출하지 않고 가짜 HTTPS 응답을 사용합니다.

## 검증

여행 준비·개인 조건의 신규 6개 API, 버전·개인정보·AI 제안 처리와 명시적 migration 002는 [구현·검증 안내](docs/preparation-implementation.md)를 확인하세요. AI 해석에는 별도 AI 패키지 0.2.0의 공개 함수를 사용하며, FE 화면의 신규 API 연결은 후속 작업입니다.

검증된 의존성 버전은 `requirements.lock`에 기록했습니다. 같은 환경을 재현할 때는 먼저 `pip install -r requirements.lock`, 이후 `pip install -e . --no-deps`를 사용할 수 있습니다. 테스트는 HTTPX 기반 TestClient로 실행하며, 현재 라이브러리의 HTTPX2 전환 권고 경고는 동작 검증 결과와 별도로 기록합니다.

```powershell
# 격리된 MySQL과 TEST_DATABASE_URL을 명시적으로 준비한 경우에만 실행
.\.venv\Scripts\python.exe -m pytest -q tests
# 공용 DB에 연결하지 않는 테스트
.\.venv\Scripts\python.exe -m pytest -m "not mysql"
```

로컬 검수 화면은 별도 HTML 복사본을 이 API에 연결합니다. [파이참 실행·검수 안내](docs/local-review.md)에 현재 PC에서 화면과 API를 켜는 방법을 정리했습니다. 정식 FE 저장소 반영과 최종 브라우저 인수 검사는 남아 있습니다.
