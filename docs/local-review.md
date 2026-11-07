# 로컬 실행과 검수

파이참에서 Python 서버를 실행하고 브라우저에서 결과를 확인합니다. HTML 파일을 더블클릭하는 방식은 기기 쿠키/API 검수에 사용할 수 없습니다.

## 현재 PC의 화면 검수

현재 확인 주소는 `http://127.0.0.1:8017/`, API 문서는 `http://127.0.0.1:8017/docs`입니다. 연결 대상은 공용 개발 DB `chungbuk_trip1`입니다. 화면에서 만든 방은 실제 DB에 저장됩니다. 인원·날짜·예산·출발 시간 등의 설정은 이번 시작 화면 범위 밖입니다.

현재 PC의 폴더 배치는 다음과 같습니다. HTML과 wrapper는 이 BE PR에 포함하지 않은 로컬 검수용 파일입니다. 다른 팀원이 BE만 clone하면 화면까지 제공되지는 않습니다.

```text
wowang/
  WOOWANGTHON_TEAM_17_BE/        # 파이참에서 여는 프로젝트
    .venv/
    .env                       # 개인 연결 설정, Git 제외
  demo/trip-room-live/          # index.html, room.html: 실제 API 호출
  .codex-work/trip-entry/
    local_preview.py           # API와 위 HTML을 같은 출처로 제공
```

파이참에서 BE 폴더를 열고 Python 실행 구성을 추가합니다.

- 인터프리터: 프로젝트의 `.venv/Scripts/python.exe` (Python 3.12)
- 실행 대상: 스크립트 대신 모듈 `uvicorn`
- 매개변수: `local_preview:app --app-dir ../.codex-work/trip-entry --host 127.0.0.1 --port 8017 --no-access-log`
- 작업 디렉터리: `WOOWANGTHON_TEAM_17_BE` 폴더

검수 wrapper가 BE의 개인 `.env`를 읽습니다. 비밀번호를 실행 매개변수나 공유 실행 구성에 넣지 않습니다. 서버는 하나만 실행합니다. 이미 주소가 열리면 실행 중인 서버를 사용하고, 파이참에서 재실행할 때는 기존 8017 서버를 먼저 종료합니다. 이 wrapper는 현재 PC의 8017 출처를 허용하므로 주소를 `localhost`로 바꾸지 않고 `127.0.0.1`로 엽니다.

BE 폴더의 파이참 터미널에서도 같은 서버를 실행할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn local_preview:app --app-dir ../.codex-work/trip-entry --host 127.0.0.1 --port 8017 --no-access-log
```

## 수동 확인 순서

1. 일반 브라우저에서 시작 페이지를 열고 방 이름·닉네임을 입력해 생성합니다. 별도 `room.html`로 넘어가고 방 이름·초대 코드·방장 닉네임이 표시되는지 확인합니다.
2. 방 화면을 새로고침해 같은 방과 본인 정보가 유지되는지 확인하고 초대 코드를 복사합니다.
3. 시크릿 창 또는 다른 브라우저에서 같은 주소를 열어 코드·다른 닉네임으로 참여합니다. 같은 방에 참여하고 일반 창의 방장 정보가 유지되는지 확인합니다. 일반 탭 두 개는 같은 기기 자격을 공유하므로 다른 사람 참여 검수로 쓰지 않습니다.
4. 같은 기기에서 다시 참여했을 때 기존 본인이 유지되는지, 없는 코드·빈 값·길이 초과가 실패 안내로 표시되는지 확인합니다.

브라우저 두 탭의 최초 동시 bootstrap과 정식 FE 저장소 통합 검증은 별도 인수 항목으로 남겨둡니다. DB 도구로 확인할 때는 위 작업에서 생성한 방 ID만 대상으로 `trips`, `trip_members`, `trip_conditions`를 조회합니다. 임의 데이터 삭제나 테스트 초기화 스크립트를 공용 DB에서 실행하지 않습니다.

## BE만 실행하기

다른 팀원이 이 PR의 BE만 검수하려면 README의 설치·개인 `.env` 설정과 대상 DB의 추가 migration 적용 여부를 확인한 뒤 다음 명령을 사용합니다. 이 실행에는 HTML 정적 제공이 없습니다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

파이참에서도 모듈 `uvicorn`, 같은 매개변수, BE 작업 디렉터리를 설정할 수 있습니다. `http://127.0.0.1:8000/docs`가 API 문서입니다. 이 앱의 모든 POST는 정확한 허용 Origin과 JSON을 요구하므로 API 문서의 기본 실행만으로 검수가 끝나지는 않습니다. 브라우저 화면 또는 명시적인 Origin·쿠키·생성/참여 Idempotency-Key를 지원하는 HTTP 도구를 사용합니다.

자동 검증은 공용 DB와 분리된 `127.0.0.1:3317/osami_entry_test`만 허용합니다. 수행한 54개 HTTP/DB 검사 결과와 공용 DB의 읽기 전용 연결 확인을 구분해 [구현 문서](trip-entry-implementation.md)에 기록했습니다.
