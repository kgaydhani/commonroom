Director run for {{repo}}. Milestone: {{milestone}}.
Triggers: {{reason}}
Follow your director instructions. Prefix every comment with `[agent:director]`. Repo file changes go through a branch and PR, never a push to main. End with one line: `ACTIONS: [...]`, a JSON list of short strings naming each board change you made (issues opened, closed or relabelled, PRs merged or bounced, owner tasks). Empty: nothing changed.
