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
- `echo_header_params`, `confirm_action` (2026-07-28 경로 전용 테스트 툴. 아래 "2026-07-28 지원 범위" 참고)

## Scenario Control

Open `/scenario` in a browser to set the server-wide default scenario. The page
also shows a live `/mcp` response preview for the selected scenario.

Use `X-MCP-Test-Scenario` to override the server-wide state for one request.
Omit it for the currently selected scenario.

The selected scenario is stored in process memory, so it resets to `ok` after a
server restart or redeploy.

```text
X-MCP-Test-Scenario: duplicate-tool-name
```

지원 시나리오:

### 기본

| 정책명 | 시나리오 | 효과 |
| --- | --- | --- |
| 정상 응답 | `ok` | 정상 Unit Expert 도구 목록을 반환합니다. |

### 서버 error

| 정책명 | 시나리오 | 효과 |
| --- | --- | --- |
| 인증 조건 - 401 | `auth-401` | JSON-RPC 처리 전에 `401 Unauthorized`를 반환합니다. |
| 인증 조건 - 403 | `auth-403` | JSON-RPC 처리 전에 `403 Forbidden`을 반환합니다. |
| MCP 버전 조건 - 최소 지원 버전 | `unsupported-min-version` | 최소 지원 버전보다 낮은 `protocolVersion: 2024-11-05`를 반환합니다. |
| 툴 목록 조건 - JSON-RPC 에러 | `tools-list-error` | `tools/list`에서 JSON-RPC error를 반환합니다. |
| 툴 목록 조건 - null 반환 | `tools-list-null` | `tools/list`에서 `tools: null`을 반환합니다. |
| 툴 목록 조건 - 빈 배열 반환 | `tools-list-empty` | `tools/list`에서 `tools: []`를 반환합니다. |
| 툴 개수 조건 - 최대 개수 | `too-many-tools` | 도구 21개를 반환합니다. |
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

## 2026-07-28 지원 범위

`/mcp`는 한 엔드포인트에서 레거시(2025-11-25 이하, `initialize` 핸드셰이크)와 모던(2026-07-28, 무상태) 요청을 함께 받습니다. 모던 경로의 기준은 공식 스펙이며, 조사 내용은 [docs/SPEC_2026-07-28_NOTES.md](docs/SPEC_2026-07-28_NOTES.md)에 있습니다.

모던 경로가 구현한 것:

- 메서드: `server/discover`, `tools/list`, `tools/call`, `subscriptions/listen`. 그 밖의 메서드(`initialize`, `ping` 포함)는 HTTP 404 + `-32601`입니다.
- 요청 검증: `MCP-Protocol-Version`·`Mcp-Method`·`Mcp-Name` 헤더, `_meta`의 protocolVersion·clientCapabilities, 헤더와 바디의 일치, `=?base64?...?=` 디코드, `Mcp-Param-*`.
- 에러 코드: `-32020`(헤더 불일치), `-32021`(capability 누락, `data.requiredCapabilities`), `-32022`(버전 미지원, `data.supported`·`data.requested`).
- 결과: 모든 결과에 `resultType`과 `_meta` serverInfo, `server/discover`와 `tools/list`에 `ttlMs`·`cacheScope`, `tools/list` 페이지네이션.
- 테스트 툴: `echo_header_params`(`x-mcp-header`가 달린 인자), `confirm_action`(`input_required` 왕복). 모던 `tools/list`에만 나옵니다.

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
/scenario 화면: "프로토콜 주입 설정" 카드의 "스펙 준수 모드"를 끕니다.
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
| `legacyModernProbe.*` | `serverEra: legacy`인 서버가 모던 요청에 주는 응답을 고릅니다. `-32601`, `-32602`, 400(Python SDK 1.x와 같은 본문), 404, 빈 400, 무응답 | `legacy-probe-http-400` |
| `toolCall.*` | `tools/call` 응답을 바꿉니다(두 경로 공용). `isError`, JSON-RPC 에러, HTTP 오류, SSE, id 불일치, 빈 바디, 연결 종료, 지연 | `tool-call-sse` |
| `modernResponse.*` | 정상인 모던 결과에 위반을 넣습니다. `resultType` 누락·변조, serverInfo 누락, 캐시 필드 누락, `Mcp-Session-Id` 발급 | `modern-result-type-omit` |
| `modern.*` | 테스트 툴 노출, 필수 클라이언트 capability(`-32021`), `-32602`의 HTTP 상태, 바디에 값이 없는 `Mcp-Param-*`의 처리 | `require-capability-elicitation` |
| `toolsList.*` | 기존 목록 오류에 더해 페이지네이션(`pageSize`), 특정 페이지 실패(`failAtPage`), 캐시 힌트 | `tools-list-paged` |
| `session.*` | 레거시 세션 검증(`validate`: 세션 없음 400, 모르는 세션 404, `DELETE` 허용), 세션 ID 미발급 | `session-validate` |
| `auth.*` | OAuth(`oauth`, `mixed-oauth`), 스코프 부족 403(`enforceScope`) | - |
| `allowedOrigins` | 목록에 없는 `Origin`을 403으로 거부합니다. 비어 있으면(기본) 검증하지 않습니다 | - |
| `rawResponses` | 메서드별로 적어 둔 응답을 그대로 돌려줍니다 | - |

`/inspect` 화면은 최근 `/mcp` 요청과 응답, 그 응답을 만든 주입 옵션을 보여줍니다.

## 테스트 클라이언트

`unit_expert_mcp.client`는 게이트웨이나 서버에 모던 또는 레거시 클라이언트로 붙는 CLI입니다. 표준 라이브러리만 씁니다.

```bash
PYTHONPATH=src python -m unit_expert_mcp.client <명령> --url http://localhost:8000/mcp
# 패키지를 설치했다면: unit-expert-client <명령> --url ...
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

테스트 툴은 `tools/list`에서 찾습니다. `echo_header_params`와 `confirm_action`이 있으면 그 툴을 쓰고, 없으면 `x-mcp-header`가 달린 툴과 이름·설명으로 찾은 툴을 씁니다. `--header-tool`, `--input-tool`, `--tool`로 지정할 수 있습니다.

```bash
# 로컬 서버의 세 세대를 차례로 검사
for era in both legacy modern; do
  PYTHONPATH=src python -m unit_expert_mcp.client run all \
    --url http://localhost:8000/mcp --cfg-json "{\"serverEra\":\"$era\"}"
done

# 규격을 어긴 요청 하나
PYTHONPATH=src python -m unit_expert_mcp.client call tools/list \
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
PYTHONPATH=src python -m unit_expert_mcp.client probe
```

| 조합 | 업스트림 등록 | 클라이언트 명령 (`PYTHONPATH=src python -m unit_expert_mcp.client` 뒤에) | 기대 결과 |
| --- | --- | --- | --- |
| modern client → modern server | modern URL | `run modern modern-violation compat-modern-client --expect gateway` | 스펙대로면 동작합니다. 게이트웨이 기대값은 미확정입니다. |
| modern client → legacy server | legacy URL | `run modern compat-modern-client --expect gateway` | 게이트웨이가 모던 요청을 레거시 업스트림 호출로 바꿔 주면 동작합니다. 바꿔 주는지는 미확정입니다. 서버에 직접 붙으면 모던 클라이언트는 실패합니다(DiscoverResult를 받지 못함). |
| legacy client → legacy server | legacy URL | `run legacy compat-legacy-client --expect gateway` | 동작합니다. `initialize`의 협상 버전은 `2025-06-18`, 세션 없는 요청은 400 + `-32600`, 모르는 세션은 404 + `-32600`입니다(게이트웨이 코드로 확인한 기대값). |
| legacy client → modern server (미지원 확인) | modern URL | `run legacy compat-legacy-client --expect gateway` | 실패해야 합니다. 서버에 직접 붙으면 `initialize`가 400 + `-32022`이고 에러에 지원 버전이 적힙니다. 게이트웨이를 거치면 실패가 어느 단계(`initialize`, `tools/list`, `tools/call`)에서 어떤 상태와 코드로 드러나는지 미확정입니다. `-v`로 실제 응답을 기록합니다. |

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
