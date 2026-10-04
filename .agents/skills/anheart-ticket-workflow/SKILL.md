---
name: anheart-ticket-workflow
description: Apply the user's Linear branch and commit traceability conventions when starting or versioning software tickets in the Anheart repository. Preserve the existing ANH-71 PR's legacy branch.
---

# Anheart ticket traceability

Use this skill for ticket branches, commits, PR titles, and handoffs in Anheart.
These are the user's conventions confirmed on 3 October 2026, not permission
to commit, publish, merge, rename remote branches, or rewrite history.

## Future ticket branches

Read the actual Linear issue before creating a branch. Use its real issue key
and current `gitBranchName`, in the form
`feature/<lowercase-ticket-key>-<descriptive-kebab-case-slug>`.
Prefer the exact generated name so Linear can connect the branch to the ticket.

`feature/eng-123-fix-login` is an example, not an instruction to invent an ENG
ticket. If Linear returns `ANH-72`, use that key; do not substitute another
team's prefix. If its generated branch conflicts with this convention, resolve
the discrepancy before publishing instead of changing Linear settings silently.
Keep the project's `main` and `develop` branch roles unchanged.

## Commits and PRs

Start new commit subjects with the actual ticket key, for example
`ANH-72: add required CI gates`. Every commit must be identifiable by its
ticket. Existing commits that already include the ticket key remain valid;
do not rewrite published history merely to move it to the beginning.
Do not guess ticket associations for pre-existing commits or retrofit their history.

Keep changes attributable to one ticket and make logical, verified commits.
Use the real key in the PR title and link the Linear issue in its description.
Record the issue, exact branch, full commit SHAs, and PR URL in the handoff or
the repository's `docs/suivi-tickets.md` when updating that ledger. Do not mark
a ticket complete merely because its commits have been pushed.

## Existing ANH-71 exception

The user explicitly grandfathered PR #3 and its current branch:
`mohamdimagh1/anh-71-commiter-et-relire-tout-le-travail-en-cours-branche-featpi`.

Keep that legacy branch for this work. Do not replace or close PR #3 to satisfy
the new format. This exception does not extend to future ticket branches.
Continue to include `ANH-71` in its commits; start new ones with `ANH-71:`.
