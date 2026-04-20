import os
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class JobConfig:
    # BigQuery tracker
    tracker_table_fqn: str
    tracker_project: str

    # Threads por contenedor (paralelismo real)
    max_workers: int

    # Batch de tablas a reclamar por ejecución
    batch_size: int

    # Multi-región para Vertex AI
    regions: List[str]

    # Reintentos LLM
    llm_retries: int

    # Delay inicial anti-thundering herd
    startup_jitter_sec: float

    @classmethod
    def from_env(cls) -> "JobConfig":
        tracker_table_fqn = os.environ["TRACKER_TABLE_FQN"]
        tracker_project = tracker_table_fqn.split(".")[0]

        return cls(
            tracker_table_fqn=tracker_table_fqn,
            tracker_project=tracker_project,
            max_workers=int(os.getenv("MAX_WORKERS", "15")),
            batch_size=int(os.getenv("BATCH_SIZE", "500")),
            regions=[
                r.strip()
                for r in os.getenv(
                    "REGIONS",
                    "us-central1,us-east1,us-west1,us-east4",
                ).split(",")
                if r.strip()
            ],
            llm_retries=int(os.getenv("LLM_RETRIES", "3")),
            startup_jitter_sec=float(os.getenv("STARTUP_JITTER_SEC", "3")),
        )
