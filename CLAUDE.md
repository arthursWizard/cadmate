# Working on cadmate

Guidance for anyone, human or agent, changing this repository. Setup and tests: see "From source" in README.md
(`uv sync`, `uv run pytest`). Releases: docs/releasing.md.

## This repo is public

Everything that lands here is world-readable and stays that way: files, git history, branch names, commit messages,
PR titles and descriptions, review comments. Deleting it later doesn't unpublish it; forks and clones keep it.

- **Nothing private.** No secrets, tokens or keys (not even "temporary" ones in tests), no real email addresses, no
  absolute paths with a username (`/Users/<name>/…`, `C:\Users\<name>\…`; write `/Users/me/…` or use `tmp_path`),
  no machine names, local setup details or account IDs.
- **No personal design data.** Don't commit boards, schematics, part libraries, renders, briefs or other outputs
  from a user's own design projects, and don't name them in code, tests, docs or commits. When a real design taught
  you something, write the lesson with neutral names (`main`, `sensor`, `demo`, a generic connector) and describe
  the problem, not the project.
- **Commits and PRs are public text.** Write them for a stranger: what changed and why. No private links, internal
  notes or personal details. Commit with a GitHub noreply email, not a personal one.
- **Third-party content needs a compatible license and attribution.** KiCad's official symbol, footprint and 3D model
  libraries are CC-BY-SA 4.0 (with the KiCad library exception), not MIT. Prefer fixtures written for cadmate.
  If you copy a library file, list it in NOTICE with its source and license. Datasheet values are fine; copied
  datasheet drawings or text are not. Don't vendor or import code under licenses that don't mix with MIT (GPL, AGPL);
  call such tools as separate programs, as README's License section says.
- **Keep the MIT license intact.** Don't change LICENSE, and keep `license = "MIT"` in pyproject.toml and
  .claude-plugin/plugin.json.
- **Before you push**, read your own diff for the above, and check where every new binary or fixture file came from.
  `python .github/check_public.py origin/main..HEAD` checks the tree and your commits for home-folder paths, emails
  other than noreply ones, and keys or tokens.
- **CI enforces the patterns.** The `public` job in tests.yml runs that check on every pull request and push to
  `main`. Design and project names can't be listed in a public file without publishing them, so CI reads them from
  the `CADMATE_PRIVATE_TERMS` repository secret (one per line) and reports a hit by file and line only.
- **Every review checks this.** A review verdict covers the diff, the commit messages and the PR description against
  this section, including names from a user's own designs that no pattern or secret catches. Say so in the verdict;
  a privacy finding blocks the merge like a bug does.
