# Publishing Setup

This guide lists the external accounts and settings needed before the first public package release.

Do not commit credentials to the repository. Do not paste PyPI or npm tokens into issues, pull requests, docs, or chat logs. Configure them only in GitHub repository settings.

## Accounts To Prepare

You need:

- A GitHub account with admin access to `huanxin0825-ctrl/context-kernel`.
- A PyPI account with two-factor authentication enabled.
- An npm account with two-factor authentication enabled.
- Either an npm organization or user scope that can publish the package name used by `packages/npm/akernel/package.json`.

Useful official docs:

- PyPI Trusted Publishing: <https://docs.pypi.org/trusted-publishers/using-a-publisher/>
- npm access tokens: <https://docs.npmjs.com/creating-and-viewing-access-tokens>
- npm trusted publishing: <https://docs.npmjs.com/trusted-publishers>
- GitHub Actions environments: <https://docs.github.com/actions/deployment/targeting-different-environments/using-environments-for-deployment>

## PyPI

Recommended path: use PyPI Trusted Publishing. It is safer than a long-lived token because GitHub Actions authenticates through OIDC.

Configure a PyPI trusted publisher with these values:

| Field | Value |
| --- | --- |
| PyPI project name | `akernel-runtime` |
| Owner | `huanxin0825-ctrl` |
| Repository name | `context-kernel` |
| Workflow filename | `release.yml` |
| Environment name | `pypi` |

Then create the matching GitHub environment:

1. Open GitHub repository settings.
2. Go to `Environments`.
3. Create an environment named `pypi`.
4. Optional but recommended: add required reviewers so publication requires manual approval.

No GitHub secret is needed for PyPI when Trusted Publishing is configured.

## npm

The current npm package name is:

```text
@context-kernel/akernel
```

Before publishing, confirm that the `context-kernel` npm scope is available to you. Usually that means creating an npm organization named `context-kernel`, or changing `packages/npm/akernel/package.json` to use a scope you control.

Recommended first-release path:

1. Create or confirm the npm scope.
2. Create an npm automation or granular access token with publish permission.
3. In GitHub repository settings, add an Actions secret named `NPM_TOKEN`.
4. In GitHub repository settings, add an Actions variable named `PUBLISH_NPM` with value `true`.
5. Keep the package public with `npm publish --access public`, which is already what the release workflow uses.

Alternative future path: npm Trusted Publishing can remove the long-lived npm token once the package and publisher relationship are configured in npm.

## Local Release Check

Run this before creating a tag:

```powershell
.\scripts\release_check.ps1
```

The check runs tests, package build, metadata validation, CLI smoke checks, npm dry-run packing, and deterministic benchmark evidence generation.

## First Release Flow

After PyPI and npm are configured:

```powershell
git status --short --branch
git tag v0.1.0
git push origin v0.1.0
```

The GitHub release workflow will build artifacts, generate benchmark evidence, publish to PyPI through Trusted Publishing, and publish the npm launcher if `NPM_TOKEN` and `PUBLISH_NPM=true` are configured.

## What To Send Back

Send only these confirmations:

- PyPI account is ready and Trusted Publisher is configured.
- GitHub environment `pypi` exists.
- npm scope decision: keep `@context-kernel/akernel` or change to another scope.
- If publishing npm now: `NPM_TOKEN` secret exists and `PUBLISH_NPM=true` variable exists.

Do not send API tokens or passwords.
