provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_cloud_run_service" "metadata_api" {
  name     = "metadata-governance-api"
  location = var.region

  template {
    spec {
      service_account_name = var.service_account_email

      containers {
        image = var.image_url

        resources {
          limits = {
            memory = "1Gi"
            cpu    = "1"
          }
        }

        env {
          name  = "ENV"
          value = "prod"
        }
      }
    }
  }

  traffics {
    percent         = 100
    latest_revision = true
  }
}

resource "google_cloud_run_service_iam_member" "public" {
  service  = google_cloud_run_service.metadata_api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allAuthenticatedUsers"
}
