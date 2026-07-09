// ----------------------------------------------------------------------------
// bus-bunch infrastructure (resource-group scope)
//
// Deploys:
//   - Storage account (required by Functions runtime)
//   - Log Analytics workspace + Application Insights
//   - Flex Consumption (FC1) plan + Linux Function App (dotnet-isolated 10)
//   - Azure SQL logical server + Standard S1 database (10 GiB, ~$20/mo;
//     originally Basic 2 GiB but the dataset outgrew it)
//   - Function App's system-assigned managed identity is granted SQL access
//     via an Entra-only authentication model: the deploying user is set as
//     the SQL Entra admin and is then expected to run sql/000_grant_mi.sql
//     once to add the Function MI as a contained user with read/write/exec
//     permissions.
//
// To deploy:
//   az deployment group create \
//     --resource-group bus-bunch \
//     --template-file infra/main.bicep \
//     --parameters \
//        sqlAadAdminLogin="$(az ad signed-in-user show --query userPrincipalName -o tsv)" \
//        sqlAadAdminObjectId="$(az ad signed-in-user show --query id -o tsv)"
// ----------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Region for all resources.')
param location string = resourceGroup().location

@description('Short prefix used to name resources. Lowercase letters and numbers only.')
@minLength(3)
@maxLength(12)
param namePrefix string = 'busbunch'

@description('Display name (UPN/email) of the Entra user/group to set as the SQL Entra admin.')
param sqlAadAdminLogin string

@description('Entra (AAD) object ID of the SQL Entra admin.')
param sqlAadAdminObjectId string

@description('GTFS-RT vehicle positions URL.')
param vehiclePositionsUrl string = 'https://tracker.itsmarta.com/gtfs/vehiclepositions.pb'

@description('GTFS-RT trip updates URL.')
param tripUpdatesUrl string = 'https://tracker.itsmarta.com/gtfs/tripupdates.pb'

@description('Static GTFS bundle URL (the agency google_transit.zip).')
param staticGtfsUrl string = 'https://itsmarta.com/google_transit_feed/google_transit.zip'

@description('Days of raw snapshot history to retain. Older rows are pruned daily.')
@minValue(7)
@maxValue(365)
param retentionRawDays int = 14

@description('SQL database name.')
param sqlDatabaseName string = 'busbunch'

@description('Origins allowed to call the Function App API directly (in addition to the Static Web App default hostname, which is added automatically). Useful for local dev (http://localhost:5173) or a custom domain.')
param extraApiCorsOrigins array = [
  'http://localhost:5173'
]

// ---------------------------------------------------------------------------
// names (deterministic per-RG so re-deploys hit the same resources)
// ---------------------------------------------------------------------------
var suffix         = uniqueString(resourceGroup().id)
// Storage account names are 3..24 chars, lowercase alphanum only. Trim
// the unique-string suffix so the total stays under the cap even if
// namePrefix uses its full 12-char allowance.
var storageName    = toLower('${namePrefix}st${take(suffix, 8)}')
var planName       = '${namePrefix}-plan'
var functionName   = '${namePrefix}-fn-${suffix}'
var logsName       = '${namePrefix}-logs'
var appInsightsName = '${namePrefix}-ai'
var sqlServerName  = '${namePrefix}-sql-${suffix}'
var staticSiteName = '${namePrefix}-web'

// ---------------------------------------------------------------------------
// storage (Functions runtime requirement)
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

// Flex Consumption pulls the app package from a blob container at scale-out.
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'deploymentpackage'
  properties: {
    publicAccess: 'None'
  }
}

var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storage.listKeys().keys[0].value}'

// ---------------------------------------------------------------------------
// log analytics + app insights
// ---------------------------------------------------------------------------
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logsName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ---------------------------------------------------------------------------
// hosting (Flex Consumption plan, Linux)
//
// Linux Consumption (Y1) does NOT support .NET 10 isolated, so the app runs on
// Flex Consumption (FC1) — the consumption-based, scale-to-zero successor that
// supports current .NET versions.
// ---------------------------------------------------------------------------
resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  kind: 'functionapp,linux'
  properties: {
    reserved: true   // Linux
  }
}

// ---------------------------------------------------------------------------
// static web app (Free SKU) — hosts the React viz frontend in web/.
// Free tier doesn't support linked backends, so the browser calls the
// Function App directly via its azurewebsites.net hostname (with CORS
// allowlisted above). The deployment token is consumed by GH Actions.
// ---------------------------------------------------------------------------
resource staticSite 'Microsoft.Web/staticSites@2023-12-01' = {
  name: staticSiteName
  // SWA Free is only offered in a handful of regions; eastus2 is the safe
  // default and is independent of `location` (which controls the rest of
  // the stack and may be set to something SWA Free does not support).
  location: 'eastus2'
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    // Repo metadata intentionally left blank: deployment uses the static
    // site's API token from GitHub Actions, not the built-in source-control
    // wiring (which would try to create its own workflow).
    provider: 'None'
  }
}

// ---------------------------------------------------------------------------
// function app
// ---------------------------------------------------------------------------
resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer.name}'
          authentication: {
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'dotnet-isolated'
        version: '10.0'
      }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      cors: {
        // The SWA free tier can't proxy /api/* to an external Function App
        // (that needs Standard SKU's linked-backend feature), so the browser
        // calls the Function App directly. CORS must allow:
        //   - the SWA's generated hostname (added below)
        //   - any extra dev/custom-domain origins (extraApiCorsOrigins)
        allowedOrigins: union(
          extraApiCorsOrigins,
          [ 'https://${staticSite.properties.defaultHostname}' ]
        )
        supportCredentials: false
      }
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: storageConnectionString
        }
        {
          name: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
          value: storageConnectionString
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'SqlConnectionString'
          value: 'Server=tcp:${sqlServer.properties.fullyQualifiedDomainName},1433;Database=${sqlDatabaseName};Authentication=Active Directory Default;Encrypt=True;TrustServerCertificate=False;Connection Timeout=30;'
        }
        { name: 'Gtfs__VehiclePositionsUrl',   value: vehiclePositionsUrl }
        { name: 'Gtfs__TripUpdatesUrl',        value: tripUpdatesUrl }
        { name: 'Gtfs__StaticUrl',             value: staticGtfsUrl }
        { name: 'Retention__RawDays',          value: string(retentionRawDays) }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// azure sql
// ---------------------------------------------------------------------------
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlServerName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    // Entra-only auth: no SQL admin login / password. Cleaner and means there
    // are no shared secrets to rotate.
    administrators: {
      administratorType: 'ActiveDirectory'
      principalType: 'User'
      login: sqlAadAdminLogin
      sid: sqlAadAdminObjectId
      tenantId: subscription().tenantId
      azureADOnlyAuthentication: true
    }
  }
}

resource sqlDb 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sqlServer
  name: sqlDatabaseName
  location: location
  // Bumped from Basic 2 GiB to S1 10 GiB because the dataset (kept-forever
  // stop_arrival_event + 7-day vehicle_position_snapshot + prediction
  // history) outgrew the Basic cap. Going back to Basic would require
  // pruning stop_arrival_event aggressively, which we don't want.
  sku: {
    name: 'S1'
    tier: 'Standard'
    capacity: 20
  }
  properties: {
    collation: 'SQL_Latin1_General_CP1_CI_AS'
    maxSizeBytes: 10737418240    // 10 GiB (S1 max)
    zoneRedundant: false
  }
}

// allow other Azure services (incl. our Function App) to reach SQL
resource sqlFwAzure 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: sqlServer
  name: 'AllowAllWindowsAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ---------------------------------------------------------------------------
// outputs
// ---------------------------------------------------------------------------
output functionAppName     string = functionApp.name
output functionAppHostname string = functionApp.properties.defaultHostName
output functionAppPrincipalId string = functionApp.identity.principalId
output sqlServerFqdn       string = sqlServer.properties.fullyQualifiedDomainName
output sqlDatabaseName     string = sqlDb.name
output sqlConnectionString string = 'Server=tcp:${sqlServer.properties.fullyQualifiedDomainName},1433;Database=${sqlDatabaseName};Authentication=Active Directory Default;Encrypt=True;'
output staticSiteName      string = staticSite.name
output staticSiteHostname  string = staticSite.properties.defaultHostname
output webApiBase          string = 'https://${functionApp.properties.defaultHostName}/api'
