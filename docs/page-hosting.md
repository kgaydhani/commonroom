# Hosting the page privately (commonroom#648)

How the room's static page (`room page export`, #647) is put somewhere the owner can open from any device, and nobody
else can. Written for #639 and the publish slice #649. Scouted 25 Nov 2026.

This file names no domain, account, email or project of the owner's. Wherever one is needed it is a placeholder:

| Placeholder | Meaning | Where the real value lives |
|---|---|---|
| `<host>` | The page's address: a new subdomain of the domain he already has on the vendor, e.g. `room.<domain>` | His vendor account, and `<room home>/page/wrangler.toml` |
| `<worker-name>` | The name of the page's Worker, different from the magazine's | `<room home>/page/wrangler.toml` |
| `<his email>` | The one address allowed in | His vendor account only |
| `<team>` | His Zero Trust team name (the login page is `<team>.cloudflareaccess.com`) | His vendor account only |

The vendor is **Cloudflare**: the magazine's site is deployed from this machine with its command-line tool, Wrangler
(`npx wrangler deploy`, Workers with static assets), and that login is already on this machine.

## 1. The products, and what they cost

**Hosting: a second Worker with static assets only, on a custom domain.** A separate Worker (its own name, its own
config file, its own `[assets]` folder) is fully apart from the magazine's Worker: deploying one never touches the
other. It has no `main` script, so every request is served straight from the assets.
- Cost: **free.** "Requests to static assets are free and unlimited" and "There is no additional cost for storing
  Assets" ([Workers static assets: billing and limitations](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/),
  updated 2026-11-12). A Worker script would be billed per request and gets 429s past the free limit; this one has
  none, so that never applies.
- The page is reachable only on `<host>`: a `[[routes]]` entry with `custom_domain = true` on a zone already in his
  account ([Custom Domains](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/), updated
  2026-11-12). `workers_dev = false` turns off the `*.workers.dev` address
  ([workers.dev](https://developers.cloudflare.com/workers/configuration/routing/workers-dev/), updated 2026-11-06), and
  `preview_urls = false` turns off the version/preview addresses, which disabling `workers.dev` alone does not
  ([Preview URLs](https://developers.cloudflare.com/workers/configuration/previews/), updated 2026-11-19). Both matter:
  Access (below) guards `<host>`, and a second address left open would be a way round it.

**Access control: Cloudflare Access (part of Cloudflare Zero Trust), a self-hosted application on `<host>`.**
Access sits in front of the hostname: a browser without a valid Access session is sent to the login page and never
reaches the page. "Access applications deny by default: a user must match an Allow policy before they are granted
access" ([Publish a self-hosted application](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/),
updated 2026-04-17). The Custom Domains page names this as the way to put sign-in in front of a Worker's custom domain.
- Policy: one **Allow** rule, **Include → Emails → `<his email>`** ([Access policies](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/),
  updated 2026-11-17).
- Login method, either of:
  - **Cloudflare account login** (the default for organisations created since mid-2026, with "Restrict to account
    members" on): he signs in with the same Cloudflare login he already has
    ([Cloudflare as identity provider](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/cloudflare/),
    updated 2026-06-19).
  - **One-time PIN**: he types his email and gets a code valid for 10 minutes; the email is only sent if a policy
    allows that address. Not added automatically any more; added in Zero Trust → Integrations → Identity providers
    ([One-time PIN](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/),
    updated 2026-08-31). Recommended in addition, because it works on a phone where he is not signed in to Cloudflare.
- A session lasts **24 hours** by default, settable up to a month
  ([Session management](https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/session-management/),
  updated 2026-11-10).
- Cost: **free** on the Zero Trust Free plan, $0, up to 50 users
  ([Zero Trust plans](https://www.cloudflare.com/plans/zero-trust-services/)). **But the vendor asks for payment
  details even on the Free plan**: "If you chose the Zero Trust Free plan, this step is still needed but you will not be
  charged" ([Create a Zero Trust organization](https://developers.cloudflare.com/cloudflare-one/setup/), updated
  2026-10-12). If his account already has a Zero Trust organisation, this was done before and is skipped. I could not
  check which: this machine's Wrangler login has no Access or Zero Trust permission (its scopes cover Workers, KV,
  routes, Pages and zone read, nothing for Access), which also means the room cannot create the Access rule itself.

**Total money: none.** One card entry in the vendor's dashboard if no Zero Trust organisation exists yet, with no
charge on the Free plan.

## 2. The publish command a beat runs

One-time setup, by the director's interactive session (not by a worker, and never in this repo): a folder in the room
home holding the page's own Wrangler config.

`<room home>/page/wrangler.toml`:

```toml
name = "<worker-name>"
compatibility_date = "2026-11-01"
workers_dev = false          # no *.workers.dev address
preview_urls = false         # no version/preview addresses

[assets]
directory = "./public"       # what `room page export` writes
not_found_handling = "404-page"

[[routes]]
pattern = "<host>"
custom_domain = true
```

No `main`: static assets only, so it is free and `_headers` rules apply (they are not applied to responses a Worker
script makes; [Headers](https://developers.cloudflare.com/workers/static-assets/headers/), updated 2026-11-12).

The command a beat runs after `room page export <room home>/page/public`:

```sh
npx --yes wrangler@4 deploy --config "<room home>/page/wrangler.toml"
```

`wrangler deploy` takes `--config` for the config path ([Wrangler commands: deploy](https://developers.cloudflare.com/workers/wrangler/commands/workers/),
updated 2026-10-26). Deploying a bare folder without a config file "only works in interactive mode (so not in CI
systems)", so a beat needs the config file above. The deploy replaces the whole asset set, so a file the export no
longer writes disappears from the site too.

**Credentials, found without anything in this repo.** Wrangler looks for, in order: `CLOUDFLARE_API_TOKEN` in the
environment, then the API key/email pair, then its OAuth login ([Wrangler general commands](https://developers.cloudflare.com/workers/wrangler/commands/general/),
updated 2026-09-02; [System environment variables](https://developers.cloudflare.com/workers/wrangler/system-environment-variables/),
updated 2026-11-12).
- **Default: the OAuth login that is already here.** The magazine's `wrangler login` lives in the Windows user profile
  (`%APPDATA%/xdg.config/.wrangler/config/default.toml`, or in the OS keychain if `--use-keyring` was chosen). It holds
  a refresh token (`offline_access`), so the short-lived access token renews itself. The beat runs as the same Windows
  user, so it finds it with nothing configured. If the refresh ever lapses, the publish fails with one logged line and
  the fix is `npx wrangler login` in an interactive session.
- **If that proves flaky: a scoped API token** made in the dashboard from the "Edit Cloudflare Workers" template,
  limited to his account and the one zone ([Create API token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/),
  updated 2026-11-06), set as a Windows user environment variable `CLOUDFLARE_API_TOKEN`. Never in this repo, never in a
  `room.toml`. This is an owner step, so it is the fallback, not the plan.
- `CLOUDFLARE_ACCOUNT_ID` is only needed if the login can see more than one account.

## 3. The owner's one step

Do this **before** the room publishes for the first time; otherwise the page is open to anyone for as long as the gap
lasts. From his phone, in Chrome, signed in at `dash.cloudflare.com`, about five minutes:

1. Open **Zero Trust** from the account's side menu. If it asks for a team name and a plan: type any short team name,
   pick **Free**, enter card details (Free is not charged). If it opens straight to the Zero Trust overview, skip this.
2. Go to **Access controls → Applications → Create new application → Self-hosted and private → Add public hostname**.
   Subdomain: the name the director gives him (e.g. `room`). Domain: pick his domain from the list.
3. Under **Access policies → Create new policy**: name it `owner`, Action **Allow**, Include **Emails**, enter his own
   email. Save the policy.
4. Under login methods, leave **Cloudflare** ticked, and tick **One-time PIN** if it is listed (if it is not, add it
   once under **Integrations → Identity providers → Add new → One-time PIN**). Then **Create**.
5. Tell the director "done" with the subdomain he used. Nothing else: the hostname, the DNS record and the deploys are
   the room's side.

## 4. Testing it

The director runs these after the owner's step and after the first publish. `<host>` and `<worker-name>` come from the
room home config.

**Nobody signed in gets nothing.**
- `curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" https://<host>/` prints `302` and a URL on
  `<team>.cloudflareaccess.com`. Anything that is `200` fails.
- The same for `https://<host>/robots.txt` and `https://<host>/index.html`: `302` to the login, never the file. (Access
  guards the whole hostname, so even `robots.txt` is behind it.)
- `curl -s -I https://<worker-name>.<account subdomain>.workers.dev/` must not return `200` with the page
  (`workers_dev = false`).
- `curl -s https://<host>/ | grep -c "<a string only the page has, e.g. its build-time label>"` prints `0`.
- On his phone in a private/incognito tab: the address shows the Cloudflare login, and a different email address never
  receives a code.

**He gets the page.** In a normal tab on his phone: sign in once (Cloudflare login or code), the page shows with its
build time; open it again within 24 hours and it shows without a login.

**It is not indexed.** Three layers, each checkable:
1. A crawler never sees the page: it gets the `302` above.
2. The page itself carries `<meta name="robots" content="noindex, nofollow">` and a `robots.txt` that disallows
   everything (#647's export; its tests check them).
3. Optionally, `public/_headers` with `/*` → `X-Robots-Tag: noindex` (applies because there is no Worker script).
   Check signed-in in the browser's network panel.
A week after launch, a web search for `site:<host>` returns nothing.

## 5. Why static, not a tunnel

A Cloudflare Tunnel from the local dashboard to `<host>` behind the same Access rule would have given a live page with
its controls (pause, notes), current to the second rather than to the last beat.
It would need a new download (`cloudflared`) running as a service, an inbound path to this machine, the write controls
exposed off-machine, and a page that is gone whenever the machine is off or asleep.
The static export needs no new software, no open path to the machine and works with the machine off; one beat of
staleness is the price, and the page says when it was built.

## Not verified

- Nothing here has been deployed: the Access rule does not exist yet, so no test above has been run.
- Whether his account already has a Zero Trust organisation (and so whether step 3.1 asks for a card): this machine's
  login cannot see it.
- The dashboard's menu labels were read from the docs, not clicked through on a phone. The vendor renames them often
  ("Access" is now "Access controls"); if a label differs, the step's order still holds.
