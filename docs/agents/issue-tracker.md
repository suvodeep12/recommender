# Issue tracker: GitHub

Issues and specifications for this repository live in GitHub Issues:

https://github.com/suvodeep12/recommender/issues

Use the `gh` CLI for issue operations. Run commands from this repository so GitHub can infer the repository.

## Conventions

- Create: `gh issue create --title "..." --body "..."`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels,comments`
- Comment: `gh issue comment <number> --body "..."`
- Add a label: `gh issue edit <number> --add-label "label"`
- Remove a label: `gh issue edit <number> --remove-label "label"`
- Close: `gh issue close <number> --comment "..."`

Pull requests are not a triage request surface for this repository.

## Wayfinding

If a wayfinding map is created, use one GitHub issue labeled `wayfinder:map`. Child work belongs in linked GitHub issues labeled with the relevant `wayfinder:` type.

Use GitHub issue dependencies when available. If dependencies are unavailable, record blockers at the top of the child issue as `Blocked by: #<number>`.
