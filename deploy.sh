#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# CONFIG BASE
# =============================================================================
ENV="nprdt"
PREFIX="rs-${ENV}-ue4-gcf"
PROJECT_ID="rs-nprd-dlk-dt-trsv-digt-f7ef"
REGION="us-central1"

JOB_NAME="${PREFIX}-metadata-generator"


REPO="gcr-metadata-jobs"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${JOB_NAME}:$(date +%Y%m%d-%H%M%S)"

SERVICE_ACCOUNT="sa-nprd-dt-gob-dataplex-deploy@rs-nprd-dlk-dt-trsv-digt-f7ef.iam.gserviceaccount.com"
TRACKER_TABLE_FQN="${PROJECT_ID}.trsv_monitoreo.tablas_mdm"

TASK_COUNT=10
PARALLELISM=5
MAX_WORKERS=10
VERTEX_CONCURRENCY=5
LLM_RETRIES=3

# =============================================================================
# RUNTIME
# =============================================================================
CPU="4"
MEMORY="4Gi"
TIMEOUT="7200s"

echo "▶ Verificando Artifact Registry..."

if ! gcloud artifacts repositories describe "${REPO}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then

  echo "▶ Repo no existe. Creando ${REPO} en ${REGION}..."

  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Docker repo metadata jobs" \
    --project="${PROJECT_ID}"
fi

echo "▶ Configurando auth Docker..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# =============================================================================
# BUILD
# =============================================================================
echo "▶ Build & push imagen: ${IMAGE}"

gcloud builds submit \
  --tag "${IMAGE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}"

# =============================================================================
# DEPLOY
# =============================================================================
echo "▶ Desplegando Cloud Run Job..."
echo "  Tasks totales:      ${TASK_COUNT}"
echo "  Tasks en paralelo:  ${PARALLELISM}"
echo "  Vertex concurrency: ${VERTEX_CONCURRENCY} por task"
echo "  RPM pico estimado:  $((PARALLELISM * VERTEX_CONCURRENCY * 4))"
echo "  RPM sostenido:      $((PARALLELISM * VERTEX_CONCURRENCY * 2))"
echo ""

gcloud run jobs deploy "${JOB_NAME}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --service-account "${SERVICE_ACCOUNT}" \
  --tasks "${TASK_COUNT}" \
  --parallelism "${PARALLELISM}" \
  --task-timeout "${TIMEOUT}" \
  --max-retries 0 \
  --cpu "${CPU}" \
  --memory "${MEMORY}" \
  --set-env-vars "\
TRACKER_TABLE_FQN=${TRACKER_TABLE_FQN},\
MAX_WORKERS=${MAX_WORKERS},\
VERTEX_CONCURRENCY=${VERTEX_CONCURRENCY},\
LLM_RETRIES=${LLM_RETRIES},\
PROJECT_ID=${PROJECT_ID}"

echo ""
echo "✅ Job desplegado: ${JOB_NAME}"
echo ""
echo "▶ Ejecutar:"
echo "  gcloud run jobs execute ${JOB_NAME} --region ${REGION} --project ${PROJECT_ID}"
echo ""
echo "▶ Logs en tiempo real:"
echo "  gcloud logging read \\"
echo "    'resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB_NAME}\"' \\"
echo "    --limit=500 --format='value(timestamp,textPayload)' --order=asc --project=${PROJECT_ID}"
echo ""
echo "▶ Progreso en BigQuery:"
echo "  SELECT"
echo "    COUNTIF(estado='OK')    AS ok,"
echo "    COUNTIF(estado='ERROR') AS errores,"
echo "    COUNTIF(estado IS NULL) AS pendientes,"
echo "    COUNT(*)                AS total"
echo "  FROM \`${TRACKER_TABLE_FQN}\`;"