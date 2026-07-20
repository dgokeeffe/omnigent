# Copy this to profiles/<name>.mk and fill in the values, then add a matching
# `<name>:` target block to databricks.yml (literal workspace.host).
# Deploy with:  make deploy PROFILE=<name>
TARGET            := <name>        # DAB target in databricks.yml
PROFILE           := <name>        # databricks CLI profile (~/.databrickscfg)
APP_NAME          := omnigent
LAKEBASE_BRANCH   := projects/omnigent/branches/production
LAKEBASE_DATABASE := projects/omnigent/branches/production/databases/databricks-postgres
VOLUME_NAME       := <catalog>.<schema>.artifacts
OTEL_TABLE_SCHEMA := <catalog>.<schema>
APP_URL           := https://<app>-<workspace-id>.<region>.databricksapps.com
