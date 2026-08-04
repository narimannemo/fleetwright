# Packaging

Three ways to install this, and they are for different people.

| | Command | Use it when |
|---|---|---|
| **uv tool** | `uv tool install superagentic` | You want the CLI on your PATH, isolated from every project's environment. **The default.** |
| **uvx** | `uvx superagentic demo` | You want to run it once without installing anything. |
| **uv pip / pip** | `uv pip install superagentic` | You are importing it in Python, not running the CLI. |
| **Homebrew** | `brew install narimannemo/tap/superagentic` | You are on macOS or Linuxbrew and want it managed with everything else. |

## Why uv is the recommendation and brew is not

`uv tool install` puts the CLI in its own environment and links it onto your
PATH, on every platform, in about a second, and upgrades with
`uv tool upgrade`. Homebrew does the same on two of the three platforms and
takes considerably longer, because it builds a virtualenv from an sdist.

Homebrew exists here because a lot of people already manage their tools with
it and will not add a second tool manager to install a third tool. That is a
completely reasonable position, so the formula is maintained.

## Why there is a CLI *and* a library in one package

The library is what a worker loop imports; the CLI is what a shell fleet calls
and what `superagentic serve` runs as an MCP server. Splitting them would mean
two packages whose versions must agree, to save a few kilobytes of argparse
usage. They stay together.

## The formula is generated, not written

`packaging/brew_formula.py <version>` reads what **PyPI actually served** for
that version — the sdist URL and its sha256 — and emits the formula. The
release workflow runs it after publishing and pushes the result to
[narimannemo/homebrew-tap](https://github.com/narimannemo/homebrew-tap).

Generating from the index rather than from a local build is the whole point. A
formula names a tarball and a checksum; if either is typed from a build that is
not the published one, `brew install` fails on a checksum mismatch for someone
else, days later, and the error says nothing about why.

**Zero runtime dependencies is what keeps this file short.** A formula for a
package with dependencies needs one `resource` block per transitive
dependency, each with its own URL and checksum, each going stale on its own
schedule. There are none here.

## Setting up publishing, once

### PyPI — Trusted Publishing, no tokens

On [pypi.org](https://pypi.org) → *Your projects* → *Publishing* → **Add a
pending publisher**:

| Field | Value |
|---|---|
| PyPI project name | `superagentic` |
| Owner | `narimannemo` |
| Repository name | `superagentic` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

**The workflow name must be `release.yml`, with a `.yml`.** Writing
`release.yaml` produces `invalid-publisher` at the moment of the first upload,
after the whole matrix has already run, and the error does not say which field
is wrong.

No API token is created or stored. PyPI verifies the workflow's OIDC identity
at upload time and the credential lasts minutes.

### Homebrew tap — optional, and the release survives without it

Create `narimannemo/homebrew-tap` on GitHub (the scaffold is in this workspace).
Then add a repository secret on **superagentic**:

| Secret | Value |
|---|---|
| `TAP_TOKEN` | a fine-grained PAT with *Contents: read and write* on `narimannemo/homebrew-tap` only |

Without it the release still succeeds — the formula is attached to the GitHub
release and the job prints a notice saying it was not pushed. A release that
silently skipped the formula would be worse than one that obviously has no tap
yet.

## Cutting a release

```bash
# 1. version and changelog, in the same commit
$EDITOR src/superagentic/__init__.py    # __version__ = "0.2.0"
$EDITOR CHANGELOG.md                    # ## [0.2.0] — YYYY-MM-DD

git commit -am "0.2.0"
git push

# 2. the tag does everything else
git tag -a v0.2.0 -m "superagentic 0.2.0"
git push origin v0.2.0
```

The workflow refuses the tag if it disagrees with `__version__` or if the
CHANGELOG has no section for it, and it runs the full matrix — Linux, macOS,
Windows, and an sdist that has to pass its own tests — before anything is
published.
