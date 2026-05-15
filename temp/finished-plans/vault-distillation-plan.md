# Vault distillation plan

**Date opened:** 2026-05-15
**Branch:** `tresor-vault`
**Goal.** Replace the 16-file `desktop-connector-vault-plan-md/`
directory in `docs/plans/` with two focused docs:

1. **`docs/vault-architecture.md`** — canonical reference for the
   shipped Vault feature, organized by concept, code-anchored. The
   "if I want to discuss Vault without getting lost, where do I
   look" doc.
2. **`docs/plans/vault-open-items.md`** — the only still-open work
   (critical-risks evaluation gate + four UI wire-up holes).

Then archive the 16 plan files into `temp/finished-plans/` and
rewrite all inbound references.

This file is the **execution tracker**. Tick items as they land.

---

## Execution order

- [x] **0. Scope + structure agreed with the user.** (2026-05-15)
- [x] **1. Draft `docs/vault-architecture.md`** — 15 sections, ~850 lines. *(landed at 1211 lines — destructive-action table + pointer blocks ran over estimate)*
- [ ] **2. Spot-check architecture doc against code** for ~8
      high-stakes claims; record drift inline rather than gloss.
- [x] **3. Draft `docs/plans/vault-open-items.md`** — 172 lines.
- [x] **4. `git mv` `desktop-connector-vault-plan-md/` →
      `temp/finished-plans/`.**
- [x] **5. Rewrite inbound references** (PLANS.md, CLAUDE.md, test
      docstrings, per-phase cross-links).
- [x] **6. Stop. Ask before commit, ask again before push.**
      *(2026-05-15: user authorized single combined commit + push;
      tracker archived in same commit.)*

---

## Architecture doc — section checklist

Target: ~850 lines, prose-readable, code-anchored. Each section ends
with explicit pointers (protocol docs, archived T0 lock, ADR entries,
code paths). Style modelled on `docs/visual-identity-guide.md`.

- [x] **§1. Overview & trust boundary.** What Vault is +
      account-less framing; what the server sees vs what stays
      client-side. Anchor from `CLAUDE.md` "## Vault (vault_v1)".
- [x] **§2. Product model.** Vault / remote folder / binding /
      device / grant. Four roles per D11 (read-only /
      browse-upload / sync / admin). Browse-only vs binding.
- [x] **§3. Identity & crypto.** Argon2id (m=128 MiB, t=4, p=1).
      HKDF labels verbatim (`dc-vault-v1/*`).
      XChaCha20-Poly1305 AEAD. AAD construction for manifest /
      chunk / header. Recovery envelope. Device-grant wrapping.
      Genesis fingerprint.
- [x] **§4. Storage model.** Manifest envelope (plaintext format
      byte + AEAD body, version chain, op-log tail cap 1000, archive
      oldest 500). Chunk envelope (content-addressed
      `ch_v1_[a-z2-7]{24}` ID). Header envelope. Server tables.
- [x] **§5. Wire protocol — summary only.** Endpoint groups.
      Capability bits (`vault_create_v1` … `vault_purge_v1` +
      aggregate `vault_v1`). Auth composition (device +
      vault-bearer). Error-code grouping. Defer byte detail to
      `docs/protocol/vault-v1.md`.
- [x] **§6. CAS merge.** D4 nine auto-mergeable ops + hard-purge as
      manual. A1 conflict 409 returns full current ciphertext.
- [x] **§7. Versions, tombstones, retention.** D10 vocabulary. D5
      retention math; A8 server-clock authoritative. §22
      local-effects lock (Disconnect / Delete / Clear / Purge).
- [x] **§8. Quota & eviction.** 1 GB default. 80/90/100 bands. D2
      strict 4-step eviction order. A16 three GC triggers. A21
      per-folder = descriptive only.
- [x] **§9. Sync engine.** §20 mode vocab. A12 binding states. D15
      preflight tombstone preview. §6 ransomware defaults
      (200 changes / 5 min OR ≥50% rename ratio). §7 ignore patterns
      + 2 GB cap. §8 / §9 special files / case sensitivity. A20
      conflict-naming.
- [x] **§10. Export / import bundles.** A10 CBOR record stream.
      Outer Argon2id envelope (D8 separate passphrase). D9 three
      modes (Overwrite / Skip / Rename — default Rename). A4
      per-folder conflict batches. §17 import preview 8 fields.
      §16 monthly reminder default.
- [x] **§11. Relay migration (H2).** Verify-then-switch state
      machine. 7-day switch-back window. Multi-device propagation.
- [x] **§12. Destructive actions & threat model.** Seven
      destructive actions ledger. Guards (typed-confirm, fresh
      unlock per §13, default 24 h hard-purge delay). Audit-event
      names. Threat model summary + what v1 explicitly doesn't
      defend against.
- [x] **§13. UI surfaces.** Main settings toggle (D16). Vault
      Settings 10-tab map. Vault Browser. Onboarding + Import
      wizards. Tray menu states by D16.
- [x] **§14. Diagnostics.** §21 two-layer audit (encrypted op-log
      vs opt-in local log). §19 Quick vs Full integrity check.
      Redacted debug bundle.
- [x] **§15. Where the canonical bits live.** Pointer map:
      protocol docs / archived T0 lock / ADR entries / code paths.

---

## Open-items doc — outline checklist

Target: ~120 lines.

- [x] **Header.** Link to architecture doc. Names one remaining
      blocker (down from two — stale UI claim retracted).
- [x] **§1. Critical-risks evaluation gate.** 15 risk areas to
      re-label (Resolved / Mitigated / Accepted / Open) against
      as-built code. Per-risk template: status + code anchor +
      verification note. Pointer to archived
      `temp/finished-plans/desktop-connector-vault-plan-md/desktop-connector-vault-critical-risks-and-weaknesses.md`.
- [ ] ~~**§2. UI wire-up holes.**~~ **DROPPED — claim was stale.**
      Spot-check 2026-05-15 confirmed every `set_sensitive(False)`
      in `tab_maintenance.py` + `tab_danger.py` is an operationally
      correct disable (while-worker / no-vault / purge-pending).
      Nothing is stuck off forever. VAULT-progress.md's status
      reconciliation §15–34 had drifted since the buttons were
      wired up.
- [x] **§2. Out of scope (deferred, not open).** T15 / T16 Android
      per D7.

---

## Spot-check code claims

The architecture doc names specific numeric / structural facts. For
each below, grep the code to confirm. Note drift inline in the doc
rather than silently glossing.

- [x] **Argon2id params.** m=128 MiB, t=4, p=1 in `vault/crypto.py`.
- [ ] **HKDF labels** match `dc-vault-v1/*` set in `vault/crypto.py`.
- [ ] **Chunk-ID regex** `^ch_v1_[a-z2-7]{24}$` in chunk-id helpers
      and on the server side.
- [ ] **AAD construction** for manifest / chunk / header matches
      what `docs/protocol/vault-v1-formats.md` already locks.
- [ ] **Manifest CAS 409 shape** matches A1 in
      `server/src/Controllers/VaultController.php`.
- [ ] **Eviction 4-step order** in `vault/ops/eviction.py`.
- [ ] **Conflict-naming format** (§A20) in
      `vault/conflict_naming.py` or equivalent.
- [ ] **Tray menu states** in `tray/vault_submenu.py` match D16.

---

## Inbound-reference rewrites

These files reference `docs/plans/desktop-connector-vault-plan-md/`
or its contents. Each needs updating after the archive move.

- [x] `docs/PLANS.md` — replace the vault row with: (a) link to
      `docs/vault-architecture.md`, (b) row for new
      `vault-open-items.md`. Add archive blurb pointer.
- [x] `CLAUDE.md` — the "## Vault (vault_v1)" block currently
      points at `docs/protocol/vault-v1.md` +
      `docs/protocol/vault-v1-formats.md`; add a leading pointer to
      `docs/vault-architecture.md`.
- [x] `docs/architecture-decisions.md` — any entry that anchored at
      a `plans/desktop-connector-vault-plan-md/` path.
- [x] `docs/plans/live-testing-followup.md` — already references
      T0 / VAULT-progress; rewrite to point at archive or at the
      architecture doc as appropriate.
- [ ] Any test docstrings or per-phase files surfaced by
      `grep -rn 'desktop-connector-vault-plan-md\|VAULT-progress\|vault-T0-decisions\|vault-critical-risks'`.

---

## Notes that surfaced during the explore-agent scans

- README has zero Vault content — that's a separate gap, not in
  scope for this distillation. Note for future README update.
- All explore agents reported "no significant drift" between plan
  and code. Spot-check pass should confirm, not re-prove.
- The five-agent digest covered T0-decisions (812 lines), plan
  files 01–11 (~3700 lines), and critical-risks (1276 lines)
  totalling ~5800 lines of source distilled into ~5500 words of
  structured notes that feed the architecture doc.
