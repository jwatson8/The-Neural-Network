# Online Evaluation
## Tech Stack, Collection, and Monitoring
Our online evaluation and monitoring takes place through:
- [A Grafana dashboard](../../src/monitoring/grafana/),
- An layered [PostGreSQL database](../../src/database/) with mulitple tables for collecting production telemetry,
- A persistent [Monitoring layer](../../src/monitoring/) that computes summaries from those tables every 5/mins.

We captures and logs our application's inputs and outputs throughout the application-chain (e.g., we capture record kafka events through [stream_events](../../src/database/interactions/stream_events.py), we capture our reccomendation outputs through [reccomendation_serves](../../src/database/interactions/recommendation_serves.py)). These data streams and tables are utilitzed by [Monitoring Worker](../../src/monitoring/monitoring_worker.py) who traverses them, computes relevent calculations from recorded telemetry, and logs those findings in [monitoring_runs](../../src/database/interactions/monitoring_runs.py). The monitoring worker is run independently in its own [docker container as monitoring-worker](../../compose.yaml).

Those snapshots are used by Dashboards in Grafana. Dashboards visualize both the individual 5-minute datapoint, and also, from each datapoint, mark a sliding 24-hour window to calculate changing averages over time.

## Online Measurement

### Calculating License Costs
*Metric*: Summed license costs in five-minute and 24-hour increments. 
*Data*: Use collected recordings of user watch-events within a 5 minute window and reaching backwards 23 hours and 55 minutes.
*Operationalization*: When the kafka event stream records a movie-watch experience, record the watch and check to see if the watch-event is part of the same viewing experience (we record the [laser time](../../src/database/interactions/user_movie_history.py) a user watched a movie, and suggest that users can continue the "same" viewing within 24-hours of their last viewing). If this is a new viewing, or outside the 24-hour grace window, check the license fee of the movie and record the license fee. Repeat this step over a definitive 5 minute window and record the license fee costs within that window. Then calculate the total-sum of that 5 minute window and 23 hours and 55 minutes before it. This gives you a rolling 24-hour window cost from that 5-min timestamp. See [sample here](../../src/monitoring/calculations/license_fees.py)

### Calculating LLM Fees (Estimate)
*Metric*: Summed LLM fees in 5 minutes and 24 hour increments.
*Data*: Database compilation that records each cold-start approach using an LLM (recording input tokens, output tokens, and provider, and model used) 
*Operationalization*: At time of logging, match the input and output token costs for the model selected with the provider's fee-per-token menu. Divide the input tokens by a million (usually, although may vary based on model - this assumes we are using gpt 4.0 mini) and mulitply the rate by the per-million token rate (in this case 15 cents). Follow the same pricing for the output tokens (60 cents). Calculate the 5-minute increment cost of those LLM calls. Then, add them to a rolling window composed of the previous 23 hours and 55 minutes. See [sample here](../../src/monitoring/calculations/llm_costs.py).

### Calculating Online Accuracy
*Metric*: Raw accuracy of model over an acceptable threhsold (rating 6 or higher).
*Data*: Keep track of the [reccomendations your model serves](../../src/database/interactions/recommendation_serves.py) and the [watch and rating events of your users](../../src/database/interactions/stream_events.py).
*Operationalization*: Over a 5 minute window, track the ratings of your users. Then, check to see if any of those users were serviced by any of your model routes (popularity_fallback, cold_start, or collaborative_filtering). 

# Avodiing Pitfalls
Data in production is always a challenge. We implemented a number of course concepts in our approach to online evaluation.

*Summarization*: Our visualization of online accuracy, noted above, summarizes in two-distinct ways. First, it collapses individual events into 5-min buckets. Second, it summarizes those buckets accross a rolling 24-hour window. This removes some of the fine-granular texture that might be visible to us at a microscopic scale, but gives us a bigger global view of the application's accuracy (particularly in terms of route comparison).
*Conformity*: Our robust [data quality pipeline](../../src/data_quality/) and [database ingestion schemas](../../src/database/schemas.py) ensures that raw data fits defined paramaters for our application, and works to keep malformed data out of pipelines.
*Wait and See*: Our approach to measuring accuracy prioritizes definite results over speculative indicators. After making a reccomendation, we don't evaluate the accuracy of reccomendations until after a user rates that movie from the list. 
