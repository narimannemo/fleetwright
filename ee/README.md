# Enterprise Edition

**Empty, on purpose.** The boundary is drawn before there is anything behind
it, so it can be argued with while it still costs nothing to move.

This directory is **not** Apache-2.0. See [`LICENSE`](LICENSE) and
[`../LICENSING.md`](../LICENSING.md).

## What will go here

Things an organisation needs and one person never misses. The test is whether
the feature exists because of *other people* — auditors, a security team,
colleagues you must not be able to impersonate:

- **SSO** — SAML and OIDC, and SCIM for provisioning. The Apache core has a
  shared access token, which is right for one person and wrong for forty.
- **Audit log** — who viewed which project, who changed a kind, who cancelled
  a run. Needed the moment someone has to answer for it.
- **RBAC** — per-project roles. The core's model is "you have the token or you
  do not".
- **Retention and deletion** — policies, and the machinery to prove they ran.
- **Alerting** — PagerDuty, Slack, webhooks on a stalled fleet. `stats()` is
  Apache-2.0, so you can always build your own.

## What will never go here

Anything the core needs to be a complete tool. A fleet coordinator that cannot
coordinate a fleet without a licence is not open source with extras, it is a
demo with a paywall.

Leases, claims, briefs, runs, skills, jobs, the MCP server, the CLI and the
dashboard are Apache-2.0 and stay that way. **Nothing already released under
Apache-2.0 will move in here** — taking something back would make every future
release untrustworthy.

## Contributions

None accepted here, and that is the point: it means nobody has to sign a
contributor licence agreement for the rest of the project. Contributions to the
Apache-2.0 core are welcome under the DCO — a sign-off, not an assignment.
