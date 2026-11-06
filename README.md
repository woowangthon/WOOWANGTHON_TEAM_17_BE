# WOOWANGTHON_TEAM_17_BE

## 로컬 백엔드 실행

이 저장소에는 최소 FastAPI 앱과 MySQL 연결 기반이 포함되어 있습니다. API 기능이나 장소 테이블 모델은 아직 추가하지 않았으며, 기존 DB에 테이블을 만들거나 변경하지 않습니다. 의존성 버전 범위는 초기 기준이며 이 환경에서 설치·호환성 검증을 거친 lockfile은 아닙니다. Python 3.12가 별도로 설치되어 있어야 아래의 `py -3.12` 명령이 동작합니다. 앱 라이브러리 설치, 서버 실행, MySQL 접속은 아직 검증되지 않았습니다.

PowerShell에서 저장소 루트로 이동한 뒤 로컬 환경을 준비합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

`.env`의 빈 값에 접속 설정을 입력하세요. 실제 자격증명은 저장소에 올리거나 채팅·로그에 남기지 마세요. 비밀번호에 `#`이 있으면 따옴표로 감싸고, `$`와 공백은 그대로 보존하세요. 앱은 OS 환경변수를 `.env`보다 우선합니다.

현재 제공된 앱에서 연결 점검은 다음 명령을 직접 실행할 때만 시도합니다. 이 명령은 `SELECT 1`만 수행합니다. 추후 DB dependency를 사용하는 API가 추가되면 해당 요청에서도 연결합니다.

```powershell
python -m app.check_db
```

현재 비어 있는 FastAPI 앱 서버는 다음처럼 실행할 수 있습니다.

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1
```

DB 환경변수는 `DB_HOST`, `DB_PORT`(기본 `3306`), `DB_USERNAME`, `DB_PASSWORD`, `DB_NAME`입니다. 앱을 import하거나 서버를 시작해도 DB에는 자동 연결하지 않습니다. `get_db`는 요청별 연결을 닫고 연결을 반환할 때 미완료 트랜잭션을 rollback합니다. 쓰기 동작을 추가할 때는 해당 기능이 명시적으로 commit해야 합니다.

설치하지 않고 실행할 수 있는 단위 검사는 다음과 같습니다.

```powershell
python -m unittest discover -s tests -v
```

단위 검사는 가짜 연결 객체를 사용하며 실제 DB에 접속하지 않습니다. MySQL TLS 설정과 서버 인증 방식은 아직 확인하지 않았습니다. 서버에서 TLS나 별도 인증 플러그인을 요구하면 연결 설정을 맞춰야 할 수 있습니다.

## 여행 조건 미리보기 (내부 CLI)

내부 CLI는 고정 여행 조건과 자유 메모를 검사한 뒤 Gemini가 지역·필수 조건·제외 조건·선호·확인 질문을 구조화하도록 요청합니다. 고정 조건과 메모가 충돌하면 사람 확인이 필요한 미리보기 결과로 표시합니다. 이 기능은 HTTP endpoint를 제공하지 않고 결과를 DB에 저장하지 않습니다.

`.env`에 `GEMINI_API_KEY`를 설정하세요. `GEMINI_MODEL`은 선택 항목이며 기본값은 `gemini-3.5-flash-lite`입니다. 설정은 환경변수에서도 받을 수 있습니다. 키는 요청 헤더로만 전달되며 URL, 프로그램 출력, 오류 문구에 넣지 않습니다.

샘플 JSON을 사용하려면 다음 명령을 실행하세요.

```powershell
python -m app.trip_preview examples/trip_input.json
```

또는 JSON을 표준 입력으로 전달할 수 있습니다.

```powershell
Get-Content examples/trip_input.json -Raw | python -m app.trip_preview
```

여행 날짜·교통·전체 예산(원)·출발 시간/지역·인원은 미리보기 안에서 바꾸지 않습니다. 인원은 방 정보에서 전달해야 합니다. 반환값의 `pending_review`와 충돌·질문을 사람이 확인하기 전에는 확정 일정으로 사용하지 마세요. 네트워크 오류, 한도 초과, 잘못된 응답은 자동 재시도하지 않습니다.

이 기능의 단위 검사는 표준 라이브러리만 사용하며 Gemini 외부 호출은 가짜 HTTPS 응답으로 대체합니다.
