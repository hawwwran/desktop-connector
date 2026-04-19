# appimage-distro-support-plan.md

## Purpose

This document defines a practical distribution and support strategy for shipping the Desktop Connector Linux desktop client as an AppImage.

The main question it answers is:

**If the app is distributed as an AppImage, how should distro support be handled in a realistic and maintainable way?**

This document is intentionally pragmatic.

It is not based on the idea of “support every Linux distro equally.”  
It is based on the idea of:

- one primary packaging format,
- one main build artifact per architecture,
- a clear support target,
- and a realistic testing and maintenance policy.

---

## Core decision

The project should treat **AppImage as the primary Linux desktop distribution format** for the Qt-based desktop client.

The project should **not** plan to build one custom package per distro.

Instead, it should follow this model:

- build **one AppImage per CPU architecture**
- define a **primary support target**
- test on a **small set of representative distros**
- document a clear distinction between:
  - officially supported
  - officially tested
  - expected to work
  - unsupported

This is the simplest and most realistic model for a small-to-medium desktop project.

---

## Main conclusion

## Do we need separate builds for each distro?
**No, normally not.**

The intended AppImage model is:

- one downloadable artifact
- one app bundle
- broad compatibility across many Linux distributions
- compatibility achieved primarily through:
  - building on an old enough base
  - bundling correctly
  - testing sensibly

This means the project should **not** adopt a “one distro = one build” strategy unless there is a very strong reason later.

That would create a much larger maintenance burden with little benefit at this stage.

---

## Recommended support model

The project should adopt this support strategy:

### Build strategy
- one **x86_64 AppImage** as the primary release artifact
- later, optionally, one **aarch64 AppImage** if ARM desktop support becomes important

### Primary distro support target
- Ubuntu-based desktop distros

### Recommended priority list
1. Ubuntu LTS
2. Linux Mint
3. Zorin OS
4. optionally Pop!_OS or another Ubuntu-derived distro if relevant

### Secondary compatibility target
- other modern mainstream desktop distros where the AppImage is expected to work, but not treated as primary support targets

Examples:
- Fedora
- openSUSE Tumbleweed
- possibly Debian-based desktop environments beyond Ubuntu derivatives

### Unsupported target class by default
- very old distros
- very unusual desktop environments
- niche window-manager-only setups
- environments missing expected tray or desktop-integration behavior
- nonstandard hardened or enterprise-restricted environments unless tested explicitly

---

## Recommended support language

The project should be explicit in its README and release notes about what “support” means.

A good wording model would be something like:

### Officially supported
The project actively targets and troubleshoots these environments.

### Officially tested
The release has been explicitly tested on these environments before publication.

### Expected to work
The project is likely to work on these environments because of the AppImage packaging model, but they are not part of the primary support promise.

### Unsupported
The project makes no guarantee and may not investigate environment-specific issues unless they reproduce on supported targets.

This distinction is important because it keeps support expectations realistic.

---

## Build policy

## 1. Build on an old enough Ubuntu base
To maximize compatibility across modern Linux systems, the AppImage should be built on the **oldest Ubuntu LTS base that the project intentionally supports**.

This should be treated as a core build policy, not an implementation detail.

### Why this matters
Building on a newer system increases the risk that the produced binary will depend on newer runtime expectations than some target distros provide.

Building on an older still-supported Ubuntu base makes broader compatibility more likely.

### Practical rule
Do not build release AppImages on:
- the newest dev laptop
- a random rolling distro
- a newer distro than the support floor

Build on a controlled, older Ubuntu-based environment.

---

## 2. Build one AppImage per architecture, not per distro
The release model should be:

- `desktop-connector-x86_64.AppImage`
- optionally later: `desktop-connector-aarch64.AppImage`

Not:
- Ubuntu AppImage
- Mint AppImage
- Zorin AppImage
- Fedora AppImage

The project should avoid distro-specific build naming unless a future packaging split is explicitly introduced.

---

## 3. Keep packaging policy simple
At this stage, the project should avoid simultaneously supporting:

- AppImage
- `.deb`
- Flatpak
- RPM
- Snap

as first-class release formats.

That would fragment testing and support too early.

### Recommended first policy
- AppImage as the main release artifact
- source checkout optional for developers only
- no distro-specific native package commitments at first

This gives the best chance of keeping release operations manageable.

---

## Testing policy

The project should not attempt exhaustive distro coverage.

Instead, it should define a **representative testing matrix**.

## Minimum recommended test matrix

### Tier 1 — primary support targets
These should be tested before release whenever possible:

- Ubuntu LTS
- Linux Mint
- Zorin OS

### Tier 2 — cross-family sanity target
At least one non-Ubuntu desktop distro should be tested occasionally to catch packaging assumptions that are too Ubuntu-shaped.

Recommended choice:
- Fedora Workstation

This does not mean Fedora becomes a primary support target.  
It simply acts as a compatibility sanity check.

### Tier 3 — optional exploratory testing
Only if resources allow:

- KDE-based distro variant
- GNOME-based distro variant
- Wayland-heavy environment
- X11 environment
- ARM desktop device if ARM support is added later

---

## What should be tested on each distro

A release should not be considered “tested” just because the AppImage launches.

At minimum, testing should cover the app’s core desktop behavior.

## Required runtime checks

### App startup
- AppImage launches correctly
- app menu/tray startup works
- no immediate dependency/runtime crash

### Pairing flow
- pairing UI works
- QR flow works
- pairing completion works

### Transfer flow
- send file works
- receive file works
- delivery status updates work
- history updates work

### Clipboard flow
- clipboard text send works
- clipboard image send works if supported
- incoming clipboard write works

### Tray behavior
- tray icon appears
- tray menu actions work
- tray status changes are visible
- quitting via tray works

### Window behavior
- settings opens
- send-files window opens
- history window opens
- find-phone window opens if feature is enabled

### Shell integration
- opening folders works
- opening URLs works
- exported logs are accessible if applicable

### File-manager integration
Where applicable on Linux targets:
- “Send to Phone” integration installs correctly
- integration works in supported file managers

This should at least be verified on the primary support target family.

---

## Support policy by distro class

## Ubuntu-based distros
These should be treated as the main support focus.

### Why
Because this is the explicitly preferred target class and gives the project a manageable support scope.

### What this means
- issues on these distros should be taken seriously as core support issues
- release testing should prioritize them
- compatibility regressions here should block release confidence

---

## Other mainstream desktop distros
These should be treated as “expected to work, but not primary support targets.”

### What this means
- bugs may still be investigated
- especially if reproducible on multiple systems
- but environment-specific issues here do not necessarily define release support guarantees

This is a realistic and professional boundary.

---

## Edge environments
Examples:
- highly minimal setups
- unusual compositors
- distro variants with unusual tray behavior
- highly customized enterprise desktops

These should be treated as unsupported unless they become strategically important later.

This is not a weakness.  
It is a normal scope limit.

---

## Release policy

Each release should clearly state:

### 1. Primary artifact
Which AppImage file is the main supported desktop artifact.

### 2. Target architecture
For example:
- x86_64

### 3. Officially tested distros
For example:
- Ubuntu LTS
- Linux Mint
- Zorin OS

### 4. Expected-to-work distros
For example:
- other modern desktop Linux distributions
- not officially supported unless explicitly listed

### 5. Known environment limitations
For example:
- tray behavior may vary by desktop environment
- Wayland/X11 differences may affect clipboard or tray integration
- file-manager integration support is limited to specified environments

This makes release expectations much clearer.

---

## Recommended README / release-note wording

A good concise wording model could be something like:

### In README
- Primary Linux release format: AppImage
- Official support target: Ubuntu-based desktop distros
- Officially tested on: Ubuntu LTS, Linux Mint, Zorin OS
- Expected to work on other modern desktop Linux distributions, but Ubuntu-based systems are the primary support focus

### In release notes
- Includes `x86_64` AppImage
- Tested on: ...
- Known desktop-environment caveats: ...
- Report issues with distro, desktop environment, display server, and architecture included

This keeps support discussions grounded in reality.

---

## Bug-report policy

When users report AppImage issues, the project should ask for enough context to classify the environment correctly.

At minimum, ask for:

- distro name and version
- desktop environment
- Wayland or X11
- CPU architecture
- whether the issue reproduces with the released AppImage
- app log if available

This is especially important because many “Linux distro issues” are actually:

- desktop environment issues
- tray issues
- Wayland/X11 issues
- packaging or permission issues
- or environment-specific integration issues

A good bug-report template reduces wasted time.

---

## When separate packaging might make sense later

The project should remain open to adding more packaging formats later, but only when there is a clear reason.

Examples of good reasons:

- large enough user base to justify native distro packaging
- repeated demand for Flatpak distribution
- update UX becomes a major issue
- enterprise or institutional deployment needs
- Windows/macOS desktop packaging becomes part of a broader release strategy

Even then, this should be treated as an expansion of release channels, not as a reason to abandon the AppImage-first strategy too early.

---

## What not to do

## 1. Do not support every distro equally
That would create unrealistic maintenance expectations.

## 2. Do not build release artifacts on arbitrary developer machines
Release builds should come from a controlled, repeatable environment.

## 3. Do not promise support wider than the testing budget can justify
Over-promising distro support damages credibility faster than narrow but honest support wording.

## 4. Do not equate “starts once” with “supported”
Support means more than successful launch.

## 5. Do not multiply packaging formats too early
One good release channel is much better than five half-maintained ones.

---

## Risks

### 1. Ubuntu-only assumptions leaking into the app
Even with an AppImage, the app can still accidentally depend on Ubuntu-shaped runtime behavior.

This is why testing at least one non-Ubuntu distro is useful.

### 2. Desktop-environment issues being mistaken for distro issues
Tray and clipboard behavior may differ more by environment than by distro family.

The support policy should reflect that reality.

### 3. Building on too new a base
This is one of the easiest ways to damage AppImage portability.

### 4. Support promises becoming too broad
Broad distro promises create long-term burden without necessarily increasing adoption meaningfully.

---

## Recommended simplicity boundary

The correct result of this plan is not:

- “the app officially supports all Linux distros equally.”

The correct result is:

- one primary AppImage per architecture
- one primary support family
- a small tested matrix
- realistic compatibility expectations
- clear release wording
- manageable support scope

---

## Practical definition of done

This plan is being followed correctly if:

- releases produce one AppImage per architecture
- the build comes from a controlled old-enough Ubuntu base
- Ubuntu-based distros are explicitly the primary support focus
- at least a small representative distro matrix is tested
- support wording is clear in README and release notes
- bug reports collect enough environment data to classify issues properly

That is the intended operating model.

---

## Final conclusion

The right distro strategy for this project is:

- **build once per architecture**
- **support Ubuntu-based distros first**
- **test a few representative systems**
- **treat other distros as expected-to-work, not equally supported**
- **avoid one-build-per-distro thinking**

That gives the best balance of:

- professionalism
- realism
- low release complexity
- and maintainable support obligations
