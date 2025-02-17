<img src=https://d1r5llqwmkrl74.cloudfront.net/notebooks/fsi/fs-lakehouse-logo-transparent.png width="600px">

# Databricks fraud framework

The financial service industry (FSI) is rushing towards transformational change to support new channels and services, delivering transactional features and facilitating payments through new digital channels to remain competitive. Unfortunately, the speed and convenience that these capabilities afford is a benefit to consumers and fraudsters alike. Building a fraud framework often goes beyond just creating a highly accurate machine learning model due ever changing landscape and customer expectation. Oftentimes it involves a complex decision science setup which combines rules engine with a need for a robust and scalable machine learning platform. In this series of notebook, we'll be demonstrating how `Delta Lake`, `MLFlow` and a unified analytics platform can help organisations combat fraud more efficiently

---
+ <a href="$./01_dff_model">STAGE1</a>: Integrating rule based with ML
+ <a href="$./02_dff_orchestration">STAGE2</a>: Building a fraud detection model
---

+ <sri.ghattamaneni@databricks.com>
+ <nikhil.gupta@databricks.com>
+ <ricardo.portilla@databricks.com>

### Flow chart diagram
This flow chart details the execution order when deploying using the "Deploy to azure" button.

```mermaid
flowchart TD
    A[Click on Deploy to azure button] --> B[Provision Azure Resources]
        %% Sub-steps for Provisioning
        subgraph Provisioning
        B --> C[Deployment Script Execution]
        C --> D[Call job-template.json]
        D --> E[Create Databricks Job & Cluster]
        E --> F[RUNME.py Execution]


        F --> G[Install Utility Packages]
        I --> J[Databricks Job Execution]
        J --> K[Execute Notebooks in Order]

        %% Sub-steps for RUNME.py Execution
        subgraph RUNME.py
        G --> H[Define Workflow]
        H --> I[Deploy Compute & Job]
        end

        K --> K1

        %% Notebook Execution Order
        subgraph Notebook execution
        K1[00_dff_context] --> K2[01_dff_model]
        K2 --> K3[02_dff_orchestration]
        end
        end

        %% Change the color of the subgraph
        style Provisioning fill:#007FFF, stroke:#333, stroke-width:2px
        style Provisioning run fill:#007FFF, stroke:#333, stroke-width:2px
```
___

&copy; 2021 Databricks, Inc. All rights reserved. The source in this notebook is provided subject to the Databricks License [https://databricks.com/db-license-source].  All included or referenced third party libraries are subject to the licenses set forth below.

| library                                | description             | license    | source                                              |
|----------------------------------------|-------------------------|------------|-----------------------------------------------------|
| shap                                   | Model explainability    | MIT        | https://github.com/slundberg/shap                   |
| networkx                               | Graph toolkit           | BSD        | https://github.com/networkx                         |
| xgboost                                | Gradient Boosting lib.  | Apache2    | https://github.com/dmlc/xgboost                     |
| graphviz                               | Network visualization   | MIT        | https://github.com/xflr6/graphviz                   |
| pandasql                               | SQL syntax on pandas    | MIT        | https://github.com/yhat/pandasql/                   |
| pydot                                  | Network visualization   | MIT        | https://github.com/pydot/pydot                      |
| pygraphviz                             | Network visualization   | BSD        | https://pygraphviz.github.io/                       |