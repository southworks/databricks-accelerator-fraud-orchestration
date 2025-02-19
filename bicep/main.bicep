@description('The name of the Azure Databricks workspace to create. It must be between 3 and 64 characters long and can only contain alphanumeric characters, underscores (_), hyphens (-), and periods (.).')
@minLength(3)
@maxLength(64)
param databricksResourceName string

@description('The pricing tier of workspace. If the Databricks service already exists, it will not be updated.')
@allowed([
  'standard'
  'premium'
])
param sku string = 'standard'

var acceleratorRepoName = 'databricks-accelerator-fraud-orchestration'
var randomString = uniqueString(resourceGroup().id, databricksResourceName, acceleratorRepoName)
var managedResourceGroupName = 'databricks-rg-${databricksResourceName}-${randomString}'
var location = resourceGroup().location

// Managed Identity
resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: randomString
  location: location
}

resource resourceGroupRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(randomString)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'b24988ac-6180-42a0-ab88-20f7382dd24c' // Contributor role ID
    )
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource createOrUpdateDatabricks 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'createOrUpdateDatabricks-${randomString}'
  location: location
  kind: 'AzurePowerShell'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    azPowerShellVersion: '9.0'
    arguments: '-resourceName ${databricksResourceName} -resourceGroupName  ${resourceGroup().name} -location ${resourceGroup().location} -sku ${sku} -managedResourceGroupName ${managedResourceGroupName}'
    scriptContent: '''
      param([string] $resourceName,
        [string] $resourceGroupName,
        [string] $location,
        [string] $sku,
        [string] $managedResourceGroupName)

      # Check if workspace exists
      $resource = Get-AzDatabricksWorkspace -Name $resourceName -ResourceGroupName $resourceGroupName | Select-Object -Property ResourceId

      if (-not $resource) {
        # Create new workspace
        Write-Output "Creating new Databricks workspace: $resourceName"
        New-AzDatabricksWorkspace -Name $resourceName `
          -ResourceGroupName $resourceGroupName `
          -Location $location `
          -ManagedResourceGroupName $managedResourceGroupName `
          -Sku $sku

        # Wait for provisioning to complete
        $retryCount = 0
        do {
          Start-Sleep -Seconds 15
          $provisioningState = (Get-AzDatabricksWorkspace -Name $resourceName -ResourceGroupName $resourceGroupName).ProvisioningState
          Write-Output "Current state: $provisioningState (attempt $retryCount)"
          $retryCount++
        } while ($provisioningState -ne 'Succeeded' -and $retryCount -le 40)
      }

      # Output the workspace ID to signal completion
      $workspace = Get-AzDatabricksWorkspace -Name $resourceName -ResourceGroupName $resourceGroupName
      echo "{\"WorkspaceId\": \"$workspace.Id\", \"Exists\": \"True"}" > $AZ_SCRIPTS_OUTPUT_PATH
    '''
    timeout: 'PT1H'
    cleanupPreference: 'OnSuccess'
    retentionInterval: 'PT2H'
  }
}

resource databricks 'Microsoft.Databricks/workspaces@2024-05-01' existing = {
  name: databricksResourceName
  dependsOn: [
    createOrUpdateDatabricks
  ]
}

resource createDatabricksJob 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'createDatabricksJob-${randomString}'
  location: location
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
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
}

// Outputs
output databricksWorkspaceId string = databricks.id
output databricksJobUrl string = createDatabricksJob.properties.outputs.job_page_url
