# Changelog

All notable changes to `musubi-claude` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0](https://github.com/sourceblender/musubi-claude/compare/v0.4.0...v0.5.0) (2026-09-26)


### Features

* /musubi-claude:setup and /musubi-claude:health ([#8](https://github.com/sourceblender/musubi-claude/issues/8)) ([1be3b00](https://github.com/sourceblender/musubi-claude/commit/1be3b0027087799e04374ec36c0d866168d5e688))
* configure musubi-claude from Claude Code's /config (userConfig) ([#3](https://github.com/sourceblender/musubi-claude/issues/3)) ([fcc8d8b](https://github.com/sourceblender/musubi-claude/commit/fcc8d8b0c2c56deba5de67cc3e6f7d71fbaec610))
* installable from scratch — explicit harness setup, offline launcher ([#6](https://github.com/sourceblender/musubi-claude/issues/6)) ([7bb6b16](https://github.com/sourceblender/musubi-claude/commit/7bb6b16c6f925d5453b4d4bb2e653db664331f2f))
* live thoughts from other agents, delivered into the session (monitor) ([#16](https://github.com/sourceblender/musubi-claude/issues/16)) ([95bc739](https://github.com/sourceblender/musubi-claude/commit/95bc73971d5392d61059d1708073aec6b6437395))
* memory survives /compact (PreCompact checkpoint → SessionStart restore) ([#14](https://github.com/sourceblender/musubi-claude/issues/14)) ([aa63f6c](https://github.com/sourceblender/musubi-claude/commit/aa63f6c1830455251cfc5caba7feb646717a5a93))
* Musubi URL and token settings; the token reaches only the drain ([#11](https://github.com/sourceblender/musubi-claude/issues/11)) ([21356d8](https://github.com/sourceblender/musubi-claude/commit/21356d8a7347bb1cb9d6aea3547abbc77f96eb53))
* prompt-aware recall (UserPromptSubmit) ([#17](https://github.com/sourceblender/musubi-claude/issues/17)) ([0b0329c](https://github.com/sourceblender/musubi-claude/commit/0b0329c6244bbd556c1de6cd673743f1afae5734))
* steward agent, /musubi-claude:recall and :remember (+ honest eval scoring) ([#12](https://github.com/sourceblender/musubi-claude/issues/12)) ([91103bd](https://github.com/sourceblender/musubi-claude/commit/91103bdea41115dd3745da1988672426d95f3cd1))


### Bug Fixes

* keep plugin state in CLAUDE_PLUGIN_DATA without splitting a live outbox ([#5](https://github.com/sourceblender/musubi-claude/issues/5)) ([d2e8f73](https://github.com/sourceblender/musubi-claude/commit/d2e8f73f39e748ae0bcac19a3e8c1040c675f419))
* recall fetches every candidate, so short memories get dates and lifecycle ([#18](https://github.com/sourceblender/musubi-claude/issues/18)) ([4384fba](https://github.com/sourceblender/musubi-claude/commit/4384fba0303b33f51a0fd47a2215276bed576546))
* refuse an outdated musubi-harness visibly instead of crashing ([#15](https://github.com/sourceblender/musubi-claude/issues/15)) ([5c7f627](https://github.com/sourceblender/musubi-claude/commit/5c7f627d1ed7c9d7dd90fed3407f84167bfe9d6b))
* **skills:** continuity reads the health report; health answers one event by id ([#9](https://github.com/sourceblender/musubi-claude/issues/9)) ([93a877b](https://github.com/sourceblender/musubi-claude/commit/93a877b818ecbb59c1045ae152b7125f769e98f8))
* **skills:** generic recall and continuity skills for any user ([#1](https://github.com/sourceblender/musubi-claude/issues/1)) ([a8814d0](https://github.com/sourceblender/musubi-claude/commit/a8814d04f7a699f3b19468ec759dc406fc9d217b))
* the version gate reads the interpreter the venv runs, not any lib/python* ([#20](https://github.com/sourceblender/musubi-claude/issues/20)) ([533f7af](https://github.com/sourceblender/musubi-claude/commit/533f7af73f2c836d56e766e6f920957eda5959a9))


### Documentation

* plugin and marketplace descriptions name what it does today ([#21](https://github.com/sourceblender/musubi-claude/issues/21)) ([7839711](https://github.com/sourceblender/musubi-claude/commit/7839711e59241d6c6edd372e8704520aeb409915))
* README for what the plugin is today ([#13](https://github.com/sourceblender/musubi-claude/issues/13)) ([f45a48a](https://github.com/sourceblender/musubi-claude/commit/f45a48a0a5a5a96240afaf74a65f90184a8484a1))
* **skills:** shadow-mode remembers stay pending (not sent, not lost) ([#10](https://github.com/sourceblender/musubi-claude/issues/10)) ([d8c72c3](https://github.com/sourceblender/musubi-claude/commit/d8c72c3ffe798538d0011d477c8a2484e48a4ee5))

## [Unreleased]

## [0.4.0] - 2026-09-26

### Changed
- **First standalone public release.** Moved out of
  `~/Vaults/fleet-tools/plugins/musubi-claude/` into its own public
  repo at `github.com/sourceblender/musubi-claude`.
- **Runtime extraction.** The `musubi_harness` shared runtime the
  adapter depended on at `../lib/` (fleet-tools layout) is now the
  real PyPI package `musubi-harness>=1.0.0`. The
  `parents[3]/lib/musubi_harness/` filesystem walk is gone — the
  harness is imported the normal way. Same identity, namespace,
  delivery-mode, and canonical-tool policy; same `MUSUBI_HARNESS_BIN`
  env override and `harness_bin` config override escape hatches.

### Added
- `.claude-plugin/marketplace.json` so users can install via
  `claude plugin marketplace add sourceblender/musubi-claude &&
  claude plugin install musubi-claude@sourceblender`.
- Apache-2.0 LICENSE.
- `tests/test_runtime_binding.py` — verifies the binding exposes
  the harness contract and contains no `parents[3]/lib` filesystem
  walk.
- `tests/test_stop_envelope.py` — exercises the Claude-specific Stop
  hook payload → envelope projection against the production defects
  that motivated it (event id stability, alias conflict refusal,
  `isMeta` fallback, ambiguous-prompt refusal, transcript-session
  mismatch refusal, fail-open degraded sink).

### Migration from `0.3.x`
- `pip install musubi-harness` is now required (Claude Code does
  this automatically when the plugin loads; for local development
  it's part of the dev install).
- `pip install -e ../musubi-harness` is no longer required for local
  development.
- The fleet-tools copy at `~/Vaults/fleet-tools/plugins/musubi-claude/`
  continues to work as the development head during the transition
  window. Any new fix lands here first, then backports.

[Unreleased]: https://github.com/sourceblender/musubi-claude/compare/HEAD
[0.4.0]: https://github.com/sourceblender/musubi-claude/releases/tag/v0.4.0
