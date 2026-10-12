# Model Evaluations
Our models were evaluated according to numerous qualities. For the purpose of this report, we will focus on 4-qualities:
1) Training time,
2) Throughput 
3) Model size
4) Reccomendation Quality

To see more data and graphical representations of evaluations, see `./modelTesting/results/graphs`. To see the code used for evaluation, see `./modelTesting/train_all.py`, `./modelTesting/inference_tests.py`, `./modelTesting/throughput.py`. 

Origional milestone models can be found here:  
[Jace's Milestone Zero](https://github.com/jwatson8/S3D17645_Movie_Recommendation)  
[Frank's Milestone Zero](https://github.com/franktsai127/ml-m0-recommender)  
[Alvajoy's Milestone Zero](https://github.com/AlvajoyAsante/SWE-AI-CMU/tree/main/ML%20in%20Production/Milestone%200)  
[Nathan's Milestone Zero](https://github.com/cheepsahoy/cmu-milestoneZero)  

Models had to be slightly modified to fit the demands of this testing regiment. Specifications can be found in `./modelTesting/NOTES.md`, changes to model-training found in folders bearing the name of each group member.

## Training Time
![Graphic representation of model differenc in training time](./modelTesting/results/graphs/average_training_time.png)
### Metric
Average time of training on the same dataset.
### Data
Used the provided `events.csv.gz` dataset.
### Operationalization
Split the `events.csv.gz` dataset according to an `80/20` split (80% of data for training, 20% for testing - training data for this test and subsequent tests need to split training/testing per user. The most recent 20% of a user's watch events go into testing, the remaining 80% in training. Make sure to only include rating events, remove watch+account_creation events). In the code for loading the model and training it, save a variable and start a performance check before training. Check performance after training and record the difference. Set a variable and start a performance check before loading the model, do the same after loading the model and record the difference. Run the test 20 times and record the average for each model.
### Results
    Jace: 703ms
    Frank: 92ms
    Alvajoy: 203ms
    Nathan: 1460ms

## Throughput
![Graphic representation of model differenc in model inference time](./modelTesting/results/graphs/average_top20_request_time.png)
### Metric
Average time, per request, to generate K-20 reccomendations for a given user.
### Data
Use the trained models and the holdout test data described above in *Training Times*.
### Operationalization
The model's prediction capacity is restricted to "warm" items (e.g., movies that exist in the training data). Go through the testing data and exclude any movie that doesn't appear at least once in the training data (this is also important because Jace's model uses the full movie catalog metadata for scoring, so this "evens" the playing field). Run performance tests before and after the model makes a prediction and record the difference. Run the test 100 times and take the average. Report ms per requrest.
### Results
    Jace: 3.589ms
    Frank: 1.136ms
    Alvajoy: 5.074ms
    Nathan: 5.985ms
## Model Size
![Graphic representation of model differenc in terms of model size](./modelTesting/results/graphs/model_artifact_size.png)
### Metric
Size of the saved, exported model.
### Data
Use the train models as described above in *Training Times*.
### Operationalization
Inspect the size of each model and graph them. Report as MB.
### Results
    Jace: 4.304 mb
    Frank: 31.269 mb
    Alvajoy: 2.969 mb
    Nathan: 1.239 mb
## Reccomendation Quality
![Graphic representation of model differenc in reccomendation quality, specifically hitrate@20](./modelTesting/results/graphs/native_recall_at_20.png)
### Metric
Recall (@20)
### Data
Use the models as described above in *Training Times*, and operationalize the testdata as described in *Throughput*
### Operationalization
To calculate the value, for each model obtain a k-20 listing for the user. Go through the test data and find every movie in the test data for that user that scored above a baseline (x > 7.5). That comprises total-known relevent movies. Now, find the intersection between total known relevent moves and the k-20 model reccomendation for that user. the ratio of the two is recall @ 20. This is essentially recall (TP / TP + FN). Now calculate the average of these reccomendations accross every user in the dataset and calculate the mean. Do this for every model. Calculate against a baseline model (reccomend the top rated moves in the movie-dataset)
### Results
    Baseline: .1%
    Jace: 16%
    Frank: 14.1%
    Alvajoy: 7%
    Nathan: 9.2%