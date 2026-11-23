# 설정 옵션 레퍼런스

`src/unit_expert_mcp/server.py`의 `DEFAULT_CONFIG`에 있는 모든 옵션이다. `tests/test_config_options.py`가 이 문서에 모든 키와 모드 값이 적혀 있는지 검사한다.

스펙 근거는 [SPEC_2026-07-28_NOTES.md](SPEC_2026-07-28_NOTES.md)를 본다.

## 설정을 넣는 방법

| 방법 | 형태 | 범위 |
|---|---|---|
| `cfg` 쿼리 | `/mcp?cfg=<토큰>`. 토큰은 기본값과 다른 부분만 담은 JSON을 base64url로 인코딩한 것(`encode_config_token`) | 그 URL로 온 요청 |
| `X-MCP-Test-Scenario` 헤더 | 시나리오 이름 하나, 쉼표로 이은 여러 개(왼쪽부터 적용), 또는 `cfg:<토큰>` | 그 요청 하나 |
| `POST /scenario` | `{"config": {...}}` 또는 `{"scenario": "<이름>"}` | 서버 전역(활성 설정) |
| `/scenario` 화면 | "프로토콜 주입 설정" 카드에서 고르고 적용하면 `cfg` URL이 만들어진다 | 그 URL로 온 요청 |

- 우선순위는 `cfg` 쿼리, `X-MCP-Test-Scenario` 헤더, 활성 설정 순이다.
- 잘못된 값은 `normalize_config`가 `ValueError`로 거부한다. `/mcp`에서는 HTTP 400(평문), `POST /scenario`에서는 `{"ok": false, "error": ...}`가 된다.
- `rawResponses` 규칙은 아래 모든 옵션보다 먼저 응답한다.

## 용어

- **모던 경로**: `Mcp-Method` 헤더나 `_meta`의 protocolVersion이 있는 요청(2026-07-28). `serverEra`가 `legacy`면 없다.
- **레거시 경로**: `initialize` 핸드셰이크와 세션 ID를 쓰는 요청(2025-11-25 이하).
- "적용 era" 열의 `모던`, `레거시`는 이 경로를 뜻한다. `둘 다`는 두 경로 모두다.

## 서버 세대와 스펙 준수

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `protocolEra` | `2025`, `2026` | `2025` | `/scenario` 화면의 미리보기가 어느 방식으로 요청을 보낼지만 정한다. 서버 동작에는 영향이 없다 | 화면 |
| `serverEra` | `both`, `legacy`, `modern` | `both` | `both`는 두 경로를 모두 처리한다. `legacy`는 2026을 모르는 서버처럼 동작한다(`legacyModernProbe` 참고). `modern`은 2026 전용이라 레거시 요청을 400 + `-32022`로 거부한다 | 둘 다 |
| `rejectLegacy` | bool | `false` | `serverEra: "modern"`의 옛 이름. 정규화하면 항상 `serverEra == "modern"`과 같은 값이 된다 | 둘 다 |
| `strict2026` | bool | `true` | `true`면 모던 경로가 공식 스펙대로 동작한다. `false`면 이전의 느슨한 동작으로 돌아간다(아래 표). 레거시 경로에는 영향이 없다 | 모던 |
| `allowedOrigins` | 문자열 목록(최대 50개) | `[]` | 비어 있으면 모든 Origin을 받는다. 값이 있으면 `Origin` 헤더가 목록에 없는 `/mcp` 요청(POST/GET/DELETE)을 403으로 거부한다. 본문은 `id` 없는 JSON-RPC 에러다. `Origin` 헤더가 없는 요청은 통과한다 | 둘 다 |

### `strict2026`에 따른 모던 경로 차이

| 항목 | `true` (기본, 스펙) | `false` (예전 동작) |
|---|---|---|
| `MCP-Protocol-Version` 헤더 누락 | 400 + `-32020` | 통과 |
| `-32022`의 `data` | `{supported: ["2026-07-28"], requested}` | 없음 |
| 지원 버전 | `2026-07-28`만 | `2025-11-25` 등 레거시 버전도 통과 |
| `Mcp-Name` 비교 | `=?base64?...?=`를 디코드한 뒤 비교 | 헤더 문자열 그대로 비교 |
| 같은 이름의 `MCP-Protocol-Version`/`Mcp-Method`/`Mcp-Name`/`Mcp-Param-*`가 두 번 이상 | 400 + `-32020`(값이 같아도) | 검사 없음(첫 번째 값 사용) |
| `Mcp-Method`/`Mcp-Name`/`Mcp-Param-*` 값의 앞뒤 공백·탭 | 떼고 비교 | 그대로 비교 |
| `Mcp-Param-*` | 검증함 | 검증 없음(테스트 툴도 없음) |
| 모르는 메서드, `initialize`, `ping` | HTTP 404 + `-32601` | `initialize`와 `ping`은 200 성공, 나머지는 200 + `-32601` |
| 알 수 없는 툴 | JSON-RPC 에러 `-32602` | `isError: true` 결과 |
| `server/discover` 결과 | `ttlMs`, `cacheScope` 포함. `serverInfo`는 `_meta`에만 | `serverInfo`가 결과 최상위, 캐시 필드 없음 |
| `_meta` serverInfo의 name | `mcp.identifier` | 고정값 `unitExpert` |
| `id`가 null이거나 문자열·정수가 아님 | 400 + `-32600` | 통과 |
| `serverEra: "modern"`의 `GET /mcp` | 항상 405 | `Mcp-Method` 헤더가 있을 때만 405. 없으면 레거시 스트림을 연다 |
| 헤더는 레거시 버전인데 `_meta`에 protocolVersion이 있는 요청 | 모던 요청으로 보고 400 + `-32020` | 레거시 경로로 처리 |
| 401의 `WWW-Authenticate` | `scope="mcp"` 포함 | 없음 |
| `tools/list`에 모르는 `cursor` | `-32602` | 무시 |
| `discover.*`, `modernResponse.*`, `modern.*`, `toolsList.pageSize` | 적용 | 적용 안 함 |

`toolCall.*`은 `strict2026` 값과 상관없이 적용된다.

## MCP 식별 정보

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `mcp.identifier` | 문자열(1~128자) | `unitExpert` | 레거시 `initialize`의 `serverInfo.name`, 모던 결과의 `_meta` serverInfo name | 둘 다 |
| `mcp.serviceName` | 문자열(1~200자) | `Unit Expert(단위전문가)` | 툴 description에 들어가는 서비스 이름 | 둘 다 |

## HTTP 수준 응답과 인증

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `server.httpStatus` | `200`, `401`, `403` | `200` | 401/403이면 JSON-RPC 처리 전에 평문으로 그 상태를 돌려준다(`WWW-Authenticate` 없음) | 둘 다 |
| `server.target` | `all`, `initialize` | `all` | `initialize`면 `server.httpStatus`를 `initialize` 요청과 GET에만 적용한다 | 둘 다 |
| `server.delayEnabled` | bool | `false` | 켜면 OPTIONS가 아닌 `/mcp` 요청을 `server.delaySeconds`만큼 늦춘다 | 둘 다 |
| `server.delaySeconds` | 0~30 | `5.0` | 위 지연 시간(초) | 둘 다 |
| `customHeader.enabled` | bool | `false` | 켜면 `customHeader.name` 헤더가 없을 때 401, 값이 `customHeader.value`와 다를 때 403 | 둘 다 |
| `customHeader.name` | 고정 | `X-Mock-Auth` | 검사할 헤더 이름. 설정으로 바꿀 수 없다 | 둘 다 |
| `customHeader.value` | 고정 | `allow` | 기대하는 헤더 값. 설정으로 바꿀 수 없다 | 둘 다 |
| `auth.mode` | `off`, `oauth`, `mixed-oauth` | `off` | `oauth`는 모든 `/mcp` 요청에, `mixed-oauth`는 보호 툴의 `tools/call`에만 Bearer 토큰을 요구한다. 없거나 무효면 401 + `WWW-Authenticate: Bearer resource_metadata="..."` | 둘 다 |
| `auth.enforceScope` | bool | `false` | 켜면 유효한 토큰의 scope에 `mcp`가 없을 때 403 + `WWW-Authenticate: Bearer error="insufficient_scope", scope="mcp", resource_metadata="..."` | 둘 다 |
| `auth.protectedTools` | 문자열 목록 | 변환 툴 5개 | `mixed-oauth`에서 토큰이 필요한 툴 | 둘 다 |

모던 경로(`strict2026: true`)의 401에는 `scope="mcp"`가 더 붙는다. 레거시 경로의 401 헤더는 그대로다.

설정과 상관없이 적용되는 HTTP 수준 규칙:

- 핸들러가 없는 HTTP 메서드(`PUT`, `PATCH`, `HEAD`, `TRACE` 등)는 405(평문 `Method Not Allowed`)와 `Allow` 헤더를 받는다. 예전에는 501이었다. `/mcp`의 `Allow`는 설정을 따른다: 기본 `GET, POST, OPTIONS`, `serverEra: modern`(`strict2026: true`)이면 `POST, OPTIONS`, `session.validate`가 켜져 있으면 `DELETE`가 더 붙는다. 다른 경로는 그 경로가 받는 메서드를, 모르는 경로는 404를 준다. 응답 뒤 연결을 닫는다.
- `POST /mcp`의 바디가 JSON으로 읽히지 않으면(UTF-8이 아닌 바이트, 잘린 JSON, 지나치게 깊은 중첩, 지나치게 긴 정수 리터럴) HTTP 400 + `-32700`, `id: null`이다.

## initialize (레거시)

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `initialize.protocolVersionEnabled` | bool | `true` | `true`면 `initialize.protocolVersion`을 그대로 응답한다. `false`면 클라이언트가 요청한 레거시 버전을 돌려주고, 모르는 버전이면 `2025-11-25`를 준다 | 레거시 |
| `initialize.protocolVersion` | 빈 문자열이 아닌 문자열 | `2025-03-26` | `initialize` 결과의 `protocolVersion`. 값 검증은 없다 | 레거시 |

화면과 `PROTOCOL_VERSION_CHOICES`가 제공하는 선택지:

| 값 | 용도 |
|---|---|
| `2025-03-26`, `2025-06-18`, `2025-11-25` | 현행 레거시 버전 |
| `2024-11-05` | 오래된 레거시 버전. 최소 지원 버전을 둔 클라이언트가 거부하는지 확인 |
| `2026-07-28` | 레거시 버전이 아닌 값. `initialize` 결과로 받은 클라이언트가 거부하는지 확인 |
| `2024-03-26` | 스펙에 없는 버전 |

## tools/list

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `toolsList.mode` | `normal`, `json-rpc-error`, `null`, `empty`, `too-many` | `normal` | `json-rpc-error`는 HTTP 200 + `-32603`, `null`은 `tools: null`, `empty`는 `tools: []`, `too-many`는 `toolsList.tooManyCount`개의 생성된 툴 | 둘 다 |
| `toolsList.tooManyCount` | 1~100 | `21` | `too-many`에서 돌려줄 툴 개수 | 둘 다 |
| `toolsList.duplicateToolName` | bool | `false` | 같은 이름의 툴 두 개를 넣는다. `mode`가 `normal`이나 `too-many`일 때만 켜진다 | 둘 다 |
| `toolsList.cacheTtlMs` | 0~3600000 | `0` | 모던 `tools/list` 결과의 `ttlMs` | 모던 |
| `toolsList.cacheScope` | `private`, `public` | `private` | 모던 `tools/list` 결과의 `cacheScope` | 모던 |
| `toolsList.pageSize` | 0~100 | `0` | 0이면 페이지네이션 없음. N이면 한 페이지에 N개씩 주고 다음 페이지가 있으면 `nextCursor`를 넣는다. 모르는 커서는 `-32602`. `tools`가 배열이 아닌 `null` 모드와 `json-rpc-error` 모드에는 적용하지 않는다 | 둘 다 |
| `toolsList.failAtPage` | 0~100 | `0` | N이면 N번째 페이지(1부터) 요청에 HTTP 200 + `-32603`을 준다. `toolsList.pageSize`가 0보다 클 때만 동작한다 | 둘 다 |
| `toolErrors` | 시나리오 이름 목록 | `[]` | 정책 위반 툴(이름·description·annotations 문제)로 목록을 바꾼다. 값은 README의 시나리오 표를 본다 | 둘 다 |

- 커서는 불투명한 문자열이다. 이 서버가 발급한 값만 유효하다. 이 서버는 빈 문자열 커서를 발급하지 않으므로 요청의 `cursor: ""`도 모르는 커서로 보고 `-32602`를 준다. 스펙의 "빈 문자열도 유효한 커서"는 클라이언트가 받은 `nextCursor`가 빈 문자열이어도 끝으로 보지 말라는 클라이언트 규칙이고(SPEC 노트 7절), 서버가 발급하지 않은 빈 문자열을 받아들이라는 뜻이 아니다. 테스트 클라이언트는 `nextCursor`가 문자열이면 빈 문자열이어도 다음 페이지를 요청한다.
- `toolsList.pageSize`가 0일 때 `cursor`가 오면 모던 경로는 `-32602`를 주고, 레거시 경로는 예전처럼 무시한다.
- 잘못된 커서의 HTTP 상태는 레거시 200, 모던은 `modern.invalidParamsHttpStatus`다.

## 모던 경로 옵션

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `modern.testTools` | bool | `true` | 모던 `tools/list`에 테스트 툴 `echo_header_params`, `confirm_action`을 노출한다. 끄면 목록에서 빠지고 호출하면 알 수 없는 툴이 된다 | 모던 |
| `modern.requiredClientCapabilities` | `elicitation`, `sampling`, `roots`의 목록 | `[]` | `server/discover`를 뺀 모든 모던 요청에서 `_meta` clientCapabilities에 이 항목이 객체로 선언돼 있지 않으면 400 + `-32021`, `data.requiredCapabilities`에 빠진 항목을 담는다 | 모던 |
| `modern.invalidParamsHttpStatus` | `200`, `400` | `200` | 스펙이 HTTP 상태를 정하지 않은 `-32602`(알 수 없는 툴, 잘못된 커서, 무효한 `requestState`, 형식이 틀린 `tools/call` 파라미터)의 HTTP 상태. 필수 `_meta` 누락의 `-32602`는 스펙대로 항상 400이다 | 모던 |
| `modern.rejectUnexpectedParamHeader` | bool | `true` | `Mcp-Param-*` 헤더가 왔는데 바디에 그 인자 값이 없을 때(`null`이거나 인자에 없음)의 처리. `true`면 400 + `-32020`, `false`면 그 헤더를 무시하고 처리한다. 스펙에 이 경우의 규정이 없어 둔 옵션이다(아래 근거) | 모던 |

### 테스트 툴

레거시 `tools/list`에는 나오지 않고, 레거시 경로에서 호출하면 예전처럼 `isError: true`("Unknown tool")가 된다.

**`echo_header_params`**: 받은 인자를 JSON 텍스트로 돌려준다.

| 인자 | 타입 | `x-mcp-header` | 헤더 |
|---|---|---|---|
| `region` | string | `Region` | `Mcp-Param-Region` |
| `greeting` | string(비ASCII 가능) | `Greeting` | `Mcp-Param-Greeting` |
| `count` | integer | `Count` | `Mcp-Param-Count` |
| `verbose` | boolean | `Verbose` | `Mcp-Param-Verbose` |
| `note` | string | 없음 | 없음 |

검증 규칙(위반하면 모두 400 + `-32020`):

- 인자 값이 있으면 헤더가 있어야 하고, 디코드한 값이 같아야 한다.
- 인자가 `null`이거나 없으면 헤더를 기대하지 않는다. 그런데도 헤더가 오면 바디에 없는 값을 가리키므로 불일치로 거부한다(`modern.rejectUnexpectedParamHeader: false`로 끌 수 있다).
- 헤더 값의 앞뒤 공백과 탭(OWS)은 값이 아니므로 떼고 비교한다. `Mcp-Method`, `Mcp-Name`도 같다.
- 스키마에 선언된 `Mcp-Param-*` 헤더가 두 번 이상 오면 값이 같아도 거부한다. 선언되지 않은 `Mcp-Param-*`는 중복이어도 무시한다.
- 정수는 수치로 비교한다(`42.0`과 `42`는 같다). boolean은 소문자 `true`/`false`만 맞다. 자세한 규칙은 아래 "SDK 2.3.0에 맞춘 엄격도"를 본다.
- 헤더 값에 허용되지 않는 문자가 있으면 거부한다. 허용 문자는 스펙 "Value Encoding"이 RFC 9110을 인용해 적은 그대로 보이는 ASCII(0x21~0x7E), 공백(0x20), 수평 탭(0x09)이다. 그 밖의 문자(ASCII 밖의 문자, 탭을 뺀 제어문자, DEL)가 있으면 거부한다. 값 가운데의 탭은 허용 문자라서 거부하지 않는다.
- `=?base64?...?=` 안이 base64·UTF-8로 풀리지 않으면 거부한다.
- 인자 타입이 스키마와 다르면(예: `count`가 문자열) 헤더가 맞더라도 `isError: true` 결과가 된다.

헤더는 있는데 바디에 값이 없는 경우의 근거(공식 문서 [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) "Server Behavior for Custom Headers", 2026-11-23 재확인):

- 규칙 표는 네 줄뿐이다: 값 있음(서버는 일치 검증 MUST), 값이 `null`(서버는 헤더를 기대하면 안 됨 MUST NOT), 인자에 없음(같음), 바디에 값이 있는데 헤더 없음(거부 MUST). 헤더만 있고 값이 없는 경우의 줄은 없다.
- "MUST NOT expect the header"는 헤더가 없어도 거부하지 말라는 뜻이고, 헤더가 왔을 때 받아들이라는 문장은 아니다.
- 일반 규칙은 "헤더 값이 바디의 대응 값과 일치하는지 검증 MUST, 실패하면 400 + `-32020`"이다. 대응 값이 없는 헤더를 불일치로 볼지는 적혀 있지 않다.
- 그래서 기본값은 불일치로 거부하는 해석(헤더와 바디를 서로 다른 구성 요소가 믿는 상황을 막는다는 Server Validation 절의 취지)을 유지하고, 반대 해석을 시험할 수 있게 옵션으로 끌 수 있게 했다.
- 공식 Python SDK 2.3.0도 같은 해석이다. `mcp/shared/inbound.py`의 `validate_mcp_param_headers`가 "header is present but the request body's argument is absent"로 `-32020`을 준다(2026-11-23 확인).
- 끄더라도 잘못된 문자가 든 헤더, 값 불일치, 헤더 누락은 그대로 `-32020`이다.

#### SDK 2.3.0에 맞춘 엄격도

스펙 노트가 정한 것은 스펙을 따르고, 스펙에 없는 세부는 공식 Python SDK 2.3.0(`mcp/shared/inbound.py`, `mcp/server/_streamable_http_modern.py`, 2026-11-23 코드 확인)을 따른다.

| 항목 | 스펙 | SDK 2.3.0 | 이 서버 |
|---|---|---|---|
| 정수 비교 | 수치 비교 SHOULD(`42.0` = `42`). 그 밖의 표기는 규정 없음 | 스키마 타입이 `integer`이고 바디 값이 정수(또는 `42.0` 같은 정수값 실수)이고 헤더가 `-?[0-9]+(\.[0-9]+)?` 꼴일 때만 수치 비교. 소수부는 모두 0이어야 한다. 그 밖에는 클라이언트가 만드는 문자열(`str(값)`)과 문자 그대로 비교 | SDK와 같음 |
| `42`, `42.0`, `42.000`, `042`, `-0`(값 0) | `42.0`만 예시 | 일치 | 일치 |
| `+42`, `4.2e1`, `42e0`, `42.`, `.42`, 디코드한 값의 앞뒤 공백(`=?base64?`로 보낸 `" 42"`) | 규정 없음 | 불일치 | 불일치(`-32020`). 이 수정 전에는 `+42`, `4.2e1`, 공백을 받아들였다 |
| base64 패딩 | 규정 없음(예시는 모두 `=` 패딩 포함) | `b64decode(validate=True)`: 패딩이 없으면 거부 | 거부. 이 수정 전에는 패딩 없는 값을 받아들였다 |
| base64 알파벳 | 명시 없음(예시에 `/`가 있어 표준 알파벳) | 표준 알파벳만. url-safe(`-`, `_`)는 거부 | 거부 |
| 정규형이 아닌 base64(남는 비트가 0이 아님, 패딩 과다) | 규정 없음 | 다시 인코딩한 값이 원문과 다르면 거부 | 거부 |
| 빈 페이로드 `=?base64??=` | 규정 없음 | 빈 문자열로 디코드 | 같음 |
| 같은 이름의 라우팅 헤더(`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`) 중복 | 규정 없음 | `find_duplicated_routing_header`: 값이 같든 다르든 `-32020`, message `<헤더> header appears more than once`. JSON-RPC 형태 검사 뒤, `_meta` 검사 앞 | 같음. message의 헤더 이름은 `MCP-Protocol-Version`처럼 표기한다(SDK는 소문자) |
| `Mcp-Param-*` 중복 | 규정 없음 | 툴 스키마에 선언된 헤더만, 값이 같든 다르든 `-32020`. 선언되지 않은 헤더는 보지 않음 | 같음 |
| 값 앞뒤 공백(OWS) | HTTP 규칙(RFC 9110 5.5)상 값이 아님 | ASGI 서버가 파싱 단계에서 뗀다 | `http.server`가 뒤쪽 공백을 남기므로 검증 전에 직접 뗀다 |
| 헤더 값의 허용되지 않는 문자 | 거부 MUST | 서버 쪽 검사 코드 없음(ASGI 서버에 맡김) | 스펙대로 거부 |

- 중복 거부의 이유는 SDK 주석과 같다: 첫 번째 값을 읽는 구성 요소와 마지막 값을 읽는 구성 요소가 서로 다르게 판단할 수 있다.
- 바디 값이 객체나 배열인 인자에 대한 처리(SDK는 헤더를 기대하지 않음)는 바꾸지 않았다. 이 서버는 값이 있으면 헤더가 없을 때 거부한다.

**`confirm_action`**: input_required(MRTR) 흐름. 인자는 `action`(string, 필수).

| 요청 | 응답 |
|---|---|
| 첫 호출 | `resultType: "input_required"`, `inputRequests.confirm`(`elicitation/create`, form 모드, `confirmed: boolean`), `requestState` |
| `inputResponses.confirm`이 `{action: "accept", content: {confirmed: true/false}}` + 받은 `requestState` | `resultType: "complete"` |
| `inputResponses.confirm.action`이 `decline` 또는 `cancel` + 받은 `requestState` | `resultType: "complete"`(거절·취소 문구) |
| `requestState`는 맞지만 `inputResponses`가 없거나 불완전 | 다시 `input_required` |
| `requestState` 없이 `inputResponses`만 | 첫 호출로 취급해 다시 `input_required` |
| 변조됐거나, 다른 인자에 쓰였거나, 10분이 지난 `requestState` | `-32602` |
| `_meta` clientCapabilities에 `elicitation` 객체가 없음 | 400 + `-32021`, `data.requiredCapabilities: {elicitation: {}}` |
| `action`이 빈 문자열이거나 문자열이 아님 | `isError: true` 결과 |

`requestState`는 툴 이름, 인자 해시, 만료 시각을 HMAC으로 서명한 값이다. 재요청의 JSON-RPC `id`가 첫 요청과 다른지는 서버가 검사하지 않는다.

## server/discover 주입

유효한 모던 `server/discover` 요청에만 적용한다. 헤더나 `_meta`가 틀린 요청은 주입 대신 스펙 에러를 받는다.

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `discover.mode` | 아래 표 | `normal` | 응답 형태 | 모던 |
| `discover.errorCode` | -32768~32767 | `-32603` | `json-rpc-error` 모드의 에러 코드 | 모던 |
| `discover.httpStatus` | 500~599 | `503` | `http-5xx` 모드의 HTTP 상태 | 모던 |
| `discover.delaySeconds` | 0~120 | `0.0` | 어떤 모드든 응답 전에 이만큼 기다린다 | 모던 |
| `discover.cacheTtlMs` | 0~86400000 | `0` | DiscoverResult의 `ttlMs` | 모던 |
| `discover.cacheScope` | `private`, `public` | `private` | DiscoverResult의 `cacheScope` | 모던 |

| `discover.mode` | HTTP | 응답 |
|---|---|---|
| `normal` | 200 | `supportedVersions: ["2026-07-28"]` |
| `no-modern-version` | 200 | `supportedVersions: ["2025-03-26", "2025-06-18", "2025-11-25"]` |
| `empty-versions` | 200 | `supportedVersions: []` |
| `missing-versions` | 200 | `supportedVersions` 필드 없음 |
| `json-rpc-error` | 200 | JSON-RPC 에러(`discover.errorCode`) |
| `header-mismatch` | 400 | `-32020` |
| `unsupported-version` | 400 | `-32022`, `data: {supported: ["2099-01-01"], requested}` |
| `http-401` | 401 | `WWW-Authenticate: Bearer resource_metadata="...", scope="mcp"` |
| `http-403` | 403 | 평문 `Forbidden` |
| `http-5xx` | `discover.httpStatus` | 평문 |
| `sse` | 200 | `text/event-stream`: 알림 1건 뒤 정상 응답 |
| `sse-no-response` | 200 | `text/event-stream`: 알림 1건만 보내고 종료 |
| `id-mismatch` | 200 | 요청과 다른 `id`의 정상 응답 |
| `invalid-json` | 200 | `Content-Type: application/json`인데 잘린 JSON |
| `disconnect` | 없음 | 응답 없이 연결을 끊는다 |

타임아웃은 `discover.delaySeconds`를 클라이언트 읽기 타임아웃보다 길게 주면 된다.

## 모던 응답 위반 주입

성공한 모던 결과에만 적용한다. 에러 응답과 레거시 응답은 건드리지 않는다.

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `modernResponse.resultType` | `normal`, `omit`, `input_required`, `unknown` | `normal` | `omit`은 `resultType` 필드를 뺀다. `input_required`는 값을 바꾸고 `requestState`를 넣는다. `unknown`은 `bogus_result_type`으로 바꾼다 | 모던 |
| `modernResponse.omitServerInfo` | bool | `false` | 결과 `_meta`에서 serverInfo를 뺀다 | 모던 |
| `modernResponse.omitCacheFields` | bool | `false` | 결과에서 `ttlMs`, `cacheScope`를 뺀다(`server/discover`, `tools/list`) | 모던 |
| `modernResponse.issueSessionId` | bool | `false` | 모던 응답에 `Mcp-Session-Id` 헤더를 붙인다 | 모던 |
| `modernResponse.methods` | `server/discover`, `tools/list`, `tools/call`의 목록 | `[]` | 위 주입을 적용할 메서드. 비면 셋 다 | 모던 |

## legacy 서버가 모던 요청을 받았을 때

`serverEra`가 `legacy`일 때만 읽는다.

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `legacyModernProbe.mode` | 아래 표 | `method-not-found` | 2026 표식이 있는 요청이나 `server/discover`에 대한 응답 | 레거시(`serverEra: legacy`) |
| `legacyModernProbe.delaySeconds` | 0~120 | `10.0` | `timeout` 모드에서 무응답으로 버티는 시간 | 레거시(`serverEra: legacy`) |

| `legacyModernProbe.mode` | 대상 요청 | 응답 | Accept 규칙 |
|---|---|---|---|
| `method-not-found` | 모든 요청 | 2026 표식을 무시하고 레거시 경로로 처리한다. `server/discover`는 HTTP 200 + `-32601` | 레거시 규칙 적용 |
| `invalid-params` | 모르는 메서드 전부 | HTTP 200 + `-32602`, message `Invalid request parameters`, `data: ""`. 아는 메서드는 그대로 처리 | 레거시 규칙 적용 |
| `http-400` | `MCP-Protocol-Version` 헤더가 레거시 버전이 아닌 요청(`initialize` 제외) | HTTP 400, 본문 `{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Unsupported protocol version: <버전>. Supported versions: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25"}}`. 헤더가 없거나 레거시 버전이면 레거시 경로로 처리하고, 그때 모르는 메서드는 `invalid-params`와 같은 본문을 받는다 | 400 응답은 건너뜀 |
| `http-404` | 2026 표식이 있는 요청, `server/discover` | HTTP 404, 평문 `Not Found` | 건너뜀 |
| `empty-400` | 위와 같음 | HTTP 400, 본문 없음 | 건너뜀 |
| `timeout` | 위와 같음 | `legacyModernProbe.delaySeconds` 동안 무응답 뒤 연결 종료 | 건너뜀 |

- 2026 표식은 `Mcp-Method` 헤더 또는 `_meta`의 protocolVersion이다. `MCP-Protocol-Version` 헤더가 레거시 버전이면 표식이 있어도 레거시 요청으로 본다.
- 레거시 Accept 규칙은 `application/json`과 `text/event-stream`이 둘 다 없으면 HTTP 400(평문)을 주는 것이다. 실제 레거시 서버처럼 메서드를 보기 전에 검사하므로, 위의 두 모드에서 `Accept: application/json`만 보낸 모던 클라이언트는 `-32601`/`-32602` 대신 이 400을 받는다.
- 나머지 네 모드는 전송 수준 응답이라 Accept와 상관없이 표의 응답을 준다. `server.httpStatus`, `customHeader`, `auth` 검사는 그보다 먼저 한다.
- 표식 없는 `initialize`와 그 뒤의 레거시 요청은 어느 모드에서도 정상 처리한다. 프로브가 실패한 뒤 `initialize`로 폴백하는 흐름을 확인할 수 있다.

### 실제 레거시 서버의 응답 (공식 Python SDK 1.27.2)

`scripts/sdk1_legacy_server.py`(FastMCP, streamable-http)를 띄워 2026-11-23에 관찰한 값이다. `invalid-params`와 `http-400` 모드의 본문은 이 값과 같다.

| 요청 | stateless 서버 | stateful 서버(기본값) |
|---|---|---|
| 모던 요청(`MCP-Protocol-Version: 2026-07-28`), 세션 없음 | HTTP 400, `{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Unsupported protocol version: 2026-07-28. Supported versions: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25"}}` | HTTP 400, `{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Missing session ID"}}` |
| 같은 요청, 유효한 세션 ID | - | stateless와 같은 400(Unsupported protocol version) |
| 버전 헤더 없는 `server/discover`, 또는 레거시 버전 헤더의 모르는 메서드 | HTTP 200, `{"jsonrpc":"2.0","id":<id>,"error":{"code":-32602,"message":"Invalid request parameters","data":""}}` | 세션이 있으면 같은 본문(SSE로 전달), 없으면 위의 Missing session ID |
| 레거시 버전 헤더 + 모던 `_meta`·`Mcp-Method`가 붙은 `tools/list` | HTTP 200, 레거시 형태 결과(`resultType` 없음) | 세션이 있으면 같음 |
| `Accept: application/json`만 | 위 400이 먼저 | HTTP 406, `-32600` `Not Acceptable: Client must accept both application/json and text/event-stream` |
| 모르는 세션 ID | 무시하고 처리 | HTTP 404, `{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Session not found"}}` |
| `initialize`에 `protocolVersion: 2026-07-28` | HTTP 200, `protocolVersion: 2025-11-25` | 같음 |

- `http-400` 모드는 stateless 열과 같다. stateful 서버가 세션 없는 모던 요청에 주는 400(Missing session ID)은 `serverEra: legacy` + `session.validate: true`로 흉내 낼 수 있다. 세션 에러 본문은 SDK 1.27.2와 바이트 단위로 같다(아래 "레거시 세션").
- 어느 응답도 모던 에러 코드(`-32020`~`-32022`)가 아니므로 듀얼 클라이언트는 `initialize`로 폴백한다.

## tools/call 주입

두 경로 공용이다. 결과를 돌려줬을 호출에만 적용한다. 프로토콜 에러(모던 경로의 헤더 불일치, 알 수 없는 툴, capability 누락 등)가 날 요청은 그 에러를 그대로 받고, 지연만 적용된다. 레거시 경로의 알 수 없는 툴은 `isError` 결과라서 주입 대상이다.

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `toolCall.mode` | 아래 표 | `normal` | 응답 형태 | 둘 다 |
| `toolCall.errorCode` | -32768~32767 | `-32603` | `json-rpc-error` 모드의 에러 코드 | 둘 다 |
| `toolCall.httpStatus` | 400~599 | `500` | `http-status` 모드의 HTTP 상태 | 둘 다 |
| `toolCall.delaySeconds` | 0~120 | `0.0` | 어떤 모드든 응답 전에 이만큼 기다린다 | 둘 다 |
| `toolCall.targetTools` | 툴 이름 목록(최대 50개) | `[]` | 주입과 지연을 적용할 툴. 비면 전체 | 둘 다 |
| `toolCall.sseNotificationCount` | 0~20 | `2` | `sse` 모드에서 최종 응답 앞에 보낼 알림 수 | 둘 다 |

| `toolCall.mode` | HTTP | 응답 |
|---|---|---|
| `normal` | 200 | 정상 실행 |
| `is-error` | 200 | `isError: true` 결과 |
| `json-rpc-error` | 200 | JSON-RPC 에러(`toolCall.errorCode`) |
| `http-status` | `toolCall.httpStatus` | 평문 |
| `sse` | 200 | `text/event-stream`: 알림 여러 건 뒤 최종 응답. 요청 `_meta`에 `progressToken`이 있으면 `notifications/progress`, 없으면 `notifications/message` |
| `id-mismatch` | 200 | 요청과 다른 `id`의 정상 응답 |
| `empty` | 200 | 빈 본문(`Content-Length: 0`) |
| `disconnect` | 없음 | 응답 없이 연결을 끊는다 |

`sse`와 `id-mismatch`는 정상 실행 결과를 감싼다. `confirm_action`의 `input_required` 결과도 결과이므로 주입 대상이다.

## 레거시 세션

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `session.validate` | bool | `false` | 켜면 `initialize`가 아닌 레거시 요청에 `Mcp-Session-Id`가 없을 때 400, 발급한 적 없거나 만료된 ID일 때 404, `DELETE`로 끝낸 ID일 때 404(본문이 다름). `DELETE /mcp`로 세션을 끝낼 수 있다(성공 200 빈 본문, 모르는 세션 404, 이미 끝낸 세션 404, 헤더 없음 400). GET 스트림도 같은 규칙이다. 본문은 아래 표 | 레거시 |
| `session.issueId` | bool | `true` | 끄면 `initialize` 응답에 `Mcp-Session-Id`가 없다. 이때는 `session.validate`도 동작하지 않는다 | 레거시 |

`session.validate`가 켜졌을 때의 세션 에러(POST, GET, DELETE 공통, `Content-Type: application/json`). 본문은 공식 Python SDK 1.27.2(`PORT=<포트> MCP_JSON=1 .venv/bin/python scripts/sdk1_legacy_server.py`, 2026-11-23 관찰)와 바이트 단위로 같다.

| 경우 | HTTP | 본문 |
|---|---|---|
| `Mcp-Session-Id` 헤더 없음 | 400 | `{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Missing session ID"}}` |
| 발급한 적 없거나 `expireSessions`로 잊은 ID | 404 | `{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Session not found"}}` |
| `DELETE /mcp`로 끝낸 ID | 404 | `{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Not Found: Session has been terminated"}}` |

- SDK와 다른 점: SDK는 세션 헤더가 없는 요청의 400에 새 `mcp-session-id` 응답 헤더를 붙이고, 모르는 세션 ID가 붙은 `initialize`도 404로 거부한다. 이 서버는 400에 세션 헤더를 붙이지 않고 `initialize`는 세션 헤더와 상관없이 처리한다.
- 기본값에서는 세션 ID를 발급만 하고 검사하지 않는다. `DELETE /mcp`는 405다. 세션 헤더가 없는 `GET /mcp`는 예전처럼 평문 400(`Session ID required in mcp-session-id header`)이다.
- `POST /scenario`에 `{"action": "expireSessions"}`를 보내면 발급된 세션과 `DELETE`로 끝낸 세션 기록을 모두 잊는다. 응답은 `{"ok": true, "action": "expireSessions", "expired": <개수>}`다. `session.validate`가 켜진 설정에서는 다음 요청이 404가 되고, `initialize`를 다시 하면 새 세션을 받는다. 활성 설정은 바뀌지 않는다.
- 모던 요청은 `Mcp-Session-Id`를 읽지도 발급하지도 않는다.
- 세션 저장소는 프로세스 전역이라 `cfg` URL이 달라도 공유한다.

## rawResponses

| 키 | 값 | 기본 | 효과 | 적용 era |
|---|---|---|---|---|
| `rawResponses` | 규칙 목록(최대 20개) | `[]` | JSON-RPC 메서드별로 개발자가 적은 응답을 그대로 돌려준다. 규칙 형식은 `tests/test_raw_responses.py`를 본다 | 둘 다 |

## `X-MCP-Test-Scenario` 헤더 전용 이름

README 표에 있는 기존 시나리오 이름에 더해 아래 이름을 쓸 수 있다. `/scenario` 화면의 시나리오 목록에는 나오지 않는다.

| 이름 | 설정 |
|---|---|
| `server-era-legacy`, `server-era-modern` | `serverEra` |
| `discover-<모드>` | `discover.mode`. 예: `discover-no-modern-version`, `discover-http-401` |
| `tool-call-<모드>` | `toolCall.mode`. 예: `tool-call-is-error`, `tool-call-sse` |
| `legacy-probe-<모드>` | `serverEra: legacy` + `legacyModernProbe.mode`. 예: `legacy-probe-http-400` |
| `modern-result-type-<값>` | `modernResponse.resultType`. `omit`, `input-required`, `unknown` |
| `modern-omit-server-info` | `modernResponse.omitServerInfo: true` |
| `modern-omit-cache-fields` | `modernResponse.omitCacheFields: true` |
| `modern-issue-session-id` | `modernResponse.issueSessionId: true` |
| `require-capability-<이름>` | `modern.requiredClientCapabilities`. `elicitation`, `sampling`, `roots` |
| `lenient-2026` | `strict2026: false` |
| `session-validate` | `session.validate: true` |
| `session-no-id` | `session.issueId: false` |
| `tools-list-paged` | `toolsList.pageSize: 2` |
| `initialize-version-<버전>` | `initialize.protocolVersion`. 예: `initialize-version-2026-07-28` |
| `cfg:<토큰>` | 토큰에 담긴 설정 전체 |

쉼표로 여러 개를 이을 수 있다. 예: `server-era-modern,discover-sse,tool-call-empty`. 모르는 이름은 무시한다.

## `/inspect` 기록

`/inspect/log`의 각 항목에 `injected` 배열이 있다. 그 응답을 만든 옵션 이름이 들어간다(예: `["toolCall.mode=sse", "toolCall.delaySeconds=2"]`, `["legacyModernProbe.mode=http-404"]`, `["strict2026=false"]`). 주입이 없으면 빈 배열이다. 화면에는 "주입 N" 배지와 응답 제목 옆 목록으로 나온다.

SSE 응답은 `responseBody.events`, 평문 응답은 `responseBody.text`, 끊은 연결은 `responseBody.disconnect: true`로 기록한다.
