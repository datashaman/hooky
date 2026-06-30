# Usage Sequences

These diagrams show the common ways Hooky is expected to run. The same loop
state shape is used in every case, namespaced by run key:
`.hooky/runs/<key>/proposal.md`, `.hooky/runs/<key>/contract.md`,
`.hooky/runs/<key>/feature_list.json`, `.hooky/runs/<key>/progress.md`,
`.hooky/runs/<key>/log.md`, and attempt artifacts under
`.hooky/runs/<key>/attempts/<id>/`.

## Auto-Approve Flow

Use this for trusted local runs where the evaluator is allowed to accept the
contract and decide whether to continue, restart, or pass without stopping for a
human gate.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant CLI as hooky run/start
    participant FS as .hooky files
    participant Planner
    participant Generator
    participant Evaluator
    participant Tools as tests/browser/tools

    User->>CLI: hooky run --proposal-file proposal.md
    CLI->>FS: create proposal, contract draft, state, log
    CLI->>Planner: gather repo context + proposal
    Planner->>FS: write proposal section in contract.md
    CLI->>Generator: propose done criteria
    Generator->>FS: update contract.md and feature_list.json
    CLI->>Evaluator: review proposed contract
    Evaluator-->>CLI: accept or reject contract

    alt contract accepted
        CLI->>FS: mark contract accepted in state/progress/log
    else contract rejected
        CLI->>Generator: revise criteria with evaluator feedback
        Generator->>FS: update contract.md and feature_list.json
        CLI->>Evaluator: review revised contract
    end

    loop attempts until pass, cap, or blocker
        CLI->>FS: create .hooky/runs/<key>/attempts/<id>
        CLI->>Generator: implement accepted contract
        Generator->>Tools: run project commands as needed
        Tools-->>Generator: test/runtime output
        Generator->>FS: write generator report and traces
        CLI->>Evaluator: evaluate attempt
        Evaluator->>Tools: run tests, inspect diffs, capture visual evidence
        Tools-->>Evaluator: evidence
        Evaluator->>FS: write evaluator_report.json
        Evaluator-->>CLI: pass / continue / restart-attempt / restart-contract / stop
    end

    CLI-->>User: final status and report paths
```

## Flow With HITL

Use this when a human should inspect the initial proposal, a restart, external
side-effects, or the final merge decision. The human is not another model role;
the human reviews durable files and either starts `hooky run`, reruns
initialization with revised proposal input, or handles merge/publish outside
Hooky.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant CLI as hooky
    participant FS as .hooky files
    participant Planner
    participant Generator
    participant Evaluator
    participant Human as Human reviewer

    User->>CLI: hooky init --proposal-file proposal.md
    CLI->>FS: initialize proposal, contract draft, state, log, git baseline
    CLI-->>Human: hooky status and durable file paths
    Human->>FS: inspect proposal.md, contract.md, feature_list.json, log.md
    alt proceed with current proposal
        Human->>CLI: hooky run
        CLI->>Planner: gather repo context + proposal
        Planner->>FS: update contract.md proposal section
        CLI->>Generator: propose done criteria
        Generator->>FS: update contract.md and feature_list.json
        CLI->>Evaluator: review contract
        Evaluator-->>CLI: accept or reject contract
        CLI->>FS: write state/progress/log updates
    else revise proposal
        Human->>CLI: hooky init --force --proposal-file proposal.md
        CLI->>FS: rewrite initial durable files
    end

    CLI->>Generator: implement attempt
    Generator->>FS: write changed files and report
    CLI->>Evaluator: evaluate attempt
    Evaluator->>FS: write evaluator report

    alt evaluator passes
        CLI-->>Human: surface final review with report, diff, screenshots
        Human->>GH: merge, publish, or stop outside Hooky
    else evaluator requests restart
        CLI-->>Human: surface restart reason when policy requires approval
        Human->>CLI: rerun, revise proposal with --force, or stop
    else evaluator finds blocker
        CLI-->>Human: ask for credentials, service access, or contract correction
    end
```

## GitHub Event Flow

Use this shape when GitHub opens or updates work and Hooky runs as automation.
The bundled workflow runs on manual dispatch, a `hooky:run` issue/PR label, or
an issue/PR comment that starts with `/hooky`. GitHub is a trigger and
publication surface; Hooky still records loop truth on disk before posting
summaries back to GitHub.

```mermaid
sequenceDiagram
    autonumber
    actor Author
    participant GH as GitHub
    participant Runner as CI/worker
    participant CLI as hooky
    participant FS as checkout + .hooky
    participant Planner
    participant Generator
    participant Evaluator

    Author->>GH: open issue, comment, label, or PR event
    GH->>Runner: webhook / workflow dispatch
    Runner->>FS: checkout repository and event payload
    Runner->>CLI: hooky run --run-key <key> --proposal-file event-proposal.md
    CLI->>FS: initialize or resume .hooky state
    CLI->>Planner: gather issue/PR payload and repo context
    Planner->>FS: write proposal section
    CLI->>Generator: propose contract criteria
    Generator->>FS: update contract.md and feature_list.json
    CLI->>Evaluator: review contract

    alt contract or attempt needs human input
        Runner->>GH: post blocker/failure summary and artifact links
        GH-->>Author: request clarification, credentials, or proposal change
        Author->>GH: comment, relabel, or update issue/PR
        GH->>Runner: new event starts/resumes same run key
    else automation can continue
        CLI->>Generator: implement accepted contract
        Generator->>FS: change workspace and write report
        CLI->>Evaluator: evaluate attempt
        Evaluator->>FS: write report, traces, visual evidence
    end

    alt evaluator passes
        Runner->>GH: push branch and open/update PR with report summary
    else evaluator recommends continue or restart
        Runner->>CLI: continue within configured cap or schedule next run
    else evaluator stops
        Runner->>GH: post failure/blocker summary with .hooky artifact paths
    end
```

## Ordinary HITL Points

The usual human review points are:

- before accepting a contract when requirements are ambiguous or high impact
- before using credentials, paid services, deployment targets, or destructive commands
- before `restart-contract`, because that changes what done means
- before publishing a branch, opening a pull request, merging, or deploying
- when the evaluator reports a blocker that automation cannot resolve from repo state
