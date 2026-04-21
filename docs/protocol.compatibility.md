# Protocol compatibility classification

This document defines how to classify behavior changes against
`docs/plans/protocol.md`.

## Categories

### Protocol-preserving
A change is protocol-preserving when externally visible behavior is unchanged.

Examples:
- Internal service/repository refactors with unchanged endpoint behavior.
- Logging/diagnostic changes only.
- Performance improvements that preserve request/response shape and meaning.

### Protocol-extending
A change is protocol-extending when it adds behavior that old clients can ignore
without breaking.

Examples:
- Adding an optional response field.
- Adding a new optional query parameter with default behavior unchanged.
- Adding a new message type while keeping existing message semantics intact.

### Protocol-breaking
A change is protocol-breaking when a previously compliant client can break.

Examples:
- Renaming/removing required request fields.
- Changing status or delivery_state meaning.
- Removing support for an existing command semantic (for example `.fn.unpair`).
- Altering auth requirements or required headers on existing endpoints.

## Review checklist before merging protocol-adjacent changes

1. Which endpoint/message contract is affected?
2. Do protocol contract tests still pass unchanged?
3. If tests changed, is this preserving, extending, or breaking?
4. Are docs and canonical examples updated in the same PR?
5. Is the change explicitly called out in PR description?

## Guardrails

- `docs/plans/protocol.md` remains the source of truth.
- Contract tests operationalize the protocol; they do not replace it.
- Avoid encoding accidental implementation quirks as mandatory protocol rules.

## Concrete classification (as of refactor-8)

Use this table when judging specific edits. "Breaking" means a pre-existing
release build of the desktop or Android client could start misbehaving.

| Area | Edit | Category |
|---|---|---|
| `/api/transfers/sent-status` — `status` value | Rename `"delivered"` → anything else | breaking |
| `/api/transfers/sent-status` — `status` value | Add a fourth status (e.g. `"archived"`) | extending — old clients must ignore unknowns |
| `/api/transfers/sent-status` — `delivery_state` | Rename `"in_progress"` or `"delivered"` | breaking |
| `/api/transfers/sent-status` row | Add new optional field | extending |
| `/api/transfers/sent-status` row | Remove existing field (e.g. `chunk_count`) | breaking |
| `/api/transfers/init` request | Add required field | breaking |
| `/api/transfers/init` request | Add optional field with a default | extending |
| `/api/transfers/init` response | Return 413 (quota, terminal) | extending — existing clients should treat 4xx as failed; new clients render "exceeds server quota" |
| `/api/transfers/init` response | Return 507 (queue full, transient) | extending — clients aware of 507 enter WAITING; unaware clients treat as generic failure |
| `DELETE /api/transfers/{id}` | New sender-cancel endpoint | extending — old clients never call it |
| Chunk upload/download URL shape | Change `/chunks/{i}` path format | breaking |
| Chunk download body | Wrap raw bytes in a JSON envelope | breaking |
| `/api/transfers/notify` | Add `test=2` mode | extending |
| `/api/transfers/notify` | Drop inline `sent_status` from the payload | breaking |
| `/api/fasttrack/send` | Tighten payload ceiling below current 128 KB | breaking (for any client relying on the old cap) |
| `/api/fasttrack/send` | Raise ceiling above 128 KB | extending |
| Auth: accept additional header name (e.g. `X-Auth-Token`) | Add as alternative | extending |
| Auth: require a new header on an existing endpoint | Add requirement | breaking |
| Error envelope | Change the top-level key from `"error"` to `"message"` | breaking |
| Error envelope | Add extra context fields (e.g. `"retry_after"`) | extending |
| `.fn.*` naming convention | Remove support for `.fn.unpair` / `.fn.clipboard.*` | breaking |
| `.fn.*` naming convention | Add `.fn.newthing.*` | extending |
| Fasttrack payload `{fn, action}` shape | Reuse existing `fn` with new semantics | breaking |
| Fasttrack payload `{fn, action}` shape | Add a new `fn` value | extending |
| `DeviceMessage.payload` shape for existing `MessageType` | Change the key set or types | breaking |
| `DeviceMessage.payload` shape for existing `MessageType` | Add a new optional key | extending |
| New `MessageType` enum value | Add on both Android + desktop + adapters | extending |
| Ping/pong | Drop the `via:"fresh"` short-circuit | breaking (clients rely on the shape) |
| Ping/pong | Add a new `via` value | extending |
| `/api/health` | Add `capabilities` array with `stream_v1` | extending — old clients ignore |
| `/api/transfers/init` request | Add optional `mode` field (`classic` \| `streaming`) | extending — absent defaults to `classic` |
| `/api/transfers/init` response | Add `negotiated_mode` field | extending |
| `/api/transfers/init` response | Ignore old client's missing `mode` | extending (old → classic) |
| `POST /api/transfers/{id}/chunks/{i}/ack` | New endpoint, streaming only | extending — old clients never call it |
| `GET /api/transfers/{id}/chunks/{i}` | Return **425 Too Early** while streaming chunk not yet stored | extending — classic path never emits 425 |
| `GET /api/transfers/{id}/chunks/{i}` | Return **410 Gone** for aborted transfer | extending — classic clients fall through to generic failure |
| `POST /api/transfers/{id}/chunks/{i}` | Return **410 Gone** when recipient aborted mid-stream | extending — sender sees terminal signal instead of retry loop |
| `POST /api/transfers/{id}/chunks/{i}` | Return **507** transiently when quota full mid-stream | extending — classic path only 507s on `init` |
| `DELETE /api/transfers/{id}` | Now accepts the recipient as caller too | extending — old clients only ever DELETE as sender |
| `DELETE /api/transfers/{id}` response | Sender DELETE still returns `status: "cancelled"` | preserving (back-compat alias) |
| `DELETE /api/transfers/{id}` request body | Optional `{reason: "sender_failed" \| …}` | extending — default derived from caller role |
| `DELETE /api/transfers/{id}` side effect | Transfer row NOW preserved with `aborted=1` instead of being deleted | extending — row surfaces in `/sent-status` as `status: "aborted"`; old sender client won't see it because their local row was already dropped |
| `/api/transfers/sent-status` row | Add `mode`, `chunks_uploaded`, `abort_reason` | extending |
| `/api/transfers/sent-status` `status` value | Add `"aborted"` | extending — per rule above |
| `/api/transfers/sent-status` `delivery_state` | Add `"aborted"` | extending |
| `/api/transfers/pending` | Streaming transfers surface as soon as first chunk stored (was: all chunks) | extending — earlier visibility, same row shape |
| FCM payload `type` | Add `"stream_ready"` | extending — old clients ignore unknown types |
| FCM payload `type` | Add `"abort"` | extending |
| Operator knob `streamingEnabled=false` | Drops `stream_v1` from `/api/health` + always negotiates `classic` | preserving — lets operator force old behaviour fleet-wide |

When in doubt, assume **breaking** until you can show that a pre-existing release
build keeps working against the edit.
