@allowed([
  'new'
  'existing'
])
param newOrExistingWorkspace string = 'new'

@description('The name of the Azure Databricks workspace to create.')
param databricksResourceName string

@description('Specifies whether to deploy Azure Databricks workspace with Secure Cluster Connectivity (No Public IP) enabled or not')
param disablePublicIp bool = false

@description('The pricing tier of workspace.')
@allowed([
  'standard'
  'premium'
])
param sku string = 'standard'

var deploymentId = guid(resourceGroup().id)
var deploymentIdShort = substring(deploymentId, 0, 8)
var acceleratorRepoName = 'databricks-accelerator-fraud-orchestration'
var managedResourceGroupName = 'databricks-rg-${databricksResourceName}-${uniqueString(databricksResourceName, resourceGroup().id)}'
var trimmedMRGName = substring(managedResourceGroupName, 0, min(length(managedResourceGroupName), 90))
var managedResourceGroupId = subscriptionResourceId('Microsoft.Resources/resourceGroups', trimmedMRGName)

// Managed Identity
resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'dbw-id-${deploymentIdShort}'
  location: resourceGroup().location
}

// Create Databricks Workspace if `newOrExistingWorkspace` is 'new'
resource newDatabricks 'Microsoft.Databricks/workspaces@2024-05-01' = if (newOrExistingWorkspace == 'new') {
  name: databricksResourceName
  location: resourceGroup().location
  sku: {
    name: sku
  }
  properties: {
    managedResourceGroupId: managedResourceGroupId
    parameters: {
      enableNoPublicIp: {
        value: disablePublicIp
      }
    }
  }
}

// Reference to an existing Databricks workspace if `newOrExistingWorkspace` is 'existing'
resource databricks 'Microsoft.Databricks/workspaces@2024-05-01' existing = if (newOrExistingWorkspace == 'existing') {
  name: databricksResourceName
}

// Role Assignment (Contributor Role)
resource databricksRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(managedIdentity.id, 'Contributor', databricks.id ?? newDatabricks.id)
  scope: databricks ?? newDatabricks
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'b24988ac-6180-42a0-ab88-20f7382dd24c' // Contributor role ID
    )
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Deployment Script
resource deploymentScript 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'setup-databricks-script'
  location: resourceGroup().location
  kind: 'AzureCLI'
  properties: {
    azCliVersion: '2.9.1'
    scriptContent: '''
      set -e

      # Install dependencies
      apt-get update && apt-get install -yq unzip curl jq

      # Clone the GitHub repository
      echo "Cloning the GitHub repository..."
      databricks repos create https://github.com/southworks/${ACCELERATOR_REPO_NAME} gitHub

      # Debugging: List the contents of the workspace
      echo "Listing workspace contents..."
      databricks workspace ls /Users/${ARM_CLIENT_ID}/${ACCELERATOR_REPO_NAME}

      # Export the job template
      echo "Exporting job template..."
      databricks workspace export /Users/${ARM_CLIENT_ID}/${ACCELERATOR_REPO_NAME}/bicep/job-template.json > job-template.json

      # Debugging: Check if the file was exported successfully
      if [ ! -s job-template.json ]; then
        echo "Error: job-template.json is empty or does not exist."
        exit 1
      fi

      # Modify the notebook path in the JSON file
      echo "Modifying notebook path in job template..."
      notebook_path="/Users/${ARM_CLIENT_ID}/${ACCELERATOR_REPO_NAME}/RUNME"
      jq ".tasks[0].notebook_task.notebook_path = \"${notebook_path}\"" job-template.json > job.json

      # Validate the modified JSON file
      if ! jq empty job.json; then
        echo "Error: job.json is invalid."
        exit 1
      fi

      # Submit the Databricks job
      echo "Submitting Databricks job..."
      databricks jobs submit --json @./job.json
    '''
    environmentVariables: [
      {
        name: 'DATABRICKS_AZURE_RESOURCE_ID'
        value: databricks.id ?? newDatabricks.id
      }
      {
        name: 'ARM_CLIENT_ID'
        value: managedIdentity.properties.clientId
      }
      {
        name: 'ARM_USE_MSI'
        value: 'true'
      }
      {
        name: 'ACCELERATOR_REPO_NAME'
        value: acceleratorRepoName
      }
    ]
    timeout: 'PT20M'
    cleanupPreference: 'OnSuccess'
    retentionInterval: 'PT1H'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  dependsOn: [
    databricksRoleAssignment
  ]
}

// Outputs
output databricksWorkspaceUrl string = 'https://${(databricks ?? newDatabricks).properties.workspaceUrl}'
output databricksJobUrl string = 'https://${(databricks ?? newDatabricks).properties.workspaceUrl}/#job/${deploymentScript.properties.outputs.job_id}'
