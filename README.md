# WOOWANGTHON_TEAM_17_BE

충북 단체여행 서비스의 FastAPI/MySQL 백엔드입니다. 현재 구현 범위는 기기 세션, 여행방 생성·초대 참여·최근 여행 조회·재접속입니다. 인원과 여행 조건은 방 생성 이후 설정하며 시작 API에서 임의 확정하지 않습니다.

Python 3.12 이상과 MySQL 8.0.16 이상이 필요합니다. 설치와 실행:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
# .env.example을 참고해 개인 .env를 설정합니다. 실제 비밀값은 커밋하지 않습니다.
.\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

앱은 테이블을 자동 생성/변경하지 않습니다. 명시적 migration, 테스트 DB 준비, FE 호출 조건은 [여행방 시작 구현 문서](docs/trip-entry-implementation.md)를 확인하세요. 사용자 지정 공용 개발 DB `chungbuk_trip1`에는 추가 테이블 3개가 적용됐습니다(기존 26개 구조 보존, 현재 29개). 다른 환경은 적용 여부를 따로 확인해야 합니다.

API 입력·응답·인증·오류는 [Notion 여행방 시작 명세](https://app.notion.com/p/40ee977e49088105a9b4ce4df0aef6f8)를 따릅니다. 실행 후 `/docs`에서 API 문서를 볼 수 있습니다. 회원가입 대신 기기 쿠키를 사용하며, 일반 실행은 worker 1개를 기준으로 합니다.

검증된 의존성 버전은 `requirements.lock`에 기록했습니다. 같은 환경을 재현할 때는 먼저 `pip install -r requirements.lock`, 이후 `pip install -e . --no-deps`를 사용할 수 있습니다. 테스트는 HTTPX 기반 TestClient로 실행하며, 현재 라이브러리의 HTTPX2 전환 권고 경고는 동작 검증 결과와 별도로 기록합니다.

```powershell
# 격리된 MySQL과 TEST_DATABASE_URL을 명시적으로 준비한 경우에만 실행
.\.venv\Scripts\python.exe -m pytest -q tests
```

로컬 검수 화면은 별도 HTML 복사본을 이 API에 연결합니다. [파이참 실행·검수 안내](docs/local-review.md)에 현재 PC에서 화면과 API를 켜는 방법을 정리했습니다. 정식 FE 저장소 반영과 최종 브라우저 인수 검사는 남아 있습니다.
