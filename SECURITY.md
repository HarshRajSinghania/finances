# Security

## The design

- Statements, exports and the SQLite store stay on your machine. The only
  network calls the project makes are to a model server on localhost, and only
  for `ask`, `enrich` and `parse --llm`.
- `finances web` binds to 127.0.0.1 and sends no CORS headers. Requests whose
  `Host` or `Origin` is not localhost are refused (403), which blocks other
  websites in the same browser and DNS-rebinding attacks.
- Writes (budgets, goals, subscription marks) require an `application/json`
  body, which a cross-site form cannot send without a preflight this server
  never answers.
- `finances ask` runs model-written SQL on a read-only connection behind a
  SQLite authorizer that allows only SELECT.
- The Docker image publishes its port on 127.0.0.1 only, and `.dockerignore`
  is a whitelist, so personal files never reach the build context.

## Reporting a problem

Please do not open a public issue for a vulnerability. Use GitHub's
[private vulnerability reporting](https://github.com/roshanlam/finances/security/advisories/new)
for this repository. Never attach real statements; reproduce with
`finances demo` data or a redacted file.
