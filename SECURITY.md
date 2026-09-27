# Security Policy

## Supported versions

Security fixes target the latest released version of `musubi-claude`. This
plugin is still pre-1.0; older release lines are not maintained. Please check
whether the issue reproduces on the latest release or `main` when you can do
so without putting real memory or credentials at risk.

## Reporting a vulnerability

Please do not open a public issue for a security vulnerability. Report it
privately by emailing `ericmey@gmail.com`. Include:

- The affected version or commit and the host environment.
- What can go wrong, and the shortest safe steps to reproduce it.
- Whether credentials, agent identity, conversation text, or stored memory
  could cross a boundary.
- Your intended disclosure timeline, if you have one.

Do not send live tokens, transcripts, or memory payloads. Redact them and use
synthetic examples. We aim to acknowledge reports within three business days
and to provide a fix or mitigation plan within 14 days for high-impact issues.
We will coordinate disclosure with you.

## Scope

This policy covers the `musubi-claude` Claude Code plugin: its hooks, MCP
integration, setup flow, local configuration and state, recall, capture, and
verified delivery. Identity and token scoping, prompt injection through
recalled text, and accidental logging of sensitive content are in scope.

The shared `musubi-harness` package is maintained in
[sourceblender/musubi-harness](https://github.com/sourceblender/musubi-harness).
The Musubi server and other host adapters have their own repositories.
Use the affected repository's security policy when it has one. If it has no
local policy, or the boundary is unclear, use the private contact above and
we will route it.
