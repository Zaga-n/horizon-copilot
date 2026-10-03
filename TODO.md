# TODO

## GitHub CI activation

The CI workflows are configured locally. Populate the placeholders below when
the GitHub repository and target branch are chosen.

| Setting | Placeholder |
| --- | --- |
| GitHub repository | `<GITHUB_OWNER>/<GITHUB_REPOSITORY>` |
| Git remote URL | `https://github.com/<GITHUB_OWNER>/<GITHUB_REPOSITORY>.git` |
| Protected branch | `<DEFAULT_BRANCH>` |

- [ ] Populate the repository, remote URL, and branch above.
- [ ] Configure the Git remote and push the code and `.github/workflows/` files.
- [ ] Complete the first GitHub Actions run and resolve any runner-specific failures.
- [ ] Configure branch protection or a ruleset for the chosen branch to require
      pull requests and all status checks listed in the
      [CI section of the testing guide](docs/guides/testing.md#ci).
- [ ] Verify that a pull request with a failing required check cannot merge.
- [ ] Verify that test reports, coverage XML, and Playwright reports/traces can
      be downloaded from the workflow artifacts.

Keep workflow job names aligned with the required checks. GitHub merge protection
remains pending until the repository settings are configured.

## Chunking and retrieval

- [ ] Adopt a more sophisticated chunking technique and retrieval approach
      (e.g. hybrid retrieval).

## System improvement

- [ ] Use LLM traces and user feedback for system improvement.
