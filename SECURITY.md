# Security policy

## Reporting a vulnerability

Use GitHub Private Vulnerability Reporting for security reports:

1. Open the repository's **Security** tab.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

Include the affected component, reproduction steps, expected impact, and any suggested mitigation.
Do not publish exploit details, credentials, personal data, or internal identifiers in a GitHub issue.

## Scope

Useful reports include authentication or authorization bypasses, injection paths, unsafe handling of
uploaded content, accidental disclosure of sensitive information, and exploitable dependency issues.
The credential-free `reference` profile is intended for local evaluation and is not an
internet-facing deployment configuration.

## Supported code

Security fixes apply to the current default branch. Historical snapshots and modified copies are not
supported.

## Dependency checks

CI runs `npm audit --audit-level=high` for both clients. Moderate and low findings
remain visible in the audit output. Backend requirements pin direct dependencies;
transitive Python packages are resolved at install time and need a fresh inventory
scan before a connected deployment. Local tests do not establish provider security.
