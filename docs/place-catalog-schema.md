# 장소 카탈로그 실제 구조 확인

2026-11-09 사용자 업데이트 이후 공용 DB의 장소 테이블만 읽기 전용으로 확인했습니다. 아래는 해당 시점의 SHOW CREATE TABLE입니다. 전체 DB 덤프·실제 데이터 행·접속 자격증명은 포함하지 않습니다. 앱 시작 시 이 DDL을 실행하지 않습니다.

## places

확인 행 수: 1857

```sql
CREATE TABLE `places` (
  `contentid` int DEFAULT NULL,
  `title` text COLLATE utf8mb4_unicode_ci,
  `contenttypeid` int DEFAULT NULL,
  `contentTypeName` text COLLATE utf8mb4_unicode_ci,
  `planningRole` text COLLATE utf8mb4_unicode_ci,
  `recommendationUse` text COLLATE utf8mb4_unicode_ci,
  `adminCity` text COLLATE utf8mb4_unicode_ci,
  `addr1` text COLLATE utf8mb4_unicode_ci,
  `addr2` text COLLATE utf8mb4_unicode_ci,
  `mapx` double DEFAULT NULL,
  `mapy` double DEFAULT NULL,
  `geoValidated` text COLLATE utf8mb4_unicode_ci,
  `tel` text COLLATE utf8mb4_unicode_ci,
  `homepageUrl` text COLLATE utf8mb4_unicode_ci,
  `overviewText` text COLLATE utf8mb4_unicode_ci,
  `firstimage` text COLLATE utf8mb4_unicode_ci,
  UNIQUE KEY `uq_places_contentid` (`contentid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

## place_details

확인 행 수: 1655

```sql
CREATE TABLE `place_details` (
  `contentid` int DEFAULT NULL,
  `openingHoursText` text COLLATE utf8mb4_unicode_ci,
  `closedDaysText` text COLLATE utf8mb4_unicode_ci,
  `feeText` text COLLATE utf8mb4_unicode_ci,
  `feeCategory` text COLLATE utf8mb4_unicode_ci,
  `bookingText` text COLLATE utf8mb4_unicode_ci,
  `parkingText` text COLLATE utf8mb4_unicode_ci,
  `parkingCategory` text COLLATE utf8mb4_unicode_ci,
  `durationText` text COLLATE utf8mb4_unicode_ci,
  `estimatedStayMinutes` int DEFAULT NULL,
  `stayTimeMethod` text COLLATE utf8mb4_unicode_ci,
  `experienceText` text COLLATE utf8mb4_unicode_ci,
  `menuText` text COLLATE utf8mb4_unicode_ci,
  `checkInTime` text COLLATE utf8mb4_unicode_ci,
  `checkOutTime` text COLLATE utf8mb4_unicode_ci,
  `petPolicyText` text COLLATE utf8mb4_unicode_ci,
  `petPolicyCategory` text COLLATE utf8mb4_unicode_ci,
  `eventStartDate` text COLLATE utf8mb4_unicode_ci,
  `eventEndDate` text COLLATE utf8mb4_unicode_ci,
  `courseText` text COLLATE utf8mb4_unicode_ci,
  `planningStatusV2` text COLLATE utf8mb4_unicode_ci
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

## place_preferences

확인 행 수: 1857

```sql
CREATE TABLE `place_preferences` (
  `contentid` int DEFAULT NULL,
  `preferenceTags` text COLLATE utf8mb4_unicode_ci,
  `preferenceTagConfidence` text COLLATE utf8mb4_unicode_ci,
  `vibeTags` text COLLATE utf8mb4_unicode_ci,
  `activityLevel` text COLLATE utf8mb4_unicode_ci,
  `indoorOutdoor` text COLLATE utf8mb4_unicode_ci,
  `natureScore` int DEFAULT NULL,
  `relaxationScore` int DEFAULT NULL,
  `cultureScore` int DEFAULT NULL,
  `activityScore` int DEFAULT NULL,
  `foodScore` int DEFAULT NULL,
  `shoppingScore` int DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

## 값 형식과 한계

- preferenceTags와 vibeTags는 실제 값에서 확인한 `|` 구분 형식입니다. 토큰 전체를 비교하며 태그 부재는 안전·불가능의 증거가 아닙니다.
- preferenceTagConfidence는 high/medium/low이며 사실의 완전성이나 부재를 보장하지 않습니다. 여섯 점수는 각각 0..5 정수 범위가 관찰됐습니다. 명시적 의미 매핑 없이 필수 제약이나 가중치로 쓰지 않습니다.
- mapx는 경도, mapy는 위도입니다. geoValidated=yes가 1,855행이며 읽기 전용 adapter의 유효 좌표 수도 1,855개였습니다.
- 상세의 feeText는 772행, openingHoursText는 1,620행에 비어 있지 않은 값이 있습니다. 자연어 요금/시간을 임의 숫자·운영 보장으로 바꾸지 않습니다. 미상 요금은 요금 확인 필요, 미상 영업시간은 표시 생략입니다.
- 장소 3개 adapter를 실제로 읽어 중복/고아/잘못된 검증 좌표 진단이 없음을 확인했습니다. 이후 데이터 변경은 runtime 검사와 catalog digest 재확인 대상입니다.
- 공용 DB는 메타데이터·장소 조회만 수행했습니다. 코드 자동 검사는 synthetic 또는 전용 loopback 테스트 DB만 사용합니다.
