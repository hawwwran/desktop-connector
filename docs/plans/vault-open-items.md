# Vault — open items before v1 ships

**Date opened:** 2026-05-15
**Architecture reference:** [`../vault-architecture.md`](../vault-architecture.md)
**Source of truth this replaces:** the "Status reconciliation" block
of the archived
[`temp/finished-plans/desktop-connector-vault-plan-md/VAULT-progress.md`](../../temp/finished-plans/desktop-connector-vault-plan-md/VAULT-progress.md).

The shipped Vault implementation (`desktop/src/vault/`,
`server/src/{Controllers,Repositories,Crypto,Auth}/Vault*.php`)
covers everything T0 locked for v1. The earlier tracker named three
threads still to close before stamping "v1"; spot-checks on
2026-05-15 closed two of them.

What remains is **one** thread: the critical-risks evaluation gate.

**Status (2026-05-15):** evaluation pass landed at
[`../vault-critical-risks-evaluation.md`](../vault-critical-risks-evaluation.md).
§3.7 rollback detection (was Open) **shipped 2026-05-15** —
per-device manifest revision floor + persistent `Adw.Banner` +
self-heal on the next successful decrypt; closed via
[`live-testing-followup.md`](live-testing-followup.md) §10. The
remaining gate is the §11 follow-up (fresh-unlock enforcement in
import + destructive UI), which once shipped flips §3.9 + §3.11
from Mitigated to Resolved. v1 can be stamped once §11 lands.

---

## §1. Critical-risks evaluation gate

The archived
[`desktop-connector-vault-critical-risks-and-weaknesses.md`](../../temp/finished-plans/desktop-connector-vault-plan-md/desktop-connector-vault-critical-risks-and-weaknesses.md)
catalogues 20 risk areas (§3.1–3.20) plus 8 acknowledged weaknesses
(§4.1–4.8). At authoring time those were *implementation
requirements*, not closed items. The implementation has since
shipped. This gate re-labels each risk against the as-built code
as one of:

- **Resolved** — code defends; verification covered by tests or
  manual smoke.
- **Mitigated** — defended but with caveats (e.g. a known limit
  or a non-test-covered path).
- **Accepted** — risk acknowledged, no further defence planned in
  v1 (e.g. relay-operator with full DB access — covered only by
  protected export).
- **Open** — defence still missing or unverified.

Output: a single follow-up document
`docs/vault-critical-risks-evaluation.md` containing one entry per
risk in the template below. Until that doc exists and every risk
carries a label, "v1" stays unlabelled.

**The doc was created on 2026-05-15** —
[`docs/vault-critical-risks-evaluation.md`](../vault-critical-risks-evaluation.md).
Read that doc, not the per-risk bullets below, when you need the
current status of any specific risk; the bullets below are kept
as a record of what the evaluation was asked to cover.

### Per-risk template

```
### §<n>. <Risk title>

**Status:** Resolved | Mitigated | Accepted | Open
**Code anchor:** path/to/file.py:func_name (or N/A)
**Verification:** what to look at or run to confirm
**Notes:** any caveats, deferred work, or test gaps

```

### The 15 risks needing close attention

These touch shipped code; the other five (§3.16 ignore rules,
§3.17 case sensitivity, §3.18 disk preflight, §3.19 integrity
check existence, §3.20 activity timeline existence) are largely
structural and resolve via "feature exists" rather than deep code
review.

1. **§3.1 — Vault key generation.** Verify the 256-bit RNG source
   and that nothing in `desktop/src/vault/crypto.py` or
   `vault.py::prepare_new` mixes timestamps / device IDs /
   `Math.random` equivalents into key material.
2. **§3.2 — Recovery envelope.** Verify the mandatory recovery
   test (`tab_recovery.py`) actually re-derives the wrap key end-
   to-end and AEAD-decrypts the master-key payload. Older kits
   that predate the embedded metadata must fail with the explicit
   "old format" message.
3. **§3.3 — Device grants & revocation.** Confirm revocation
   blocks future ops but explicit UX wording (`§14` lock) is
   surfaced so users understand local plaintext copies are not
   erased.
4. **§3.4 — QR-assisted device joining.** Inspect the join QR
   payload — must contain only `{relay_url, vault_id, join_id,
   expiry}`. Never the master key or recovery passphrase. Source:
   `desktop/src/vault/grant/`.
5. **§3.5 — AEAD nonce safety.** Per-context AAD verified via
   the protocol-formats test vectors. Nonce uniqueness per
   chunk / manifest / header — spot-check the nonce-derivation
   subkeys (`dc-vault-v1/chunk-nonce` and friends).
6. **§3.6 — Manifest CAS.** Server: 409 returns the full current
   ciphertext + hash + revision (A1 — confirmed already at
   `server/src/Controllers/VaultController.php:463`). Client:
   merge applies §D4 rules; tested in
   `tests/protocol/test_desktop_vault_*`.
7. **§3.7 — Rollback detection.** Client must track the highest
   manifest revision it has seen and warn on a downgrade. Check
   `desktop/src/vault/state/`.
8. **§3.8 — Chunk upload integrity.** Manifest must reference
   only chunks that successfully PUT. Verify the upload state
   machine in `desktop/src/vault/upload/`.
9. **§3.9 — Import & merge safety.** Verify
   `vault_identity_mismatch` 409 fires on different genesis
   fingerprints; `merge_import_into` preserves both sides on
   per-folder conflict; default mode is Rename.
10. **§3.10 — Export protection.** Verify outer Argon2id envelope,
    inner AEAD-per-record chain hash, footer verification. Test
    vectors at `tests/protocol/vault-v1/export_bundle_v1.json`.
11. **§3.11 — Delete vs purge.** Verify the four §22 vocabulary
    terms render correctly in UI strings and the typed-confirm
    guards in `tab_danger.py` cover clear-folder, clear-vault,
    schedule-purge.
12. **§3.12 — Local binding after restore.** Verify preflight runs
    and tombstones don't delete local files before the binding
    baseline is laid down (`vault/binding/preflight.py`).
13. **§3.13 — Sync defaults.** Default mode for a new binding must
    be **Backup only**, not Two-way. Verify the binding-create
    path in `vault/binding/`.
14. **§3.14 — File stability.** Verify the watcher waits for
    same-size-and-mtime before queuing; spot-check the temp/swap
    file patterns. (`vault/binding/watcher.py`,
    `vault/binding/scan.py`.)
15. **§3.15 — Ransomware / mass-change detector.** Verify defaults
    (200 changes / 5 min OR ≥50 % rename ratio), the
    immediate-pause behaviour (no pre-prompt per A15), and the UX
    flow that requires user review before resume.

### Process notes

- The evaluation pass is a single focused doc-write session, not
  a series of PRs. Each risk re-label that surfaces a *real* gap
  becomes its own item in
  [`live-testing-followup.md`](./live-testing-followup.md) §10+
  *or* a code fix PR, whichever fits.
- Risks already explicitly closed elsewhere should cite the
  closing reference (e.g. §3.7 ↔ ADR 2026-05-12, §3.11 ↔ §22 lock).
- Use this doc, the architecture doc, and the archived T0 lock
  as the working set during the evaluation. The risks doc itself
  is the input.

---

## §2. Out of scope (deferred, not "open")

- **T15 — Android browse / import / manual upload / QR grant.**
  Per **D7**, Android Vault is post-v1. Not blocking the v1
  label.
- **T16 — Android folder sync.** Same. Background folder watching
  on Android is unreliable compared with desktop inotify /
  FSEvents; the plan defers it explicitly.

Both items live under `T15` / `T16` in the archived
`VAULT-progress.md`; they don't move to this doc.

---

## Stale-claim retraction

The archived `VAULT-progress.md` "Status reconciliation" block
(2026-05-12) named a third open thread: **"UI wire-up holes — a
handful of buttons in `tab_maintenance.py` + `tab_danger.py` start
`sensitive=False` and never enable."**

Spot-check on 2026-05-15: every `set_sensitive(False)` call in
those two files is operationally correct.

- `tab_maintenance.py:82` — debug-bundle button disables during
  the worker run, re-enables on completion.
- `tab_maintenance.py:184–185, 275–276` — integrity-check buttons
  disable during a worker run and stay disabled while no vault is
  loaded (correct guard at lines 275–276).
- `tab_danger.py:202, 315` — clear-folder / clear-vault buttons
  disable during their workers.
- `tab_danger.py:412, 428` — purge button disables when no vault
  is loaded *or* when a purge is already pending (cancel button
  shown instead).

Nothing is stuck off forever. This thread is **closed without
action**.
