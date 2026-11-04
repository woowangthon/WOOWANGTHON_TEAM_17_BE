# 팀 Codex 시작 가이드

**FE가 Notion에 API 규격을 정리하고, BE는 그걸 읽고 구현한다.**

## 1. 처음 한 번 준비하기

- [FE 레포](https://github.com/woowangthon/WOOWANGTHON_TEAM_17_FE)와 [BE 레포](https://github.com/woowangthon/WOOWANGTHON_TEAM_17_BE)를 clone하고 작업할 레포 루트를 Codex 프로젝트의 primary 폴더로 연다.
- 레포의 AGENTS.md, .codex, .agents/skills, docs를 확인한다. 프로젝트 내용을 검토해 신뢰를 설정하고 새 채팅에서 규칙 적용 여부를 확인한다.
- 각자 본인 계정으로 GitHub와 Notion 플러그인을 연결한다. clone으로 토큰·페이지 권한·모델 접근 권한이 공유되지는 않는다. 팀 API 페이지의 읽기 권한을 확인하고, FE 명세 작성이나 BE 상태 갱신을 하려면 필요한 편집 권한도 준비한다.
- 앱 설치·실행·검사는 각 레포의 실제 README와 설정을 따른다. BE는 아직 앱·Python 버전·ORM·인증·검사 도구가 미정이다.
- DB 구조는 사용자 제공 덤프의 24개 테이블을 [DB 구현 기준](database-schema.md)과 [전체 DDL 참조](database-schema-reference.md)에 기록했다. 팀원은 clone한 문서를 바로 참고할 수 있다. 접속 호스트·DB 이름·계정·비밀번호는 별도로 준비하며 Notion이나 Git에 비밀값을 올리지 않는다.

~~~text
git clone https://github.com/woowangthon/WOOWANGTHON_TEAM_17_FE.git
git clone https://github.com/woowangthon/WOOWANGTHON_TEAM_17_BE.git
~~~

기존 폴더가 있다면 중복 clone하거나 변경을 덮어쓰지 말고 먼저 각 레포에서 git status --short와 git remote -v를 확인한다. 처음 연결 확인은 아래처럼 요청한다.

~~~text
이 레포의 AGENTS.md·스킬·실행 명령을 확인하고 아래 Notion API 페이지를 읽어줘.
https://app.notion.com/p/40ee977e49088030a073ffd13d06c144
이번에는 조회만 하고 파일·Notion·GitHub는 수정하지 마.
~~~

[기존 FE clone 안내](https://app.notion.com/p/40de977e490880c98264f3208bcba6b0)는 앱 시작에 참고하되 실제 설치 명령·버전은 최신 레포에서 확인한다.

## 2. FE에서 기능 요청하기

FE Codex는 기존 API를 먼저 찾는다. 재사용할 API가 없거나 부족하면 필요한 규격을 Notion에 작성한다. 화면은 BE 준비 전까지 mock으로 개발할 수 있다.

~~~text
[기능] 화면을 만들어줘.
Notion에서 기존 API를 찾아 재사용하고, 없거나 부족하면 필요한 API 규격을 직접 정리해줘.
method/path·요청/응답 예시·권한·오류를 적고, 작성이 끝난 항목은 구현 요청으로 표시해줘.
BE가 준비되기 전에는 mock으로 개발해줘.
~~~

## 3. BE에서 구현 요청하기

BE Codex는 Notion의 구현 요청 항목과 관련 DB 테이블 정의를 읽고 기존 구조에 맞춰 구현·검증한다. 별도 FE·BE 이중 승인 절차는 없다. 명세 누락·DB 제약 충돌·구현/보안상 문제만 확인한다. API 규격 본문은 FE가 수정하고, DB 구조 변경이 필요하면 BE가 그 영향과 변경안을 먼저 제시한다.

~~~text
Notion API 계약 페이지의 [기능] 구현 요청 항목을 읽고 그대로 구현해줘.
명세가 부족하거나 구현상 문제가 있는 부분만 구체적으로 알려줘.
구현·검증 결과와 관련 PR 링크를 남겨줘.
~~~

FE가 Notion에 작성했다고 BE가 자동 실행되는 것은 아니다. BE 채팅에서 위처럼 작업을 요청한다. BE가 완료하면 FE가 실제 API를 연결하고 화면 동작을 확인한다.

## 4. API 페이지에 적을 내용

[API 계약 페이지](https://app.notion.com/p/40ee977e49088030a073ffd13d06c144)에 기능별로 다음을 작성한다.

- 기능명과 상태: 작성 중 / 구현 요청 / 구현 완료.
- HTTP method/path, 인증·권한.
- 요청 필드·예시, 성공 응답 예시, 오류 응답.
- 해당 기능에 필요한 특이사항과 관련 FE/BE PR.

필수 API ID·개정 번호·양쪽 승인자 목록은 두지 않는다. FE가 완성해 구현 요청으로 표시한 명세를 BE 구현 기준으로 쓴다. 구현 요청은 BE 구현 완료를 뜻하지 않는다. 실제 검증을 마친 범위만 구현 완료로 기록한다.

기존 호출을 깨는 변경은 FE가 영향받는 화면·BE에 알리고 명세를 수정한다. 작업 중 명세가 바뀌면 BE는 바뀐 부분을 확인해 관련 구현과 검사만 갱신한다. 동일 API의 규격은 FE에서 한 작성자가 맡고, BE는 구현 상태·BE PR 링크를 갱신한다. 단순 명세 추가마다 허락을 반복하지 않는다.

## 5. 결과 확인과 PR

PR에는 관련 Notion 페이지, 구현한 method/path와 명세 핵심 내용·변경 요약, 실행한 검사 결과를 남긴다. 별도 계약 스냅샷 파일을 의무적으로 만들 필요는 없다. 내용 재현이 필요하면 관련 부분을 PR/문서에 인용한다.

FE·BE는 각각 별도 이슈·브랜치·커밋·PR로 작업하고 관련 PR을 연결한다. 실제 설정된 검사만 실행한다. 검증이 끝나면 사람 리뷰를 위한 Ready, 문제가 남거나 필수 검증을 못 했으면 Draft로 보고한다. 별도의 API 이중 승인 대기만을 이유로 Draft를 강제하지 않는다. 사람만 병합한다.

기존 역할·모델 규칙은 각 레포 문서를 따른다. root가 Notion 조회/쓰기·조율·통합·최종 검증·Git 전달을 맡고 비단순 구현은 planner 계획 후 진행한다. 사용자가 지정한 모델·작업 범위가 우선한다.

## 6. 예외와 배포 상태

Notion 접근 실패를 API 부재로 단정하거나 실패한 쓰기를 완료라고 하지 않는다. 확인 가능한 독립 작업은 계속하고 필요한 부분만 알려준다. 읽기 전용·로컬 전용이면 외부 쓰기를 하지 않는다. Notion은 수정하되 Git 전달만 보류하려면 그렇게 구분해 요청한다.

팀 설정·스킬은 각 레포의 PR을 검토하고 팀 기준 브랜치에 반영한 뒤 clone/pull해야 공유된다. 아직 병합되지 않은 변경은 해당 PR의 브랜치에서 확인한다. 개인 계정 연결은 각자 해야 한다. 일반 기능 작업은 레포 규칙의 Git 전달 범위를 따르고, 로컬 작업만 원할 때 별도로 지정한다.

백그라운드 watcher·양방향 동기화·타입 자동 갱신은 없다. 이 절차는 기능을 요청할 때 Codex가 문서를 읽고 작업하는 방식이다.

공통자료 게시본: [팀 Codex 작업 가이드](https://app.notion.com/p/40ee977e4908813e9226ce66a9be8cf2).

공식 참고: [프로젝트와 primary 폴더](https://learn.chatgpt.com/docs/projects), [프로젝트 신뢰와 설정](https://learn.chatgpt.com/docs/config-file/config-basic), [팀 스킬](https://learn.chatgpt.com/docs/build-skills), [플러그인 연결과 권한](https://learn.chatgpt.com/docs/plugins).
