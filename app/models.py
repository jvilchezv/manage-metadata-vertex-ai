from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

# ---------- Output models ----------


class TableDescription(BaseModel):
    description: str = Field(..., min_length=0, max_length=1000)
    confidence: float = Field(..., ge=0.0, le=1.0)


class ColumnMetadata(BaseModel):
    name: str
    description: str = Field(..., min_length=0, max_length=1000)
    confidence: float = Field(..., ge=0.0, le=1.0)
    is_confidencial: bool


class ModelInfo(BaseModel):
    name: str
    version: str


class TableMetadata(BaseModel):
    table_fqn: str
    table_description: TableDescription
    columns: List[ColumnMetadata]
    model: ModelInfo
    generated_at: datetime


# ---------- Input model ----------


class GenerateMetadataRequest(BaseModel):
    project: str
    dataset: str
    table: str


class ColumnStatus(BaseModel):
    name: str
    type: str
    mode: str
    description: Optional[str]
    is_partitioning_column: bool


class TableStatus(BaseModel):
    table_fqn: str
    exists: bool
    is_partitioned: bool
    partition_field: Optional[str]
    row_count: int
    size_mb: float
    description: Optional[str]
    columns: List[ColumnStatus]
    labels: dict
    last_modified: datetime
