# Employee Attendance & Analytics API

A backend service for employee attendance tracking, manual attendance record regularization, and department-level analytics, built using **FastAPI** and **MongoDB**.

## Prerequisites

- **Python:** 3.11+
- **MongoDB:** 6.0+ (local instance, Docker `mongo:7`, or MongoDB Atlas M0 cluster)

## Setup & Installation

### 1. Create and Activate a Virtual Environment

Create a virtual environment to isolate the project's Python dependencies.

```bash
python -m venv .venv
```

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux:**

```bash
source .venv/bin/activate
```

### 2. Install Dependencies

Install the required Python packages:

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

The application reads `MONGO_URI` and `MONGO_DB` from environment variables. System environment variables take priority over values defined in `.env`.

Create a `.env` file in the repository root for local development:

```ini
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Ensure that MongoDB is running and accessible using the configured connection URI.

### 4. Seed Sample Data (Optional)

Populate the database with sample data for local testing:

```bash
python sample_seed.py
```

## Running the Application

Start the FastAPI server from the repository root:

```bash
uvicorn app.main:app --port 8000
```

The application will be available at `http://localhost:8000`.

### API Endpoints

- **Health Check:** `GET http://localhost:8000/health`
  - Returns `200 OK` when MongoDB responds successfully to a ping.
  - Returns `503 Service Unavailable` if the database health check fails.

- **Interactive API Documentation:** [http://localhost:8000/docs](http://localhost:8000/docs)

- **Alternative API Documentation:** [http://localhost:8000/redoc](http://localhost:8000/redoc)

## Implementation Details

### 1. Complete Contract Coverage

- Implements the API endpoints, HTTP status codes, schema validations, and business rules defined in Parts A, B, and C.
- Business rules `R1`–`R10` are implemented in `app/main.py`.

### 2. Idempotent Index Creation

- Automatically creates the required database indexes during application startup.
- Enforces uniqueness for `employees.emp_code`.
- Enforces uniqueness for the compound index `attendance_logs.(emp_code, date)`.
- Creates additional indexes to support efficient query execution.

### 3. Time Zone and Boundary Handling

- Validates and exchanges API boundary timestamps as integer epoch milliseconds.
- Truncates timestamps to whole seconds in accordance with rule `R1`.
- Stores timestamps as UTC BSON `datetime` objects in MongoDB.
- Evaluates attendance and shift rules using Indian Standard Time (IST, UTC+05:30).
- Supports overnight shifts where `shift_end <= shift_start`.

### 4. Concurrency and Race Safety

- Prevents duplicate employee creation and simultaneous duplicate punch-ins using database-level unique indexes.
- Maps MongoDB `DuplicateKeyError` exceptions to `409 Conflict` responses.
- Uses atomic `find_one_and_update` operations to handle simultaneous punch-outs and concurrent attendance record regularizations through `PATCH` requests.
- Preserves audit history during concurrent record updates to prevent lost updates.

### 5. Database-Native Analytics and Explainability

Computes analytics directly within MongoDB aggregation pipelines, reducing the need to transfer large datasets into application memory.

The analytics include:

- **`employee_monthly`:** Monthly employee attendance analytics.
- **`department_summary`:** Department-level attendance summaries.
- **`late_leaderboard`:** Employee rankings based on late arrivals.
- **`department_trend`:** Department-level trends using a sliding seven-day average.

Key MongoDB aggregation features include:

- `$lookup` for joining related collections.
- `$setWindowFields` with `$rank` and a sliding seven-day `$avg`.
- `$densify` for filling missing calendar dates in time-series analytics.

The endpoint `GET /admin/explain/{endpoint}` runs `explain("executionStats")` against the underlying query or aggregation pipeline to inspect query execution and verify index utilization, including `IXSCAN` where applicable.

## Repository Structure

```text
.
├── app/
│   └── main.py          # FastAPI application, models, helpers,
│                        # CRUD routes, analytics pipelines,
│                        # and explain endpoint
├── requirements.txt     # Python dependencies
├── sample_seed.py       # Optional sample data seeding script
├── REVIEW.md            # Part A: Code review and defect analysis
├── DECISIONS.md          # Part C: Architectural and design decisions
└── README.md             # Project documentation
```

## Technology Stack

- **Language:** Python
- **Backend Framework:** FastAPI
- **Database:** MongoDB
- **Database Operations:** MongoDB queries and aggregation pipelines
- **API Documentation:** OpenAPI, Swagger UI, and ReDoc
- **Server:** Uvicorn

## Notes

- Run all commands from the repository root unless otherwise specified.
- Ensure MongoDB is running and the environment variables are configured before starting the application.
- Sample data seeding is optional and intended for local development and testing.
- Refer to `REVIEW.md` for the code review and defect analysis, and `DECISIONS.md` for architectural and design explanations.