# WOOWANGTHON_TEAM_17_BE

충북 단체여행 서비스 FastAPI/MySQL 백엔드입니다. 기기 세션과 여행방 시작, 공유 여행 준비·개인 조건, 일정 후보 선별·AI 초안·실제 경로 검증·명시적 저장을 제공합니다. 인원과 여행 조건은 방 생성 이후 설정하며 시작 API에서 임의 확정하지 않습니다.

공동 캔버스 초안은 장소 카드·댓글·배치·색·연결을 revision 기반으로 저장하고, 장소 상세 조회와 Gemini의 단일 장소 후보 추천을 제공합니다. 테이블 설치에는 `migrations/004_canvas.up.sql`, v4 저장에는 `migrations/005_canvas_v4.up.sql`을 각각 명시적으로 적용해야 합니다. 앱 시작이나 PyCharm의 `run.py` 실행은 migration을 적용하지 않습니다. 상세 규격과 실행 범위는 [공동 캔버스](docs/canvas-studio.md)를 참고하세요.

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

Gemini 실제 호출과 합성 사용자 입력, 충돌 처리, 일정 최적화의 평가 절차·한계는 [AI 품질 검증](docs/ai-quality-evaluation.md)을 참고하세요. 코드 테스트 통과와 실제 사용자 만족도는 별도로 기록합니다.

[2026-11-13 합성 평가 결과](docs/ai-quality-evaluation-results.md): 실제 파싱 19회와 일정 모델 호출 1회를 기록했습니다. 예약 누락과 미지원 조건, 생성 일정의 미확인 항목을 공개하며 실제 사용자 만족도는 미측정입니다.

후보 장소 필터·개인별/단체 점수와 AI 날짜별 방문 초안(3·4단계)은 [후보·초안 안내](docs/candidate-draft.md)를 참고하세요. AI 패키지 0.4.0을 사용하며 실제 경로·시간 검증·저장·FE 연결은 [일정 생성 안내](docs/generation-implementation.md)를 참고하세요. migration 003은 공용 DB에 자동 적용하지 않습니다.

일정 생성의 첫 두 단계인 확정 조건 캡처·읽기 전용 장소 매핑과 확정 취향 AI 구조화는 [내부 기반 안내](docs/generation-foundation.md)를 참고하세요. 새 HTTP API·후보 점수 계산·실제 일정 생성은 포함하지 않습니다.

여행 준비·개인 조건의 신규 6개 API, 버전·개인정보·AI 제안 처리와 명시적 migration 002는 [구현·검증 안내](docs/preparation-implementation.md)를 확인하세요. AI 해석에는 별도 AI 패키지 0.2.0의 공개 함수를 사용하며, FE 개인 조건 연결과 별도 해석 검토·확정 흐름은 일정 생성 작업에 포함됩니다.

검증된 의존성 버전은 `requirements.lock`에 기록했습니다. 같은 환경을 재현할 때는 먼저 `pip install -r requirements.lock`, 이후 `pip install -e . --no-deps`를 사용할 수 있습니다. 테스트는 HTTPX 기반 TestClient로 실행하며, 현재 라이브러리의 HTTPX2 전환 권고 경고는 동작 검증 결과와 별도로 기록합니다.

```powershell
# 격리된 MySQL과 TEST_DATABASE_URL을 명시적으로 준비한 경우에만 실행
.\.venv\Scripts\python.exe -m pytest -q tests
# 공용 DB에 연결하지 않는 테스트
.\.venv\Scripts\python.exe -m pytest -m "not mysql"
```

로컬 검수 화면은 별도 HTML 복사본을 이 API에 연결합니다. [파이참 실행·검수 안내](docs/local-review.md)에 현재 PC에서 화면과 API를 켜는 방법을 정리했습니다. 정식 FE 저장소 반영과 최종 브라우저 인수 검사는 남아 있습니다.
