@description('The name of the Azure Databricks workspace to create. It must be between 3 and 64 characters long and can only contain alphanumeric characters, underscores (_), hyphens (-), and periods (.).')
@minLength(3)
@maxLength(64)
param databricksResourceName string

@description('The pricing tier of workspace.')
@allowed([
  'standard'
  'premium'
])
param sku string = 'standard'
var managedResourceGroupName = 'databricks-rg-${databricksResourceName}-${uniqueString(databricksResourceName, resourceGroup().id)}'
var acceleratorRepoName = 'databricks-accelerator-fraud-orchestration'
var randomString = uniqueString(databricksResourceName)

// Managed Identity
resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'mi-${randomString}'
  location: resourceGroup().location
}

resource createOrUpdateDatabricks 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'createDatabricksIfNotExists'
  location: resourceGroup().location
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

// Conditional Deployment: Reference the workspace only if it exists
module databricksModule './databricks.bicep' = {
  name: 'databricksModule'
  params: {
    acceleratorRepoName: acceleratorRepoName
    databricksResourceName: databricksResourceName
    managedIdentity: managedIdentity
    randomString: randomString
    workspaceExists: createOrUpdateDatabricks.properties.outputs.Exists
  }
}

// Outputs
output databricksJobUrl string = databricksModule.outputs.databricksJobUrl
