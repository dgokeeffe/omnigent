# lakemeter — FE VM Lakemeter (fe-vm-lakemeter)
# NOTE: fill VOLUME_NAME / OTEL_TABLE_SCHEMA / APP_URL from the live workspace
# before first use (databricks apps get omnigent --profile lakemeter).
TARGET            := lakemeter
PROFILE           := lakemeter
APP_NAME          := omnigent
LAKEBASE_BRANCH   := projects/omnigent/branches/production
LAKEBASE_DATABASE := projects/omnigent/branches/production/databases/databricks-postgres
VOLUME_NAME       := main.omnigent.artifacts
OTEL_TABLE_SCHEMA := main.omnigent_logs
APP_URL           :=
