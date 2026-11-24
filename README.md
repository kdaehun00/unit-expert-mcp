# Unit Expert MCP

Minimal Streamable HTTP MCP server for PlayMCP.

## PlayMCP Settings

```text
MCP 이름: Unit Expert(단위전문가)
MCP 식별자: unitExpert
MCP URL: https://unit-expert-mcp.onrender.com/mcp
인증: 없음
```

## HTTP Endpoints

```text
GET    /
GET    /healthz
POST   /mcp
GET    /mcp
DELETE /mcp
GET    /scenario
POST   /scenario
GET    /scenario/meta
GET    /send
GET    /inspect
GET    /inspect/log
```

`POST /mcp` expects JSON-RPC with:

```text
Accept: application/json, text/event-stream
Content-Type: application/json
```

## Tools

- `convert_length`
- `convert_weight`
- `convert_temperature`
- `convert_area`
- `convert_volume`
- `list_supported_units`
- `echo_header_params`, `confirm_action`, `convert_interactive` (2026-07-28 경로 전용 테스트 툴. 아래 "2026-07-28 지원 범위" 참고)

## Scenario Control

브라우저에서 `/scenario`(또는 `/`)를 엽니다. 화면은 세 개이고 위쪽 내비게이션으로 오갑니다: `MCP 서버 만들기`(`/scenario`), `MCP 클라이언트`(`/send`), `호출 기록`(`/inspect`).

MCP 서버 만들기 화면에서 고른 설정은 MCP 주소의 `cfg` 값(화면에서는 "설정 코드")에만 담깁니다. 그 주소로 온 요청에만 적용되고 서버 기본값은 바뀌지 않습니다. 폼은 기본값에서 시작합니다.

화면이 쓰는 말: 서버 쪽은 "지원 스펙"(`serverEra`: 둘 다 `both` / 2025 방식만 `legacy` / 2026-07-28만 `modern`), 요청 쪽은 "요청 방식"(2025 방식: `initialize`로 시작 / 2026 방식: 요청마다 버전 2026-07-28을 실음)입니다.

- 왼쪽(설정)
  - 기본 설정(항상 펼침): 서버 이름, 서비스 이름, 지원 스펙, 인증 네 가지입니다. 이것만 정하고 주소를 복사하면 됩니다. 지원 스펙과 인증은 선택지마다 한 줄 설명이 붙습니다.
  - 설정 불러오기(접힘): MCP 주소, 설정 코드(`cfg=<코드>`, `cfg:<코드>`, 헤더 한 줄 포함), 또는 설정 JSON을 붙여 넣으면 폼에 반영됩니다. 설정 JSON은 기본값과 다른 부분만 담은 객체입니다(예: `{"toolCall":{"mode":"is-error"}}`). "설정 JSON 형식"은 옵션 경로·종류·기본값·허용값 목록을 복사합니다. AI에게 주고 설정을 만들게 할 때 씁니다.
  - 자세한 설정(접힘): 정상 동작하는 서버의 나머지 설정입니다. 지원 스펙(엄격 검증, initialize 응답 버전 등), 인증(보호할 툴, 스코프 검사, 허용할 Origin), 툴(테스트 툴, 페이지, 캐시), 세션 카드로 나뉩니다. 제목에 "기본값과 다른 설정 N개"가 나옵니다.
  - 에러 넣기(접힘): 일부러 실패하거나 스펙을 어기게 하는 설정입니다. 제목에 "켜진 것 N개"가 나옵니다. "자주 쓰는 에러" 버튼(누르면 넣고 다시 누르면 뺍니다) 아래에 대상별 묶음이 있습니다: tools/call, tools/list(처음부터 펼침), 모든 요청, server/discover, 응답 형식 깨기, 2025 방식 서버가 2026-07-28 요청을 받았을 때, 직접 응답 지정.
  - 접힌 두 부분은 N이 1 이상이 되면 저절로 펼쳐집니다. 화면에서 바꿨을 때, 설정을 불러왔을 때, 설정이 든 주소(`#cfg=`)로 들어왔을 때 모두 같습니다.
  - 옵션마다 한 줄 설명이 있고, 검사 항목이나 예시가 더 있는 옵션은 "자세히"를 펼쳐 봅니다(예: 엄격 검증이 검사하는 것과 어겼을 때의 응답, 허용할 Origin의 동작).
  - 어느 옵션이 어디에 나오는지는 `GET /scenario/meta`의 옵션마다 있는 `basic`(기본 설정)과 `section`(`build`: 자세한 설정 / `error`: 에러 넣기)이 정합니다. 스펙을 지키는 서버가 가질 수 있는 설정이면 `build`, 스펙을 어기거나 실패를 흉내 내면 `error`입니다.
  - 바꾼 옵션은 "바꿈"으로 표시되고 묶음 제목에 "바꿈 N"이 붙습니다. 지금 설정에서 효과가 없는 옵션은 흐리게 나오고 이유가 적힙니다. "설정 찾기"는 이름·키·설명("자세히" 포함)으로 옵션과 에러 버튼을 찾습니다.
  - 2026-07-28 호환 테스트(접힘): 요청 방식 × 서버의 조합 표입니다. 칸을 누르면 지원 스펙이 그 열의 값으로 바뀌고 기대 결과와 검사 명령이 나옵니다.
  - 서버 기본값 바꾸기(접힘): 지금 설정을 서버 기본값으로 쓰기, 되돌리기, 세션 모두 끊기. 주소에 설정이 없는 모든 요청(다른 사용자 포함)에 적용됩니다. 기본값이 바뀌어 있으면 화면 위에 알림이 나옵니다.
- 오른쪽(내 MCP 서버 주소. 좁은 화면에서는 창 위쪽에 붙습니다)
  - 주소와 "주소 복사", 기본값에서 바꾼 설정 목록(칩의 ×는 그 항목만, "모두 되돌리기"는 전부), "MCP 클라이언트로 요청 보내 보기"(`/send?cfg=<코드>`), "이 주소의 호출 기록 보기"(`/inspect?cfg=<코드>`).
  - 설정에 오류가 있으면 `설정 오류: <옵션 이름 (경로)> — <사유>`가 나오고, 고칠 때까지 주소가 비며 복사가 막힙니다.
  - 다른 형태로 복사(접힘): 설정 JSON, 설정 코드, `X-MCP-Test-Scenario` 헤더, curl 두 가지, 테스트 클라이언트 명령. 항목마다 복사되는 문자열이 그대로 보이고(두 줄, "전체 보기"로 펼침), 복사할 것이 없으면 이유가 적힙니다.

화면 상태는 주소의 `#cfg=<토큰>`에 들어가므로 새로고침하거나 주소를 공유하면 같은 설정으로 열립니다. 화면이 쓰는 옵션, 그룹, 프리셋 정의는 `GET /scenario/meta`가 줍니다.

### MCP 클라이언트 (/send)

MCP 주소로 요청 하나를 만들어 보내고 응답을 봅니다. MCP 서버 만들기와 호출 기록의 링크로 오면 주소가 채워져 있습니다(`/send?cfg=<코드>` 또는 `/send?url=<주소>`). 보낸 요청은 기록에 `X-Unit-Expert-Source: inspect-console`로 남습니다.

- 폼: MCP 주소, 요청 방식(2026 방식 / 2025 방식), 메서드, 툴과 인자(JSON). "보낼 헤더와 바디 직접 고치기"에서 실제로 나갈 헤더와 바디를 고칠 수 있습니다. 2025 방식은 먼저 `initialize`를 보내 받은 세션과 버전을 붙입니다(끌 수 있음).
- 붙여 넣어 채우기: 아래 셋 중 하나를 붙여 넣으면 폼이 채워집니다. 틀린 곳은 한 줄로 알려 줍니다(예: `JSON을 읽지 못했습니다: 1번째 줄 25번째 글자 근처`).
  1. JSON-RPC 요청 바디(`{"jsonrpc":"2.0","method":…}`). `_meta`가 있으면 2026 방식으로 봅니다.
  2. 요청 JSON: `{"url", "mode", "method", "tool", "args", "params", "id", "headers", "omit", "body"}`. `method`만 필수입니다. `mode`는 `"2026"`(기본) 또는 `"2025"`, `omit`은 일부러 뺄 것(`meta-version`, `capabilities`, `meta`, `version-header`, `method-header`, `name-header`. 테스트 클라이언트 `call`의 `--omit-*`와 같은 이름)입니다.
  3. curl 명령(URL, `-X`, `-H`, `-d`). 이 화면들이 만든 curl은 그대로 다시 읽습니다.
- "요청 JSON 형식"은 2번 형식의 설명과 예 2개를 복사합니다(AI에게 주고 요청을 만들게 할 때). "지금 요청을 JSON으로", "지금 요청을 curl로"는 현재 폼을 내보냅니다.
- 이어서 보내기: `tools/call`의 응답이 `input_required`면 응답 아래에 버튼이 나옵니다. 누르면 받은 `requestState`와, `inputRequests`의 `requestedSchema`를 보고 기본값을 채운 `inputResponses`가 들어간 다음 요청(2번 형식)으로 붙여 넣기 칸과 폼을 채웁니다. 전송은 "보내기"를 눌러야 됩니다. 예시의 "입력 요청 왕복"은 `elicitation` capability를 선언한 `convert_interactive` 호출입니다.
- 캐시 확인: 응답의 `ttlMs` 동안 같은 `tools/list`를 다시 보내지 않는 클라이언트를 흉내 냅니다.

요청 하나에만 시나리오를 넣으려면 `X-MCP-Test-Scenario` 헤더를 씁니다. 헤더가 없으면 `cfg` 토큰, 그것도 없으면 서버 전역 설정이 적용됩니다.

서버 전역 설정은 프로세스 메모리에 있어서 서버를 다시 시작하거나 다시 배포하면 `ok`(기본값)로 돌아갑니다.

```text
X-MCP-Test-Scenario: duplicate-tool-name
```

지원 시나리오:

### 기본

| 정책명 | 시나리오 | 효과 |
| --- | --- | --- |
| 정상 응답 | `ok` | 정상 Unit Expert 툴 목록을 반환합니다. |

### 서버 error

| 정책명 | 시나리오 | 효과 |
| --- | --- | --- |
| 인증 조건 - 401 | `auth-401` | JSON-RPC 처리 전에 `401 Unauthorized`를 반환합니다. |
| 인증 조건 - 403 | `auth-403` | JSON-RPC 처리 전에 `403 Forbidden`을 반환합니다. |
| MCP 버전 조건 - 최소 지원 버전 | `unsupported-min-version` | 최소 지원 버전보다 낮은 `protocolVersion: 2024-11-05`를 반환합니다. |
| 툴 목록 조건 - JSON-RPC 에러 | `tools-list-error` | `tools/list`에서 JSON-RPC error를 반환합니다. |
| 툴 목록 조건 - null 반환 | `tools-list-null` | `tools/list`에서 `tools: null`을 반환합니다. |
| 툴 목록 조건 - 빈 배열 반환 | `tools-list-empty` | `tools/list`에서 `tools: []`를 반환합니다. |
| 툴 개수 조건 - 최대 개수 | `too-many-tools` | 툴 21개를 반환합니다. |
| 응답속도 조건 - 지연 | `delayed-response` | OPTIONS가 아닌 `/mcp` 요청을 5초 지연시킵니다. |

### tool error

| 정책명 | 시나리오 | 효과 |
| --- | --- | --- |
| 툴 이름 조건 - 중복 | `duplicate-tool-name` | 동일한 `name`을 가진 중복 tool을 반환합니다. |
| 툴 이름 조건 - 허용 문자 | `invalid-tool-name-char` | 허용되지 않는 문자가 포함된 tool name을 반환합니다. |
| 툴 이름 조건 - 길이 | `invalid-tool-name-length` | 129자 길이의 tool name을 반환합니다. |
| 툴 필수 속성 - name | `missing-name` | `name`이 없는 tool을 반환합니다. |
| 툴 필수 속성 - description | `missing-description` | `description`이 없는 tool을 반환합니다. |
| 툴 필수 속성 - inputSchema | `missing-input-schema` | `inputSchema`가 없는 tool을 반환합니다. |
| 툴 필수 속성 - annotations | `missing-annotations` | `annotations`가 없는 tool을 반환합니다. |
| 툴 이름 조건 - 금지어 | `forbidden-kakao-name` | 금지어가 포함된 tool name을 반환합니다. |
| 툴 설명 조건 - 길이 | `long-description` | 1,051자 `description`을 반환합니다. |
| 툴 설명 조건 - 서비스명 | `missing-service-name-in-description` | 서비스명이 빠진 `description`을 반환합니다. |
| 툴 annotations 조건 - 필수 힌트 | `incomplete-annotations` | 필수 필드가 빠진 `annotations`를 반환합니다. |

## 호출 기록 (/inspect)

`/inspect`는 MCP 주소로 온 요청과 서버가 보낸 응답을 보여 줍니다. 서버는 가장 최근 200건만 메모리에 두고, 서버를 함께 쓰는 모든 사용자가 같은 기록을 봅니다(`GET /inspect/log`는 인증 없이 열려 있습니다).

인증 값은 서버가 기록할 때 가립니다. 원래 값은 기록에 남지 않습니다.

- 가리는 것: 요청 헤더 `Authorization`과, 이름에 `authorization`·`cookie`·`token`·`secret`·`password`·`api-key`(`apikey`, `api_key`)가 들어간 헤더, 응답 헤더 `Set-Cookie`, `/mcp` 주소 쿼리의 `access_token`·`token`류 값(`***`로 바꿈). `Mcp-`로 시작하는 헤더, `X-MCP-Test-Scenario`, `WWW-Authenticate`, 쿼리의 `cfg`(설정 코드)는 가리지 않습니다.
- 형식: `Bearer ***cdef (len 43, fp 1a2b3c4d)`. 스킴, 끝 4자, 길이, 지문이 남습니다. 8자 이하인 값은 `Bearer ***`만 남습니다. 지문이 같으면 같은 값이므로, 게이트웨이를 거친 요청과 직접 보낸 요청의 토큰이 같은지 비교할 수 있습니다. 지문은 서버를 다시 시작하면 바뀝니다.
- 가리지 않는 것: 요청·응답 바디(JSON-RPC 내용이 테스트 대상이라 그대로 남습니다), `rawResponses`에 직접 적은 응답 헤더와 바디(`Set-Cookie`만 가림). 바디에는 실제 비밀 값을 넣지 않습니다.
- 서버 인증은 받은 값 그대로 검사합니다. 가리는 것은 기록뿐이고 `/mcp` 응답은 달라지지 않습니다.
- 원문 기록: 환경변수 `MCP_INSPECT_RAW_SECRETS=1`로 서버를 띄우면 받은 그대로 기록합니다(기본은 꺼짐). 로컬에서만 씁니다. 켜져 있으면 서버가 시작할 때 경고를 찍고, `GET /inspect/log`의 `secrets`가 `"raw"`가 되며, 화면에 "값 보기"가 나옵니다.
- 짧거나 흔한 값(쉬운 비밀번호의 Basic 인증 등)은 끝 4자·길이·지문으로 추측할 수 있습니다. 이 서버에는 테스트용 값만 보냅니다.

- 어느 주소로 온 요청을 볼까요?: 맨 위의 입력란에 MCP 주소(설정 코드도 됩니다)를 붙여 넣고 "보기"를 누르면 그 주소로 온 요청만 봅니다. 그 주소에서 바꾼 설정이 칩으로 나오고 "MCP 서버 만들기에서 열기", "MCP 클라이언트에서 열기" 링크가 붙습니다. 비우면 "모든 주소" 또는 "기본 주소만(설정 없는 `/mcp`)"을 고릅니다. MCP 서버 만들기의 "이 주소의 호출 기록 보기"로 오면 주소가 채워져 있습니다(`/inspect?cfg=<코드>`, 기본 주소만은 `/inspect?plain=1`). 주소의 글자가 다르면 같은 설정이어도 다른 주소로 봅니다.
- 최근 요청이 온 주소: 기록에 있는 주소마다 바꾼 설정 요약, 요청 수, 마지막 시각이 나오고, 누르면 그 주소를 고릅니다(`GET /inspect/log?groups=1`의 `groups`).
- 자세히 거르기(접힘): 검색(메서드, 툴 이름, 헤더, 바디, 에러 이름, 결과), 결과, HTTP 상태, 요청 방식, 메서드, "에러만". MCP 클라이언트 화면에서 보낸 요청과 `OPTIONS`는 기본으로 숨깁니다. 필터가 요청을 가리면 어떤 필터가 몇 건을 가렸는지 적고 그 필터만 끄는 버튼을 줍니다. 필터는 주소의 쿼리에 들어갑니다(예: `/inspect?cfg=<코드>&kind=rejected`).
- 목록: 최신 요청이 위입니다. 열은 시각, HTTP 메서드, 메서드와 툴 이름, 요청 방식(알 수 없으면 `-`), 서버(그 요청에 적용된 지원 스펙), 상태, 에러 코드와 이름, 결과, 넣은 에러, 소요 시간(ms), 주소(바꾼 설정 요약 또는 헤더 / 기본)입니다.
- 결과: `정상`, `넣은 에러`, `서버가 거절`, `직접 지정`(`rawResponses` 규칙의 응답), `연결 종료`(응답 없이 끊음).
  - `넣은 에러`는 기본 설정이었다면 달라졌을 응답입니다. 서버는 그 응답을 만든 옵션을 기록의 `injected`에 남깁니다. 예: `auth.mode=oauth`(401/403), `toolsList.mode=empty`, `toolErrors=…`, `toolCall.mode=sse`, `discover.mode=…`, `modern.testTools=false`, `initialize.protocolVersion=…`, `session.validate`.
  - `서버가 거절`은 넣은 에러 없이 요청이 규칙에 맞지 않아 서버가 거절한 것만입니다(헤더와 바디가 다름, 잘못된 Accept, 읽을 수 없는 설정 코드 등).
  - 늦게만 답한 응답(`server.delaySeconds=…`, `toolCall.delaySeconds=…`, `discover.delaySeconds=…`)은 내용이 그대로이므로 `정상`(거절이었다면 `서버가 거절`)으로 두고, 넣은 에러 열에 배지를 보여 줍니다.
  - 지원 스펙(`serverEra`)과 표시용 이름(`mcp.identifier`, `mcp.serviceName`)은 `injected`에 넣지 않습니다.
- 갱신: 2초마다 새로 온 요청만 받아 목록 위에 더합니다. 열어 둔 상세와 선택한 텍스트는 그대로입니다. 탭이 가려져 있으면 멈춥니다.
- 상세: 행을 누르거나 Enter를 누르면 열립니다. 거절한 이유(에러 코드와 이름, `error.message`, `error.data`), 그 요청에 쓰인 설정과 온 곳, 요청·응답의 헤더와 바디가 나옵니다. 2026 방식 요청은 헤더(`MCP-Protocol-Version`·`Mcp-Method`·`Mcp-Name`·`Mcp-Param-*`)와 바디 값을 나란히 놓은 "헤더와 바디 비교" 표가 있습니다. SSE 응답은 이벤트별로 나눕니다. `Authorization` 같은 인증 값은 서버가 가려서 기록한 그대로 나옵니다(원문 기록을 켠 서버에서만 "값 보기"가 나옵니다).
- curl로 다시 보내기: 기록된 경로·쿼리·헤더·바디로 만든 명령을 복사합니다. 호스트는 화면을 연 주소입니다. 가린 헤더는 `<Authorization 값>`으로 들어가므로 실제 값으로 바꿔서 보냅니다(원문 기록을 켠 서버에서는 "값 보기"를 켠 뒤 복사하면 실제 값이 들어갑니다). MCP 클라이언트 화면에 붙여 넣으면 폼이 채워집니다.
- 지금까지 기록 숨기기: 지금 있는 기록을 이 화면에서만 숨깁니다. 서버의 기록은 지우지 않고, "다시 보기"로 되돌립니다.

기록 항목의 필드와 `GET /inspect/log`의 쿼리는 `docs/CONFIG_REFERENCE.md`의 "`/inspect` 기록"에 있습니다.

## 2026-07-28 지원 범위

`/mcp`는 한 엔드포인트에서 레거시(2025-11-25 이하, `initialize` 핸드셰이크)와 모던(2026-07-28, 무상태) 요청을 함께 받습니다. 모던 경로의 기준은 공식 스펙이며, 조사 내용은 [docs/SPEC_2026-07-28_NOTES.md](docs/SPEC_2026-07-28_NOTES.md)에 있습니다.

모던 경로가 구현한 것:

- 메서드: `server/discover`, `tools/list`, `tools/call`, `subscriptions/listen`. 그 밖의 메서드(`initialize`, `ping` 포함)는 HTTP 404 + `-32601`입니다.
- 요청 검증: `MCP-Protocol-Version`·`Mcp-Method`·`Mcp-Name` 헤더, `_meta`의 protocolVersion·clientCapabilities, 헤더와 바디의 일치, `=?base64?...?=` 디코드, `Mcp-Param-*`.
- 에러 코드: `-32020`(헤더 불일치), `-32021`(capability 누락, `data.requiredCapabilities`), `-32022`(버전 미지원, `data.supported`·`data.requested`).
- 결과: 모든 결과에 `resultType`과 `_meta` serverInfo, `server/discover`와 `tools/list`에 `ttlMs`·`cacheScope`, `tools/list` 페이지네이션.
- 테스트 툴: `echo_header_params`(`x-mcp-header`가 달린 인자), `confirm_action`(`input_required` 왕복 1회), `convert_interactive`(`input_required` 왕복 2회: 1차 호출은 바꿀 단위를, 2차는 소수 자릿수를 묻고 3차에 변환 결과를 줍니다). 모던 `tools/list`에만 나옵니다.

### strict2026 기본 동작 변경

`strict2026`의 기본값은 `true`이고, 이때 모던 경로는 스펙대로 동작합니다. 이전 동작(`strict2026: false`)과 달라진 점은 다음과 같습니다.

| 항목 | 지금(기본) | 이전 |
| --- | --- | --- |
| `MCP-Protocol-Version` 헤더 누락 | 400 + `-32020` | 통과 |
| 헤더는 레거시 버전, `_meta`는 2026-07-28 | 400 + `-32020` | 레거시 경로로 처리 |
| 모르는 메서드, `initialize`, `ping` | 404 + `-32601` | 200 |
| 알 수 없는 툴 | JSON-RPC `-32602` | `isError: true` 결과 |
| `-32022` | `data.supported`, `data.requested` 포함 | message만 |
| `server/discover` 결과 | `ttlMs`·`cacheScope` 포함, `serverInfo`는 `_meta`에만 | `serverInfo`가 최상위 |

전체 차이는 [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md)의 "`strict2026`에 따른 모던 경로 차이" 표에 있습니다. 예전의 느슨한 동작이 필요하면 `strict2026`을 끕니다. 레거시 경로는 이 값의 영향을 받지 않습니다.

```text
/scenario 화면: "지원 스펙" 카드의 "엄격 검증"을 끕니다.
요청 헤더:      X-MCP-Test-Scenario: lenient-2026
cfg 토큰:       {"strict2026": false}
```

## serverEra

`serverEra`는 서버가 어느 세대를 구현한 것처럼 동작할지 정합니다.

| `serverEra` | 헤더 시나리오 | 동작 |
| --- | --- | --- |
| `both` (기본) | - | 2026-07-28 요청과 레거시(initialize 핸드셰이크) 요청을 모두 처리합니다. |
| `legacy` | `server-era-legacy` | 2026을 모르는 레거시 서버처럼 동작합니다. `Mcp-Method`·`_meta` 표식을 무시하고 모든 요청을 레거시 경로로 처리하며, `server/discover`는 HTTP 200 + JSON-RPC `-32601`로 응답합니다. 모던 요청에 주는 응답은 `legacyModernProbe.mode`로 바꿀 수 있습니다. |
| `modern` | `server-era-modern` | 2026 전용입니다. 2026 표식이 없는 요청은 HTTP 400 + `-32022`로 거부하고, `GET`·`DELETE /mcp`는 405입니다. |

- `rejectLegacy: true`는 `serverEra: "modern"`의 옛 이름입니다. 둘 다 주면 명시한 `legacy`/`modern`이 우선합니다.
- `rawResponses` 규칙은 어느 모드에서든 가장 먼저 응답합니다.

## 설정을 넣는 방법

| 방법 | 형태 | 범위 |
| --- | --- | --- |
| `cfg` 쿼리 | `/mcp?cfg=<토큰>` | 그 URL로 온 요청 |
| `X-MCP-Test-Scenario` 헤더 | 시나리오 이름(쉼표로 여러 개) 또는 `cfg:<토큰>` | 그 요청 하나 |
| `POST /scenario` | `{"config": {...}}` 또는 `{"scenario": "<이름>"}` | 서버 전역 |

우선순위는 `cfg` 쿼리, 헤더, 서버 전역 설정 순입니다. 토큰은 기본값과 다른 부분만 담은 JSON을 base64url로 인코딩한 값입니다. `/scenario` 화면에서 설정을 고르면 URL이 만들어지고, 명령으로도 만들 수 있습니다.

```bash
PYTHONPATH=src python -c 'from unit_expert_mcp.server import encode_config_token; print(encode_config_token({"serverEra": "modern"}))'
```

## 에러 주입 옵션

모든 옵션과 값, 응답 형태는 [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md)에 있습니다. 아래는 그룹별 요약입니다.

| 그룹 | 하는 일 | 헤더 시나리오 예 |
| --- | --- | --- |
| `discover.*` | `server/discover` 응답을 바꿉니다. 레거시 버전만 광고, 빈 목록, JSON-RPC 에러, `-32020`/`-32022`, 401/403/5xx, SSE, id 불일치, 깨진 JSON, 연결 종료, 지연 | `discover-no-modern-version` |
| `legacyModernProbe.*` | `serverEra: legacy`인 서버가 모던 요청에 주는 응답을 고릅니다. `-32601`, `-32602`, 400(Python SDK 1.x와 같은 바디), 404, 빈 400, 무응답 | `legacy-probe-http-400` |
| `toolCall.*` | `tools/call` 응답을 바꿉니다(두 경로 공용). `isError`, JSON-RPC 에러, HTTP 오류, SSE, id 불일치, 빈 바디, 연결 종료, 지연 | `tool-call-sse` |
| `modernResponse.*` | 정상인 모던 결과에 위반을 넣습니다. `resultType` 누락·변조, serverInfo 누락, 캐시 필드 누락, `Mcp-Session-Id` 발급 | `modern-result-type-omit` |
| `modern.*` | 테스트 툴 노출, 필수 클라이언트 capability(`-32021`), `-32602`의 HTTP 상태, 바디에 값이 없는 `Mcp-Param-*`의 처리 | `require-capability-elicitation` |
| `toolsList.*` | 기존 목록 오류에 더해 페이지네이션(`pageSize`), 특정 페이지 실패(`failAtPage`), 캐시 힌트 | `tools-list-paged` |
| `session.*` | 레거시 세션 검증(`validate`: 세션 없음 400, 모르는 세션 404, `DELETE` 허용), 세션 ID 미발급 | `session-validate` |
| `auth.*` | OAuth(`oauth`, `mixed-oauth`), 스코프 부족 403(`enforceScope`) | - |
| `allowedOrigins` | 목록에 없는 `Origin`을 403으로 거부합니다. 비어 있으면(기본) 검증하지 않습니다 | - |
| `rawResponses` | 메서드별로 적어 둔 응답을 그대로 돌려줍니다 | - |

`/inspect` 화면은 서버가 메모리에 둔 `/mcp` 요청과 응답(가장 최근 200건), 그 응답을 만든 옵션(넣은 에러)을 보여줍니다(위 "호출 기록 (/inspect)" 참고).

## 테스트 클라이언트

`unit_expert_mcp.client`는 게이트웨이나 서버에 모던 또는 레거시 클라이언트로 붙는 CLI입니다. 표준 라이브러리만 씁니다.

```bash
PYTHONPATH=src python3 -m unit_expert_mcp.client <명령> --url http://localhost:8000/mcp
# 패키지를 설치했다면: unit-expert-client <명령> --url ...
```

클라이언트 명령은 `python3`으로 적습니다(`/scenario` 화면과 `GET /scenario/meta`가 주는 명령과 같습니다). Python 3.11 이상이 필요하고 저장소 루트에서 실행합니다. 가상환경이나 설치한 명령으로 실행하려면 아래처럼 합니다.

```bash
# 1) 가상환경의 파이썬을 직접 지정 (저장소 루트에서)
python3 -m venv .venv
PYTHONPATH=src .venv/bin/python -m unit_expert_mcp.client probe --url http://localhost:8000/mcp

# 2) 설치한 뒤 명령 이름으로 실행 (PYTHONPATH가 필요 없습니다)
.venv/bin/pip install -e .
.venv/bin/unit-expert-client probe --url http://localhost:8000/mcp
```

| 명령 | 하는 일 |
| --- | --- |
| `probe` | 스펙의 HTTP 폴백 절차로 대상을 `modern`/`legacy`/`dual`로 판정합니다. |
| `run [시나리오\|그룹\|all]` | 시나리오를 실행하고 기대값과 비교해 PASS/FAIL/WARN/SKIP을 냅니다. |
| `list` | 시나리오와 기대값, 근거를 보여줍니다. `--expect gateway`를 주면 게이트웨이 근거도 나옵니다. |
| `call <method>` | 요청 하나를 보냅니다. 규격을 일부러 어기는 플래그가 있습니다. |

자주 쓰는 옵션:

- `--url`, `--bearer`: 대상과 토큰입니다. 환경변수 `UNIT_EXPERT_CLIENT_URL`, `UNIT_EXPERT_CLIENT_BEARER`로도 줄 수 있습니다. 토큰은 출력에서 가려집니다.
- `--expect server|gateway`: 기대값 기준입니다. `server`는 스펙대로 동작하는 서버, `gateway`는 서버 앞에 둔 게이트웨이(MCP 프록시)입니다.
- `--cfg-json '<JSON>'`, `--scenario-header <이름>`: 단위전문가 서버에 직접 붙을 때 서버 설정을 넣습니다.
- `-v`, `--json`: 전체 요청·응답, 기계 판독용 출력입니다.
- `--strict`: WARN(SHOULD 불일치)도 실패로 셉니다. 합계 줄에 실패로 센 건수가 함께 나오고, `--json` 출력에는 `strict`와 `failed`(종료 코드를 정한 실패 건수)가 들어갑니다.

종료 코드:

| 명령 | 0 | 1 | 2 |
| --- | --- | --- | --- |
| `call` | 마지막 요청이 HTTP 응답을 받음. 상태 코드(4xx, 5xx 포함)나 JSON-RPC 에러와 무관합니다 | - | 응답을 받지 못함(연결 실패, 타임아웃 등) |
| `run` | FAIL 없음(WARN, SKIP은 실패가 아님) | FAIL이 있음. `--strict`면 WARN도 포함 | 모르는 시나리오·그룹 이름 |
| `probe` | 판정이 `modern`, `legacy`, `dual` | 판정 불가(인증 필요, 연결 실패, 어느 쪽도 아님) | - |
| `list` | 항상 | - | - |

- `call`로 서버가 요청을 거부했는지 확인하려면 종료 코드가 아니라 출력의 HTTP 상태와 JSON-RPC code를 봅니다(`--json`이면 `exchanges[-1].status`, `rpcCode`).
- 옵션 형식이 틀리면 argparse가 2로 끝냅니다. `--url`이 없거나 JSON 인자(`--args`, `--cfg-json` 등)가 JSON이 아니면 메시지를 내고 1로 끝냅니다.
- 사람용 출력에서는 대상이 보낸 제어 문자(ESC 등)를 `\x1b`처럼 보이는 문자로 바꿔 보여줍니다. `--json` 출력은 JSON 문자열 그대로입니다.
- `-v`와 `--json` 출력에서 토큰류는 `***`로 가립니다: `Authorization` 등 헤더, JSON 본문의 `access_token`·`refresh_token`·`client_secret`·`code_verifier`, form 본문과 URL 쿼리·`Location` 헤더의 `code`·`code_verifier`·`refresh_token`·`client_secret`·`access_token`.

시나리오 그룹:

| 그룹 | 내용 |
| --- | --- |
| `modern` | 규격에 맞는 2026-07-28 요청(discover, tools/list, 페이지네이션, tools/call, `Mcp-Param-*`, `input_required` 왕복, listen) |
| `modern-violation` | 서버가 거부해야 하는 요청(헤더·`_meta` 누락과 불일치, 삭제된 메서드, GET/DELETE) |
| `legacy` | initialize 핸드셰이크, 버전 협상, 세션 |
| `auth` | 401/403 챌린지, OAuth 흐름 |
| `compat` | 호환 표(모던·레거시·듀얼 클라이언트) |
| `injection` | 서버의 주입 옵션을 하나씩 켜고 클라이언트의 판정을 확인합니다. 단위전문가 서버에 직접 붙을 때만 실행됩니다. |

`--expect server`에서는 시나리오가 기본값으로 꺼져 있는 서버 옵션(`session.validate`, `auth.enforceScope`, `toolsList.pageSize`)을 `?cfg=`로 켜서 실행합니다. 서버 기본 동작 그대로 보려면 `--no-inject`를 줍니다. 대상이 `?cfg=`를 받지 않는 서버면 주입 없이 실행하고 `injection` 그룹은 건너뜁니다.

테스트 툴은 `tools/list`에서 찾습니다. `echo_header_params`와 `confirm_action`이 있으면 그 툴을 쓰고, 없으면 `x-mcp-header`가 달린 툴과 이름·설명으로 찾은 툴을 씁니다. `--header-tool`, `--input-tool`, `--tool`로 지정할 수 있습니다. `input-required-multi-round`는 `convert_interactive`가 있을 때만 실행하고(없으면 SKIP) 1차·2차 `input_required`, 3차 `complete`를 확인합니다. `call --auto-input`은 `input_required`가 이어지는 동안(최대 5회) 직전 응답의 `requestState`와 `requestedSchema`에 맞춘 값(enum은 첫 값, 숫자는 minimum·maximum 안의 값)으로 재요청합니다. `call`은 `clientCapabilities`를 기본 `{}`로 보내므로 `elicitation`을 요구하는 툴에는 `--capabilities '{"elicitation":{}}'`를 함께 줍니다.

```bash
# 로컬 서버의 세 세대를 차례로 검사
for era in both legacy modern; do
  PYTHONPATH=src python3 -m unit_expert_mcp.client run all \
    --url http://localhost:8000/mcp --cfg-json "{\"serverEra\":\"$era\"}"
done

# 규격을 어긴 요청 하나
PYTHONPATH=src python3 -m unit_expert_mcp.client call tools/list \
  --url http://localhost:8000/mcp --version-header 2025-11-25 -v
```

## 게이트웨이 테스트 매트릭스

클라이언트(이 CLI) → 게이트웨이 → 업스트림 서버(이 서버) 구성에서 클라이언트 세대와 서버 세대의 네 조합을 확인하는 절차입니다.

준비:

1. 서버를 게이트웨이가 닿는 주소에 띄웁니다.
2. 조합마다 서버 세대를 정한 URL을 게이트웨이에 업스트림으로 등록합니다. 설정은 URL의 `cfg` 토큰으로 넣습니다. `X-MCP-Test-Scenario` 헤더는 게이트웨이가 그 헤더를 업스트림에 붙여 주도록 등록할 수 있을 때만 쓸 수 있습니다. 클라이언트가 보낸 헤더를 게이트웨이가 업스트림까지 전달하는지는 미확정입니다.
3. 게이트웨이 주소와 토큰을 환경변수로 둡니다.

```bash
export UNIT_EXPERT_CLIENT_URL=<게이트웨이의 MCP 엔드포인트>
export UNIT_EXPERT_CLIENT_BEARER=<게이트웨이 토큰>
```

등록할 업스트림 URL:

| 서버 세대 | cfg JSON | 등록 URL | 헤더로 대신할 때 |
| --- | --- | --- | --- |
| modern | `{"serverEra":"modern"}` | `<서버>/mcp?cfg=eyJyZWplY3RMZWdhY3kiOnRydWUsInNlcnZlckVyYSI6Im1vZGVybiJ9` | `X-MCP-Test-Scenario: server-era-modern` |
| legacy | `{"serverEra":"legacy"}` | `<서버>/mcp?cfg=eyJzZXJ2ZXJFcmEiOiJsZWdhY3kifQ` | `X-MCP-Test-Scenario: server-era-legacy` |

각 조합에서 먼저 `probe`로 게이트웨이가 클라이언트에게 어떤 세대로 보이는지 확인하고, 이어서 `run`을 실행합니다.

```bash
PYTHONPATH=src python3 -m unit_expert_mcp.client probe
```

| 조합 | 업스트림 등록 | 클라이언트 명령 (`PYTHONPATH=src python3 -m unit_expert_mcp.client` 뒤에) | 기대 결과 |
| --- | --- | --- | --- |
| 2026 방식 요청 → 2026-07-28만 지원하는 서버 | modern URL | `run modern modern-violation compat-modern-client --expect gateway` | 스펙대로면 동작합니다. 게이트웨이 기대값은 미확정입니다. |
| 2026 방식 요청 → 2025 방식만 지원하는 서버 | legacy URL | `run modern compat-modern-client --expect gateway` | 게이트웨이가 모던 요청을 레거시 업스트림 호출로 바꿔 주면 동작합니다. 바꿔 주는지는 미확정입니다. 서버에 직접 붙으면 모던 클라이언트는 실패합니다(DiscoverResult를 받지 못함). |
| 2025 방식 요청 → 2025 방식만 지원하는 서버 | legacy URL | `run legacy compat-legacy-client --expect gateway` | 동작합니다. `initialize`의 협상 버전은 `2025-06-18`, 세션 없는 요청은 400 + `-32600`, 모르는 세션은 404 + `-32600`입니다(게이트웨이 코드로 확인한 기대값). |
| 2025 방식 요청 → 2026-07-28만 지원하는 서버 (미지원 확인) | modern URL | `run legacy compat-legacy-client --expect gateway` | 실패해야 합니다. 서버에 직접 붙으면 `initialize`가 400 + `-32022`이고 에러에 지원 버전이 적힙니다. 게이트웨이를 거치면 실패가 어느 단계(`initialize`, `tools/list`, `tools/call`)에서 어떤 상태와 코드로 드러나는지 미확정입니다. `-v`로 실제 응답을 기록합니다. |

- `--expect gateway`에서는 게이트웨이 기대값이 미확정인 시나리오를 SKIP으로 표시합니다. 스펙 기대값으로라도 실행하려면 `--include-unconfirmed`를 붙입니다. 어떤 시나리오가 확정·조건부·미확정인지는 `list --expect gateway`로 봅니다.
- 조합별 서버 쪽 기준값은 게이트웨이 없이 서버에 직접 붙어 얻습니다. 예: `run legacy compat --url <서버>/mcp --cfg-json '{"serverEra":"modern"}'`.
- 듀얼 클라이언트의 폴백(모던 요청 → `initialize`)은 `compat-probe`, `compat-dual-client`로 봅니다. 레거시 서버가 모던 요청에 주는 응답을 바꿔 보려면 등록 URL의 cfg에 `legacyModernProbe.mode`를 더합니다. 예: `{"serverEra":"legacy","legacyModernProbe":{"mode":"http-400"}}`.
- 업스트림에 실제로 무엇이 갔는지는 서버의 `/inspect`에서 확인합니다.

## 알려진 한계

서버:

- 모던 경로는 툴만 다룹니다. `resources/*`, `prompts/*`, `completion/complete`, tasks 확장은 없고 404 + `-32601`입니다.
- `subscriptions/listen`은 ack를 보낸 뒤 바로 종료 응답을 주고 스트림을 닫습니다. 변경 알림은 보내지 않습니다.
- 여러 위반이 겹칠 때의 검증 순서는 스펙에 없어 스펙 노트의 체크리스트 순서를 따릅니다.
- 스펙이 HTTP 상태를 정하지 않은 `-32602`(알 수 없는 툴, 잘못된 커서)는 기본 200입니다. `modern.invalidParamsHttpStatus`로 400으로 바꿀 수 있습니다.
- `Mcp-Param-*` 헤더만 있고 바디에 값이 없는 경우는 스펙에 규정이 없습니다. 기본은 거부이고 `modern.rejectUnexpectedParamHeader`로 끌 수 있습니다.
- `Origin` 검증은 기본으로 꺼져 있습니다(`allowedOrigins`가 빈 목록).
- `serverEra: both`에서 `MCP-Protocol-Version: 2026-07-28`만 있고 `Mcp-Method`가 없는 `GET /mcp`는 405가 아니라 레거시 스트림 규칙으로 처리합니다. 버전 헤더는 2026 표식으로 보지 않습니다.
- 레거시 경로는 느슨합니다. 세션 검증은 기본으로 꺼져 있고, 모르는 `MCP-Protocol-Version` 헤더를 거부하지 않으며, 401의 `WWW-Authenticate`에 `scope`가 없습니다.
- 설정, 세션, OAuth 상태는 프로세스 메모리에 있어 재시작하면 사라집니다.

클라이언트:

- Streamable HTTP만 지원합니다. stdio는 없습니다.
- 게이트웨이 기대값은 코드 조사에 근거한 것이고 실제 게이트웨이로 검증하지 않았습니다. 미확정 항목은 SKIP입니다.
- `auth-oauth-flow`, `auth-insufficient-scope-403`은 이 서버의 자체 인증 서버에서만 동작합니다.
- 폴백 판정에서 스펙이 직접 정하지 않은 경우(5xx, 깨진 응답, 타임아웃, `supportedVersions`에 아는 버전이 없는 DiscoverResult)는 해석을 넣었습니다. 해석은 `list`의 `inject-discover-modes` 기대값에 적혀 있습니다.

SDK baseline:

- 공식 Python SDK 2.3.0의 무상태 서버는 `initialize`도 받아서 `probe` 판정이 `dual`입니다. modern 전용 서버가 필요하면 이 서버의 `serverEra: modern`을 씁니다.
- SDK 서버는 스펙 노트와 다른 점이 있습니다(알 수 없는 툴이 `isError` 결과, 잘못된 커서 무시, 버전 헤더가 없거나 레거시 버전이면 모던 `_meta`가 있어도 레거시로 처리).

## Run Locally

```bash
PYTHONPATH=src PORT=8000 python -m unit_expert_mcp.server
```

Smoke test:

```bash
curl -i -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-03-26' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

2026-07-28 요청:

```bash
curl -i -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

## Official SDK 2026 Baseline

The existing `unit_expert_mcp.server` implementation is the fault-injection
server used for 2025/legacy and intentionally invalid scenarios. For a clean
2026-07-28 baseline backed by the official MCP Python SDK, run the separate
entrypoint:

```bash
pip install ".[sdk2026]"
PYTHONPATH=src PORT=8000 python -m unit_expert_mcp.sdk_2026_server
```

This starts a Streamable HTTP MCP endpoint at `/mcp` using the SDK's ASGI app.
It exposes the same six Unit Expert tools, but does not implement the scenario
UI or malformed-response cases.

The SDK validates the `Host` header on `/mcp` (HTTP 421 for an unlisted host)
and the `Origin` header (403). The defaults allow localhost and the two Render
hosts; set `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` (comma-separated) for
any other host name.

Expected Render endpoint:

```text
https://unit-expert-mcp-2026.onrender.com/mcp
```

Docker:

```bash
docker build -f Dockerfile.sdk2026 -t unit-expert-mcp-sdk2026 .
docker run --rm -p 8000:8000 unit-expert-mcp-sdk2026
```

레거시 서버의 실제 응답을 볼 때는 SDK 1.x로 만든 `scripts/sdk1_legacy_server.py`를 씁니다(`mcp>=1,<2` 필요, 배포 대상 아님).

## Test

```bash
python -m pytest -q
ruff check
```
