# Licensing

**Everything in this repository is Apache-2.0 except the `ee/` directory.**

| | Licence | You may sell a service built on it |
|---|---|---|
| Everything else — leases, CLI, MCP server, dashboard, runs, skills | [Apache-2.0](LICENSE) | **Yes** |
| [`ee/`](ee/) | [commercial](ee/LICENSE) | No |

This is the open-core model — the same shape Langfuse, GitLab and Grafana use.

## What that means in practice

**You can run FleetWright, for anything, forever, without asking.** Including
inside a company, including as part of a product you sell, including as a
managed service you charge for. Apache-2.0 grants that and it is not revocable.

**The `ee/` directory is different.** Its source is published so you can read
and audit it — a component that touches your authentication and your audit
trail should not be a binary — but running it in production needs a licence.

## Where the line is, and why

The boundary is deliberate, and it is not "the useful parts cost money."

**The core has to be complete on its own.** A fleet coordinator that cannot
coordinate a fleet unless you pay is not open source with extras, it is a demo.
Everything needed to run agents over a corpus and see what happened is
Apache-2.0 and stays that way: leases, claims, briefs, runs, skills, the MCP
server, the CLI, the dashboard.

**`ee/` is for what an organisation needs and one person never misses.** The
test is whether the feature exists because of *other people* — auditors,
compliance, a security team, colleagues you must not be able to impersonate:

| Belongs in `ee/` | Stays Apache-2.0 |
|---|---|
| SSO / SAML / SCIM | the shared access token |
| Audit log of who viewed or changed what | the dashboard itself |
| Role-based access control | projects, runs, jobs |
| Retention and deletion policies | the SQLite file you own |
| Alerting to PagerDuty / Slack | `stats()`, from which you can build your own |
| Support with a response time | the issue tracker |

**A feature never moves from Apache-2.0 into `ee/`.** Anything released under
Apache-2.0 stays there. Taking something back would make every future release
untrustworthy, and it is the specific move that turns a community against a
project.

## Contributing

Contributions go to the **Apache-2.0 core**, under the
[DCO](CONTRIBUTING.md#developer-certificate-of-origin) — a sign-off line, not a
copyright assignment.

**`ee/` does not accept outside contributions.** That is not unfriendliness: it
is what removes the need for a contributor licence agreement over the rest of
the project. Nobody signs away rights so that one directory can be commercial.

## Questions people reasonably ask

**Can I fork it?** Yes — everything outside `ee/`, for any purpose, including
commercial.

**Can I host it and charge for it?** Yes. Apache-2.0 permits exactly that. What
you cannot do is ship the `ee/` features without a licence.

**Will the core be relicensed later?** Every version already published stays
Apache-2.0 permanently; that cannot be undone and would not be attempted. If
the licence for future versions ever changed it would be announced, argued for,
and be a fork-able moment — which is the point of the guarantee.

**Is `ee/` empty right now?** Yes. The boundary is drawn before there is
anything to put behind it, so it can be argued with while it costs nothing.
