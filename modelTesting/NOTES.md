## What are our models?

- Nathan is a rating predictor. It learns user and movie embeddings plus biases by minimizing mean-squared error against the observed 1–10 ratings.
- Alvajoy is a rating predictor. Surprise SVD learns factors and biases from 1–10 ratings and returns an estimated rating.
- Frank finds movies similar to those the user rated, subtracts the rating midpoint 5.5, and sums similarity-weighted preferences. A high positive number means “recommend more strongly”; a negative number means “recommend less strongly.” 
- Jace factorizes a matrix where ratings were transformed with (rating - 5.5) / 4.5. Its dot product is a reconstructed preference on that normalized scale.

## Things I've Done
1) Ensure the models have the same input / training data split (ensure user's are represented in training and testing and split them 80% v 20% for the purpose of comparison)

2) Modify Alvajoy's Model: current model has two prediction modes (SVD and the td-tf thing). I thought the SVD was the core idea and so left data for the td-tf but didn't test it.

3) Cold Items: Jace's training catalog included data on the entire corpus. this is not necessarily a leakage issue, but it is a fairness issue (since it means that Jace's algo scores 546 movies the other three models don't consider). This builds nice resilency in Jace's model.

Testing data for inferences is combed to ensure that only movies that were in every models training data is included in the test data (no "cold" items).

4) All mdoels are 