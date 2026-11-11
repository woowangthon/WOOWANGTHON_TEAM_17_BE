# 생성 결과를 공동 캔버스로 연결

새 버전을 처음 가져오는 성공 시점에 캔버스 전체를 최신 계획으로 교체한다. 3일→2일 축소·2일→3일 확대·여행 날짜 전체 이동에도 새 계획의 날짜와 실제 PLACE·PLACE 간선만 남긴다. 기존 수동 장소·대안·댓글·사용자 날짜·연결·색·배치는 새 문서에 합치지 않는다. v4 날짜 연결은 새 날짜 순서의 기본 체인으로 다시 만든다. 같은 버전 재열기·재시도는 현재 편집·삭제를 유지하고 다시 초기화하지 않는다.

여행 준비의 방장 일정 만들기는 현재 저장·준비 완료 상태를 확인하고 기존 생성 job을 시작하거나 진행 중인 job에 다시 연결한다. 실제 job 성공 후 `POST /api/v1/trips/{tripId}/plans/{planId}/canvas`로 공동 초안에 가져온다. 요청은 `{versionId, expectedCanvasRevision}`와 UUID `Idempotency-Key`다. 성공 응답은 기존 `CanvasState`이며 별도의 개인 태그 재검토나 정식 일정 저장 동의로 대체하지 않는다.

현재 OWNER·ACTIVE·OPEN과 생성 source/inputToken·catalog·현재 plan/version을 같은 트랜잭션에서 검증한다. 새 문서 조립·장소 조회·유효성 검사와 revision CAS가 끝난 후 문서·sourcePlan·import marker·멱등 결과를 한 번에 저장한다. 실패·stale·권한 오류·CAS 충돌은 기존 문서와 marker를 유지한다. START/END는 가짜 장소 카드로 만들지 않는다. 최대 31일·500개 노드·500개 연결·JSON 1MiB는 새 문서에 적용하며 기존 댓글 수 때문에 작은 새 계획이 용량 오류가 나지 않는다.

일반 PUT /canvas는 `{expectedRevision, expectedSourcePlan, document}`를 필수로 받는다. `expectedSourcePlan`은 정확한 `{planId, versionId}` 또는 `null`이며 현재 저장 source와 다르면 `409 VERSION_CONFLICT`다. 누락된 이전 FE 요청은 400이고, 새 빌드로 새로고침해야 한다. source를 포함한 전체 body가 fingerprint와 재시도 단위다. 현재 source가 아닌 managed 메타데이터를 복원하는 PUT도 허용하지 않는다. 같은 source의 정상 편집·삭제·undo는 유지한다. FE는 새 source를 확인하면 이전 대기 요청·충돌·실행 취소·댓글 버퍼를 합치지 않고 정리한다.

카드 ID는 version/원래 node ID를 기반으로 한 UUID5다. `source:{planId,versionId,nodeId}`, `schedule:{startAt,endAt,locked}`는 서버의 원래 plan에서 가져온 값이다. 확인된 이동·체류로 계산한 시각을 우선하며, 새 일정에 대해서는 계획용 예상 시각도 표시한다. 조회한 경로의 검증 상태와 구분한다. 장소 상세의 estimatedStayMinutes는 DB에 저장된 권장 체류 분이며 미확인 시 null이다. PLACE 교체·복사본은 이 정보를 제거한다. 일반 편집은 메타데이터를 보존하며, undo가 복원하는 정보는 원래 plan의 장소·안정적 ID·시각과 일치하는지 서버가 다시 확인한다. 캔버스 편집이 기존 생성 plan의 검증 상태나 정식 저장 결과를 수정하지 않는다.

멱등 요청은 기존 `trip_canvas_requests`의 SAVE/POST로 기록한다. 기존 `plan_versions.validation_result` JSON의 내부 `_canvasImport` 표식을 캔버스 변경과 같은 트랜잭션으로 저장하므로 요청 기록 만료·다른 코스 가져오기·사용자의 카드 삭제 후에도 같은 version을 중복 추가하지 않는다. 이 내부 표식은 공개 plan DTO에 포함하지 않는다. 이미 여러 버전이 겹쳐 저장된 방은 현재 버전을 다시 가져올 때 요청 revision이 맞으면 같은 트랜잭션에서 이전 자동 경로를 정리한다. 정리할 카드가 없으면 revision도 유지한다. 전체 문서를 다시 보내는 예전 탭의 PUT에도 현재 경로 기준을 적용한다. 일반 공동 PUT은 최근 `sourcePlan`을 보존한다. 응답 유실 재시도는 같은 body/key를 사용하고, FE는 오래된 replay를 화면에 덮어쓰지 않고 최신 공동 문서를 다시 조회한다.

현재 DB의 준비 JSON·canvas 문서/요청 DDL은 읽기 전용으로 확인했으며 이 변경에는 새 테이블·migration·기동 시 DDL이 없다. 검증은 in-process SQLite/fake generation service로 수행한다. MySQL 경합·실제 Gemini/Kakao·서버·브라우저 검수는 이 검사와 별개다. 사용자가 기존 `run.py`로 검수하며 에이전트는 로컬 서버를 시작하지 않는다.

공용 원본은 [일정 생성·공동 캔버스 계약](https://app.notion.com/p/413e977e4908817a9af9ca9c7fa0ddee)과 [공통 규칙](https://app.notion.com/p/40ee977e49088189b512e6e817984d48)이다.
