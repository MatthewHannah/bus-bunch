-- 001_grant_mi.sql
-- ----------------------------------------------------------------------------
-- Grant the bus-bunch Function App's system-assigned managed identity
-- read/write/exec permissions on the database.
--
-- Run this ONCE after the first `az deployment group create`, connected as
-- the Entra admin (the user passed as sqlAadAdminLogin):
--
--   sqlcmd -S <SQL_SERVER_FQDN> -d busbunch -G -i sql/001_grant_mi.sql
-- ----------------------------------------------------------------------------

DECLARE @miName sysname = N'busbunch-fn-dkhvkrmhbltmw';

DECLARE @sql nvarchar(max) = N'
    IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N''' + @miName + N''')
        CREATE USER [' + @miName + N'] FROM EXTERNAL PROVIDER;
    ALTER ROLE db_datareader ADD MEMBER [' + @miName + N'];
    ALTER ROLE db_datawriter ADD MEMBER [' + @miName + N'];
    ALTER ROLE db_ddladmin   ADD MEMBER [' + @miName + N'];
    GRANT EXECUTE TO [' + @miName + N'];
';
EXEC sp_executesql @sql;

PRINT N'Granted db_datareader, db_datawriter, db_ddladmin, EXECUTE to ' + @miName;
