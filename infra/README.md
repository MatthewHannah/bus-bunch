# infra/

[`main.bicep`](./main.bicep) declares the entire bus-bunch stack at
resource-group scope.

## What it deploys

| Resource | SKU | Why |
|---|---|---|
| Storage account | Standard_LRS | Functions runtime requirement |
| Log Analytics workspace | PerGB2018 (30 day retention) | App Insights backend |
| Application Insights | workspace-based | Function logs / traces |
| App Service plan | **Y1 Dynamic (Consumption, free tier)** | Hosts the Function App |
| Function App | Linux, dotnet-isolated 8 | Runs the pollers |
| Azure SQL server | Entra-only auth | No SQL passwords |
| Azure SQL database | **Basic, 5 GiB (~$5/mo)** | Snapshots + fact tables |
| SQL firewall rule | `AllowAllWindowsAzureIps` | Lets the Function reach SQL |

The Function App's **system-assigned managed identity** is what authenticates
to SQL — no secrets in app settings.

## One-time deploy

```sh
RG=bus-bunch

az deployment group create \
  --resource-group $RG \
  --template-file infra/main.bicep \
  --parameters \
    sqlAadAdminLogin="$(az ad signed-in-user show --query userPrincipalName -o tsv)" \
    sqlAadAdminObjectId="$(az ad signed-in-user show --query id -o tsv)"
```

Capture the outputs:

```sh
az deployment group show -g $RG -n main \
  --query 'properties.outputs.{fn:functionAppName.value,sql:sqlServerFqdn.value}'
```

## Then, one-time SQL setup

1. **Apply schema.** From `sql/`:
   ```sh
   FQDN=<sqlServerFqdn from output>
   for f in sql/001_raw_and_fact.sql sql/002_dim_static_gtfs.sql sql/003_derive_arrivals.sql sql/004_views.sql; do
     sqlcmd -S $FQDN -d busbunch -G -i "$f"
   done
   ```

2. **Grant the Function MI access.** Edit `sql/000_grant_mi.sql`,
   set `@miName` to the Function App name (`functionAppName` output),
   then:
   ```sh
   sqlcmd -S $FQDN -d busbunch -G -i sql/000_grant_mi.sql
   ```

## Deploying the Function code

```sh
cd src/BusBunch.Functions
func azure functionapp publish <functionAppName>
```

## Cost summary

- Functions: $0 (Consumption free grant covers ~86k execs/mo).
- App Insights: free up to 5 GiB/mo ingestion; we'll be well under.
- Storage: pennies/month.
- SQL Basic: ~$5/month flat.
