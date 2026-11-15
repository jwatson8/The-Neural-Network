# Experiment Manager

The manager returns a typed, persistent assignment decision for an explicitly
selected experiment. It is disabled by default and is not called by Flask.

## Interface

```python
from src.experimentation.manager import ExperimentManager

manager = ExperimentManager(enabled=True)
decision = manager.decide(
    experiment_id=experiment_id,
    user_id=user_id,
    eligible=True,
)
if decision.assigned:
    assignment_id = decision.assignment_id
    variant = decision.variant
    policy, version = decision.policy, decision.version
```

`ExperimentDecision` is immutable and includes a `DecisionReason`, assignment ID,
variant, policy/version, and `newly_assigned`. Unassigned decisions have no policy
or assignment reference. Disabled calls make no database connection. Database
errors propagate; the future serving integration must choose its failure policy.
Users must already exist. Callers supply eligibility as a boolean and select the
experiment; the manager does not infer cohorts or resolve overlapping experiments.
An optional timezone-aware `now` supports reproducible evaluation.

## Enrollment and Persistence

Only `running` experiments with a configured start and `starts_at <= now < ends_at`
are active; a missing end means no expiry. Draft, paused, completed, aborted,
unscheduled, future, expired, ineligible, and unknown-user requests do not enroll.

Traffic allocation is the enrolled share of eligible users. Treatment allocation
is the treatment share of enrolled users. Canonical UTF-8 JSON tuples containing
`experiment-assignment-v1`, experiment UUID, purpose, and the exact user ID are
hashed with SHA-256. Separate enrollment/variant purposes prevent correlated splits.
Integer comparisons implement exact half-open thresholds without float rounding.
Zero enrolls nobody; one enrolls everyone. Membership expands monotonically when
traffic increases. Ratios describe expected populations, not exact quotas.

Stored assignments take precedence over new hash predictions or changed thresholds.
Pausing, expiry, or losing eligibility suppresses the decision but preserves the
row. Resuming reuses its UUID and variant. Reducing traffic stops new enrollment;
it does not evict existing assignments. User IDs are validated but not trimmed.

`experiment_assignments.get_or_create()` uses the unique experiment/user key and
`ON CONFLICT DO NOTHING`, then reads the winning row in the same transaction.
It returns `(assignment, created)` and never overwrites a conflicting assignment.
Use PostgreSQL READ COMMITTED; retry the entire operation after serialization or
deadlock failures at stronger isolation. SQLite supports sequential tests only.
The manager holds a shared experiment row lock through commit; lifecycle changes
use an exclusive lock. Decisions already committed may finish serving after a pause.
Assignment is not exposure: serving must separately log the actual result.

## Lifecycle

`experiments.transition(ExperimentLifecycleUpdate(...))` permits draft to running
or aborted; running to paused, completed, or aborted; paused to running, completed,
or aborted. Repeating a status is a no-op. Completed/aborted experiments cannot
reopen. Running requires a start and a nonexpired end, if present; scheduling a
future start is supported. Transition timestamps must be aware and cannot move
`updated_at` backward. No automatic activation or expiry updates are performed.

Policy, version, hash contract and eligibility definitions must stay fixed once
an experiment starts. No configuration-update API is provided. Use a new experiment
for a policy change. Existing schema defaults remain draft and zero traffic.

## Tests and Serving Integration

Run pure and isolated database tests from the repository root:

```sh
python -m pytest src/tests/test_experiment_assignment.py src/database/tests
```

Concurrent PostgreSQL tests reuse the disposable local cluster described in the
[database README](../database/README.md); set `M3_TEST_POSTGRES_BIN_DIR` and allow
Unix sockets when using pytest-socket. No test connects to an existing database.

Flask still needs an enable flag, experiment selection and eligibility rules,
user creation before decisions, policy/artifact resolution, fallback handling,
and assignment IDs linked to actual recommendation serves. Model hosting and
cold-start profile/version coordination remain separate integration dependencies.
