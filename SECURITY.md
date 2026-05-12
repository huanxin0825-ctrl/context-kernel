# Security Policy

Context Kernel can read files, write files, execute local commands, and call model providers. Treat it as developer tooling with access to sensitive local context.

## Supported Versions

The project is currently pre-1.0. Security fixes target the latest commit on the default branch.

## Reporting A Vulnerability

Please do not publish exploit details in a public issue.

Preferred reporting path:

1. Use GitHub private vulnerability reporting if it is enabled for the repository.
2. If private reporting is not available, open a public issue asking for a maintainer contact path without including exploit details.

Include:

- affected command or module
- impact and reproduction steps
- whether secrets, local files, provider credentials, or command execution are involved
- suggested fix, if known

## Secret Handling

Do not commit:

- `.env`
- API keys or provider tokens
- local `.akernel` state
- raw provider responses containing private data
- benchmark fixtures derived from private repositories without redaction

The CLI should avoid printing API keys. If you find a path that leaks secrets into traces, reports, or command output, treat it as a security issue.
