# Releasing OpenFOIA

Releases are cut by merging a release branch into `main`. The merge is the
release: `.github/workflows/release.yml` tags the merge commit `vX.Y.Z` and
publishes the GitHub release, with the notes taken from `CHANGELOG.md`.

Nothing is published anywhere else. There is no PyPI upload; users install
from the repository or a tag.

## Cutting a release

Versions follow [Semantic Versioning](https://semver.org/): a bug-fix-only
release bumps the patch (`4.2.0` → `4.2.1`), a release that adds commands or
options bumps the minor (`4.2.0` → `4.3.0`), and one that removes or breaks
behavior bumps the major.

1. Branch from an up-to-date `main`, named for the version:

   ```bash
   git switch main && git pull
   git switch -c release/4.3.0
   ```

   `release/v4.3.0` and `release/release-4.3.0` work too. The version must be
   three numbers; `release/4.3` is rejected.

2. Set the version in **both** places:

   - `pyproject.toml` → `version = "4.3.0"`
   - `openfoia/__init__.py` → `__version__ = "4.3.0"`

3. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [4.3.0] - YYYY-MM-DD`
   (or add that section), and add the compare link at the bottom:

   ```
   [4.3.0]: https://github.com/JordanCoin/openfoia/compare/v4.2.0...v4.3.0
   ```

   Everything in that section, down to the next `## [` heading, becomes the
   GitHub release notes, so write it for the people who will read the release
   page. Say exactly what changed, including what is still not protected; see
   `docs/THREAT_MODEL.md`.

4. Open a pull request from `release/4.3.0` into `main`. Besides the normal CI,
   a **Release check** job runs and fails if:

   - the branch name, `pyproject.toml` and `__version__` disagree
   - `CHANGELOG.md` has no non-empty `## [4.3.0]` section
   - tag `v4.3.0` already exists

   Its job summary shows a preview of the release notes.

5. Merge the pull request. The **Publish release** job then:

   1. checks out the exact commit the merge put on `main`
   2. re-runs the release check, lint, format check and test suite on that
      commit, the same selection as `ci.yml`
   3. creates tag `v4.3.0` on that commit and publishes the release titled
      `OpenFOIA v4.3.0`

   If any step fails, nothing is tagged or published.

Edit the release title or notes on GitHub afterwards if you want a friendlier
headline; the tag and commit are what matter.

## If publishing fails after the merge

The version is already on `main`, so do not open a second release branch for
it. Fix the cause on `main` through a normal pull request, then run the
workflow by hand: **Actions → Release → Run workflow**, branch `main`,
version `4.3.0`. It performs the same checks and refuses a version that
already has a tag.

## What the workflow will not do

- **Re-tag or overwrite a release.** If `vX.Y.Z` exists, it stops. Bump the
  version instead of moving a published tag.
- **Release from a fork.** Only branches in this repository trigger it.
- **Build or attach `pdf-extract` binaries.** Those are built by the separate
  glyph-api pipeline and uploaded to a release by hand. `install.sh` searches
  every release, newest first, for them, so a release without binaries still
  installs correctly using the most recent ones.
- **Pre-releases.** Only plain `X.Y.Z` versions are supported. SemVer writes
  `4.3.0-rc.1` where Python packaging writes `4.3.0rc1`, and a tag that
  disagrees with the installed version is worse than not having release
  candidates.

## Checking a release locally

```bash
python scripts/release.py --branch release/4.3.0 --notes-out notes.md
```

It prints the version on success, or lists every inconsistency at once.
