# Databricks notebook source
# MAGIC %md 
# MAGIC You may find this series of notebooks at https://github.com/databricks-industry-solutions/fraud-orchestration. For more information about this solution accelerator, visit https://www.databricks.com/solutions/accelerators/fraud-detection.

# COMMAND ----------

# MAGIC %md
# MAGIC <img src=https://brysmiwasb.blob.core.windows.net/demos/dff/databricks_fsi_white.png width="600px">

# COMMAND ----------

# MAGIC %md
# MAGIC 
# MAGIC # Databricks fraud framework - Orchestration
# MAGIC 
# MAGIC The financial service industry (FSI) is rushing towards transformational change to support new channels and services, delivering transactional features and facilitating payments through new digital channels to remain competitive. Unfortunately, the speed and convenience that these capabilities afford is a benefit to consumers and fraudsters alike. Building a fraud framework often goes beyond just creating a highly accurate machine learning model due ever changing landscape and customer expectation. Oftentimes it involves a complex decision science setup which combines rules engine with a need for a robust and scalable machine learning platform. In this series of notebook, we'll be demonstrating how `Delta Lake`, `MLFlow` and a unified analytics platform can help organisations combat fraud more efficiently
# MAGIC 
# MAGIC ---
# MAGIC + <a href="$./01_dff_model">STAGE1</a>: Integrating rule based with ML
# MAGIC + <a href="$./02_dff_orchestration">STAGE2</a>: Building a fraud detection model
# MAGIC ---
# MAGIC 
# MAGIC + <sri.ghattamaneni@databricks.com>
# MAGIC + <nikhil.gupta@databricks.com>
# MAGIC + <ricardo.portilla@databricks.com>

# COMMAND ----------

# DBTITLE 1,Install binaries for graphviz
# MAGIC %sh -e sudo apt-get install graphviz libgraphviz-dev pkg-config -y

# COMMAND ----------

# MAGIC %pip install networkx==2.4 pandasql==0.7.3 graphviz==0.16 sqlalchemy==1.4.46 pygraphviz==1.7 pydot==1.4.2
# MAGIC %pip install databricks-sdk --upgrade

# COMMAND ----------

# COMMAND ----------

# DBTITLE 1,Build graph from DMN format
# Standard library imports
from typing import Any, Callable, Dict, List
from xml.dom import minidom

# Third-party imports
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import EndpointCoreConfigInput, ServedModelInput, ServedModelInputWorkloadSize
from graphviz import Digraph
from mlflow.pyfunc import PythonModel
from pandasql import sqldf
import mlflow
import mlflow.pyfunc
import networkx as nx
import pandas as pd
import random
import requests

# Databricks specific
import sklearn
import xgboost

# COMMAND ----------

# DBTITLE 1,Packages versions for debugging
import sys
import numpy

print("Python Version:", sys.version)
print("NumPy Version:", numpy.__version__)
print("NetworkX Version:", nx.__version__)
print("MLflow Version:", mlflow.__version__)

# COMMAND ----------

filename = '/tmp/dff_model'
extension = 'svg'

# COMMAND ----------

# DBTITLE 1,Decision Graph Construction
def parse_ruleset(ruleset_path: str) -> nx.DiGraph:
    """Parse DMN ruleset file and construct decision graph.
    
    Args:
        ruleset_path: Path to DMN ruleset file
        
    Returns:
        NetworkX directed graph representing decision workflow
    """
    xmldoc = minidom.parse(ruleset_path)
    itemlist = xmldoc.getElementsByTagName('dmn:decision')

    G = nx.DiGraph()
    for item in itemlist:
        node_id = item.attributes['id'].value
        node_decision = str(item.attributes['name'].value)
        G.add_node(node_id, decision=node_decision)
        
        infolist = item.getElementsByTagName("dmn:informationRequirement")
        if infolist:
            info = infolist[0]
            for req in info.getElementsByTagName("dmn:requiredDecision"):
                parent_id = req.attributes['href'].value.split('#')[-1]
                G.add_edge(parent_id, node_id)
    
    return G

ruleset_path = "/dbfs/tmp/dff/DFF_Ruleset.dmn"
G = parse_ruleset(ruleset_path)

# COMMAND ----------
# DBTITLE 1,Visualization Utilities

def render_decision_graph(g: nx.DiGraph) -> Digraph:
    """Generate Graphviz visualization of the decision graph.
    
    Args:
        g: NetworkX graph to visualize
        
    Returns:
        Graphviz Digraph object ready for rendering
    """
    dot = Digraph(comment='The Fraud Engine', format=extension)
    atts: Dict[str, str] = nx.get_node_attributes(G, 'decision')
    
    for node_id, decision in atts.items():
        dot.node(node_id, decision, color='blue', shape='box', fontname="courier")
    
    for edge in g.edges():
        dot.edge(edge[0], edge[1])
        
    return dot

dot = render_decision_graph(G)
dot.render(filename=filename)
displayHTML(dot.pipe().decode('utf-8'))

# COMMAND ----------
# DBTITLE 1,Workflow Validation

if not nx.is_directed_acyclic_graph(G):
    raise ValueError("Workflow is not a valid DAG")

# COMMAND ----------

# DBTITLE 1,Topological sorting
# Our core logic is to traverse our graph in order, calling parent rules before children
# Although we could recursively parse our tree given a root node ID, it is much more convenient (and less prone to error) to sort our graph topologically
# ... accessing each rule in each layer
decisions: Dict[str, str] = nx.get_node_attributes(G, 'decision')
execution_order = [decisions[rule] for rule in nx.topological_sort(G)]
pd.DataFrame(execution_order, columns=['stage'])

# COMMAND ----------

# DBTITLE 1,Create our orchestrator model

class DFF_Model(PythonModel):
  """For rule based, we simply match record against predefined SQL where clause
    If rule matches, we return 1, else 0

  Attributes:
    G: Decision workflow graph
    sensitivity: Threshold for rule activation
    rules: Loaded decision rules
  """
  def __init__(self, G: nx.DiGraph, sensitivity: float):
    '''
    We define our PyFunc model using a DAG (a serialized NetworkX object) and a predefined sensitivity
    Although rule based would be binary (0 or 1), ML based would not necessarily, and we need to define a sensitivity upfront 
    to know if we need to traverse our tree any deeper (in case we chain multiple ML models)
    '''
    self.G = G
    self.sensitivity = sensitivity
    self.rules: List[Any] = []

  def _create_sql_rule(self, sql: str) -> Callable[[pd.DataFrame], int]:
    """Create SQL-based decision rule function.
    
    Warning: This implementation contains potential SQL injection vulnerabilities
    and should not be used in production without proper sanitization.
    """
    def _execute_rule(input_df: pd.DataFrame) -> int:
      query = f"SELECT CASE WHEN {sql} THEN 1 ELSE 0 END AS predicted FROM input_df"
      return sqldf(query).predicted.iloc[0]

    return _execute_rule
  
  def _create_model_rule(self, model_uri: str) -> Callable[[pd.DataFrame], float]:
    """Create ML model-based decision rule function."""
    model = mlflow.pyfunc.load_model(model_uri)
    return lambda df: model.predict(df).predicted.iloc[0]  
  
  '''
  At model startup, we traverse our DAG and load all business logic required at scoring phase
  Although it does not change much on the rule execution logic, we would be loading models only once at model startup (not at scoring)
  '''
  def load_context(self, context) -> None:
    """Initialize model execution context."""
    decisions = nx.get_node_attributes(self.G, 'decision')
    
    for rule_id in nx.topological_sort(self.G):
      # we retrieve the SQL syntax of the rule or the URI of a model
      decision = decisions[rule_id]
      if decision.startswith("models:/"):
        # we load ML model only once as a function that we can call later
        self.rules.append((rule_id, self._create_model_rule(decision)))
      else:
        # we load a SQL statement as a function that we can call later
        self.rules.append((rule_id, self._create_sql_rule(decision)))
  
  def _process_record(self, record: pd.Series) -> str:
    """Process individual transaction record through decision workflow."""
    input_df = pd.DataFrame([record.values], columns=record.index)
    
    for rule_id, rule_func in self.rules:
      # run next rule on
      prediction = rule_func(input_df)
      if prediction >= self.sensitivity:
        return rule_id

    return None
  
  def predict(self, context, df: pd.DataFrame) -> pd.Series:
    '''
    After multiple considerations, we defined our model to operate on a single record only and not against an entire dataframe
    This helps us to be much more precise in what data was triggered against what rule / model and what chunk would need to be 
    evaluated further
    '''
    return df.apply(self._process_record, axis=1)

# COMMAND ----------

# DBTITLE 1,Include 3rd party dependencies
# we may have to store additional libraries such as networkx and pandasql
conda_env = mlflow.pyfunc.get_default_conda_env()
conda_env['dependencies'][2]['pip'].extend([
    f'networkx=={nx.__version__}',
    'pandasql==0.7.3',
    f'xgboost=={xgboost.__version__}',
    f'scikit-learn=={sklearn.__version__}',
    'numpy<1.20'
])
conda_env

# COMMAND ----------

# DBTITLE 1,Create our experiment
user_email = dbutils.notebook.entry_point.getDbutils().notebook().getContext().userName().get()
mlflow.set_experiment(f"/Users/{user_email}/dff_orchestrator")

with mlflow.start_run(run_name='fraud_model'):
  # we define a sensitivity of 0.7, that is that probability of a record to be fraudulent for ML model needs to be at least 70%
  # TODO: explain how sensitivity could be dynamically pulled from a MLFlow model (tag, metrics, etc.)
  mlflow.pyfunc.log_model('model', python_model=DFF_Model(G, 0.7), conda_env=conda_env)
  mlflow.log_artifact(f"{filename}.{extension}")
  run_id = mlflow.active_run().info.run_id

# COMMAND ----------

# DBTITLE 1,Register framework
client = mlflow.tracking.MlflowClient()
model_uri = f"runs:/{run_id}/model"
model_name = "dff_orchestrator"
result = mlflow.register_model(model_uri, model_name)
version = result.version

# COMMAND ----------

# DBTITLE 1,List registered models
from mlflow.tracking import MlflowClient

client = MlflowClient()
models = [model.name for model in client.search_registered_models()]
print(models)

# COMMAND ----------

# DBTITLE 1,List model versions
versions = client.get_latest_versions(name="dff_orchestrator", stages=None)
for version in versions:
  print(f"Version: {version.version}, Stage: {version.current_stage}")

# COMMAND ----------

# DBTITLE 1,Create/Update Serving Endpoint
def create_or_update_endpoint(model_name: str, version: int, endpoint_name: str = "dff-orchestrator-endpoint"):
  """Automatically creates or updates a serving endpoint for the specified model.

  Args:
    model_name (str): Name of the registered model in the Model Registry.
    version (int): Version of the model to deploy.
    endpoint_name (str, optional): Name of the serving endpoint. Defaults to "dff-orchestrator-endpoint".

  Raises:
    Exception: If the Databricks API request fails.

  Notes:
    - If the endpoint already exists, it updates the endpoint with the new model version.
    - If the endpoint does not exist, it creates a new endpoint with the specified model.
    - The endpoint is configured with a "Small" workload size and scale-to-zero enabled.
  """
  w = WorkspaceClient()
  print(f"Token: {w.config.token}") 
  
  # Check if endpoint exists
  try:
    print(f"Fetching endpoint: {endpoint_name}")
    print(f"WorkspaceClient Config: {w.config}")

    endpoint = w.serving_endpoints.get(endpoint_name)
    print(f"Endpoint found: {endpoint}")

    # Update existing endpoint
    print(f"Updating endpoint: {endpoint_name}")
    w.serving_endpoints.update_config(
      name=endpoint_name,
      served_models=[
        ServedModelInput(
          model_name=model_name,
          model_version=version,
          workload_size=ServedModelInputWorkloadSize.SMALL,
          scale_to_zero_enabled=True
        )
      ]
    )
    print("Endpoint updated successfully")
  except Exception as e:
    print(f"EXCEPTION: WorkspaceClient Config: {w.config}")
    print(f"Endpoint not found or error: {str(e)}")

    # Create new endpoint
    print(f"Creating new endpoint: {endpoint_name}")
    w.serving_endpoints.create(
      name=endpoint_name,
      config=EndpointCoreConfigInput(
        served_models=[
          ServedModelInput(
            model_name=model_name,
            model_version=version,
            workload_size=ServedModelInputWorkloadSize.SMALL,
            scale_to_zero_enabled=True
          )
        ]
      )
    )
    print("Endpoint created successfully")

print(f"Model Name: {model_name} (Type: {type(model_name)})")
print(f"Model Version: {version.version} (Type: {type(version.version)})")

# Call this right after model version staging transition
create_or_update_endpoint(model_name, version.version)

# COMMAND ----------

# DBTITLE 1,Register model to staging
# archive any staging versions of the model from prior runs
for mv in client.search_model_versions("name='{0}'".format(model_name)):
  
    # if model with this name is marked staging
    if mv.current_stage.lower() == 'staging':
      # mark is as archived
      client.transition_model_version_stage(
        name=model_name,
        version=mv.version,
        stage='archived'
        )
      
client.transition_model_version_stage(
  name=model_name,
  version=version,
  stage="staging",
)

# COMMAND ----------

# DBTITLE 1,Create widgets
dbutils.widgets.text("CDHLDR_PRES_CD", "0")
dbutils.widgets.text("ACCT_CL_AMT", "10000")
dbutils.widgets.text("LAST_ADR_CHNG_DUR", "301")
dbutils.widgets.text("ACCT_AVL_CASH_BEFORE_AMT", "100")
dbutils.widgets.text("AUTHZN_AMT", "30")
dbutils.widgets.text("DISTANCE_FROM_HOME", "1000")
dbutils.widgets.text("AUTHZN_OUTSTD_CASH_AMT", "40")
dbutils.widgets.text("AVG_DLY_AUTHZN_AMT", "25")

# COMMAND ----------

#run_id
# Score dataframe against DFF orchestration engine
model = mlflow.pyfunc.load_model(f"runs:/{run_id}/model")

# COMMAND ----------

# DBTITLE 1,Validate framework
df_dict = {}
for col in ['ACCT_PROD_CD', 'ACCT_AVL_CASH_BEFORE_AMT', 'ACCT_AVL_MONEY_BEFORE_AMT',
       'ACCT_CL_AMT', 'ACCT_CURR_BAL', 'APPRD_AUTHZN_CNT',
       'APPRD_CASH_AUTHZN_CNT', 'AUTHZN_AMT', 'AUTHZN_OUTSTD_AMT',
       'AVG_DLY_AUTHZN_AMT', 'AUTHZN_OUTSTD_CASH_AMT', 'CDHLDR_PRES_CD',
       'HOTEL_STAY_CAR_RENTL_DUR', 'LAST_ADR_CHNG_DUR',
       'HOME_PHN_NUM_CHNG_DUR', 'PLSTC_ISU_DUR', 'POS_COND_CD',
       'POS_ENTRY_MTHD_CD', 'DISTANCE_FROM_HOME', 'FRD_IND']:
  try:
    df_dict[col] = [float(dbutils.widgets.get(col))]
  except:
    df_dict[col] = [random.uniform(1, 10)]

pdf = pd.DataFrame.from_dict(df_dict)

# Score dataframe against DFF orchestration engine
model = mlflow.pyfunc.load_model(f"runs:/{run_id}/model")
decision = model.predict(pdf).iloc[0]

def toGraphViz_triggered(g):
  """Visualize our rule set and which one was triggered (if any)
  
  Args:
    g: NetworkX graph to visualize
      
  Returns:
    Graphviz Digraph object with triggered node highlighted
  """
  dot = Digraph(
    comment='The Fraud Engine',
    format=extension,
    filename='/tmp/dff_triggered'
  )
  
  # Get node attributes
  atts = nx.get_node_attributes(g, 'decision')
  
  # Add nodes with conditional styling
  for node, att in atts.items():
    node_style = {
      'color': 'red' if att == decision else 'blue',
      'shape': 'box',
      'fontname': 'courier'
    }
    dot.node(node, att, **node_style)
  
  # Add edges
  for edge in g.edges:
    dot.edge(edge[0], edge[1])
  return dot

dot = toGraphViz_triggered(G)
dot.render()
displayHTML(dot.pipe().decode('utf-8'))

# COMMAND ----------

# DBTITLE 1,Model Serving Test (Enable Model Serving to run)
def score_model(dataset: pd.DataFrame) -> Dict[str, Any]:
  """Score transaction data using deployed model endpoint.
  
  Args:
    dataset: Pandas DataFrame containing transaction features
      
  Returns:
    Model prediction response as dictionary
      
  Raises:
    RuntimeError: If model serving request fails
    ValueError: If response contains unexpected format
  """
  # Get Databricks API token
  try:
    token = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().getOrElse(None)
    if not token:
      raise ValueError("Missing Databricks API token")
  except Exception as e:
    raise RuntimeError("Failed to retrieve API token") from e

# Get workspace URL from Spark config
  workspace_host = spark.conf.get("spark.databricks.workspaceUrl")
  if not workspace_host:
    raise ValueError("Workspace URL not found in Spark configuration")
  
  # Construct model endpoint URL
  endpoint_name = "dff-orchestrator-endpoint"
  url = f"https://{workspace_host}/serving-endpoints/{endpoint_name}/invocations"
  headers = {'Authorization': f'Bearer {token}'}
  data_json = {"dataframe_split": dataset.to_dict(orient='split')}
  response = requests.request(method='POST', headers=headers, url=url, json=data_json)
  if response.status_code != 200:
    raise Exception(f'Request to {url} failed with status {response.status_code}. Message: {response.text}')
  return response.json()

try:
  decision = score_model(pdf)['predictions'][0]['0']
  if (decision is None ):
    displayHTML("<h3>VALID TRANSACTION</h3>")
  else:
    displayHTML(f"<h3>FRAUDULENT TRANSACTION: {decision}</h3>")
except Exception as e:
  displayHTML(f"<h3>EXCEPTION</h3><p>{str(e)}</p>")

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC + <a href="$./01_dff_model">STAGE1</a>: Integrating rule based with ML
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
# MAGIC | pandasql                               | SQL syntax on pandas    | Yhat, Inc  | https://github.com/yhat/pandasql/                   |
# MAGIC | pydot                                  | Network visualization   | MIT        | https://github.com/pydot/pydot                      |
# MAGIC | pygraphviz                             | Network visualization   | BSD        | https://pygraphviz.github.io/                       |
