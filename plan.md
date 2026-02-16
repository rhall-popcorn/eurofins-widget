# Plan: Comprehensive Rebuild Prompt for Eurofins Integration

## Goal
Create a detailed, self-contained prompt document that instructs Claude Code to rebuild the eurofins-widget Lambda function as a native Express service inside the fishers-portal web app.

## Key Architectural Decisions (from user input)
- **Tech stack**: Express (backend) + React (frontend)
- **Trigger**: Scheduled cron job (no Lambda)
- **Database**: fishers-portal's own database (Postgres/MySQL) — NOT Bizowie API
- **Auth**: Gmail credentials via environment variables (same as current)

## What the Prompt Will Cover

### 1. Context & Background
- Explain what the Lambda does today (Gmail → PDF parse → database)
- Why it's being rebuilt (simplify integration, eliminate Lambda)

### 2. Gmail Integration Service
- Port the Python Gmail client to a Node.js/TypeScript service
- OAuth2 authentication using googleapis npm package
- Email search: `from:ft.eurofinsus.com has:attachment filename:pdf`
- PDF attachment download and base64 decoding
- "Processed" label marking to prevent reprocessing
- Credential refresh logic

### 3. PDF Parsing Service
- Port Python pypdf-based parser to Node.js (using pdf-parse or pdf.js)
- Include ALL regex patterns verbatim from the Python code:
  - Report number: `AR-\d{2}-[A-Z]{2}-\d{6}-\d{2}`
  - Eurofins sample code: `\d{3}-\d{4}-\d{8}`
  - Client sample code, order code, PO number patterns
  - Date formats: `05Jan2026`, `1/5/2026`, `2026-01-05`
- Include ALL test patterns:
  - UMB4D (Enterobacteriaceae)
  - UMPSK (Salmonella species)
  - UMQDQ (Listeria species)
  - UMLM (Listeria monocytogenes)
  - APC (Aerobic Plate Count)
  - YM (Yeast and Mold)
- Result value extraction (numeric, "Not Detected", units)
- Fallback parsing logic

### 4. Database Schema & Queries
- Define the database tables needed (replacing Bizowie tables 18 & 19):
  - `emp_swabs` table (reference samples, with `coc_ref` for matching)
  - `emp_swab_results` table (lab results)
- Matching logic: client_sample_code → emp_swabs.coc_ref → emp_swab_results.emp_sample_id
- Duplicate detection: check sample_id + organism before insert
- Define all columns with types

### 5. Scheduled Job Configuration
- Use node-cron or similar for scheduled execution
- Configurable interval via env var
- Processing summary logging
- Dry-run mode support

### 6. Environment Variables
- All required env vars with descriptions
- Default values where applicable

### 7. Data Models / TypeScript Interfaces
- EurofinsReportHeader
- EurofinsTestResult
- EurofinsReport
- SwabResult (for database insertion)

### 8. Error Handling & Logging
- Structured error collection
- Processing summary object
- Winston or built-in logging

### 9. Edge Cases
- Duplicate detection
- Missing sample ID fallback
- Invalid PDF handling
- Gmail token refresh
- Pagination for email search

## Deliverable
A single markdown file (`REBUILD_PROMPT.md`) containing the complete prompt, ready to be copy-pasted into a Claude Code session in the fishers-portal repo.

## Steps
1. Create `REBUILD_PROMPT.md` with the comprehensive prompt
2. Commit and push to the feature branch
