# Releasing cadmate

A release is one git tag. The `release` workflow builds cadmate, runs the tests, checks the wheel runs from a clean
environment, publishes it to PyPI and makes a GitHub release with the files attached.

## Each release

1. Bump the version in three places, to the same number:
   - `src/cadmate/__init__.py` (`__version__`; the package version is read from here)
   - `.claude-plugin/plugin.json` (`version`)
   - `.claude-plugin/marketplace.json` (the cadmate entry's `version`)

   `tests/test_packaging.py` fails if they disagree. The plugin version matters on its own: Claude Code offers plugin
   users an update only when it changes.
2. Merge that to `main`.
3. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`. The tag must be `v` plus the version, or the workflow
   stops before publishing.

PyPI never accepts the same version twice. If a release goes wrong, fix it and release the next version.

## One-time setup (before the first release)

1. Make an account on [pypi.org](https://pypi.org) and turn on two-factor authentication.
2. Add a pending trusted publisher: on PyPI, Your projects > Publishing > Add a new pending publisher, GitHub tab:
   - PyPI project name: `cadmate`
   - Owner: the GitHub owner of the repo; Repository name: `cadmate`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   This lets the workflow publish without any token stored in GitHub. A pending publisher doesn't reserve the name:
   until the first release is up, someone else could still register `cadmate`.
3. In the GitHub repo, Settings > Environments > New environment, named `pypi`. Adding yourself as a required reviewer
   there makes each publish wait for your click, which is a cheap safety net.
