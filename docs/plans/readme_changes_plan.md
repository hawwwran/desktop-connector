# readme-changes-plan.md

Status: P.0 - P.5 complete; post-P.0 cleanup applied. README rewrite plan is complete.

Branch: `readme-rewrite-plan`

Execution model: this is now the live implementation ledger for the README
rewrite. Work one `P.X` chunk at a time, update that chunk's status and
verification notes before moving to the next chunk, and do not broaden the
rewrite beyond the stated README presentation goals without a new decision.

## Implementation chunks

### P.0 - Baseline and claims audit

Status: completed 2026-05-01

Goal: establish the current README shape and identify claims that must remain
true after the rewrite.

Scope:

- Map the current README section order, screenshots, links, install commands,
  and security claims.
- Check nearby project docs for authoritative wording: roadmap, protocol,
  desktop README, release/signing docs, and self-hosting notes.
- List any claims that need current verification before they are made more
  prominent, especially comparison claims against other tools.

Verification notes:

- No README content changes were made in P.0.
- Current README section order:
  1. banner image
  2. title + two-sentence positioning/security summary
  3. Android and desktop screenshot strips
  4. flat `Features` list
  5. Linux desktop install
  6. Android install
  7. setup
  8. server/self-hosting/local development
  9. `How it works`
  10. `Security`
  11. roadmap
  12. license
- Root README local image/doc targets checked and present:
  `docs/assets/banner.png`, all six `images/*` screenshots,
  `docs/release/desktop-signing.pub.asc`, `desktop/README.md`, `CLAUDE.md`,
  and `docs/ROADMAP.md`.
- `gpg --show-keys --with-fingerprint docs/release/desktop-signing.pub.asc`
  confirms the README fingerprint:
  `FBEF CEC1 3D7A EC08 1081 2975 491C 9043 90F4 E03B`.
- Authoritative docs checked for rewrite facts:
  `desktop/README.md`, `android/README.md`, `server/README.md`,
  `docs/protocol/protocol.md`, `docs/protocol/explain.protocol.md`,
  `docs/protocol.compatibility.md`, `docs/PLANS.md`,
  `docs/ROADMAP.md`, `docs/release/desktop-signing-recovery.md`,
  `docs/release/android-signing-recovery.md`,
  `desktop/packaging/appimage/README.md`, `CLAUDE.md`,
  `version.json`, and release workflow/version references.
- Claims that should be preserved:
  - Desktop released form is a signed AppImage, installed by
    `desktop/install.sh`, placed at
    `~/.local/share/desktop-connector/desktop-connector.AppImage`, with
    first-launch relay URL onboarding and system integration.
  - Desktop release signing key identity is
    `Desktop Connector Releases <github@hawwwran.com>` with the fingerprint
    above.
  - Manual desktop verification should import
    `docs/release/desktop-signing.pub.asc`, verify the AppImage `.sig`,
    verify `SHA256SUMS.sig`, then run `sha256sum -c SHA256SUMS`.
  - In-app desktop updates are AppImage/zsync based and use the rolling
    `desktop-latest` update stream; they are not equivalent to a fresh GPG
    verification on every update.
  - Android install path is sideloaded APK from GitHub Releases. Release APKs
    are signed locally with the Android keystore; there is no Android GitHub
    Actions signing path yet.
  - Server requirements are PHP 8.0+, SQLite3, URL rewriting, and optional
    curl/openssl for FCM push wake. The server auto-creates SQLite/storage
    directories and can run under a subdirectory.
  - Protocol security model uses long-lived X25519 device keys,
    X25519 ECDH, HKDF-SHA256, and AES-256-GCM. Metadata is encrypted; chunks
    are 2 MiB except the final chunk.
  - The relay stores ciphertext and routing metadata only. It must not be
    described as seeing plaintext file contents, plaintext filenames,
    clipboard content, GPS coordinates, symmetric keys, auth tokens, or FCM
    tokens.
  - The relay still sees metadata such as device IDs/pairing relationships,
    timing, and chunk counts/approximate size.
  - Desktop multi-device support is implemented in the current branch history:
    target selection, per-peer history, per-device file-manager send targets,
    `Find my Device`, and desktop-to-desktop pairing are represented by
    `docs/plans/desktop-multi-device-support.md`.
- Claims or wording fixed in the post-P.0 cleanup pass:
  - README clipboard wording now distinguishes clipboard text from clipboard
    image saved-file/history behavior.
  - README opening/setup/architecture wording now uses Android device /
    connected-device language where the feature is no longer phone-only.
  - README setup and pairing wording now mentions desktop-to-desktop pairing
    key exchange at a high level.
  - README security table now distinguishes plaintext metadata from
    approximate transfer size metadata.
  - `desktop/VERSION.md` now matches `version.json`'s desktop version
    (`0.3.2`).
  - `docs/ROADMAP.md` now marks multi-device support done and rewords
    user-facing "Find my phone" / "Send to Phone" references.
  - `docs/PLANS.md` now marks brand rollout and desktop multi-device support
    done.
  - Missing-plan links to `docs/plans/desktop-appimage-packaging-plan.md` and
    `docs/plans/secrets-and-signing-plan.md` were removed from the docs that
    linked them.
- Remaining local wording risks:
  - `docs/protocol/protocol.md` was lightly reworded for multi-device scope,
    but it is still a formal protocol spec and should be reviewed separately
    before any larger protocol wording rewrite.
  - Legacy `Send to Phone`, `find-phone`, and `phone` strings remain in code,
    tests, compatibility notes, and historical plan ledgers where they describe
    legacy filenames, window names, wire values, or migration behavior.
- Claims needing external/current verification before P.2 comparison:
  - KDE Connect and LocalSend feature cells should be verified from current
    primary docs before writing specific comparison claims.
  - If verification is not done in P.2, keep the comparison at stable,
    conservative fit-level dimensions or omit uncertain cells.

### Post-P.0 cleanup - Locally verifiable wording drift

Status: completed 2026-05-01

Goal: fix high-confidence stale wording and broken local doc references found
during P.0 before starting the larger README rewrite chunks.

Scope completed:

- Reworded the root README for Android devices / connected devices rather than
  phone-only phrasing where appropriate.
- Corrected README clipboard-image behavior, pairing overview, transfer chunk
  size wording, and server metadata/security wording.
- Refreshed `docs/ROADMAP.md` for multi-device, Find my Device, and per-device
  file-manager send targets.
- Refreshed `docs/PLANS.md` statuses for completed brand and multi-device
  ledgers.
- Updated `desktop/VERSION.md` to match `version.json`.
- Removed links to missing plan files from contributor, desktop packaging, and
  release docs.
- Lightly corrected deep-dive wording in `CLAUDE.md` and
  `docs/protocol/protocol.md` where it was locally stale.

Verification notes:

- Changes are limited to local, repo-verifiable drift. External comparison
  claims remain out of scope for this cleanup.
- P.1 is still pending because the README has not yet been restructured into
  the planned positioning/status/tradeoffs flow.

### P.1 - Positioning and reader-fit pass

Status: completed 2026-05-01

Goal: make the first-screen and near-top README answer what the project is, who
it is for, why it exists, and what tradeoffs readers should understand.

Scope:

- Strengthen the opening without making it heavier.
- Add concise "Who this is for" and "Why this exists" sections.
- Add a clear "Current status" section.
- Add short "Tradeoffs / current limitations" content.
- Keep wording factual, technically credible, and not over-marketed.

Verification notes:

- Strengthened the opening to position Desktop Connector as self-hosted,
  end-to-end encrypted Android/Linux sharing through a user-controlled PHP
  relay.
- Added `Who this is for`, focused on Android + Linux users, self-hosters,
  end-to-end encryption, practical Linux desktop integration, and multi-device
  target selection.
- Added `Why this exists`, describing the blind-relay reachability tradeoff and
  Linux-native desktop workflow without naming or comparing specific
  alternatives.
- Added `Current status`, stating that the project is usable now for Android
  and Linux desktop workflows and listing locally verified shipped surfaces.
- Added `Tradeoffs`, covering Linux-focused desktop support, sideloaded Android
  APKs, personal/small relay deployment fit, relay-visible metadata, and modern
  distro expectations for the AppImage.
- Top-level wording uses Android device / connected device language instead of
  phone-only framing where the feature applies to desktops or tablets too.
- P.1 intentionally did not add the P.2 comparison table, P.3 feature grouping,
  or P.4 architecture/security/doc-map rewrite.

### P.2 - Differentiation and comparison

Status: completed 2026-05-01

Goal: help readers understand what Desktop Connector is optimized for compared
with nearby tools, without attacking alternatives or making brittle claims.

Scope:

- Add a compact comparison section or table.
- Prefer stable comparison dimensions: self-hosted relay, relay E2EE model,
  Android/Linux focus, clipboard/file workflow, desktop integration, share
  intent, and file-manager send targets.
- Verify any non-obvious current claims before writing them into the README.
- Phrase comparison as fit/optimization, not superiority.

Verification notes:

- Added a compact `How it differs` section after `Why this exists`.
- Used current primary sources:
  - KDE Connect site/download/UserBase docs for broad device-integration scope,
    Linux/Android availability, local-network pairing, Bluetooth, VPN/manual IP
    paths, and plugin examples.
  - LocalSend official site for cross-platform file/text sharing, local
    network/offline model, no account/login/server, and E2EE/HTTPS transfer
    claims.
- Kept the comparison fit-oriented:
  - KDE Connect: broad device integration.
  - LocalSend: nearby/local-network cross-platform transfer.
  - Desktop Connector: Android/Linux workflow through a self-hosted blind relay
    with relay E2EE and Linux desktop integration.
- Avoided unsupported or brittle claims about every feature/cell of alternatives.
- Did not add P.3 feature grouping or P.4 security/architecture restructuring.

### P.3 - Features, install, and release clarity

Status: completed 2026-05-01

Goal: make the README easier to scan for people deciding whether to try the
project today.

Scope:

- Group the current flat feature list into practical categories.
- Make Linux, Android, and server install paths easier to spot.
- Add or sharpen a short requirements/release expectations block if it reduces
  scanning friction.
- Keep existing commands and release/signing details accurate.

Verification notes:

- Replaced the flat feature list with grouped categories:
  - `Transfer and clipboard`
  - `Multi-device workflow`
  - `Linux desktop integration`
  - `Delivery and reliability`
  - `Self-hosting`
- Added a `Quick install paths` table covering Linux desktop, Android, relay
  server, and desktop development.
- Preserved the full Linux desktop installer command in both the quick table
  and the detailed `Install (Linux Desktop)` section.
- Preserved the GitHub Releases Android path, relay server requirements, manual
  AppImage verification link, signing fingerprint, updater note, uninstall
  command, and `desktop/README.md` contributor path.
- Confirmed feature grouping still surfaces multi-device, clipboard text,
  clipboard image handling, share intents, file-manager integration, offline
  behavior, and self-hosting.
- P.3 did not rewrite the deeper architecture/security sections; those were
  covered in P.4.

### P.4 - Architecture, security, docs, and contributor route

Status: completed 2026-05-01

Goal: make the README communicate engineering maturity and route deeper readers
to the right documents.

Scope:

- Strengthen "How it works" into a concise architecture-at-a-glance section.
- Improve the security model explanation while keeping the existing server
  visibility table.
- Add a compact documentation map to relevant project docs.
- Add a short contributor-facing section with practical contribution areas.

Verification notes:

- Replaced the old short `How it works` section with `Architecture at a
  glance`, covering the Android app, Linux desktop app, PHP relay server, the
  encrypted upload/download path, X25519 device keys, and AES-256-GCM
  encryption/decryption responsibilities.
- Expanded the core flow to cover registered device keys and auth tokens,
  QR-code and desktop-to-desktop pairing, HKDF-SHA256 key derivation, encrypted
  file metadata, 2 MiB encrypted chunks, clipboard synthetic filenames,
  delivery tracking, and encrypted fasttrack commands.
- Reworded the security model around the correct trust boundary: the relay
  routes encrypted data, but paired devices own content keys and plaintext
  content handling.
- Preserved and sharpened the server visibility table so it distinguishes
  content confidentiality from metadata the relay still handles: device IDs,
  pairing relationships, timing, delivery state, and approximate transfer size.
- Added a compact `Project docs` map to protocol, compatibility, examples,
  roadmap, plans, diagnostics, desktop, Android, server, and release signing
  documents.
- Added a `Contributing` section that routes readers to `CONTRIBUTING.md` and
  `CLAUDE.md`, then names practical contribution areas.
- Confirmed all new README doc links resolve to local files.
- `git diff --check` passed for the P.4 changes.
- P.5 handled the final top-to-bottom polish and verification pass.

### P.5 - Final polish and verification

Status: completed 2026-05-01

Goal: finish the README as one coherent document and update this plan ledger.

Scope:

- Review narrative flow from top to bottom.
- Remove duplicated claims introduced during phased edits.
- Check Markdown rendering, image references, relative links, and headings.
- Run lightweight verification commands for Markdown/link-sensitive changes
  available in the repo.
- Mark completed chunks and record verification results in this plan.

Verification notes:

- Read the final README top to bottom after P.1-P.4 and kept the overall flow:
  opening, audience, rationale, comparison, status, tradeoffs, features,
  install/setup, server, architecture, security, docs, contributing, roadmap.
- Removed final awkwardness and duplication introduced during phased edits:
  tightened relay wording, replaced `phone-finding` comparison wording with
  `device-finding`, changed `relay E2EE` to `relay-based E2E encryption`,
  fixed the signing fingerprint spacing, and routed self-hosting details to
  `server/README.md` instead of the contributor/agent guide.
- Kept the quick install table command render-safe by using an HTML code span
  with `&#124;` for the shell pipe, while preserving the normal fenced command
  in the detailed Linux install section.
- Confirmed the README answers the final questions: what it does, who it is
  for, why it exists, how it differs, what ships today, what the tradeoffs are,
  how to install it, what the security model is, and where to go next.
- Confirmed all local README image and document targets referenced by the final
  file exist.
- Confirmed the final heading outline is coherent and has no duplicate section
  titles.
- `git diff --check` passed after final polish.

## Purpose

This document describes how the README should be improved so the project presents more strongly to:

- first-time visitors,
- technically curious users,
- potential contributors,
- and people comparing it with alternative tools.

The goal is **not** to turn the README into marketing fluff.  
The goal is to make the project look:

- clearer,
- more intentional,
- more credible,
- and easier to understand quickly.

This plan is based on one core idea:

**the project already has strong substance; the README should expose that strength faster and more deliberately.**

---

## Main presentation problem

The current README already does several things well:

- it explains the core value proposition,
- it shows screenshots,
- it states the security model,
- it lists important features,
- it includes installation instructions,
- and it gives a reasonable high-level architecture overview.

That is a strong foundation.

The problem is not that the README is weak.  
The problem is that it can still present the project **more sharply**.

Right now, a new visitor can understand what the project does, but the README can do a better job of answering these questions immediately:

- Who is this for?
- Why does this project exist instead of just using something else?
- What are its strongest differentiators?
- What are its current tradeoffs?
- How mature is it right now?
- What should a new user trust, and what should they realistically expect?

That is what this plan addresses.

---

## High-level goals for the README rewrite

The README should become better at doing these five things:

### 1. Establish positioning quickly
A reader should understand in a few seconds what kind of project this is and who it is for.

### 2. Make differentiation obvious
A reader should quickly understand why this project exists and how it differs from alternatives.

### 3. Signal engineering maturity
A reader should see that this is not just a rough prototype, but a thought-through system with real design decisions.

### 4. Show tradeoffs honestly
A reader should trust the project more because the README clearly states what it is and what it is not.

### 5. Guide deeper reading cleanly
A reader should know where to go next depending on what they care about:
- installation,
- architecture,
- protocol,
- roadmap,
- limitations,
- releases,
- self-hosting,
- contributing.

---

## Recommended structure changes

## 1. Strengthen the opening section

### Current issue
The current README opens with a solid description, but it can still become more specific in terms of audience and category.

A stronger opening should immediately answer:

- what kind of tool this is,
- what problem it solves,
- what makes it different,
- and who it is for.

### Recommended change
Add a short opening block directly below the title that makes the positioning explicit.

### Suggested direction
The opening should clearly communicate something like:

- this is a self-hosted, end-to-end encrypted Android ↔ Linux transfer and clipboard tool,
- it is aimed at users who want control over their own relay/server,
- and it prioritizes privacy, directness, and practical desktop integration.

### Why this helps
A lot of projects lose impact because they explain features before they explain category and intended audience.

This change gives readers a fast mental model.

---

## 2. Add a short “Who this is for” section

### Current issue
The README makes the functionality clear, but the intended user is still somewhat implicit.

### Recommended change
Add a short section near the top such as:

- “Who this is for”
- or “Best fit for”

This should be brief and practical.

### Suggested content direction
Examples of users this project fits well:

- Android + Linux users,
- users who want self-hosting,
- users who care about end-to-end encryption,
- users who want clipboard + file transfer in one workflow,
- users who prefer direct integration over cloud relay services.

### Why this helps
It increases clarity immediately and helps the project feel intentional rather than merely feature-assembled.

---

## 3. Add a short “Why this exists” section

### Current issue
The README explains what the project does, but not strongly enough why a reader should care that it exists separately from other known tools.

### Recommended change
Add a concise section explaining the reason for the project.

### Suggested content direction
This should answer questions like:

- Why build this instead of relying on existing peer-to-peer or relay-based tools?
- Why use a blind relay model?
- Why self-hosted PHP?
- Why focus on Android + Linux specifically?

This should stay factual and practical, not emotional.

### Why this helps
Many technically strong projects undersell themselves because they do not state the problem they are trying to solve in explicit comparative terms.

This section turns the project from “here is a tool” into “here is a deliberate solution to a specific gap”.

---

## 4. Add a comparison section

### Current issue
The README currently does not clearly help readers compare this project to known alternatives.

### Recommended change
Add a compact comparison table.

### Suggested comparison targets
Use a small set of relevant comparison points, for example:

- KDE Connect
- LocalSend
- Desktop Connector

The goal is not to attack alternatives.  
The goal is to clarify what this project is optimized for.

### Suggested comparison dimensions
Good dimensions could include:

- Android support
- Linux desktop integration
- self-hosted relay
- end-to-end encryption over relay
- clipboard sync
- file transfer
- share intent support
- tray integration
- right-click send integration
- find-my-phone support
- dependency on third-party cloud service

### Why this helps
Comparison tables are one of the fastest ways for users to understand why a project exists.

Without a comparison, readers must infer differentiation on their own.

---

## 5. Add a concise “Current status” section

### Current issue
The project looks functional, but the README does not yet communicate maturity and scope in the most direct way.

### Recommended change
Add a short “Current status” section near the top or after the feature list.

### Suggested content direction
This section should honestly state things like:

- current supported platforms,
- current maturity level,
- whether the project is usable today,
- whether it is still evolving actively,
- what is stable versus still developing.

This should not be defensive.  
It should be clear and matter-of-fact.

### Why this helps
Users trust projects more when they know whether they are looking at:
- an experiment,
- an early but usable project,
- or a polished stable release.

Ambiguity lowers trust.  
Honest status increases trust.

---

## 6. Add an explicit “Tradeoffs / current limitations” section

### Current issue
The project has strong ideas, but the README can increase trust further by being more explicit about limitations.

### Recommended change
Add a short section such as:

- “Tradeoffs”
- “Current limitations”
- or “Design tradeoffs”

### Suggested content direction
Good candidates include:

- Linux desktop only for now
- desktop implementation currently shaped by Linux desktop/runtime/toolkit constraints
- PHP relay optimized for simplicity and self-hosting, not large-scale multi-tenant operation
- current desktop environment assumptions
- current packaging/install assumptions

This should stay short and honest.

### Why this helps
Readers trust engineering maturity more when limitations are stated clearly instead of hidden.

This also helps set correct expectations for contributors and early adopters.

---

## 7. Improve the feature list by grouping it

### Current issue
The current feature list is strong but somewhat flat.

### Recommended change
Group the features into meaningful categories instead of presenting them as one continuous list.

### Suggested grouping
Possible groups:

- Core transfer features
- Desktop integration
- Delivery and reliability
- Security and privacy
- Convenience features
- Self-hosting features

### Why this helps
A grouped feature list is easier to scan and makes the project feel more structured and product-like.

It also highlights that the project is not just “file transfer”, but a broader workflow tool.

---

## 8. Add an “Architecture at a glance” section

### Current issue
The current README already contains a simple architecture diagram and a “How it works” section, which is good.

### Recommended change
Keep that, but slightly strengthen it with a short explicit architecture summary block.

### Suggested content direction
This section should summarize:

- Android client
- blind relay server
- Linux desktop client
- pairing and shared-key derivation
- encrypted upload and delivery
- delivery acknowledgment and sender status updates

Keep it concise and readable.

### Why this helps
This gives technically minded readers a faster conceptual model before they dive into detailed docs.

It also supports the impression that the project is thoughtfully engineered.

---

## 9. Add a “Security model in one minute” section

### Current issue
Security is already mentioned, but it can be framed more clearly as a trust-building section.

### Recommended change
Add a short section that explains the security model in simple terms.

### Suggested content direction
It should clarify:

- what the relay can see,
- what it cannot see,
- what device pairing does,
- what the encryption guarantees,
- what metadata still exists,
- and what users should realistically understand about privacy.

This should complement, not replace, the existing security table.

### Why this helps
Readers interested in privacy often decide very quickly whether they trust a project based on whether the security explanation feels clear and honest.

A stronger explanation improves credibility.

---

## 10. Make releases and install paths more visible

### Current issue
Installation is present, but release and version maturity could be made easier to understand at a glance.

### Recommended change
Surface a clearer install/release block near the top.

### Suggested content direction
Make it easier to spot:

- Linux install command
- Android APK release location
- current release expectations
- server hosting requirements

You may also consider adding a short “Requirements” subsection.

### Why this helps
Readers deciding whether to try the project should not have to scan deeply for the basic installation path.

Fast install clarity improves conversion from “interesting repo visitor” to “actual tester/user”.

---

## 11. Add a clearer document map

### Current issue
The project already has supporting docs, but the README can do a better job of routing readers to them.

### Recommended change
Add a short “More documentation” or “Project docs” section.

### Suggested targets
Likely links include:

- roadmap
- protocol
- explain.protocol
- architecture-oriented docs if added later
- release notes or changelog if added later

### Why this helps
A strong README is not only self-contained.  
It is also a good index into the rest of the project.

This makes the project feel more complete and easier to navigate.

---

## 12. Add a stronger contributor-facing section

### Current issue
The project already looks technically interesting, but it can become more inviting to contributors with better framing.

### Recommended change
Add a short section such as:

- “Contributing”
- “Good contribution areas”
- “Current priorities”

### Suggested content direction
Keep it practical, for example:

- protocol compatibility work
- desktop platform abstraction work
- Windows-readiness tasks
- Android stability polishing
- diagnostics/logging improvements
- self-hosting deployment polish

### Why this helps
It makes the repo look more alive and gives technically interested readers a way to engage beyond passive usage.

---

## 13. Improve narrative flow

### Current issue
The README already contains useful sections, but the reading order can likely become sharper.

### Recommended target flow
A stronger flow would be something like:

1. Title
2. One-sentence positioning
3. Screenshot strip
4. Who this is for
5. Why this exists
6. Key differentiators / comparison
7. Features (grouped)
8. Current status
9. Install
10. Setup
11. Architecture at a glance
12. Security model
13. Self-hosting/server
14. Limitations/tradeoffs
15. Documentation map
16. Roadmap / contributing
17. License

### Why this helps
This order follows how most readers evaluate a project:

- What is it?
- Is it for me?
- Why is it different?
- Does it look credible?
- Can I install it?
- Can I trust it?
- Can I learn more?

---

## Recommended change priorities

Not all changes are equally important.  
This is the recommended order.

## Priority 1 — strongest leverage
These changes will likely improve the README the most:

1. strengthen the opening section
2. add “Who this is for”
3. add “Why this exists”
4. add comparison section
5. add “Current status”
6. add “Tradeoffs / current limitations”

These changes improve positioning, trust, and differentiation immediately.

---

## Priority 2 — readability and structure
These changes improve readability and technical clarity:

7. group the feature list
8. strengthen architecture summary
9. strengthen security framing
10. make install/release path more visible

These changes improve comprehension and reduce friction.

---

## Priority 3 — repo completeness and contributor appeal
These changes make the project feel more mature and easier to navigate:

11. add documentation map
12. add stronger contributor-facing section
13. improve overall narrative flow

These changes are valuable, but they have slightly less impact than the earlier positioning work.

---

## Recommended writing style

The README should be written in a style that is:

- factual
- confident
- clear
- technically honest
- not defensive
- not over-marketed

It should avoid:

- exaggerated claims
- vague hype language
- pretending current limitations do not exist
- overselling maturity if the project is still evolving

The strongest tone for this project is:

**practical, technically credible, and self-aware.**

---

## What to avoid

When improving the README, avoid these common mistakes:

### 1. Turning it into marketing copy
That would weaken trust instead of increasing it.

### 2. Over-explaining everything at the top
The opening should be sharper, not heavier.

### 3. Hiding limitations
Honest limitations help this kind of project more than they hurt it.

### 4. Comparing aggressively with alternatives
The comparison should clarify fit, not attack other tools.

### 5. Mixing protocol/architecture details too early
The top of the README should help users classify the project quickly.  
Deep architecture should come later or be linked out.

---

## Practical outcome of this plan

If these changes are applied well, the README should leave readers with a clearer impression that the project is:

- intentionally designed,
- technically serious,
- practically useful today,
- honest about its tradeoffs,
- differentiated from alternatives,
- and worth trying or contributing to.

That is the real goal of the README rewrite.

---

## Suggested final checkpoint

After the README is revised, it should be easy for a new reader to answer these questions in under a minute:

- What does this project do?
- Who is it for?
- Why would I use it instead of something else?
- Is it usable today?
- What are its tradeoffs?
- Can I install it quickly?
- Where do I go for deeper technical details?

If the README can answer those well, it will already present the project much more strongly.
