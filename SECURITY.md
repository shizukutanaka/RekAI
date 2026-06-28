# Security Policy

## Supported Versions

RekAI is pre-1.0. Security fixes are applied to the `main` branch and the most
recent tagged release.

## Reporting a Vulnerability

Please **do not** open a public issue for security vulnerabilities.

Instead, report them privately via
[GitHub Security Advisories](https://github.com/shizukutanaka/RekAI/security/advisories/new).
We aim to acknowledge reports within 72 hours and to provide a remediation
timeline after triage.

## Handling of API keys (BYOK)

RekAI is designed around **Bring Your Own Key**:

- Provider API keys are accepted per request via the `X-Provider-Key` header.
- Keys are used transiently to call the upstream provider and are **never**
  written to disk, logs, or the cache.
- The optional `rekai.security` helpers (Fernet encryption) exist only for
  deployments that explicitly choose to persist keys; this is **off by default**.

When self-hosting, always terminate TLS in front of RekAI so keys are never
sent in clear text.
