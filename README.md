# Manage Metadata Vertex AI

A FastAPI-based application for automatically generating and managing metadata for BigQuery tables using Google Vertex AI's large language models.

## Overview

This project leverages Vertex AI to intelligently analyze BigQuery table structures and generate comprehensive metadata descriptions. It provides a REST API to retrieve table information, profiles, and AI-generated metadata for better data governance and documentation.

## Features

- **Metadata Generation**: Automatically generate metadata descriptions using Vertex AI LLM
- **Table Profiling**: Analyze BigQuery table structures and gather statistics
- **BigQuery Integration**: Direct integration with Google Cloud BigQuery
- **Vertex AI LLM**: Leverage advanced language models for intelligent metadata generation
- **Schema Validation**: Validate generated metadata against defined schemas
- **RESTful API**: FastAPI-based REST endpoints for easy integration
- **Docker Support**: Containerized deployment ready

## Tech Stack

- **Framework**: FastAPI
- **Language**: Python 3.x
- **Cloud Services**: Google Cloud BigQuery, Vertex AI
- **Infrastructure**: Terraform (IaC)
- **API Server**: Uvicorn

## Project Structure

```
.
├── app/
│   ├── main.py                 # FastAPI application entry point
│   ├── models.py               # Pydantic data models
│   ├── adapters/               # External service integrations
│   │   ├── bq_reader.py        # BigQuery table reader
│   │   └── vertex_llm.py       # Vertex AI LLM adapter
│   ├── services/               # Business logic
│   │   ├── profiling.py        # Table profiling service
│   │   └── prompt_builder.py   # LLM prompt construction
│   └── validators/             # Data validation
│       └── metadata_schema.py  # Metadata schema validation
├── infra/                      # Terraform infrastructure
│   ├── main.tf
│   └── variables.tf
├── Dockerfile                  # Container configuration
├── requirements.txt            # Python dependencies
└── README.md                   # This file
```

## Installation

### Prerequisites

- Python 3.9+
- Google Cloud SDK configured with appropriate credentials
- BigQuery and Vertex AI APIs enabled in your GCP project

### Setup

1. **Clone the repository**

   ```bash
   git clone <repository-url>
   cd manage-metadata-vertex-ai
   ```
2. **Create a virtual environment**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```
4. **Set up Google Cloud credentials**

   ```bash
   gcloud auth application-default login
   ```

## Usage

### Running the Application

**Using Uvicorn directly:**

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Using Docker:**

```bash
docker build -t manage-metadata-vertex-ai .
docker run -p 8000:8000 manage-metadata-vertex-ai
```

### API Endpoints

#### Health Check

```http
GET /
```

Returns the application health status.

#### Post Table Metadata

```http
POST /projects/{project}/datasets/{dataset}/tables/{table}
```

Retrieves comprehensive metadata for a BigQuery table, including:

- Table structure and schema
- Data profiling information
- AI-generated metadata descriptions
- Validation status

**Example:**

```bash
curl http://localhost:8000/projects/my-project/datasets/my_dataset/tables/my_table
```

## Dependencies

Key dependencies (see `requirements.txt` for full list):

- `fastapi` - Web framework
- `uvicorn[standard]` - ASGI server
- `google-cloud-bigquery` - BigQuery client
- `google-cloud-aiplatform` - Vertex AI integration
- `vertexai` - Vertex AI Python SDK
- `pydantic` - Data validation
- `jsonschema` - JSON schema validation

## Architecture

1. **Adapters Layer**: Interfaces with external services (BigQuery, Vertex AI)
2. **Services Layer**: Business logic for profiling and prompt building
3. **Validators Layer**: Ensures data quality and schema compliance
4. **Models Layer**: Pydantic models for type safety and validation
5. **API Layer**: FastAPI endpoints for external consumption

## Deployment

### Infrastructure as Code

This project uses Terraform for infrastructure management. Deploy using:

```bash
cd infra
terraform init
terraform plan
terraform apply
```

### Environment Variables

Configure the following environment variables:

- `GCP_PROJECT_ID` - Your Google Cloud project ID
- `BQ_DATASET_ID` - Default BigQuery dataset (optional)
- `VERTEX_AI_LOCATION` - Vertex AI region (default: `us-central1`)

## Contributing

Contributions are welcome! Please follow these steps:

1. Create a feature branch (`git checkout -b feature/improvement`)
2. Commit your changes (`git commit -am 'Add improvement'`)
3. Push to the branch (`git push origin feature/improvement`)
4. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

For issues, questions, or suggestions, please open an issue on the repository.
