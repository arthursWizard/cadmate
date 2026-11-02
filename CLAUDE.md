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
  A quick scan of the added lines:
  `git diff origin/main | grep -nE '^\+.*(/Users/|/home/|Users\\|@[a-z0-9-]+\.(com|net|org)|token|secret|password)'`
