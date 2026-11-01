# Releasing cadmate

## Branches and channels

| Branch | What lands there | Who runs it |
| --- | --- | --- |
| `main` | every pull request, once CI passes | the `cadmate-dev` plugin |
| `stable` | only release tags, moved by the release workflow | the `cadmate` plugin |

Work happens on feature branches and merges to `main` by pull request. `main` is the integration branch: it can hold
changes nobody has tried by hand yet. `stable` never gets commits of its own. A release fast-forwards it to a tagged
commit on `main`, so it is always a commit `main` already has, and the next release fast-forwards again with nothing
to merge back.

The marketplace file, `.claude-plugin/marketplace.json`, is read from `main`, because `main` is the default branch
and `claude plugin marketplace add arthursWizard/cadmate` reads the default branch. It lists two plugins:

- `cadmate` points at the `stable` branch on GitHub.
- `cadmate-dev` is `./`, the marketplace's own checkout of `main`.

So the marketplace file must stay on the default branch, and the default branch must stay `main`. Renaming `main`,
changing the default branch, or moving the marketplace file breaks every install that was added without a `#ref`.
Changes to the marketplace file reach users as soon as they merge to `main`, not at a release, so treat it as live.

Neither `plugin.json` nor the marketplace entries carry a `version`. Claude Code then compares commits: `cadmate`
users are offered an update when `stable` moves, and `cadmate-dev` users at every merge to `main`. A `version` in
`plugin.json` would win over both and freeze `cadmate-dev` between releases (`tests/test_packaging.py` checks that it
stays out). The version number lives in one place, `__version__` in `src/cadmate/__init__.py`, and shows in
`cadmate --version` and the `environment` tool.

## Testing a change before release

Merge it to `main`, then in Claude Code switch to the dev plugin
(see [Getting started](getting-started.md#trying-unreleased-changes)):

```sh
claude plugin marketplace update cadmate
claude plugin install cadmate-dev@cadmate      # first time only; later: claude plugin update cadmate-dev@cadmate
claude plugin disable cadmate@cadmate
```

Start a new session and try it. Switch back with `claude plugin disable cadmate-dev@cadmate` and
`claude plugin enable cadmate@cadmate`. Both plugins are named `cadmate` inside, so their tools and skills have the
same names (`cadmate:kicad-footprints` and so on). That keeps the skills' cross-references working, and it is why only
one may be enabled at a time: with both on, Claude Code treats them as a name conflict and you can't tell which
one you are running. `/plugin` shows which is enabled.

To test a branch that isn't merged yet, run it from a clone instead: check the branch out, then
`claude plugin marketplace add ./` and install `cadmate-dev@cadmate` from that local marketplace.

## Each release

1. On a branch, bump `__version__` in `src/cadmate/__init__.py`, and merge it to `main` by pull request.
2. Tag that commit on `main` and push the tag:

   ```sh
   git checkout main && git pull
   git tag v0.2.0 && git push origin v0.2.0
   ```

   The tag must be `v` plus the version, and the commit must be on `main`, or the workflow stops.
3. The `release` workflow runs the tests, builds and checks the wheel, fast-forwards `stable` to the tag, and makes a
   GitHub release. From then on `cadmate@cadmate` users are offered the update.

If the tests fail, `stable` doesn't move. Fix it on `main` and tag the next patch version; don't reuse a tag.

If something released turns out bad, release a fix the same way. Moving `stable` backwards would need a force-push, and
Claude Code would still offer it to users as an update, so going forward is the simpler path.

## One-time setup

**The `stable` branch** must exist before the `cadmate` entry can install. It was created at the 0.1.0 commit.

**Protect `stable`** in the repo's Settings > Rules > Rulesets > New branch ruleset: target `stable`, tick
"Restrict deletions" and "Block force pushes", nothing else. That keeps it fast-forward only. Don't add "Require a
pull request" or "Restrict updates": the release workflow pushes to `stable` with its own token, and those rules would
block it.

**Protect `main`** the same way, and if you want CI to gate merges, add "Require status checks to pass" with the
`unit` and `package` jobs.

## PyPI (off for now)

Publishing to PyPI is for clients that would run `uvx cadmate`. The `release` workflow skips it unless the repo
variable `PUBLISH_PYPI` is `true` (Settings > Secrets and variables > Actions > Variables). To turn it on:

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
4. Set the `PUBLISH_PYPI` variable to `true`.

PyPI never accepts the same version twice. If a release goes wrong, fix it and release the next version.

## In a fork

The `cadmate` entry names `arthursWizard/cadmate` as its source. A fork that wants its own stable channel changes that
`repo` in `.claude-plugin/marketplace.json` and in `tests/test_packaging.py`.
