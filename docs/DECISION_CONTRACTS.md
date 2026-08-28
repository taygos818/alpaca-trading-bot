# Decision Contracts

Milestone 1 introduces immutable schema version `1.0` records for every boundary in the planned agent coordinator. The implementation is additive and remains unreachable while `AGENT_COORDINATOR_ENABLED=false`.

## Trust chain

```text
EvidenceItem(s) -> EvidenceBundle -> AgentAnalysis -> OptionsProposal
    -> AdversarialObjection -> RiskAuthorization -> ExecutionCommand
    -> OrderEvent -> PositionAssessment
```

Every record carries a stable record ID, a shared trace ID, a UTC creation time, and a schema version. References are validated across the complete `DecisionTrace`. Evidence, proposal, and authorization fingerprints prevent downstream consumers from substituting modified upstream records.

## Fail-closed rules

- Unknown schema versions, naive timestamps, mutable collections, invalid decimals, fractional option-contract quantities, missing citations, unknown references, and duplicate IDs are rejected.
- Evidence is frozen into a bundle before analysis. Each analysis binds to the bundle fingerprint and cites specific evidence IDs.
- Execution commands require an unexpired, non-rejected risk authorization and bind to its fingerprint.
- The contract module contains no broker credentials, client, network calls, or submission methods.
- Canonical JSON encodes decimals as exact strings and UTC timestamps consistently.

## Replay

`DecisionTrace.replay_fingerprint` sorts independently produced records by stable ID before hashing. Therefore concurrent agent completion order cannot change the replay identity. A changed proposal, authorization, citation, event, or assessment produces a different fingerprint or breaks trace validation.

Milestone 2 will persist and consume these records. Until then, rollback is simply leaving `AGENT_COORDINATOR_ENABLED=false` (the default) or reverting the Milestone 1 commit.
