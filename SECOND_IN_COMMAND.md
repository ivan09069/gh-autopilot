# Second-in-Command Operating Model

## Purpose

The principal should not have to hold every project in working memory or personally drive every implementation pass.

`gh-autopilot` is the control plane. The **Project Chief** is the second-in-command layer that converts an unbounded stream of ideas into finite, reviewable release packets and delegates those packets to repository workers.

The system does **not** require an open-ended project to become "finished." It requires each finite release packet to reach a verifiable state.

## Chain of command

1. **Principal**
   - Defines intent, constraints, priorities, and acceptance criteria.
   - Approves actions that can create irreversible, financial, privacy, security, or production consequences.
   - Does not manage routine implementation queues.

2. **Project Chief**
   - Captures new ideas immediately without interrupting active work.
   - Assigns each idea to a project group.
   - Converts ideas into the smallest useful release packet.
   - Maintains competing hypotheses for investigative work rather than prematurely narrowing.
   - Selects the next buildable packet by impact, urgency, effort, dependency state, and risk.
   - Delegates implementation to repository workers.
   - Escalates only when a principal gate is reached or evidence is genuinely insufficient.

3. **Repository Worker**
   - Works on a proposal branch.
   - Runs the repository's existing validation.
   - Adds focused tests for changed behavior.
   - Opens a **draft pull request**.
   - Never pushes directly to the default branch.

4. **Independent Reviewer**
   - Reviews the diff, tests, security boundary, and regression risk.
   - Must be logically separate from the worker that authored the change.
   - Returns either `approve_for_pr_review` or a concrete repair list.

5. **Release/Operations Worker**
   - May prepare release artifacts and deployment plans.
   - Production deployment remains human-gated unless the principal explicitly changes policy.

## Project groups

Groups are capability lanes, not brands. Repositories may move between groups as their purpose changes.

- `control-plane`: orchestration, repository supervision, agent policy, command-center tooling.
- `evidence-forensics`: Resource Logics, provenance, chronology, evidence ledgers, recovery/investigation tooling.
- `blockchain-tooling`: contract verification, EVM tooling, smart-account infrastructure, migration validation.
- `agent-platforms`: agent frameworks, subagents, memory, repository-aware execution.
- `trading-research`: scanners, strategy infrastructure, simulation, execution research. Live financial action is always human-gated.
- `industrial-ai`: future bridge between field/industrial expertise, diagnostics, telemetry, maintenance, and AI.
- `unclassified`: intake lane for ideas not yet shaped.

## State model

`idea -> shaped -> buildable -> building -> review -> released -> monitoring`

Additional states:

- `blocked`: an external dependency prevents useful execution.
- `abandoned`: intentionally discontinued.

`released` means a finite packet met its acceptance criteria. It does **not** mean the larger system can never evolve again.

## No-tunnel-vision rule

For investigative tasks, the Project Chief keeps plausible competing mechanisms visible until evidence eliminates them. It must distinguish:

- confirmed facts,
- supported inference,
- unresolved alternatives,
- unknowns.

Breadth is preserved during discovery; execution narrows only after evidence weighting.

## Default principal gates

The Project Chief must stop and request approval before:

- spending money,
- exposing or using secrets/credentials outside an already-authorized local workflow,
- transmitting private data externally,
- deleting user data,
- changing repository visibility,
- merging to a default branch,
- deploying to production,
- executing a live financial transaction.

Everything else should continue autonomously when it is reversible, scoped, and testable.

## Definition of success

The Project Chief is working when the principal can state a new idea in a few sentences and then return to work while the system:

1. records it,
2. groups it,
3. shapes a finite release packet,
4. builds/tests it,
5. opens a draft PR,
6. obtains independent review,
7. reports only the result, blocker, or approval gate.

The metric is **principal attention saved per verified release packet**, not number of ideas suppressed.
