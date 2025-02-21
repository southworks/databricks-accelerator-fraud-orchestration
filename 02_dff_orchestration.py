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

# COMMAND ----------

# COMMAND ----------

# DBTITLE 1,Import Libraries
# Import necessary libraries for graph manipulation, model orchestration, and visualization.
from graphviz import Digraph
from mlflow.pyfunc import PythonModel
from pandasql import sqldf
from typing import Any, Callable, Dict, List
from xml.dom import minidom
import mlflow
import mlflow.pyfunc
import networkx as nx
import pandas as pd
import random
import sklearn
import xgboost

# Define file paths and extensions for visualization outputs.
filename = '/tmp/dff_model'
extension = 'svg'

# COMMAND ----------

# DBTITLE 1,Decision Graph Construction
# Parse a DMN ruleset file to construct a decision graph using NetworkX.
# This function reads the DMN file, extracts decision nodes and their relationships, and builds a directed acyclic graph (DAG).
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

# Path to the DMN ruleset file.
ruleset_path = "/dbfs/tmp/dff/DFF_Ruleset.dmn"
G = parse_ruleset(ruleset_path)

# COMMAND ----------

# DBTITLE 1,Visualization Utilities
# Generate a Graphviz visualization of the decision graph.
# This function creates a visual representation of the DAG, highlighting nodes and edges.
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

# Render the decision graph and display it.
dot = render_decision_graph(G)
dot.render(filename=filename)
displayHTML(dot.pipe().decode('utf-8'))

# COMMAND ----------

# DBTITLE 1,Workflow Validation
# Validate that the decision graph is a valid Directed Acyclic Graph (DAG).
# Ensures there are no cycles in the graph, which would prevent proper execution.
if not nx.is_directed_acyclic_graph(G):
    raise ValueError("Workflow is not a valid DAG")

# COMMAND ----------

# DBTITLE 1,Topological Sorting
# Perform topological sorting to determine the execution order of rules and models.
# This ensures that parent nodes are executed before their children.
decisions: Dict[str, str] = nx.get_node_attributes(G, 'decision')
execution_order = [decisions[rule] for rule in nx.topological_sort(G)]
pd.DataFrame(execution_order, columns=['stage'])

# COMMAND ----------

# DBTITLE 1,Create Orchestrator Model
# Define a custom PyFunc model to orchestrate the execution of rules and ML models.
# This class traverses the decision graph and applies rules/models to input data.
class DFF_Model(PythonModel):
  """For rule-based logic, match records against predefined SQL WHERE clauses.
    If a rule matches, return 1; otherwise, return 0.

  Attributes:
    G: Decision workflow graph
    sensitivity: Threshold for rule activation
    rules: Loaded decision rules
  """
  def __init__(self, G: nx.DiGraph, sensitivity: float):
    '''
    Define PyFunc model using a DAG (a serialized NetworkX object) and a predefined sensitivity
    Although rule based would be binary (0 or 1), ML based would not necessarily.
    Define a sensitivity upfront to know if we need to traverse our tree any deeper (in case we chain multiple ML models)
    '''
    self.G = G
    self.sensitivity = sensitivity
    self.rules: List[Any] = []

  # Create a SQL-based decision rule function.
  def _create_sql_rule(self, sql: str) -> Callable[[pd.DataFrame], int]:
    """Create SQL-based decision rule function.

    Warning: This implementation contains potential SQL injection vulnerabilities
    and should not be used in production without proper sanitization.
    """
    def _execute_rule(input_df: pd.DataFrame) -> int:
      query = f"SELECT CASE WHEN {sql} THEN 1 ELSE 0 END AS predicted FROM input_df"
      return sqldf(query).predicted.iloc[0]

    return _execute_rule

  # Create an ML model-based decision rule function.
  def _create_model_rule(self, model_uri: str) -> Callable[[pd.DataFrame], float]:
    """Create ML model-based decision rule function."""
    model = mlflow.pyfunc.load_model(model_uri)
    return lambda df: model.predict(df).predicted.iloc[0]

  # Load all business logic required at scoring phase.
  def load_context(self, context) -> None:
    """Initialize model execution context."""
    decisions = nx.get_node_attributes(self.G, 'decision')
    
    for rule_id in nx.topological_sort(self.G):
      # Retrieve the SQL syntax of the rule or the URI of a model
      decision = decisions[rule_id]
      if decision.startswith("models:/"):
        # Load ML model only once as a function that we can call later.
        self.rules.append((rule_id, self._create_model_rule(decision)))
      else:
        # Load a SQL statement as a function that we can call later.
        self.rules.append((rule_id, self._create_sql_rule(decision)))

  # Process individual transaction records through the decision workflow.
  def _process_record(self, record: pd.Series) -> str:
    """Process individual transaction record through decision workflow."""
    input_df = pd.DataFrame([record.values], columns=record.index)
    
    for rule_id, rule_func in self.rules:
      # Run next rule on
      prediction = rule_func(input_df)
      if prediction >= self.sensitivity:
        return rule_id

    return None

  # Predict outcomes for a batch of records.
  def predict(self, context, df: pd.DataFrame) -> pd.Series:
    '''
    After multiple considerations, we defined our model to operate on a single record only and not against an entire dataframe.
    This helps us to be much more precise in what data was triggered against what rule/model and what chunk would need to be evaluated further.
    '''
    return df.apply(self._process_record, axis=1)

# COMMAND ----------

# DBTITLE 1,Include 3rd Party Dependencies
# Add third-party dependencies (e.g., NetworkX, Pandasql) to the Conda environment for serving the model.
conda_env = mlflow.pyfunc.get_default_conda_env()
conda_env['dependencies'][2]['pip'].extend([
    f'networkx=={nx.__version__}',
    'pandasql==0.7.3',
    f'xgboost=={xgboost.__version__}',
    f'scikit-learn=={sklearn.__version__}'
])
conda_env

# COMMAND ----------

# DBTITLE 1,Create Experiment
# Set up an MLflow experiment to track the training and registration of the orchestrator model.
user_email = dbutils.notebook.entry_point.getDbutils().notebook().getContext().userName().get()
mlflow.set_experiment(f"/Users/{user_email}/dff_orchestrator")

with mlflow.start_run(run_name='fraud_model'):
  # Define a sensitivity threshold of 0.7 for ML model predictions, , that is that probability of a record to be fraudulent for ML model needs to be at least 70%
  # TODO: explain how sensitivity could be dynamically pulled from a MLFlow model (tag, metrics, etc.)
  mlflow.pyfunc.log_model('model', python_model=DFF_Model(G, 0.7), conda_env=conda_env)
  mlflow.log_artifact(f"{filename}.{extension}")
  run_id = mlflow.active_run().info.run_id

# COMMAND ----------

# DBTITLE 1,Register Framework
# Register the orchestrator model in the MLflow Model Registry.
client = mlflow.tracking.MlflowClient()
model_name = "dff_orchestrator"
model_uri = f"runs:/{run_id}/model"
result = mlflow.register_model(model_uri, model_name)
version = result.version

# COMMAND ----------

# DBTITLE 1,Register Model to Staging
# Archive any existing staging versions of the model and transition the new version to "Staging".
for mv in client.search_model_versions(f"name='{model_name}'"):
    if mv.current_stage.lower() == 'staging':
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

# DBTITLE 1,Create Widgets
# Create widgets for interactive scoring of transactions.
dbutils.widgets.text("CDHLDR_PRES_CD", "0")
dbutils.widgets.text("ACCT_CL_AMT", "10000")
dbutils.widgets.text("LAST_ADR_CHNG_DUR", "301")
dbutils.widgets.text("ACCT_AVL_CASH_BEFORE_AMT", "100")
dbutils.widgets.text("AUTHZN_AMT", "30")
dbutils.widgets.text("DISTANCE_FROM_HOME", "1000")
dbutils.widgets.text("AUTHZN_OUTSTD_CASH_AMT", "40")
dbutils.widgets.text("AVG_DLY_AUTHZN_AMT", "25")

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

# Load the model from the MLflow Model Registry and score the input data.
model = mlflow.pyfunc.load_model(f"runs:/{run_id}/model")
decision = model.predict(pdf).iloc[0]

# Highlight the triggered node in the decision graph.
def toGraphViz_triggered(g):
  """Visualize our rule set and which one was triggered (if any)
  
  Args:
    g: NetworkX graph to visualize
      
  Returns:
    Graphviz Digraph object with triggered node highlighted
  """
  dot = Digraph(
    comment='Fraud Detection Engine',
    format=extension,
    filename='/tmp/dff_triggered'
  )
  
  # Get node attributes
  atts = nx.get_node_attributes(g, 'decision')
  
  # Add nodes with conditional styling
  for node, att in atts.items():
    node_style = {
      'color': 'red' if att == decision else 'green',
      'shape': 'box',
      'fontname': 'courier',
      'fontcolor': 'yellow'
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

# Display the result
if decision is None:
  displayHTML("VALID TRANSACTION")
else:
  displayHTML(f"FRAUDULENT TRANSACTION: {decision}")

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
