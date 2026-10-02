# Security boundary

This worker is designed around **credential separation and a fail-closed supervisor**.

## What the model can do

- Read and edit the checked-out repository workspace.
- Run local build/test commands permitted by the Codex workspace sandbox.
- Produce a proposed diff.

## What only the trusted supervisor can do

- Read GitHub authentication from `gh`.
- Fetch the repository.
- Create exactly one `agent/*` proposal commit.
- Push only an `agent/*` branch.
- Open a **draft pull request**.

There is no merge, auto-merge, release, deployment, repository-settings, secrets-management, or default-branch push operation in `agent_worker.py`.

## Fail-closed gates

A proposal is blocked when any of these are true:

- issue author is outside the allowlist;
- branch is not under the configured `agent/` prefix;
- a protected path changed;
- file/line limits are exceeded;
- validation fails;
- the remote proposal branch already exists;
- the inspected diff and staged file set differ;
- more than one proposal commit would be pushed;
- the PR is not configured as draft.

## Credential handling

The agent subprocess receives no `GH_TOKEN`/`GITHUB_TOKEN`, uses an empty `GH_CONFIG_DIR`, disables global Git config, and is explicitly instructed not to use GitHub or discover credentials. GitHub authentication remains in the supervisor process. Logs run through token-pattern redaction before they are persisted.

For stronger isolation, run the worker in a dedicated OS account or VM/container that has only the repository workspace mounted. OpenAI's current sandbox guidance likewise recommends isolating workloads and keeping third-party credentials outside the agent environment.
