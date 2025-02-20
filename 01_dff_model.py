# Databricks notebook source
# MAGIC %md 
# MAGIC You may find this series of notebooks at https://github.com/databricks-industry-solutions/fraud-orchestration. For more information about this solution accelerator, visit https://www.databricks.com/solutions/accelerators/fraud-detection.

# COMMAND ----------

# MAGIC %md
# MAGIC <img src=https://brysmiwasb.blob.core.windows.net/demos/dff/databricks_fsi_white.png width="600px">

# COMMAND ----------

# MAGIC %md
# MAGIC 
# MAGIC # Databricks fraud framework - Model building
# MAGIC 
# MAGIC The financial service industry (FSI) is rushing towards transformational change to support new channels and services, delivering transactional features and facilitating payments through new digital channels to remain competitive. Unfortunately, the speed and convenience that these capabilities afford is a benefit to consumers and fraudsters alike. Building a fraud framework often goes beyond just creating a highly accurate machine learning model due ever changing landscape and customer expectation. Oftentimes it involves a complex decision science setup which combines rules engine with a need for a robust and scalable machine learning platform. In this series of notebook, we'll be demonstrating how `Delta Lake`, `MLFlow` and a unified analytics platform can help organisations combat fraud more efficiently
# MAGIC 
# MAGIC 
# MAGIC ---
# MAGIC + <a href="$./02_dff_orchestration">STAGE2</a>: Building a fraud detection model
# MAGIC ---
# MAGIC 
# MAGIC + <sri.ghattamaneni@databricks.com>
# MAGIC + <nikhil.gupta@databricks.com>
# MAGIC + <ricardo.portilla@databricks.com>

# COMMAND ----------

# DBTITLE 1,Interpretable ML Visualization Library Import
# MAGIC %pip install shap

# COMMAND ----------

# DBTITLE 1,Import libraries
from pyspark.sql.functions import col, monotonically_increasing_id
from sklearn.model_selection import cross_val_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler 
from xgboost import XGBClassifier

import mlflow          # For experiment tracking and model management
import mlflow.pyfunc   # For creating custom PyFunc models
import numpy as np     # For array, vector, matrix calculations
import pandas as pd    # For DataFrame handling
import shap            # For model explainability
import sklearn         # For preprocessing and evaluation
import xgboost as xgb  # Gradient Boosting Machines (GBMs)

# COMMAND ----------

# DBTITLE 1,Persist Txn Flat Files to Delta Lake for Audit and Performance
# Reads a CSV file containing transaction data and writes it to a Delta Lake table.
# Delta Lake ensures data reliability, scalability, and versioning, which are critical for fraud detection systems.

raw_data_path = "/tmp/dff/delta_txns"

spark.read.option("inferSchema", "true") \
          .option("header", "true") \
          .option("delim", ",") \
          .csv("/tmp/dff/Fraud_final-1.csv") \
          .write \
          .format("delta") \
          .mode("overwrite") \
          .option("overwriteSchema", "true") \
          .save(raw_data_path)

# COMMAND ----------

# MAGIC %md 
# MAGIC Let's first define an outline for feature preprocessing and modeling. We will call the respective preprocessing and modeling functions after we have imported our data.

# COMMAND ----------
# Preprocess numeric columns using a fitted scaler.
# This function standardizes numeric features to ensure consistent scaling across training and testing datasets.

def preprocess_data(
    source_df: pd.DataFrame, 
    numeric_columns: list[str], 
    fitted_scaler: StandardScaler
) -> pd.DataFrame:
  """
  Preprocess numeric columns using a fitted scaler.
  
  Args:
      source_df: Input DataFrame
      numeric_columns: List of numeric column names
      fitted_scaler: Fitted StandardScaler instance
      
  Returns:
      DataFrame with scaled numeric columns
  """
  res_df = source_df[numeric_columns].copy()
  
  ## Scale the numeric columns with the pre-built scaler
  res_df[numeric_columns] = fitted_scaler.transform(res_df[numeric_columns])
  
  return res_df

# COMMAND ----------

# DBTITLE 1,PyFunc Wrapper for Fraud Model
# This class wraps the XGBoost model into a custom PyFunc model for seamless integration with MLflow.
# It includes preprocessing logic to handle raw input data directly.

class XGBWrapper(mlflow.pyfunc.PythonModel):
  '''
    XGBClassifier model with embedded pre-processing.
    
    This class is an MLflow custom python function wrapper around an XGB model.
    The wrapper provides data preprocessing so that the model can be applied to input dataframe directly.
    :Input: to the model is pandas dataframe
    :Output: predicted price for each listing

    The model declares current local versions of XGBoost and pillow as dependencies in its
    conda environment file.  
  '''
  def __init__(self,
               model: xgb.XGBClassifier,
               X: pd.DataFrame,
               y: pd.Series,
               numeric_columns: list[str]) -> None:
    """
    Initialize the XGBWrapper with a trained model, data, and preprocessing configuration.

    Args:
        model: Trained XGBoost classifier.
        X: Feature data used for training and testing.
        y: Target labels corresponding to the feature data.
        numeric_columns: List of numeric column names to be preprocessed.
    """
    self.model = model

    self.X_train, self.X_test, self.y_train, self.y_test = train_test_split(X, y, test_size=0.30, random_state=2019)
    self.numeric_columns = numeric_columns
    
    # Create a scaler for our numeric variables
    # Only run this on the training dataset and use to scale test set later.
    scaler = StandardScaler()
    self.fitted_scaler = scaler.fit(self.X_train[self.numeric_columns])
    self.X_train_processed = preprocess_data(self.X_train, self.numeric_columns, self.fitted_scaler)
    self.X_test_processed  = preprocess_data(self.X_test, self.numeric_columns, self.fitted_scaler)

    def _accuracy_metrics(model: xgb.XGBClassifier, X: pd.DataFrame, y: pd.Series) -> float:
      """
      Calculate the AUC metric for the model.

      Args:
          model: Trained XGBoost classifier.
          X: Feature data for evaluation.
          y: True labels for evaluation.

      Returns:
          AUC score as a float.
      """
      y_pred = model.predict_proba(X)[:,1]
      fpr, tpr, thresholds = sklearn.metrics.roc_curve(y, y_pred)
      self.auc = sklearn.metrics.auc(fpr, tpr)
      print("Model AUC is:", self.auc)

      return self.auc
    
    self.auc = _accuracy_metrics(model=self.model, X=self.X_test_processed, y=self.y_test)

    
  def predict(self, context: mlflow.pyfunc.PythonModelContext, input: pd.DataFrame) -> pd.DataFrame:
    """
    Generate predictions from the input DataFrame.

    Args:
        context: MLflow context (unused in this implementation).
        input: Input DataFrame with numeric columns to be scored.

    Returns:
        DataFrame with predicted probabilities for the positive class.
    """
    input_processed = self._preprocess_data(X=input, numeric_columns=self.numeric_columns, fitted_scaler=self.fitted_scaler)
    return pd.DataFrame(self.model.predict_proba(input_processed)[:,1], columns=['predicted'])

  
  def _preprocess_data(
        self,
        X: pd.DataFrame,
        numeric_columns: list[str],
        fitted_scaler: StandardScaler
    ) -> pd.DataFrame:
    """
    Preprocess input data using the fitted scaler.

    Args:
        X: Input DataFrame.
        numeric_columns: List of numeric column names.
        fitted_scaler: Fitted StandardScaler instance.

    Returns:
        Preprocessed DataFrame with scaled numeric columns.
    """
    res_df = preprocess_data(X, numeric_columns, fitted_scaler)
    self._df = res_df
    
    return res_df

# COMMAND ----------

# DBTITLE 1,Create XGBoost Classifier Model Fit Method - Return Probability and XGB Model
# This function trains an XGBoost classifier and computes cross-validation scores.
# The AUROC metric is chosen to evaluate model performance.

def fit(X: pd.DataFrame, y: pd.Series) -> dict:
  """
  Train XGBoost classifier and compute cross-validation score.
  
  Args:
      X: Feature matrix
      y: Target labels
      
  Returns:
      Dictionary with 'loss' (cross-validation score) and 'model' (trained XGBoost instance)
  """
  _model =  XGBClassifier(learning_rate=0.3,
                          gamma=5,
                          max_depth=8,
                          n_estimators=15,
                          min_child_weight = 9, objective='binary:logistic')

  xgb_model = _model.fit(X, y)
  
  score = -cross_val_score(_model, X, y, scoring='roc_auc').mean()
  
  return {'loss': score, 'model': xgb_model}

# COMMAND ----------

# MAGIC %md 
# MAGIC Our input dataset has several fields which will be used for rule-based modeling and machine learning. 
# MAGIC In this notebook, we will rely on our machine learning model to identify important features that are effective at predicting fraud. 
# MAGIC Let's take a look into descriptions of these features to understand our downstream modeling and interpretability results.
# MAGIC  
# MAGIC * LAST_ADR_CHNG_DUR     - Duration in days since the last address change on the account.
# MAGIC * AVG_DLY_AUTHZN_AMT    - The average daily authorization amount on the plastic since the day of first use.
# MAGIC * DISTANCE_FROM_HOME	  - Approximate distance of customer's home from merchant.
# MAGIC * HOME_PHN_NUM_CHNG_DUR - Duration in days since the home phone number was changed on the account.

# COMMAND ----------

# DBTITLE 1,Read Delta Lake for Transactions
# Read the Delta Lake table created earlier and convert it to a Pandas DataFrame for further processing.

df = spark.read.format("delta") \
  .load(raw_data_path)

data = df.toPandas()
data = data.drop(columns=['AUTH_ID', 'ACCT_ID_TOKEN'])
numeric_columns = data.columns.to_list()
numeric_columns.remove('FRD_IND')
data.head()

# COMMAND ----------

# MAGIC %md
# MAGIC <img src="https://brysmiwasb.blob.core.windows.net/demos/dff/dff_data.png">

# COMMAND ----------

# DBTITLE 1,Add xgboost and sklearn to be used in the Docker environment for serving later on
# Define the Conda environment for the Docker container that will serve the model.
# This includes adding XGBoost and Scikit-learn as dependencies.

conda_env = mlflow.pyfunc.get_default_conda_env()
conda_env['dependencies'][2]['pip'] += [f'xgboost=={xgb.__version__}']
conda_env['dependencies'][2]['pip'] += [f'scikit-learn=={sklearn.__version__}']

# COMMAND ----------

# DBTITLE 1,MLFlow Tracking and PyFunc Model Saving
# Track the model training process using MLflow and save the custom PyFunc model.
# The model is logged with metrics, parameters, and artifacts for reproducibility.

useremail = dbutils.notebook.entry_point.getDbutils().notebook().getContext().userName().get()
experiment_name = f"/Users/{useremail}/dff_orchestrator"
mlflow.set_experiment(experiment_name) 
model_run_name = 'fraud-xgb-wrapper'

with mlflow.start_run() as run:
  mlflow.log_param('Input-data-location', raw_data_path)
  X_train, X_test, y_train, y_test = train_test_split(data.drop(["FRD_IND"], axis=1), data["FRD_IND"], test_size=0.33, random_state=42)

  # Create a scaler for our numeric variables
  # Only run this on the training dataset and use to scale test set later.
  scaler = StandardScaler()
  fitted_scaler = scaler.fit(X_train[numeric_columns])
  X_train_processed = preprocess_data(source_df=X_train, numeric_columns=numeric_columns, fitted_scaler=fitted_scaler )

  # Train a model and get the loss
  train_dict = {}
  train_dict = fit(X=X_train_processed, y=y_train)
  xgb_model = train_dict['model']
  mlflow.log_metric('loss', train_dict['loss'])
  
  ##------- Log pyfunc custom model -------##
  # Make an instance of the Pyfunc Class
  myXGB = XGBWrapper(model = xgb_model,
                     X = data[numeric_columns].copy(), 
                     y = data['FRD_IND'], 
                     numeric_columns = numeric_columns)
  
  mlflow.pyfunc.log_model(model_run_name, python_model=myXGB, conda_env=conda_env)

  mlflow.log_metric('auroc', myXGB.auc)
  
# Programmatically get the latest Run ID
runs = mlflow.search_runs(mlflow.get_experiment_by_name(experiment_name).experiment_id)
latest_run_id = runs.sort_values('end_time').iloc[-1]["run_id"]
print('The latest run id: ', latest_run_id)

# COMMAND ----------

# Generate predictions using the trained model and display the results.
# This demonstrates how the trained model can be applied to the entire dataset (or a subset of it) to predict whether each transaction is fraudulent or not.
# X: Contains the feature matrix (independent variables) used for prediction. These are the numeric columns extracted from the dataset.
# y: Contains the target labels (FRD_IND), which indicate whether a transaction is fraudulent (1) or legitimate (0).

X = data[numeric_columns].copy() # To ensure that modifications to X or y do not affect the original dataset.
y = data['FRD_IND']

train_dict = fit(X=X, y=y)  # To train an XGBoost classifier on the entire dataset (X and y)
                            # The fit function also computes cross-validation scores and returns
                            # 'loss': The negative mean AUC score (used for optimization purposes)
                            # 'model': The trained XGBoost classifier

xgb_model = train_dict['model'] # The trained model (xgb_model) is extracted from the dictionary returned by fit.

predictions = myXGB.predict(spark, X) # myXGB is an instance of the custom PyFunc wrapper (XGBWrapper) that encapsulates the trained XGBoost model and preprocessing logic.
# Applies the preprocessing steps (e.g., scaling numeric features) defined in the XGBWrapper class.
# Uses the trained XGBoost model to generate predictions for the input data (X).
# Returns a DataFrame with a single column (predicted), which contains the predicted probabilities for the positive class (fraudulent transactions).
     
predictions.head()
# Each value represents the probability that the corresponding transaction is fraudulent.
# For example:
#   A value close to 1 indicates a high likelihood of fraud.
#   A value close to 0 indicates a low likelihood of fraud.

# COMMAND ----------

# DBTITLE 1,Register Model
# Register the trained model in the MLflow Model Registry (a centralized repository for managing machine learning models).
# Transition it to the "Production" stage, enabling the model to be deployed for real-time inference.
# The MLflow Model Registry provides a single source of truth for all models, making it easier to manage and track changes over time.
# By assigning a version number, you can track which version of the model is deployed in production and roll back to a previous version if necessary.

client = mlflow.tracking.MlflowClient()                # Interface to interact with the MLflow tracking server and Model Registry.
model_uri = f"runs:/{latest_run_id}/{model_run_name}"  # URI of the model to be registered. This tells MLflow where to find the model artifacts
model_name = "fraud_xgb_model"                         # Name of the model to be registered.
result = mlflow.register_model(model_uri, model_name)  # Registers the model in the MLflow Model Registry. Each time a model is registered under the same name, a new version is created
version = result.version                               # Version of the model that was registered.

# COMMAND ----------

# DBTITLE 1,Transition the model to Production
# Archive any existing production versions of the model and transition the new version to "Production".
for mv in client.search_model_versions(f"name='{model_name}'"):
  
    # If model with this name is marked staging
    if mv.current_stage.lower() == 'production':
      # Mark it as archived
      client.transition_model_version_stage(
        name=model_name,
        version=mv.version,
        stage='archived'
        )

client.transition_model_version_stage(
  name=model_name,
  version=version,
  stage="Production",
)

# COMMAND ----------

# MAGIC %md 
# MAGIC After running SHAP on the model, we can see how some of the features such as duration since address change, transaction amount, and available cash in the account were proven to be most important. 
# MAGIC While this is purely a machine learning-driven approach, we will look at ways to improve customer satisfaction with rule-based modeling versus relying entirely on ML.

# COMMAND ----------

# DBTITLE 1,Use SHAP for Model Explainability
# SHAP is a powerful tool for explaining the output of machine learning models, particularly in understanding how each feature contributes to the model's predictions.
# Use SHAP (SHapley Additive exPlanations) to explain the model's predictions and identify the most important features contributing to the model's decisions.

# Initializes a SHAP explainer specifically designed for tree-based models like XGBoost.
explainer = shap.TreeExplainer(xgb_model)

# Computes SHAP values for the input data X.
# X: The feature matrix used for prediction.
# y: The true labels (FRD_IND) corresponding to the input data.
shap_values = explainer.shap_values(X, y=y.values)

# Takes the absolute value of SHAP values to measure the magnitude of each feature's contribution, regardless of direction (positive or negative).
# Computes the mean absolute SHAP value for each feature across all observations.
mean_abs_shap = np.absolute(shap_values).mean(axis=0).tolist()

# Combines the mean absolute SHAP values with their corresponding feature names into pairs.
# Sorts the pairs in descending order based on the mean absolute SHAP values.
# Selects the top 8 features with the highest SHAP values.
# Converts the sorted list into a Spark DataFrame for display.
display(spark.createDataFrame(sorted(list(zip(mean_abs_shap, X.columns)), reverse=True)[:8], ["Mean |SHAP|", "Column"]))

# COMMAND ----------

# Validate the dimensions of the SHAP values and ensures they align with the input data.
# The .shape property returns a tuple (rows, columns), where rows corresponds to the number of observations and columns corresponds to the number of features.
# For example, (10000, 8) indicates 10,000 transactions and 8 features.
shap_values = explainer.shap_values(X, y=y.values)
print(shap_values.shape)

# COMMAND ----------

# Visualize the contribution of each feature to the prediction for a single observation (the first row in the dataset).
# This provides an interpretable visualization of how individual features contribute to the model's prediction for a specific transaction
# highlighting the positive and negative influences  of each feature on the predicted probability of fraud.
display(shap.force_plot(explainer.expected_value, shap_values[0,:], X.iloc[0,:],matplotlib=True))

# COMMAND ----------

# Convert the SHAP values (a NumPy array) into a Spark DataFrame.
# Add an id column to uniquely identify each observation.
# Rename the columns to avoid naming conflicts when joining with other tables.
# Register the resulting DataFrame as a temporary view (fraud_shap_values) for further querying and analysis.
# This can help integrate SHAP values into the broader fraud detection framework, enabling interactive querying and combining SHAP values with other data sources.

# Converts the SHAP values (a NumPy array) into a Pandas DataFrame.
# Assigns column names from the original feature matrix X to ensure alignment between features and their corresponding SHAP values.
# Converts the Pandas DataFrame into a Spark DataFrame
# Adds a unique identifier (id) to each row in the DataFrame.
df = spark.createDataFrame(pd.DataFrame(shap_values, columns=X.columns)).withColumn("id", monotonically_increasing_id())

# Renames all columns in the DataFrame by prefixing them with shap_v_.
for col in df.columns:
  df = df.withColumnRenamed(col, 'shap_v_' + col)

# Registers the DataFrame as a temporary view (fraud_shap_values) for interactive querying and analysis.
df.createOrReplaceTempView("fraud_shap_values")

# COMMAND ----------

# Combine the original feature matrix (X), the model's predictions (predictions), and the true labels (y) into a unified dataset.
# Add a unique identifier (id) to each row for alignment with other datasets (e.g., SHAP values).
# Register the resulting DataFrame as a temporary SQL view (txns) for further querying and analysis.

# pd.DataFrame(X, columns=X.columns): Converts the feature matrix X into a Pandas DataFrame with appropriate column names.
# pd.DataFrame(predictions, columns=['predicted']): Converts the predictions array into a DataFrame with a single column named predicted.
# pd.DataFrame(y, columns=['FRD_IND']): Converts the true labels (y) into a DataFrame with a single column named FRD_IND.
# pd.concat(..., axis=1): Concatenates these DataFrames horizontally (column-wise) to create a unified dataset.
     
spark.createDataFrame(pd.concat( \
  [pd.DataFrame(X, columns=X.columns), \
    pd.DataFrame(predictions, columns=['predicted']), \
    pd.DataFrame(y, columns=['FRD_IND'])], \
      axis=1))\
  .withColumn("id", monotonically_increasing_id())\
  .createOrReplaceTempView("txns")

# COMMAND ----------

# MAGIC %md
# MAGIC 
# MAGIC ### Model Result Saving 
# MAGIC 
# MAGIC In addition to saving model fraud scores, we want to be able to interactively query SHAP values on each observation also. 
# MAGIC We will persist these values on each observation so we can query in tabular form using SQL Analytics.

# COMMAND ----------

# Persist SHAP values and model predictions to a Delta Lake table for downstream analysis.
# Combine the transaction data (txns) with SHAP values (fraud_shap_values) into a unified dataset.
# Persist the combined dataset as a Delta Lake table (silver_fraud_shap_values) for downstream analysis and reporting.
# Enable interactive querying of SHAP values alongside transaction features, predictions, and true labels.

spark.sql("drop table if exists silver_fraud_shap_values")  # Drop the table if it already exists to avoid conflicts.

spark.sql("""
          select t.*, s.*
          from txns t
            join fraud_shap_values s on t.id = s.shap_v_id
          """)\
    .write\
    .format("delta")\
    .option('overwriteSchema', 'true')\
    .mode('overwrite')\
    .saveAsTable("silver_fraud_shap_values")

# COMMAND ----------

# MAGIC %md
# MAGIC The following cell evaluates model performance: Assess how well the model distinguishes between fraudulent and legitimate transactions.
# MAGIC It generates a Confusion Matrix: Group predictions and actual labels into categories (e.g., true positives, false positives, etc.) to understand the model's behavior.
# MAGIC Threshold-Based Classification: Convert the model's predicted probabilities (predicted) into binary predictions (fraudulent or not) using a threshold (0.5 in this case).
# MAGIC There will be four possible combinations:
# MAGIC - True Positive (TP): Predicted = 1, Actual = 1 (Correctly identified fraud).
# MAGIC - False Positive (FP): Predicted = 1, Actual = 0 (Incorrectly flagged as fraud).
# MAGIC - True Negative (TN): Predicted = 0, Actual = 0 (Correctly identified as legitimate).
# MAGIC - False Negative (FN): Predicted = 0, Actual = 1 (Missed fraud).


# COMMAND ----------

# DBTITLE 1,Fraud Absolute Dollar Amounts - Predicted vs Actual Amount Lost
# MAGIC %sql 
# MAGIC 
# MAGIC select case when predicted > 0.5 then 1 else 0 end predicted_Ind, frd_ind, count(1) ct
# MAGIC from silver_fraud_shap_values
# MAGIC group by case when predicted > 0.5 then 1 else 0 end, frd_ind

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC + <a href="$./02_dff_orchestration">STAGE2</a>: Building a fraud detection model
# MAGIC ---

# COMMAND ----------

# MAGIC %md
# MAGIC 
# MAGIC &copy; 2021 Databricks, Inc. All rights reserved. The source in this notebook is provided subject to the Databricks License [https://databricks.com/db-license-source].  All included or referenced third party libraries are subject to the licenses set forth below.
# MAGIC 
# MAGIC | library                                | description             | license    | source                                              |
# MAGIC |----------------------------------------|-------------------------|------------|-----------------------------------------------------|
# MAGIC | shap                                   | Model explainability    | MIT        | https://github.com/slundberg/shap                   |
# MAGIC | networkx                               | Graph toolkit           | BSD        | https://github.com/networkx                         |
# MAGIC | xgboost                                | Gradient Boosting lib.  | Apache2    | https://github.com/dmlc/xgboost                     |
# MAGIC | graphviz                               | Network visualization   | MIT        | https://github.com/xflr6/graphviz                   |
# MAGIC | pandasql                               | SQL syntax on pandas    | MIT        | https://github.com/yhat/pandasql/                   |
# MAGIC | pydot                                  | Network visualization   | MIT        | https://github.com/pydot/pydot                      |
# MAGIC | pygraphviz                             | Network visualization   | BSD        | https://pygraphviz.github.io/                       |
