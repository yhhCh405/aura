# MR Rules & Templates — v1.0 (ACTIVE)

Source: Confluence page "MR Rules & Templates — v1.0 (Active)".

## Required MR Header
Every MR must include the Flutter MR Standard badge link:

- WM Flutter MR Standard 1.0 badge

## Standalone Repositories

Checklist:
- [ ] Run dep-push and checked references (MANDATORY)
- [ ] All packages MR are approved (OPTIONAL)
- [ ] Packages references from pubspec.yaml updated with reference from develop (OPTIONAL)

## Package Repositories

Checklist:
- [ ] Compatible with current standalone apps (MANDATORY)
- [ ] Public APIs documented (OPTIONAL)
- [ ] Unit tests updated or added (OPTIONAL)
- [ ] No breaking change without version bump (OPTIONAL)

## Dependency Rule

- Package MR -> requested
- Standalone MR -> update pubspec.yaml
- Standalone MR -> requested

## Review Bot Ignore Guidance

- `pubspec.yaml` dependency reference changes (version bumps, git refs, commit hashes) are usually handled by `dep-push`.
- The review bot should **not** comment on those changes unless:
- The MR checklist is missing the mandatory `Run dep-push and checked references`, or
- The change is clearly wrong/inconsistent (e.g., breaks formatting, invalid yaml, or contradicts the stated dependency/update plan).

## Standalone MR Template

```
# [ECO-123] Feature title
[![Flutter MR Standard](https://img.shields.io/badge/WM%20Flutter%20MR%20Standard-1.0-blue)](https://wavemoneytransform.atlassian.net/wiki/spaces/MD/pages/4238049281/MR+Rules+Templates+v1.0+ACTIVE)

## Jira
Ticket: ECO-123
Link: https://jira.company.com/browse/ECO-123

## Description
What the user will get after this is released.

## Repositories in scope
Standalone:
- merchant-app (this MR)

Packages:
|Package|MR Link|Remark|
|-------|-------|------|
|flutter-core|<link>||
|inbox|<link>||
|send-money-mini-app|<link>||

## Changes
- UI changes
- State management changes
- New flows
- Package integrations

## Peer Test Guide
1. flutter pub get
2. Run app
3. Follow Jira acceptance criteria
4. Verify expected result

## Rollback Plan
Revert this MR and downgrade pubspec package versions.

## Checklist
- [ ] Run dep-push and checked references
- [ ] All packages MR are approved
- [ ] Packages references from pubspec.yaml updated with reference from develop
```

## Package MR Template

```
# [ECO-123] Package feature or fix
[![Flutter MR Standard](https://img.shields.io/badge/WM%20Flutter%20MR%20Standard-1.0-blue)](https://wavemoneytransform.atlassian.net/wiki/spaces/MD/pages/4238049281/MR+Rules+Templates+v1.0+ACTIVE)

## Jira
Ticket: ECO-123
Link: https://jira.company.com/browse/ECO-123

## Package
Repository: flutter-core

Version Impact:
- [ ] Patch
- [ ] Minor
- [ ] Major

## Description
Describe what this package change provides and which app flows it supports.

## Dependent Standalone Apps
- merchant-app
- wavepay-standalone-aar
- wave-zaysine

## Changes
- New APIs
- Bug fixes
- Refactors
- Dependency updates

## Breaking Changes
None <or describe>

## Peer Test Guide
1. flutter pub get
2. Run package tests
3. Integrate into dependent standalone app
4. Verify Jira acceptance criteria

## Rollback Plan
Revert this MR and restore previous package version or Git reference.

## Checklist
- [ ] Public APIs documented
- [ ] Unit tests updated or added
- [ ] Compatible with current standalone apps
```
