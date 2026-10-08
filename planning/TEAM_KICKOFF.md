# The Neural Network — kickoff pack

Team: jwatson8, Nathan (@cheepsahoy), Frank (@franktsai127), Alvajoy (@AlvajoyAsante). Repository:
[The-Neural-Network](https://github.com/jwatson8/The-Neural-Network).

**Deadline: Sunday October 11, 2026, 11:59 PM Eastern (8:59 PM Pacific).**
Internal submission target: 11:00 PM Eastern. All task dates below use Eastern.
Ownership is proposed, not yet agreed by the team. Update issues and meeting notes
when assignments are accepted. GitHub usernames are now recorded. Assignments requiring repository invitation acceptance are noted on issues. Team roles are confirmed; technical work allocations remain proposals.

## Proposed work areas

| Person | Lead responsibility | Reviewer / pairing |
|---|---|---|
| jwatson8 | Developer and GitHub project/task maintainer; proposed live cold-start implementation | Pair with Frank on request-path integration |
| Nathan (@cheepsahoy) | Project/team lead; coordinates review/submission and mentor scheduling; proposed benchmark/report lead | Each member supplies their model; jwatson8 reviews metrics |
| Frank (@franktsai127) | Project developer; proposed HTTP service and traffic verification | Alvajoy reviews deployment behavior |
| Alvajoy (@AlvajoyAsante) | Project developer and meeting note taker; proposed infrastructure/deployment work | Frank reproduces deployment |

These are a starting proposal, not assumptions about anyone's skills. Adjust based
on availability and interests. Everyone presents their M0 model, learns the other
components, attends the mentor debrief and completes their own survey.

## Critical schedule

- **Friday October 9:** confirm access and project board, agree contract, present
  all four models, fix the measurement protocol and verify VM/Kafka access.
- **Saturday October 10, 10 AM:** complete comparable measurements for all models.
  **11 AM:** choose a model. Work on the service wrapper and deployment skeleton
  can begin earlier, but final integration depends on the selected model.
- **Saturday, 4 PM:** target a working deployed service, including automatic cold
  start. **5 PM:** verify real course-request status-200 Kafka entries.
- **Sunday October 11:** observe course traffic, fix failures, finalize report and
  architecture. **9 PM:** evidence review. **11 PM:** submit exact commit URL.
- Keep the service running through the selected 24-hour grading window. Schedule
  the mentor debrief within one week after actual submission (by October 18 for an
  October 11 submission). Confirm the survey's separate Canvas deadline.

Starting Saturday afternoon gives time to discover connection/parsing issues;
it does not guarantee 2,000 course requests. Watch actual arrival rates early and
contact the mentor if the course stream is unavailable or unexpectedly quiet.

## What counts as done

| Required evidence | Points / requirement |
|---|---|
| Four defined metrics: metric, data, operationalization; results for **every model**; code links | 20 total |
| Ranked service implementation and automatic live-user cold start matching report | 10 |
| Running containers, Compose, reproducible SSH deployment; architecture answering a question and a deployment trade-off | 10 |
| Team contract covering all five prompts; notes recording **who / what / when**; board and issue links | 10 |
| >=2,000 successful course recommendation requests in the applicable 24-hour window | 10 |
| Personalized successful responses >=10% of **all incoming requests** in that window | 10 |
| Mentor debrief within one week; individual understanding and reflection | 10 per person |
| Individual teamwork survey | 3 per person |
| Optional social activity and learning beyond comfort zone | Up to 3 + 3 bonus |

Only status-200 course Kafka entries establish successful parsing/response for the
traffic criteria. The response contract is HTTP on VM port 8082 at
`/recommend/<userid>`, one comma-separated line of up to 20 movie IDs, best first,
within 600 ms. JSON is not accepted. Do not confuse 10% of all requests with 10%
of 2,000 successes, and do not equate a local benchmark with production evidence.

## Suggested 50-minute meeting agenda

1. 0–5 minutes: confirm deadline, availability, access and team manager.
2. 5–21: four M0 walkthroughs, four minutes each: approach, cold start, results,
   dependencies and risks.
3. 21–31: agree common metrics/split/hardware; arrange comparison runs. Do not
   select the model solely from incompatible scores obtained by different people.
4. 31–40: agree responsibilities, due dates, reviewer pairings and fallback plans.
5. 40–47: confirm service/cold-start/deployment architecture and Saturday target.
6. 47–50: record decisions, unresolved blockers and next check-in; update GitHub.

## Draft team contract — agree and replace placeholders at kickoff

Slack is the primary communication channel. Proposed response expectation: within
four waking hours during the milestone sprint, accounting for everyone's agreed
availability. A blocker should be posted immediately with its impact and the help
needed. For urgent problems, tag the owner and backup in Slack; agree any secondary
contact method privately rather than publishing phone numbers.

Confirmed project/team lead and scheduling coordinator: **Nathan (@cheepsahoy)**.
Meeting note taker: **Alvajoy (@AlvajoyAsante)**. GitHub project/task maintainer:
**jwatson8**. Frank (@franktsai127) is a project developer. The task owner updates the
issue status and evidence when work changes. The manager checks deadlines and
unassigned tasks daily; no work is considered agreed until its owner and deadline
are in the issue and meeting notes.

Divide tasks by capacity, dependencies and learning goals, not only prior strengths.
Assign one accountable owner and a reviewer; pair for unfamiliar work. Before
changing a deadline, post the reason and a revised plan. If a teammate is blocked
or unresponsive past the agreed response window, the manager contacts them and
the backup, narrows or reallocates work with the team, and involves the mentor if
the issue persists or threatens the deadline. Keep discussions respectful and
separate technical disagreement from judgments about people.

Use pull requests and a teammate review when possible. Commit lasting code and
configuration fixes; do not rely on undocumented VM changes. Keep secrets outside
Git. Record model/data versions and decisions. All members review the final system
and report so each can explain the parts they did not implement. Confirm everyone
can attend the debrief and has located their individual survey.

**Agreement status:** draft. **Agreed by/date:** fill in at the meeting.

## Meeting notes template — not completed meeting evidence

Date/time/timezone: ___ | Attendees: ___ | Facilitator: ___ | Note taker: ___

Decisions: selected comparison protocol ___; manager ___; architecture ___;
model selection pending comparison / selected model and rationale ___.

| Who (name + GitHub username) | What (issue link) | Due date/time/timezone | Reviewer | Agreed? |
|---|---|---|---|---|
| jwatson8 | ___ | ___ | ___ | ___ |
| Nathan | ___ | ___ | ___ | ___ |
| Frank | ___ | ___ | ___ | ___ |
| Alvajoy | ___ | ___ | ___ | ___ |

Blockers and help needed: ___ | Next check-in: ___ | Notes URL: ___

## Board layout

Use one GitHub Project for the course, with a Board view grouped by Status:
Todo, In Progress, Review, Blocked, Done. Add a Due date date field, Priority
(P0/P1/P2), Milestone (M1/M2/M3) and Proposed owner. Display actual Assignees.
Use a separate view for M1 due dates and a future-work view. Until the extra status
options are added, GitHub's default Todo/In Progress/Done is sufficient.

Link the project to the team repository and add the issues. Both repository access
and project access must be checked for teammates and staff. Set any pending actual issue assignees after repository invitations are accepted.

M2 and M3 tasks are planning placeholders only: M2 adds automated tests/model-update
automation; M3 adds regular updates/model-version switching. Obtain their full
rubrics before committing to dates or detailed scope. The overall project also
emphasizes monitoring, reliability, drift and feedback loops; keep deployment and
artifact boundaries simple enough to extend.

Reference: [GitHub Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/about-projects)
supports linked issues, board/table views, assignees, and custom date fields.
