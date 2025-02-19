param acceleratorRepoName string
param databricksResourceName string
param location string
param managedIdentityName string

resource databricks 'Microsoft.Databricks/workspaces@2024-05-01' existing = {
  name: databricksResourceName
}

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: managedIdentityName
}

// Deployment Script
resource jobCreation 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'job-creation-${acceleratorRepoName}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  kind: 'AzureCLI'
  properties: {
    azCliVersion: '2.9.1'
    scriptContent: '''
      set -e
      curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh

      databricks repos create https://github.com/southworks/${ACCELERATOR_REPO_NAME} gitHub

      databricks workspace export /Users/${ARM_CLIENT_ID}/${ACCELERATOR_REPO_NAME}/bicep/job-template.json > job-template.json
      notebook_path="/Users/${ARM_CLIENT_ID}/${ACCELERATOR_REPO_NAME}/RUNME"
      jq ".tasks[0].notebook_task.notebook_path = \"${notebook_path}\"" job-template.json > job.json

      job_page_url=$(databricks jobs submit --json @./job.json | jq -r '.run_page_url')
      echo "{\"job_page_url\": \"$job_page_url\"}" > $AZ_SCRIPTS_OUTPUT_PATH
      '''
    environmentVariables: [
      {
        name: 'DATABRICKS_AZURE_RESOURCE_ID'
        value: databricks.id
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
    timeout: 'PT1H'
    cleanupPreference: 'OnSuccess'
    retentionInterval: 'PT2H'
  }
}

output databricksWorkspaceUrl string = 'https://${databricks.properties.workspaceUrl}'
output databricksJobUrl string = jobCreation.properties.outputs.job_page_url
