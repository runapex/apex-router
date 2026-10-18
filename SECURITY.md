# Security policy

## Security maintainer

Juri Kern is the security maintainer of apex-router. The security maintainer triages each report, fixes confirmed issues and publishes the advisory.

## Report a vulnerability

Report privately with GitHub's "Report a vulnerability" button on the repository's Security tab.
Do not open a public issue for a vulnerability.

Include the affected version or commit, the steps to reproduce and the impact you expect.

## What happens next

| Step | Target |
|---|---|
| First reply | 3 working days |
| Triage result (confirmed or not) | 10 working days |
| Fix or mitigation for a confirmed issue | 30 days, or a date we agree with you |
| Advisory | Published with the fix, with credit to you unless you ask otherwise |

## Supported versions

Only the latest commit on `main` gets security fixes.

## Scope

In scope: the code in this repository, including the proxy, the router, `codeqa`, the mesh and its worker.
Out of scope: third-party services and models that apex-router calls, and local setups that change the default settings.

See "Security posture" in the README for the design rules.
