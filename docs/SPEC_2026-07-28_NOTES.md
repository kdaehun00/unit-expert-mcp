# MCP 2026-07-28 스펙 요약 노트 (공식 문서 기준, 2026-11-23 조회)

## 요약

- 2026-07-28 스펙 페이지는 modelcontextprotocol.io에서 모두 열렸고, 아래 규범은 그 원문에서 직접 확인했다. 다만 `schema.ts`는 필드 목록 수준으로만 확인했고, SEP 개별 문서는 다루지 않았다.
- 2026의 핵심은 무상태다. `initialize`·세션·GET 스트림이 사라지고, 모든 POST가 헤더(`MCP-Protocol-Version`, `Mcp-Method`, 필요 시 `Mcp-Name`, `Mcp-Param-*`)와 바디 `_meta`(protocolVersion, clientCapabilities 필수)를 함께 싣는다.
- 헤더와 바디가 다르거나 필수 헤더가 없으면 서버는 400과 `-32020`으로 거부해야 한다.
- 조사 시점의 `server.py`는 뼈대는 맞지만 스펙과 다른 점이 14개 있었다(이후 수정됨). 영향이 큰 것은 다음 다섯 가지다.
  - `-32022`에 `data.supported/requested`가 없다.
  - `MCP-Protocol-Version` 헤더 누락을 통과시킨다.
  - `Mcp-Param-*`와 base64 센티널을 처리하지 않는다.
  - 미구현 메서드에 HTTP 200을 준다(스펙은 404).
  - 알 수 없는 툴을 `isError` 결과로 준다(스펙은 `-32602`).

표기: [공식] 공식 문서에서 확인 / [미확인] 확인 못 함.

## 본문

### 1. Streamable HTTP 요청 헤더

출처는 모두 [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)다.

**기본 헤더** [공식]

| 헤더 | 규칙 | 원문 |
|---|---|---|
| `Accept` | 클라이언트 MUST: `application/json`과 `text/event-stream` 둘 다 나열 | "MUST include an `Accept` header listing both" |
| `MCP-Protocol-Version` | 모든 POST에 MUST, `_meta`의 protocolVersion과 일치 MUST | "Every POST request to the MCP endpoint MUST include" |
| `Mcp-Method` | 모든 요청, 바디 `method`와 일치 | "These headers are REQUIRED for compliance." |
| `Mcp-Name` | `tools/call`, `resources/read`, `prompts/get`에만. 원본은 `params.name` 또는 `params.uri` | 같은 표 |

- 헤더 이름은 대소문자 무시 MUST, 값은 대소문자 구분이다.
- 알림(notification) POST의 헤더 요구는 이 리비전이 정의하지 않는다: "header requirements for notification POSTs are not defined by this revision".

**`Mcp-Param-*`** [공식]

- 정의: 서버가 툴 `inputSchema`의 속성 스키마에 `"x-mcp-header": "Region"`을 달면, 클라이언트가 그 인자 값을 `Mcp-Param-Region` 헤더로 복제한다.
- 서버는 MAY, 클라이언트는 MUST다: "clients MUST support this feature".
- `x-mcp-header` 값 제약(모두 MUST):
  - 비어 있지 않고, HTTP 토큰 문법(`1*tchar`)을 따르며, CR/LF 등 제어문자가 없다.
  - 한 `inputSchema` 안에서 대소문자 무시로 유일하다.
  - integer, string, boolean 타입에만 붙인다. `number`는 불가이고, 정수는 ±(2^53−1) 범위다.
  - 루트에서 `properties` 키만으로 도달하는 속성에만 붙인다. `items`, `oneOf/anyOf/allOf/not`, `if/then/else`, `$ref`를 거치면 무효다.
- 위반한 툴은 클라이언트가 `tools/list` 결과에서 제외 MUST, 경고 로그 SHOULD다. stdio 클라이언트는 무시해도 된다(MAY).
- 값 변환: string은 그대로, integer는 10진 문자열, boolean은 소문자 `true`/`false`.
- base64 센티널: 비ASCII, 제어문자, 앞뒤 공백이 있으면 UTF-8을 base64로 인코딩해 `=?base64?{값}?=` 형태로 보낸다(MUST).
  - 원문: "These markers are case-sensitive and MUST appear exactly as shown".
  - 평문이 센티널 패턴과 같아도 인코딩 MUST. 같은 규칙이 `Mcp-Name`에도 적용된다.
  - 예: `"Hello, 世界"` → `=?base64?SGVsbG8sIOS4lueVjA==?=`.
  - 예시에 `=` 패딩과 `/`가 나오므로 표준 base64 알파벳으로 보인다. 알파벳을 명시한 문장은 찾지 못했다(추정).
- 헤더 유무 규칙:

| 상황 | 클라이언트 | 서버 |
|---|---|---|
| 값 있음 | 헤더 포함 MUST | 바디와 일치 검증 MUST |
| 값이 `null` 또는 인자에 없음 | 헤더 생략 MUST | 헤더 기대 금지 MUST NOT |
| 바디에 값이 있는데 헤더 없음 | 비준수 | 거부 MUST |

- 불일치 응답: "MUST reject requests with a `400 Bad Request` HTTP status and JSON-RPC error code `-32020`".
- 검증 실패 조건은 세 가지다: 필수 표준 헤더 누락, 헤더와 바디 값 불일치(base64는 디코드 후 비교 MUST), 헤더 값에 잘못된 문자.
- 정수 비교는 수치로 하는 것이 SHOULD다(`42.0`과 `42`는 같음).
- 클라이언트가 `-32020`을 받으면 `tools/list`로 스키마를 다시 확인하고 재시도 SHOULD.
- 중계 서버는 모르는 `Mcp-Param-*`를 그대로 전달 MUST. 검증 실패 시 HTTP 오류는 MUST지만 JSON-RPC 바디는 필수가 아니다.

### 2. 요청 `_meta` 필수 필드

출처: [Base Protocol의 `_meta`](https://modelcontextprotocol.io/specification/2026-07-28/basic/index#_meta) [공식]

| 키 | 타입 | 필수 |
|---|---|---|
| `io.modelcontextprotocol/protocolVersion` | string | 예 |
| `io.modelcontextprotocol/clientCapabilities` | ClientCapabilities | 예 |
| `io.modelcontextprotocol/clientInfo` | Implementation | 아니오(클라이언트 SHOULD) |
| `io.modelcontextprotocol/logLevel` | LoggingLevel | 아니오(Logging 자체가 deprecated) |
| `progressToken` | - | 아니오 |

- 필수 필드 누락: "MUST reject it with JSON-RPC error code `-32602`", HTTP는 400 MUST.
- 선언하지 않은 capability가 필요하면 `-32021`과 `data.requiredCapabilities`, HTTP 400 MUST.
- 헤더 버전과 `_meta` 버전이 다르면 400과 `-32020`, 지원하지 않는 버전이면 400과 `-32022`.
- 여러 위반이 겹칠 때 어느 에러가 우선하는지는 찾지 못했다 [미확인].

### 3. 에러 코드

출처: [Error Codes](https://modelcontextprotocol.io/specification/2026-07-28/basic/index#error-codes), [Schema](https://modelcontextprotocol.io/specification/2026-07-28/schema) [공식]

| 코드 | 이름 | `error.data` | HTTP |
|---|---|---|---|
| `-32020` | HeaderMismatch | 규정 없음(예시는 message만) | 400 |
| `-32021` | MissingRequiredClientCapability | `{ requiredCapabilities: ClientCapabilities }` | 400 |
| `-32022` | UnsupportedProtocolVersion | `{ supported: string[], requested: string }` | 400 |
| `-32601` | Method not found | - | 404 |
| `-32602` | Invalid params | - | 필수 `_meta` 누락 시 400 |

- `-32602`는 필수 `_meta` 누락, 알 수 없는 툴, 리소스 없음, 잘못된 커서에 쓰인다. 알 수 없는 툴 등 나머지 경우의 HTTP 상태는 명시를 찾지 못했다 [미확인].
- 범위 정책: `-32000`~`-32019`는 레거시라 새 구현은 쓰지 않는 것이 SHOULD NOT이고, `-32020`~`-32099`는 스펙 전용이다.
- 원문: "MUST NOT emit any code from this sub-range that is not defined".
- 이 버전에서 내보내면 안 되는 코드(MUST NOT): `-32002`(리소스 없음, `-32602`로 대체), `-32042`(URL elicitation).
- 변경 이력에 따르면 초안의 `-32001/-32003/-32004`가 `-32020/-32021/-32022`로 재번호됐다. 옛 번호를 쓰는 구현은 틀렸다([Changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)).

### 4. `server/discover`

출처: [Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover), [Versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)

- 서버 구현은 MUST, 클라이언트 호출은 MAY다: "Servers MUST implement it." [공식]
- 요청: params는 표준 `_meta`뿐이다. [공식]
- 응답(`DiscoverResult`)의 필드: [공식]
  - `resultType`
  - `supportedVersions: string[]`
  - `capabilities: ServerCapabilities`
  - `instructions?`
  - `ttlMs`, `cacheScope` (캐시 가능 결과라 MUST)
- `serverInfo`는 결과 최상위 필드가 아니라 `_meta["io.modelcontextprotocol/serverInfo"]`에 넣는다(SHOULD). [공식]
- 인증 필요 여부: discover를 인증 없이 열어야 한다는 문장도, 예외로 둔다는 문장도 찾지 못했다. 인가 문서는 일반 규칙만 둔다("authorization MUST be included in every HTTP request"). [미확인]
- 레거시 서버가 받았을 때: 구현 정의 에러(흔히 `-32601`, `-32602`)를 주거나 무응답이다. 클라이언트 폴백은 특정 코드에 묶으면 안 된다: "fallback MUST NOT be keyed to one specific error code". [공식]
- 클라이언트 폴백 절차: [공식]
  - stdio: discover를 먼저 보낸다(SHOULD). `DiscoverResult`면 모던, 인식되는 모던 에러면 `supported`에서 골라 재시도(initialize로 가지 않음), 그 외 에러나 타임아웃이면 `initialize`로 폴백한다.
  - HTTP: 모던 요청을 먼저 보낸다. 400이면 바디를 본다(SHOULD). 인식되는 모던 에러면 고쳐서 재시도하고, 바디가 비었거나 모던 에러가 아니면 `initialize`로 폴백한다.
  - 판정 결과는 origin 단위로 캐시 SHOULD.

### 5. 세션, initialize, GET/DELETE

출처: [Changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog), [Streamable HTTP의 Backward Compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#backward-compatibility) [공식]

- 프로토콜 세션과 `Mcp-Session-Id`, `initialize`/`notifications/initialized`가 제거됐다.
- 호출 간 상태가 필요하면 서버가 발급한 핸들을 일반 툴 인자로 넘긴다.
- `tools/list`는 연결별로 달라지면 안 된다(MUST NOT). 요청에 실린 인가에 따라 달라지는 것은 허용된다(MAY).
- 이 리비전만 지원하는 서버의 동작(SHOULD):
  - MCP 엔드포인트로 온 GET/DELETE에는 "respond with `405 Method Not Allowed`".
  - `Mcp-Session-Id`가 와도 무시하고, 세션 ID를 발급하거나 되돌려주지 않는다.
  - `Last-Event-ID`는 무시한다. 스트림 재개가 없다.
- `Origin` 검증은 MUST이고, 무효하면 403이다.

### 6. `resultType`과 input_required 흐름

출처: [Base Protocol의 ResultType](https://modelcontextprotocol.io/specification/2026-07-28/basic/index), [MRTR](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr) [공식]

- 값은 `"complete"`와 `"input_required"` 두 개다. 확장이 값을 추가할 수 있다(MAY).
- 서버는 모든 결과에 포함 MUST다: "The `result` MUST include a `resultType` field".
- 클라이언트는 모르는 값을 무효로 처리 MUST, 필드가 없으면 `"complete"`로 간주 MUST다.
- 서버 응답(`InputRequiredResult`) 필드:
  - `resultType: "input_required"`
  - `inputRequests?`: 서버가 붙인 키 → `{method, params}` 맵. method는 `elicitation/create`, `sampling/createMessage`, `roots/list` 중 하나.
  - `requestState?`: 서버만 해석하는 불투명 문자열.
  - 둘 중 하나 이상은 MUST.
- 허용 메서드는 `tools/call`, `resources/read`, `prompts/get`뿐이다. 다른 요청에는 MUST NOT.
- 클라이언트가 선언하지 않은 capability의 inputRequests는 보내면 안 된다(MUST NOT).
- 클라이언트 재요청 규칙:
  - 원래 params에 `inputResponses`(같은 키 → 결과)를 더한다.
  - `requestState`는 받은 값 그대로 되돌린다(MUST). 받지 않았으면 넣지 않는다(MUST NOT).
  - JSON-RPC `id`는 달라야 한다(MUST).
  - 재시도 결과는 캐시하면 안 된다(MUST NOT).
- 서버는 `requestState`를 공격자 입력으로 취급하고, 권한이나 로직에 영향을 주면 무결성 보호 MUST다.
- 응답이 불완전하면 에러 대신 새 `InputRequiredResult`를 주는 것이 SHOULD다.
- 서버가 결과 `_meta`에 넣는 것: `io.modelcontextprotocol/serverInfo`(SHOULD). listen 스트림의 메시지에는 `io.modelcontextprotocol/subscriptionId`(MUST).

### 7. tools/list

출처: [Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), [Caching](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching), [Pagination](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination) [공식]

- 캐시 필드:
  - `ttlMs`: 0 이상 정수(밀리초). "Servers MUST provide a `ttlMs` value that is `>= 0`". 0이면 즉시 stale이다.
  - `cacheScope`: `"public"` 또는 `"private"`.
  - 두 필드는 `resultType: "complete"`인 결과에 MUST다. 대상은 `server/discover`, `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list`, `resources/read`.
  - 페이지마다 `ttlMs`는 달라도 되지만 `cacheScope`는 같아야 한다(MUST).
- 페이지네이션:
  - 요청 `params.cursor`, 응답 `nextCursor`.
  - 커서는 불투명한 문자열이다. 클라이언트는 형식을 가정하거나 해석·수정하면 안 된다(MUST).
  - 클라이언트는 `nextCursor`가 null이 아닌 값으로 왔는지만 본다. 빈 문자열도 유효한 커서라서, 빈 문자열 `nextCursor`를 받았다고 결과의 끝으로 보면 안 된다(MUST NOT). 원문: "an empty string is a valid cursor and thus **MUST NOT** be treated as the end of results". 이 문장은 "Clients MUST treat cursors as opaque tokens" 항목 아래에 있는 클라이언트 규칙이다.
  - 서버가 어떤 커서를 받아들일지는 서버가 정한다. 잘못된 커서는 `-32602`(SHOULD). 서버가 발급한 적 없는 값은 빈 문자열이어도 잘못된 커서다. 서버가 빈 문자열을 받아들여야 한다는 문장은 없다(2026-11-23 Pagination 페이지 재확인).
- 툴 순서는 결정적이어야 한다(SHOULD).
- 툴 정의에서 바뀐 점:
  - `inputSchema` 속성에 `x-mcp-header`를 달 수 있다.
  - `inputSchema`/`outputSchema`는 JSON Schema 2020-12의 모든 키워드를 허용한다.
  - `structuredContent`는 임의의 JSON 값이 가능하다.
  - 스키마 요약 기준으로 `ToolExecution` 타입이 없다(tasks는 확장으로 이동).
- `Tool`의 필드별 정의는 Tools 페이지 기준으로만 확인했다. 스키마 페이지의 해당 정의와는 대조하지 않았다.

### 8. tools/call

출처: [Tools의 Error Handling](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#error-handling), [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) [공식]

- 응답 형식: 요청마다 서버가 `application/json`(단일 객체) 또는 `text/event-stream` 중에서 고른다(MUST 둘 중 하나). 클라이언트는 둘 다 지원 MUST.
- SSE일 때:
  - 그 요청과 관련된 알림만 최종 응답 전에 보낼 수 있다.
  - 서버가 독립 요청을 보내면 안 된다(MUST NOT).
  - 최종 응답이 스트림을 끝내는 것이 SHOULD.
  - `X-Accel-Buffering: no` 헤더 SHOULD.
  - 스트림 재개는 없다.
- 클라이언트가 SSE 스트림을 닫으면 취소다. HTTP에서는 `notifications/cancelled`를 쓰지 않는다.
- `isError: true`는 툴 실행 오류다(API 실패, 입력값 검증, 비즈니스 로직). 모델이 고쳐서 재시도할 수 있는 피드백이다.
- 알 수 없는 툴은 프로토콜 에러다: 예시가 `"code": -32602, "message": "Unknown tool: invalid_tool_name"`.

### 9. 신규·삭제·폐기 메서드

출처: [Changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog), [Subscriptions](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions), [Deprecated](https://modelcontextprotocol.io/specification/2026-07-28/deprecated) [공식]

- 신규:
  - `server/discover`
  - `subscriptions/listen`: params는 `notifications: { toolsListChanged?, promptsListChanged?, resourcesListChanged?, resourceSubscriptions?: string[] }`.
  - `notifications/subscriptions/acknowledged`: 스트림 첫 메시지 MUST. 서버가 수락한 부분집합을 담는다.
- `subscriptions/listen` 동작:
  - 스트림의 모든 메시지 `_meta`에 `io.modelcontextprotocol/subscriptionId`를 넣는다. 값은 listen 요청의 JSON-RPC id다.
  - 서버가 스스로 끝낼 때는 `{resultType:"complete", _meta:{subscriptionId}}` 응답을 보내고 닫는다(SHOULD).
- 삭제:
  - `initialize`, `notifications/initialized`
  - `ping`, `logging/setLevel`, `notifications/roots/list_changed`
  - `resources/subscribe`, `resources/unsubscribe`, HTTP GET 스트림
  - `notifications/elicitation/complete`와 `elicitationId`
  - 서버가 직접 보내는 요청(`roots/list`, `sampling/createMessage`, `elicitation/create`). 이제 `inputRequests` 안에서만 쓴다.
  - SSE 재개(`Last-Event-ID`)
- 확장으로 이동: tasks가 `io.modelcontextprotocol/tasks` 확장이 됐다. `tasks/result`와 `tasks/list`는 없어지고 `tasks/get`, `tasks/update`를 쓴다.
- 폐기(Deprecated, 동작은 유지): Roots, Sampling, Logging, Dynamic Client Registration, HTTP+SSE 전송, `includeContext`의 `thisServer`/`allServers`.
  - 앞의 네 개는 2027-07-28 이후 첫 리비전부터 제거될 수 있다.
- 스키마 요약 기준 `ClientRequest`는 10개다: discover, complete, prompts/get, prompts/list, resources/list, resources/templates/list, resources/read, subscriptions/listen, tools/call, tools/list.
- 클라이언트가 보내는 알림은 `notifications/cancelled` 하나뿐이고 stdio에서만 쓴다.

### 10. 하위 호환

출처: [Versioning의 Compatibility Matrix](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning#compatibility-matrix) [공식]

| 클라이언트 | 서버 | 결과 |
|---|---|---|
| 모던 | 레거시 | 실패. 서버가 구현 정의 에러를 주거나, 무응답이거나, 레거시 의미로 처리할 수도 있다 |
| 듀얼 | 레거시 | 동작. HTTP에서는 모던 에러 바디가 없는 4xx를 받고 `initialize`로 폴백한다 |
| 레거시 | 모던 | 실패. HTTP에서는 필수 헤더 누락으로 400. "Legacy clients have no fall-forward mechanism." |
| 레거시 | 듀얼 | 동작. `initialize`가 레거시 의미를 선택한다 |

- 듀얼 서버의 분기: 모던 `_meta`를 실은 요청은 무상태로 처리하고, `initialize` 요청은 세션 범위의 레거시로 처리한다. 같은 엔드포인트에서 동시에 서빙해도 된다(MAY).
- 모던 전용 서버는 `initialize`에 주는 에러에 지원 버전을 적는 것이 SHOULD다. 에러 코드는 구현 정의다.
- `MCP-Protocol-Version` 헤더가 없는 요청:
  - 2025-06-18 이전 클라이언트를 지원하는 서버는 `2025-03-26`으로 간주해도 된다(MAY).
  - 지원하지 않는 서버는 거부 MUST.

### 11. 인가: 서버가 401/403에 넣어야 하는 것

출처: [Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization), [Authorization Server Discovery](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery) [공식]

- 401:
  - `WWW-Authenticate: Bearer resource_metadata="..."`. 이 헤더와 well-known URI 중 하나는 제공 MUST.
  - `scope="..."`는 SHOULD. 원문: "SHOULD include a `scope` parameter in the `WWW-Authenticate` header".
  - 무효하거나 만료된 토큰은 401 MUST.
- 403(스코프 부족, SHOULD): `WWW-Authenticate: Bearer error="insufficient_scope", scope="...", resource_metadata="..."`. `error_description`은 선택이다.
  - 필요한 스코프는 한 번의 챌린지에 모두 담는다(SHOULD).
- Protected Resource Metadata(RFC 9728) 구현은 MUST이고, `authorization_servers`가 1개 이상이어야 한다.
- 토큰 audience 검증 MUST.
- `offline_access`는 챌린지 scope나 `scopes_supported`에 넣지 않는다(SHOULD NOT).
- 2026 변경 중 서버 응답 관련: 변경 이력의 인가 항목은 `iss` 검증, DCR의 `application_type`, 자격증명의 issuer 바인딩, DCR 폐기로, 모두 AS·클라이언트 쪽이다.
- 401/403 헤더 규칙이 2025-11-25 대비 달라졌는지는 이전 버전과 대조하지 않았다 [미확인].

### 조사 시점의 `server.py` 구현이 스펙과 다른 점

> 2026-11-23 조사 시점의 상태이며 이후 수정됨.

대상: `src/unit_expert_mcp/server.py`. 괄호 안 숫자는 조사 시점의 줄 번호다.

1. **`-32022`에 `data`가 없다** (1518-1524, 1649-1655). 스펙은 `data: {supported: [...], requested: "..."}`를 요구한다. 지금은 message 문자열에만 들어 있다.
2. **`MCP-Protocol-Version` 헤더 누락을 통과시킨다** (1511: `if header_version and ...`). 스펙은 필수 헤더 누락을 `-32020`과 400으로 본다.
3. **모던 경로의 지원 버전 목록이 틀렸다** (26, 1518). `SUPPORTED_PROTOCOL_VERSIONS`를 그대로 써서 `_meta` 버전이 `2025-11-25`인 요청도 모던 경로에서 통과한다.
4. **`Mcp-Name`을 base64 디코드 없이 비교한다** (1551-1561). `=?base64?...?=` 값은 디코드 후 비교 MUST다.
5. **`Mcp-Param-*` 검증이 없다.** 지금 툴에는 `x-mcp-header`가 없어 실제 위반은 아니다. 클라이언트 테스트용으로는 주석 달린 툴과 검증이 필요하다.
6. **미구현 메서드에 HTTP 200을 준다** (1766). 스펙은 404와 `-32601` MUST다.
7. **모던 경로가 `initialize`(1722-1730)와 `ping`(1743-1744)에 성공 응답을 준다.** 둘 다 2026에서 삭제된 메서드라 404와 `-32601`이 맞다.
8. **알 수 없는 툴을 `isError: true` 결과로 준다** (1797). 스펙은 프로토콜 에러 `-32602`다. 이름 없는 요청(1771)도 같은 문제다.
9. **`server/discover` 결과 형태가 다르다** (1736-1741). `ttlMs`와 `cacheScope`가 없고(MUST), `serverInfo`가 결과 최상위에 있다.
   - `_meta`의 serverInfo 이름은 1574에서 `SERVER_NAME` 고정이라 `config["mcp"]["identifier"]`와 어긋난다.
10. **모던 요청의 Accept를 `application/json`만으로도 통과시킨다** (5067). 클라이언트는 둘 다 보내야 MUST다. 서버가 거부해야 한다는 문장은 없으니 위반이라기보다 검증 누락이다.
11. **모던 전용 모드에서도 GET이 405가 아니다** (4948). `Mcp-Method` 헤더가 있을 때만 405이고, 없으면 레거시 스트림 처리로 떨어진다. 스펙은 모던 전용 서버에 405 SHOULD다.
12. **`Origin` 검증이 없다** (5239-5241). 어떤 Origin이든 그대로 반사한다. 스펙은 검증 MUST, 무효 시 403이다.
13. **401 챌린지에 `scope`가 없다** (5369, SHOULD). 시나리오용 401/403(5299-5304)은 `WWW-Authenticate` 없이 평문이다. 의도된 오류 주입이면 무방하다.
14. **`PLAN_2026-07-28.md` 9번이 스펙과 다르다.** "ttlMs/cacheScope는 확장 성격, baseline엔 불필요"라고 적혀 있지만 `tools/list`에는 MUST다. 코드(1752-1753)는 이미 넣고 있다.

스펙과 맞는 부분도 확인했다: `_meta` 누락 시 `-32602`와 400, 메서드·이름 불일치 시 `-32020`과 400, `resultType`과 `_meta` serverInfo 부착, listen의 ack 선행과 종료 응답, DELETE 405.

### 테스트 서버 검증 체크리스트

검증 순서는 스펙이 정하지 않았다. 아래 순서는 제안이다.

1. POST만 허용한다. GET/DELETE는 405, `Mcp-Session-Id`와 `Last-Event-ID`는 무시하고 세션 ID를 발급하지 않는다.
2. `Origin`이 있고 허용 목록 밖이면 403.
3. 바디가 단일 JSON-RPC 요청인지 확인한다. `id`는 문자열 또는 정수이고 null은 불가다.
4. `MCP-Protocol-Version`과 `Mcp-Method` 헤더가 있는지 확인한다. 없으면 400과 `-32020`.
5. `tools/call`, `resources/read`, `prompts/get`이면 `Mcp-Name`이 있는지 확인한다. 없으면 400과 `-32020`.
6. `_meta`에 protocolVersion과 clientCapabilities가 있는지 확인한다. 없으면 400과 `-32602`.
7. 헤더 버전과 `_meta` 버전이 같은지 확인한다. 다르면 400과 `-32020`.
8. 버전이 `2026-07-28`인지 확인한다. 아니면 400과 `-32022`, `data: {supported: ["2026-07-28"], requested}`.
9. `Mcp-Method`가 바디 `method`와 같은지 확인한다(대소문자 구분). 다르면 400과 `-32020`.
10. `Mcp-Name`은 센티널이면 base64 디코드 후 `params.name`/`params.uri`와 비교한다. 다르면 400과 `-32020`.
11. 툴 스키마의 `x-mcp-header` 속성마다 다음을 확인한다. 어긋나면 모두 400과 `-32020`.
    - 인자 값이 있으면 `Mcp-Param-{Name}`이 있어야 하고, 디코드 후 일치해야 한다. 정수는 수치로 비교한다.
    - 인자가 null이거나 없으면 헤더를 기대하지 않는다.
    - 헤더 값에 잘못된 문자가 있으면 거부한다.
12. 모르는 메서드(`initialize`, `ping` 포함)는 404와 `-32601`.
13. 알 수 없는 툴은 `-32602`, 실행 오류는 `isError: true`.
14. 모든 결과에 `resultType`을 넣고, `_meta`에 serverInfo를 넣는다. discover와 list 결과에는 `ttlMs`(0 이상)와 `cacheScope`를 넣는다.
15. 필요한 클라이언트 capability가 없으면 400과 `-32021`, `data.requiredCapabilities`.
16. 인증: 토큰이 없거나 무효면 401과 `WWW-Authenticate: Bearer resource_metadata=..., scope=...`. 스코프 부족이면 403과 `error="insufficient_scope"`.

### 정상 요청 예시

호스트, 클라이언트 이름, 툴 인자는 예시 값이다. 스펙 예시 형태를 따랐고, 툴은 현재 서버의 `convert_length`를 썼다. 이 툴에는 `x-mcp-header`가 없으므로 `Mcp-Param-*`는 붙지 않는다.

**discover**
```http
POST /mcp HTTP/1.1
Host: localhost:8000
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2026-07-28
Mcp-Method: server/discover

{"jsonrpc":"2.0","id":"discover-1","method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"TestClient","version":"1.0.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}
```

**tools/list**
```http
POST /mcp HTTP/1.1
Host: localhost:8000
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/list

{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"TestClient","version":"1.0.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}
```

**tools/call**
```http
POST /mcp HTTP/1.1
Host: localhost:8000
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: convert_length

{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"convert_length","arguments":{"value":1,"from_unit":"km","to_unit":"m"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"TestClient","version":"1.0.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}
```

- 인증이 켜져 있으면 세 요청 모두 `Authorization: Bearer <token>`을 추가한다.
- `x-mcp-header: "Region"`이 달린 인자가 있으면 `Mcp-Param-Region: us-west1`을 추가한다. 비ASCII 값은 `=?base64?...?=`로 보낸다.

## 출처

공식 문서(모두 2026-07-28 버전 경로, 2026-11-23 조회):
- [Changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [Transports Overview](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)
- [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)
- [Versioning and Compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning) (`basic/lifecycle` 경로로 열어도 이 내용이 나온다)
- [Base Protocol Overview](https://modelcontextprotocol.io/specification/2026-07-28/basic/index)
- [Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- [Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [Multi Round-Trip Requests](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr)
- [Subscriptions](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions)
- [Caching](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching)
- [Pagination](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination)
- [Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [Authorization Server Discovery](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery)
- [Deprecated Features](https://modelcontextprotocol.io/specification/2026-07-28/deprecated)
- [Schema Reference](https://modelcontextprotocol.io/specification/2026-07-28/schema) (`ListToolsResult`, `Tool`, `ToolAnnotations` 정의는 대조하지 않음)
- [schema.ts](https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2026-07-28/schema.ts) (필드 목록 수준으로만 확인)

이 노트가 다루지 않은 것: SEP 개별 문서(2243, 2322, 2549, 2567, 2575 등), GitHub `docs/specification/2026-07-28` 원본, Cancellation·Progress·Resources·Prompts·Client Registration·Security Considerations 페이지.

대조한 레포 파일: `src/unit_expert_mcp/server.py`, `PLAN_2026-07-28.md`