# aws-syd — FE Sydney AWS sandbox (dbc-d8b32eb0-866b)
# Verified 2026-07-20: app already live at APP_URL below.
TARGET            := aws-syd
PROFILE           := aws-syd
APP_NAME          := omnigent
LAKEBASE_BRANCH   := projects/omnigent/branches/production
LAKEBASE_DATABASE := projects/omnigent/branches/production/databases/databricks-postgres
VOLUME_NAME       := dais.omnigent.artifacts
OTEL_TABLE_SCHEMA := dais.omnigent
APP_URL           := https://omnigent-7474653718950231.aws.databricksapps.com
