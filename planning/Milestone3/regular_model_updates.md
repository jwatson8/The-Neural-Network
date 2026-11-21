# Regular model updates

An [hourly GitHub Actions trigger](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/.github/workflows/daily-model.yml)
uses persisted VM state to enforce 24 hours between attempts. Each attempt
evaluates the previous Eastern calendar day, combining the original dataset with
a bounded sample of recent interactions. A candidate is compared with the serving
model on held-out ratings. Passing the accuracy, latency, and drift gates permits
a final refit and automatic activation without restarting the API.
Implementation: [scheduler](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/deployment/scheduled_update.py),
[training](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/daily.py),
[promotion policy](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/src/model_updates/policy.py),
[continuity probes](https://github.com/jwatson8/The-Neural-Network/blob/8e8de53d5aa60c7d3c959c279f97862fef6043fe/deployment/observe_update.py).

**Production evidence to finalize:** attach two completed promotions within the
72 hours before submission, with version changes, timestamps, continuity reports,
and course-log availability. The [November 20 attempt](https://github.com/jwatson8/The-Neural-Network/actions/runs/44693525057)
retained the original model because of insufficient data; its 29/29 successful
local probes do not demonstrate a model switch.
