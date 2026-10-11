## Models
Our models were evaluated according to 4-qualities:
1) Training time,
2) Throughput 
3) Reccomendation relevence
4) Model size

**Training Time**
*Metric*: Average of training cycles on the same dataset.
*Data*: Used the provided `events.csv.gz` dataset.
*Operationalization*: Split the `events.csv.gz` dataset according to an `80/20` split (80% of data for training, 20% for testing). In the code for loading the model and training it, save a variable `train_started` as `perf_counter()` before training, and right after training `training_time` is `perf_counter()` - `train_started`. Do the same approach logging before the model is loaded
