# Local Claude–ChatGPT bridge

This project translates the Claude Messages protocol to the OpenAI/Codex subscription Responses backend using Python `requests`. It keeps OpenAI as the provider and automatically refreshes supported Codex file credentials when needed. It does not install software or update an existing installation.

## Folder organization

- Root Python modules: live gateway, protocol conversion, and credential refresh. Keep these paths stable because the local service launches `gateway.py` here.
- `claude` / `claude-astra`: executable launchers.
- `tests/`: offline regression tests using temporary fake credentials and local mock services.
- `scripts/test.sh`: one-command test runner; respects `BRIDGE_PYTHON` and avoids bytecode caches.
- `requirements.txt`: gateway dependency; `.gitignore`: excludes local credentials, logs, environments and caches.

Keep real credentials in the external paths below, never in this repository. Logs and runtime locks belong in the configured state/log directories outside the source folder. Do not add backup copies, virtual environments or generated files here. Folder reorganization does not restart or change a running gateway.

## Run this snapshot

Use an existing Python environment containing the dependency in `requirements.txt`. The launcher uses `/usr/bin/env python3`; set `BRIDGE_PYTHON` to an existing interpreter containing `requests` for the gateway subprocess. No installation or system-service changes are performed automatically.

Run `./claude` (or `./claude-astra`). It checks authenticated `/health`, starts the sibling gateway only if needed, waits for readiness, and refuses to reuse a different service or a bridge with a different token. A file lock serializes launcher startup. Startup logs go to the state directory. Only loopback is bound. Running this snapshot does not update an already installed wrapper or restart an installed gateway. Choose a separate `BRIDGE_PORT` when testing alongside an installed service.

| Environment variable | Default / purpose |
| --- | --- |
| `BRIDGE_CONFIG` | `~/.config/claude-chatgpt/gateway.json`; existing JSON with nonempty `local_token`, optional `default_model`, `model_aliases`, and account-scoped `available_models` |
| `BRIDGE_CODEX_AUTH` | `~/.codex/auth.json`; existing `tokens.access_token` and `tokens.account_id`; automatic refresh also requires `tokens.refresh_token`. Rotated credentials are saved here. File-backed ChatGPT auth only; not keyring/external/API-key auth. |
| `BRIDGE_PORT` | `18734`; loopback gateway port |
| `BRIDGE_CLAUDE_BIN` | `~/.local/lib/claude-chatgpt/native/node_modules/.bin/claude`; existing native executable |
| `BRIDGE_PYTHON` | Gateway interpreter override; otherwise uses existing `~/.local/lib/claude-chatgpt/venv/bin/python3` when present, then the launcher interpreter. Must contain `requests`. |
| `BRIDGE_GATEWAY` | Sibling `gateway.py` |
| `BRIDGE_STATE_DIR` | `~/.cache/claude-chatgpt-bridge`; launcher lock and gateway log |
| `BRIDGE_UPSTREAM_URL` | `https://chatgpt.com/backend-api/codex/responses`; override only for a trusted endpoint (requests carry existing credentials) |

No credential values belong in source, logs, bug reports, or examples. Native management commands retain the original passthrough behavior and are not invoked by tests.

## Automatic Codex token refresh

- Refresh is lazy, before a model request: a parseable access-token JWT `exp` within five minutes triggers refresh. JWT metadata is unverified and used only for scheduling, never as authentication proof. If expiry is unknown, a timezone-aware Codex `last_refresh` older than eight days triggers scheduled refresh. Missing or malformed timing data is not guessed from filesystem timestamps.
- On an upstream 401, the bridge closes that response and reloads credentials first. If another process already changed credentials, it uses them; otherwise it performs one refresh. Each model request has at most one refresh call and one upstream retry, before streaming starts. A proactive refresh consumes that refresh allowance. Other HTTP errors do not trigger refresh. Account switches abort the in-flight request rather than sending it to another account.
- The refresh request is JSON to `https://auth.openai.com/oauth/token`, with `grant_type: refresh_token`, `client_id: app_EMoamEEZ73f0CkXaXp7hrann`, the existing `refresh_token`, and `refresh_reason` (`proactive_expiry`, `scheduled`, or `unauthorized_recovery`). No client secret, scope, resource, or invented grant fields are added. The token endpoint cannot be overridden through environment variables.
- Optional returned `access_token`, `refresh_token`, and `id_token` fields replace only those supplied; omitted fields and `account_id` are preserved. Successful refresh updates `last_refresh`. Unknown top-level and token fields survive. Detectable token/account mismatch fails closed.
- A persistent `auth.json.bridge-refresh.lock` sidecar uses a cross-process file lock with a 45-second acquisition limit. Refresh transport has 5-second connect and 15-second read timeouts, a 25-second checked body deadline, a 64-KiB response limit, and no redirects or automatic HTTP retries. Socket/DNS behavior may add transport overhead; this is not a hard real-time guarantee.
- Writes use a same-directory temporary file, mode `0600`, file/directory fsync, and atomic replacement. Credentials and file identity/content are rechecked before replacement; external rotation is reused instead of overwritten, and unrelated external metadata changes are merged. Independent Codex processes do not honor the bridge lock: the final check/replace race cannot be eliminated with portable file operations. Avoid simultaneous login/logout or independent refresh clients when possible. A save failure after server-side rotation may require signing in again.
- Refresh responses and credential values are never logged. Revoked, expired, reused, HTTP-401, and HTTP-400 `invalid_grant` failures produce sanitized instructions to run `codex logout` then `codex login`. Permanent failures are cached by credential digest within each bridge process until credentials change (or the process restarts); transient service/network failures return a retryable 503. Credentials are not erased on failure.

### Authoritative protocol sources

Implementation checked against the public [OpenAI Codex repository](https://github.com/openai/codex) at commit [`89461ea6833832b0551ce24ee32205824d5c108d`](https://github.com/openai/codex/commit/89461ea6833832b0551ce24ee32205824d5c108d):

- [Refresh endpoint, optional rotation persistence, JSON encoding, error classification, and client ID](https://github.com/openai/codex/blob/89461ea6833832b0551ce24ee32205824d5c108d/codex-rs/login/src/auth/manager.rs#L1600-L1733).
- [Exact refresh grant fields and diagnostic reason strings](https://github.com/openai/codex/blob/89461ea6833832b0551ce24ee32205824d5c108d/codex-rs/login/src/oauth/client.rs#L38-L60), [grant construction and JSON transport](https://github.com/openai/codex/blob/89461ea6833832b0551ce24ee32205824d5c108d/codex-rs/login/src/oauth/client.rs#L97-L144).
- [Five-minute and eight-day constants](https://github.com/openai/codex/blob/89461ea6833832b0551ce24ee32205824d5c108d/codex-rs/login/src/auth/manager.rs#L204-L213), [proactive scheduling](https://github.com/openai/codex/blob/89461ea6833832b0551ce24ee32205824d5c108d/codex-rs/login/src/auth/manager.rs#L3030-L3048), and [reload-before-refresh recovery](https://github.com/openai/codex/blob/89461ea6833832b0551ce24ee32205824d5c108d/codex-rs/login/src/auth/manager.rs#L2848-L2893).

The bridge adds bounded timeouts, cross-process coordination and guarded atomic writes; those safety measures are local implementation choices, not claims about upstream Codex guarantees.

## Translation and limitations

- The built-in model allowlist remains the compatibility ceiling. When `available_models` is configured, `/models` and request validation use that account-scoped subset; refresh it from the signed-in Codex account catalog when account access changes. Alias names are not assumed to be provider model names, and `[1m]` suffix normalization is preserved.
- Text and image tool results become Responses `function_call_output.output` content blocks (`input_text` / `input_image`), not JSON strings containing image descriptions. Base64 and URL images are supported. Other tool-result block types fail explicitly.
- Tool calls keep call IDs and stream argument deltas once. An output-item done event without arguments defers its block stop until final arguments arrive; item-done argument payloads also work.
- Positive integer `max_tokens` maps to standard Responses `max_output_tokens`. Acceptance and enforcement depend on the subscription endpoint. The bridge does not claim a local tokenizer or an exact local output limit. Backend rejection remains an explicit sanitized HTTP error rather than silently dropping the cap. Backend `incomplete` with reason `max_output_tokens` becomes `stop_reason: max_tokens` for text; a token-truncated tool call fails explicitly instead of returning executable partial arguments.
- Nonempty `stop_sequences` returns an explanatory 400 before an upstream request. Empty list or omission is accepted. No local stop matching is claimed.
- `tool_choice` supports `auto`, `any` → `required`, `none`, and `tool` → named function. `disable_parallel_tool_use` maps to `parallel_tool_calls`. Named choices must reference supplied tools. Backend-specific unsupported choices remain explicit errors.
- Reasoning effort and speed remain independent. Fast maps to priority service tier; echoed tier is not proof of billing or scheduling priority.
- Upstream HTTP 4xx/5xx status codes and message details (including top-level `detail` errors) are preserved after removal of known credential values and common token patterns. Non-JSON error bodies are withheld. Stream errors after headers use SSE error events, not a second HTTP response. Unrecognized local exceptions expose only class names.
- The count-tokens endpoint remains a rough character-based estimate, not an official tokenizer. This bridge is not a complete implementation of every Messages API feature. No model billing, output caps, or live backend compatibility can be established by fake-upstream tests alone.

## Offline tests

From this directory, run `./scripts/test.sh`, or use an existing interpreter with `requests`:

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/python -B -m unittest discover -s tests -v
/bin/sh -n ./claude-astra
```

Tests use temporary dummy configuration/auth files, loopback HTTP fake upstreams, and mocked launcher subprocesses. No real account files, external services, model quota, installed-runtime changes, or authentication writes are needed. Tests cover streaming text/tools/final fallback/failures, mode validation, CLI-shaped `max_tokens` mapping, tool choices, multimodal tool results, JSON and SSE HTTP responses, upstream statuses and redaction, authenticated identity checks, startup reuse/refusal/wait/timeout/exit paths. System Python may lack `requests`; no dependency is installed by the test runner.

## Validation of this revision

The refresh revision is validated with 42 offline tests, using temporary fake auth files and local mock HTTP endpoints only. Coverage includes exact refresh request fields, token rotation and optional fields, thread/process refresh coordination, expiry scheduling, 401 reload/refresh/retry limits, JSON/SSE gateway integration, external write preservation, account mismatch rejection, atomic-write permissions, lock/transport/response bounds, revoked/reused/expired errors, failure-cache reset, sanitized errors, and all existing translation/streaming/launcher regressions. No real `~/.codex/auth.json` contents, real refresh requests, or real credential mutations are used by these tests. Shell syntax checking also passes. Offline tests establish implementation behavior, not live account validity, model availability, billing, or backend compatibility.
