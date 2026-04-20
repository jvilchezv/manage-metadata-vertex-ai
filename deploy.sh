#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# CONFIG BASE
# =============================================================================
ENV="nprdd"
PREFIX="rs-${ENV}-ue4-gcf"
PROJECT_ID="rs-nprd-dlk-dd-trsv-ede4"
REGION="us-central1"
JOB_NAME="${PREFIX}-metadata-generator"
IMAGE="gcr.io/${PROJECT_ID}/${JOB_NAME}:$(date +%Y%m%d-%H%M%S)"
SERVICE_ACCOUNT="sa-nprd-dd-gob-dataplex-deploy@rs-nprd-dlk-dd-trsv-ede4.iam.gserviceaccount.com"
TRACKER_TABLE_FQN="${PROJECT_ID}.trsv_delivery_calidad.tablas_mdm"

# =============================================================================
# DISEÑO DE CAPACIDAD — leer antes de cambiar cualquier valor
#
# Cuota Vertex AI:  420 RPM
# LLM latencia p50: ~15s  (puede llegar a 30-40s bajo carga → jitter real)
# 3500 tablas
#
# ── Fórmula conservadora ──────────────────────────────────────────────────────
#
#   RPM_real = PARALLELISM × VERTEX_CONCURRENCY × (60 / latencia_p95)
#
#   Usamos p95 (~25s) en vez de p50 (15s) para absorber jitter y retries:
#
#   RPM_real = 5 × 5 × (60/25) = 25 × 2.4 = 60 RPM en burst sostenido
#
#   Con margen hacia cuota:
#   RPM_pico = 5 × 5 × (60/15) = 100 RPM  ← si todo responde rápido
#
#   Ambos bien por debajo de 420 RPM → sin riesgo de 429
#
# ── PARALLELISM=5 (no 10) ────────────────────────────────────────────────────
#   Con PARALLELISM < TASK_COUNT, Cloud Run escalonea el arranque:
#   las primeras 5 tasks arrancan, las otras 5 esperan a que termine alguna.
#   Esto evita el burst instantáneo de cold start con 10 containers
#   abriendo 70 streams simultáneos a Vertex en el mismo segundo.
#
# ── VERTEX_CONCURRENCY=5 (no 7) ──────────────────────────────────────────────
#   Baja de 7 a 5 para dejar buffer a:
#     - LLM_RETRIES (cada retry suma RPM extra)
#     - Variabilidad de scheduling de Cloud Run
#     - Latencia variable (spikes a 40s son reales con Gemini bajo carga)
#
# ── Tiempo estimado ───────────────────────────────────────────────────────────
#   Throughput sostenido: 5×5 = 25 streams × (60/20s_avg) = 75 tablas/min
#   3500 / 75 ≈ 47 minutos  ← dentro del timeout de 2h
#
# ── Para subir throughput DESPUÉS de validar estabilidad ─────────────────────
#   Paso 1: VERTEX_CONCURRENCY=7  → ~105 RPM pico, 3500 tablas en ~33 min
#   Paso 2: PARALLELISM=10        → solo si no hay 429 en paso 1
# =============================================================================
TASK_COUNT=10        # slices totales del trabajo (3500/10 = 350 tablas c/u)
PARALLELISM=5        # tasks activas simultáneamente (controla burst)
MAX_WORKERS=10       # threads I/O bound por container (BQ, Dataplex — sin cuota estricta)
VERTEX_CONCURRENCY=5 # streams simultáneos a Vertex AI por task
LLM_RETRIES=3

# =============================================================================
# RUNTIME
# 2 CPU es suficiente para I/O bound con 10 workers.
# El cuello de botella es Vertex AI, no el CPU del container.
# =============================================================================
CPU="2"
MEMORY="2Gi"
TIMEOUT="7200s"

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
echo "  Tasks totales:      ${TASK_COUNT}  (${TASK_COUNT} slices de ~350 tablas)"
echo "  Tasks en paralelo:  ${PARALLELISM} (las otras esperan turno)"
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