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
