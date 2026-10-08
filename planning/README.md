# The Neural Network — meeting preparation

- [Shared project board](https://github.com/users/jwatson8/projects/1)
- [Milestone 1 issues](https://github.com/jwatson8/The-Neural-Network/issues?q=is%3Aissue+label%3Amilestone%3Am1)
- [Issue index: owners, dates and links](ISSUE_INDEX.md)
- [Kickoff agenda, proposed contract and notes template](TEAM_KICKOFF.md)
- [jwatson8 model measurements and explanations](MODEL_BRIEF.md)
- [Report outline](M1_REPORT_OUTLINE.md)
- [Raw model measurements](model_metrics.json)

Deadline: **Sunday October 11, 2026 at 11:59 PM Eastern**. Internal submission
target: 11:00 PM Eastern. The dates and owners on individual tasks are proposals
to confirm at kickoff. Nathan, Frank and Alvajoy need their actual GitHub usernames
added as assignees and project collaborators. The prepared notes are not a claim
that the meeting has happened.

The benchmark describes jwatson8's unchanged M0 model only; it is not the team's
completed comparison or a selection decision. The team must compare all four
models under one agreed protocol.

To reproduce the shared benchmark, obtain the individual model source from
[this exact M0 commit](https://github.com/jwatson8/S3D17645_Movie_Recommendation/tree/b131afb5a05f0e86b6047508a2e9a29a1fbcf2e5),
set up its requirements and verified course data, copy `benchmark_m1.py` into that
repository root beside `recommender.py`, and run it with that environment's Python.
The script writes `meeting/model_metrics.json` and a model artifact under `artifacts/`.
It does not modify the recommender. The reported measurements were made locally,
not through HTTP on the team VM.

M2/M3 issues are future planning placeholders. Obtain the full milestone rubrics
before scheduling implementation. M1 issue bodies include exact due dates and
completion criteria; use the project board for status and the issues for the
authoritative who/what/when record.
