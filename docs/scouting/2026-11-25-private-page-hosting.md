# Private hosting for the room's page (commonroom#648)

Scouted 25 Nov 2026. The full write-up, with every vendor page cited and its date, is [docs/page-hosting.md](../page-hosting.md).

**What it is.** The page from `room page export` (#647) is deployed after each beat as a second, static-assets-only
Cloudflare Worker on a new subdomain of the owner's domain, behind a Cloudflare Access rule that lets in his email only.
The magazine already deploys to the same vendor with Wrangler from this machine, so the login exists.

**Evidence.** Static asset requests on Workers are free and unlimited; Access denies by default and is $0 on the Zero
Trust Free plan (up to 50 users), which still asks for card details at signup; `workers_dev = false` and
`preview_urls = false` close the side addresses Access would not cover. Links in the doc.

**What it would change.** The owner opens one address on any device and sees the room; nothing about his domain or
account is in this repo; no new software on this machine.

**Cost.** No money. One owner step in the vendor dashboard (about five minutes on a phone), which may include entering
card details if no Zero Trust organisation exists yet. Flagged for his approval: that card entry, and that the room
deploys a second Worker into his account.

**Unproven.** Nothing is deployed yet. Whether his account already has a Zero Trust organisation is unknown: this
machine's Wrangler login has no Access permission, so it cannot see or create Access rules.

**Experiment.** After the owner's step and #649:
- Pass: `curl` of `https://<host>/` and `https://<host>/robots.txt` without a session both return `302` to
  `<team>.cloudflareaccess.com`; the `workers.dev` address does not serve the page; he opens the page on his phone after
  one login and sees its build time within one beat of the last run.
- Fail: any of those returns the page's content without a login, or the publish needs a credential stored in this repo.
