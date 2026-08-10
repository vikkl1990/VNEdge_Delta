# Security audit — 2026-08-04

## Executive finding

No evidence of a malicious contributor, embedded credential, live-order
bypass, or dependency compromise was found in this review. The unfamiliar
GitHub account has **no current repository access**. Its two attributed commits
are unsigned research/shadow changes and contain no secret access, shell
execution, dependency modification, network exfiltration, or live order route.

The repository's larger risk is provenance and governance: `main` is
unprotected, commits are not required to be signed, and GitHub secret scanning,
push protection, and Dependabot security updates are disabled. Those controls
should be enabled before any live-capital deployment.

## Scope and method

The review covered:

- current GitHub collaborators, contributors, branch protection, Actions, and
  repository security settings;
- the exact diffs and metadata of the unfamiliar contributor's commits;
- current source and Git history for common credential patterns;
- Python and frontend dependency vulnerability databases;
- Python static security analysis across `src/vnedge`;
- live-trading gates, dynamic-code loading, URL access, containers, and tracked
  secret/config files.

This is a source and repository-configuration audit, not a host-forensics or
exchange-account audit. It cannot prove who physically used a credential or
typed an unsigned commit.

## Contributor attribution

GitHub currently reports one collaborator: `vikkl1990` with administrator
access. `claude` and `xxxxxxxxxxxxx` appear in the contributor graph but are not
collaborators and have no current push permission.

The unfamiliar contribution is local Git metadata `x <x@x>`, which GitHub maps
to `xxxxxxxxxxxxx` by email association. Both commits are unsigned:

| Commit | Change | Security review |
| --- | --- | --- |
| `eb4734889d958e6dbde3e0f9eab0d30e494e2f96` | locked research-winner to shadow-manifest bridge | Default-off (`MULTI_LANE_MANIFEST_ENABLED=0`); output remains `can_trade=false`, `can_promote=false`; no execution route |
| `e55d019cf6221821a211dfc6b9fe8c5e65ba3db6` | restart-safe L2 research checkpoints | Local atomic JSON checkpoint/resume logic; no secrets, commands, dependencies, or trading route |

Because `main` is unprotected and only the owner currently has push permission,
the commits were either pushed by the owner, by local tooling using the owner's
credentials, or by someone with access to those credentials at that time.
Unsigned metadata cannot distinguish those cases. If the author is genuinely
unrecognized, rotate GitHub and exchange/API credentials and review GitHub's
security log; source review alone cannot rule out credential access outside the
repository.

## Findings

### High — default branch has no protection

The GitHub API reports `main` as unprotected. A credential with push access can
write directly to the production history without review, required tests, or
signed provenance. This is the condition that makes an unfamiliar unsigned
commit difficult to attribute.

Recommended controls: require pull requests, required status checks, block
force pushes and deletion, require conversation resolution, and require signed
commits. A solo-maintainer exception can retain emergency administration while
normal changes still pass the PR path.

### High — repository secret protections are disabled

Secret scanning, non-provider pattern scanning, validity checks, and push
protection are disabled. Dependabot security updates are also disabled. This is
material for a trading repository even though the current regex scan found no
committed keys.

Enable all available secret-scanning and push-protection controls. Keep exchange
keys trade-only, IP-restricted, withdrawal-disabled, and supplied only through
the runtime secret store/environment.

### Medium — unsigned and weakly attributable history

The unfamiliar commits are unsigned and the history mixes verified GitHub
merges with unsigned local commits. Require signed commits and verified PR
merges. Do not rely on the displayed contributor name as proof of the human
author.

### Medium — vulnerable frontend development toolchain

`npm audit` found no production-dependency vulnerabilities, but found two
development-tool findings in the pinned lockfile: a high-severity Vite path
handling issue and a moderate esbuild development-server issue. They affect the
development/preview server rather than the built static production bundle, but
the dev server should not be exposed to untrusted networks. Upgrade Vite and
its React plugin in a separate compatibility-tested change.

### Medium — dynamic AI strategy execution is process-local

`ai_sandbox.py` uses `exec` after a deny-by-default AST check, restricted
builtins, guarded imports, forced `ai_` namespacing, and no auto-registration.
The controls are thoughtful and tested, but a Python namespace restriction is
not an OS security boundary. Treat AI-authored code as untrusted: run its
research evaluation in an isolated container/process without exchange secrets,
network access, or writable core source.

### Medium — supply-chain builds are not reproducible

Python dependencies specify lower bounds without a committed lock/constraint
set, and Docker base images use mutable tags (`node:20-slim` and
`python:3.12-slim`) rather than digests. A fresh build can therefore resolve a
different dependency graph. Generate reviewed locks/SBOMs and pin release
images by digest while retaining a scheduled update process.

### Medium — public dashboard proxy defaults to token-only source access

The core dashboard port is loopback-bound, but the default `dashboard-tls`
service publishes port 8765. An empty `DASHBOARD_ALLOWLIST` permits every
source IP, leaving the high-entropy dashboard token as the primary access
control. The dashboard is read-only and TLS-protected, which limits impact,
but an internet-wide authentication surface is unnecessary. Set a non-empty
IP allowlist, prefer a VPN/SSH tunnel or private network, use a publicly trusted
certificate, and add rate limiting if public exposure is retained.

### Low — URL-open findings need explicit scheme contracts

Static analysis reported three `urlopen` sites. Two use fixed HTTPS Delta India
endpoints. The Pine catalog fetch accepts a passed URL and catches failures; it
should explicitly require HTTPS and an approved host if it is ever exposed to
remote/untrusted input. It is currently an operator research path, not an
unauthenticated server-side fetch route.

### Low — Actions policy is broader than necessary

Actions is enabled for all actions and SHA pinning is not required. No tracked
workflow files were present during this audit, so there is no current workflow
execution path. Before adding workflows, allow only required actions and pin
third-party actions to full commit SHAs. Default workflow token permissions are
read-only and cannot approve pull requests, which is a positive control.

## Clean checks

- Current collaborators: owner only.
- No Actions secrets, environments, deploy keys, or webhooks were configured.
- No common private-key/token patterns were found in the current tree or Git
  patch history scan.
- Tracked config contains `.env.example` placeholders only; `.env`, `*.pem`,
  and `*.key` are ignored.
- `pip-audit` found no known vulnerability in the installed Python dependency
  set.
- `npm audit --omit=dev` found no production dependency vulnerability.
- Bandit reported zero high-severity issues across about 93,500 source lines.
- Docker Compose did not expose Docker socket mounts or privileged containers
  in the reviewed configuration. The core dashboard port is loopback-bound;
  the separate TLS proxy exposure and allow-all source default are recorded as
  a finding above.
- The live settings retain the three required gates, and the unfamiliar commits
  did not touch them.

## Remediation order

1. Rotate credentials and inspect the GitHub security log if the author is not
   recognized as local tooling.
2. Protect `main` and require PR checks plus signed commits.
3. Enable secret scanning, push protection, and Dependabot security updates.
4. Upgrade the frontend dev toolchain and rerun its build/audit.
5. Restrict the dashboard proxy to trusted source IPs/private networking.
6. Add Python constraints/lock plus SBOM generation; pin container digests.
7. Isolate AI strategy evaluation at the process/container boundary.
8. Restrict future GitHub Actions and pin action SHAs before enabling workflows.
