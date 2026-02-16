# Rebuild Eurofins Lab Report Integration into fishers-portal

## Background & Goal

We currently have a standalone AWS Lambda function (Python) that automates Eurofins lab report processing. It:
1. Polls Gmail for emails from Eurofins (`from:ft.eurofinsus.com`) with PDF attachments
2. Parses the PDF lab reports using regex-based text extraction
3. Matches each report to an existing swab sample record via the Client Sample Code
4. Uploads the parsed test results to a database

We want to **eliminate the Lambda function** and rebuild this functionality as a native service inside our **fishers-portal Express + React web app**. The app has its own PostgreSQL database — we will NOT use the Bizowie API. Instead, results go directly into our own `emp_swab_results` table.

---

## Architecture Overview

Build the following components inside the Express backend:

```
server/
├── services/
│   ├── gmail/
│   │   └── gmailService.ts          # Gmail API client (OAuth2, email search, attachment download)
│   ├── eurofins/
│   │   ├── eurofinsParser.ts         # PDF text extraction + regex parsing
│   │   ├── eurofinsProcessor.ts      # Orchestrator: Gmail → Parse → DB (replaces Lambda handler)
│   │   └── types.ts                  # TypeScript interfaces for all data models
│   └── scheduler/
│       └── eurofinsJob.ts            # node-cron scheduled job
├── routes/
│   └── eurofins.ts                   # (Optional) API routes for manual trigger / status check
└── migrations/
    └── XXXXXX_create_emp_swab_results.ts  # Database migration
```

---

## 1. TypeScript Interfaces (`types.ts`)

Define these interfaces based on the exact data structures from the existing Lambda:

```typescript
export interface EurofinsReportHeader {
  reportNumber: string;           // e.g. "AR-25-QP-119455-01"
  eurofinsSampleCode: string;     // e.g. "498-2025-12240272"
  clientCode: string;             // e.g. "QP0004621"
  clientSampleCode: string;       // e.g. "122325-5A"
  orderCode: string;              // e.g. "006-10547-2285931"
  poNumber: string;               // e.g. "6"
  sampleDescription: string;      // e.g. "|COOK-DRAIN-1 (2)|"
  sampleReference: string;        // e.g. "122325-5A"
  receivedDate: Date | null;
  reportedDate: Date | null;
  registrationDate: Date | null;
  conditionUponReceipt: string;
  labName: string;                // Default: "Eurofins Microbiology Laboratories (Lancaster)"
}

export interface EurofinsTestResult {
  testCode: string;       // UMB4D, UMPSK, UMQDQ, UMLM, APC, YM
  testName: string;       // "Enterobacteriaceae", "Salmonella species", etc.
  parameter: string;      // "Enterobacteriaceae", "Salmonella spp.", etc.
  result: string;         // Full result string: "Not Detected per Sponge", "30 (est) cfu/Sponge"
  resultValue: string;    // Extracted value without units: "Not Detected", "30 (est)", "< 10"
  units: string;          // Extracted units: "cfu/Sponge", "per Sponge", "CFU/g", "MPN/g", "N/A"
  method: string;         // "AOAC 2003.01", "AOAC-RI 121501", "AOAC-RI 061702"
  accreditation: string;  // "ISO/IEC 17025:2017 A2LA 3329.03"
  completedDate: Date | null;
}

export interface EurofinsReport {
  header: EurofinsReportHeader;
  results: EurofinsTestResult[];
  rawText: string;
  sourceFile: string | null;
  parseErrors: string[];
}

export interface SwabResult {
  empSampleId: string;        // Foreign key to emp_swabs table
  organism: string;           // "Enterobacteriaceae", "Salmonella spp.", etc.
  method1: string;            // Test method
  resultValue: string;        // "Not Detected", "30 (est)", "< 10", etc.
  units: string;              // "cfu/Sponge", "per Sponge", "N/A"
  dateReported: Date | null;  // Date from email or report
  reviewedBy: string;         // Empty string — assigned manually later
  status: string;             // "Pending Review"
  notes: string;              // e.g. "Report: AR-25-QP-119455-01"
}

export interface ProcessingSummary {
  timestamp: string;
  emailsProcessed: number;
  pdfsParsed: number;
  resultsFound: number;
  resultsUploaded: number;
  resultsSkipped: number;
  errors: string[];
  dryRun: boolean;
  success: boolean;
}
```

---

## 2. Gmail Service (`gmailService.ts`)

Port the Python Gmail client to Node.js using the `googleapis` npm package.

### Dependencies
```
npm install googleapis
```

### Gmail API Scopes
```typescript
const SCOPES = [
  'https://www.googleapis.com/auth/gmail.readonly',
  'https://www.googleapis.com/auth/gmail.modify'
];
```

### Authentication
Support two modes:
1. **Production (env var)**: Base64-encoded token JSON in `GMAIL_TOKEN_JSON` env var. Decode it, parse as JSON, and create credentials with `google.auth.fromJSON()` or the OAuth2 client.
2. **Local development**: Load token from a file path.

Handle **token refresh** — if the token is expired and has a refresh_token, refresh it automatically:
```
if (credentials.expiry_date && credentials.expiry_date < Date.now()) {
  // refresh using the refresh_token
}
```

### Email Search
Search query (exact): `from:ft.eurofinsus.com has:attachment filename:pdf`

Add date filtering using Gmail's `newer_than` syntax:
```
query = `${query} newer_than:${daysSince}d`
```

Support pagination — loop through pages using `nextPageToken` until all results (up to `maxResults`) are collected.

### Attachment Extraction
For each email:
1. Get full email with `format: 'full'`
2. Recursively traverse MIME `parts` looking for attachments (parts with `filename` and `body.attachmentId`)
3. Download attachment data via the Gmail Attachments API
4. Base64url-decode the attachment data
5. Filter to only `.pdf` files (by filename extension OR mime type `application/pdf`)

### Mark as Processed
After processing an email, add a Gmail label `Eurofins-Processed`:
1. List all labels, find the one named `Eurofins-Processed`
2. If it doesn't exist, create it
3. Add the label to the message using `messages.modify`

### Get Email Received Date
Extract from the email's `internalDate` field (milliseconds since epoch):
```typescript
const receivedDate = new Date(parseInt(message.internalDate));
```

---

## 3. PDF Parser (`eurofinsParser.ts`)

This is the most critical component. Port the Python regex-based parser to TypeScript.

### Dependencies
```
npm install pdf-parse
```
(or `pdfjs-dist` if pdf-parse doesn't work well enough)

### PDF Text Extraction
```typescript
import pdfParse from 'pdf-parse';

async function extractText(pdfBuffer: Buffer): Promise<string> {
  const data = await pdfParse(pdfBuffer);
  return data.text;
}
```

### Header Parsing — Exact Regex Patterns

**IMPORTANT**: These regex patterns are tested against real Eurofins PDFs. Port them exactly.

#### Report Number
```
Pattern: /(AR-\d{2}-[A-Z]{2}-\d{6}-\d{2})/
Example match: "AR-25-QP-119455-01"
```

#### Eurofins Sample Code + Client Sample Code
The PDF text has a specific layout where labels appear on separate lines followed by values:
```
Client Sample Code:
Eurofins Sample Code:
122325-4A              ← Client Sample Code value
498-2025-12240273      ← Eurofins Sample Code value
```

**Primary pattern** (handles this multi-line format):
```
/Client Sample Code[:\s]*\n?Eurofins Sample Code[:\s]*\n?([^\n]+)\n(\d{3}-\d{4}-\d{8})/
```
- Group 1 = Client Sample Code
- Group 2 = Eurofins Sample Code

**Fallback 1** (labels and values on same line):
```
/Eurofins Sample Code[:\s]*(\d{3}-\d{4}-\d{8})/
```

**Fallback 2** (find the pattern anywhere):
```
/(\d{3}-\d{4}-\d{8})/
```

**Separate client sample code fallback**:
```
/Client Sample Code[:\s]*([A-Za-z0-9-]+)/
```

#### Client Code
```
Pattern: /Client Code[:\s]*([A-Z]{2}\d+)/
Example: "QP0004621"
```

#### Order Code
```
Pattern: /Order Code[:\s]*(\d{3}-\d{5}-\d{7})/
Example: "006-10547-2285931"
```

#### PO Number
```
Pattern: /PO#[:\s]*(\d+)/
Example: "6"
```

#### Sample Description
```
Pattern: /Sample Description[:\s]*([^\n]+)/
Example: "|COOK-DRAIN-1 (2)|"
```

#### Sample Reference
```
Pattern: /Sample Reference[:\s]*([^\n]+)/
Example: "122325-5A"
```

#### Condition Upon Receipt
```
Pattern: /Condition Upon Receipt[:\s]*([^\n]+)/
```

#### Dates (Received On, Reported On, Sample Registration Date)
Each date field uses a prefix followed by one of three date formats:

Prefix patterns:
- `Received On[:\s]*`
- `Reported On[:\s]*`
- `Sample Registration Date[:\s]*`

Date format patterns (try in order):
1. `/(\d{2}[A-Za-z]{3}\d{4})/` → format `DDMonYYYY` (e.g. `05Jan2026`)
2. `/(\d{1,2}\/\d{1,2}\/\d{4})/` → format `M/D/YYYY` (e.g. `1/5/2026`)
3. `/(\d{4}-\d{2}-\d{2})/` → format `YYYY-MM-DD` (e.g. `2026-01-05`)

Date parsing formats:
```typescript
function parseDate(dateStr: string): Date | null {
  // Try DDMonYYYY: "05Jan2026"
  const ddMonYyyy = dateStr.match(/^(\d{2})([A-Za-z]{3})(\d{4})$/);
  if (ddMonYyyy) {
    const months: Record<string, number> = {
      Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5,
      Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11
    };
    return new Date(parseInt(ddMonYyyy[3]), months[ddMonYyyy[2]], parseInt(ddMonYyyy[1]));
  }
  // Try M/D/YYYY
  const mdy = dateStr.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (mdy) return new Date(parseInt(mdy[3]), parseInt(mdy[1]) - 1, parseInt(mdy[2]));
  // Try YYYY-MM-DD
  const ymd = dateStr.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (ymd) return new Date(parseInt(ymd[1]), parseInt(ymd[2]) - 1, parseInt(ymd[3]));
  return null;
}
```

### Test Result Parsing — Test Section Detection

The parser identifies test sections by matching these patterns in the PDF text:

```typescript
const TEST_PATTERNS: Array<[string, RegExp, string]> = [
  // [testCode, sectionHeaderPattern, organismName]
  ['UMB4D', /UMB4D\s*-\s*Enterobacteriaceae/i,           'Enterobacteriaceae'],
  ['UMPSK', /UMPSK\s*-\s*Salmonella\s+species/i,          'Salmonella spp.'],
  ['UMQDQ', /UMQDQ\s*-\s*Listeria\s+species/i,            'Listeria spp.'],
  ['UMLM',  /UMLM\s*-\s*Listeria\s+monocytogenes/i,      'Listeria monocytogenes'],
  ['APC',   /Aerobic\s+Plate\s+Count/i,                    'Aerobic Plate Count'],
  ['YM',    /Yeast\s+and\s+Mold/i,                         'Yeast and Mold'],
];
```

### Test Section Splitting

1. Find all matches of the test patterns in the text, recording their start positions
2. Sort by position
3. Each section extends from its match position to the start of the next section (or to `Respectfully Submitted` / `Results shown in this report` / end of text)

### Parsing Each Test Section

For each section, extract:

**Method (Reference)**:
```
Pattern: /Reference\s*\n?([A-Z0-9-]+(?:\s*[0-9.]+)?)/
```

**Accreditation**:
```
Pattern: /Accreditation\s*\n?(.+?)(?=\s*Completed)/s
```
Clean whitespace: replace multiple spaces/newlines with single spaces.

**Completed Date**:
```
Pattern: /Completed\s*\n?(\d{2}[A-Za-z]{3}\d{4})/
```
Parse with the `DDMonYYYY` date parser.

**Parameter and Result Value**:
Look for the `Parameter Result` header, then parse the next line:
```
Pattern: /Parameter\s+Result\s*\n(.+)/s
```

Take the first line of the match, then try these patterns in order:
```typescript
const resultPatterns = [
  /(Enterobacteriaceae)\s+(.+)/i,
  /(Salmonella\s+spp\.?)\s+(.+)/i,
  /(Listeria\s+spp\.?)\s+(.+)/i,
  /(Listeria\s+monocytogenes)\s+(.+)/i,
  /([A-Za-z][A-Za-z\s.]+?)\s+((?:Not Detected|Detected|<|>|\d).+)$/i,
];
```
- Group 1 = parameter (organism name)
- Group 2 = full result string (e.g. "30 (est) cfu/Sponge", "Not Detected per Sponge")

If no pattern matches, use the entire first line as the result.

**Test Name** (from section header):
```
Pattern: /UM[A-Z0-9]+\s*-\s*([^-\n]+)/
```

### Result Value and Units Extraction

From the full result string, extract the value (without units) and the units separately:

**Strip units from result**:
```
Pattern: /\s*(?:cfu\/Sponge|per Sponge|CFU\/g|MPN\/g).*$/i
```
Remove this from the result string to get `resultValue`.

**Extract units**:
```typescript
function extractUnits(result: string): string {
  if (result.includes('cfu/Sponge')) return 'cfu/Sponge';
  if (result.includes('per Sponge')) return 'per Sponge';
  if (result.includes('CFU/g')) return 'CFU/g';
  if (result.includes('MPN/g')) return 'MPN/g';
  return 'N/A';
}
```

### Fallback Parsing

If the section-based parsing finds zero results, use these simpler fallback patterns:

```typescript
const fallbackPatterns = [
  {
    pattern: /Enterobacteriaceae\s+((?:Not Detected|<|>|\d)[^\n]+)/i,
    code: 'UMB4D', testName: 'Enterobacteriaceae', parameter: 'Enterobacteriaceae',
    methodPattern: /AOAC\s*2003\.01/
  },
  {
    pattern: /Salmonella\s+spp\.?\s+((?:Not Detected|Detected)[^\n]+)/i,
    code: 'UMPSK', testName: 'Salmonella species', parameter: 'Salmonella spp.',
    methodPattern: /AOAC-RI\s*121501/
  },
  {
    pattern: /Listeria\s+spp\.?\s+((?:Not Detected|Detected)[^\n]+)/i,
    code: 'UMQDQ', testName: 'Listeria species', parameter: 'Listeria spp.',
    methodPattern: /AOAC-RI\s*061702/
  },
];
```

### Report Validation

A report is considered valid if both `reportNumber` and `eurofinsSampleCode` are non-empty.

---

## 4. Database Schema

### Migration: `emp_swab_results` table

If this table doesn't already exist, create it. If it does exist, ensure it has all required columns. Here are the columns needed:

```sql
CREATE TABLE IF NOT EXISTS emp_swab_results (
  id              SERIAL PRIMARY KEY,
  emp_sample_id   VARCHAR(255) NOT NULL,   -- FK reference to emp_swabs.id (or coc_ref string)
  organism        VARCHAR(255) NOT NULL,   -- "Enterobacteriaceae", "Salmonella spp.", etc.
  method_1        VARCHAR(255),            -- "AOAC 2003.01", "AOAC-RI 121501", etc.
  result_value    VARCHAR(255),            -- "Not Detected", "30 (est)", "< 10", etc.
  units           VARCHAR(100),            -- "cfu/Sponge", "per Sponge", "N/A"
  date_reported   DATE,
  reviewed_by     VARCHAR(255) DEFAULT '', -- Left empty for manual assignment
  status          VARCHAR(100) DEFAULT 'Pending Review',
  notes           TEXT DEFAULT '',
  report_number   VARCHAR(100),            -- "AR-25-QP-119455-01" (for traceability)
  created_at      TIMESTAMP DEFAULT NOW(),
  updated_at      TIMESTAMP DEFAULT NOW(),

  -- Prevent duplicate results for the same sample + organism
  UNIQUE(emp_sample_id, organism)
);

CREATE INDEX idx_emp_swab_results_sample_id ON emp_swab_results(emp_sample_id);
CREATE INDEX idx_emp_swab_results_status ON emp_swab_results(status);
```

### Existing `emp_swabs` table

The app should already have an `emp_swabs` table (or equivalent) with a `coc_ref` column. This is the **Chain of Custody Reference** — the Client Sample Code from the Eurofins PDF (e.g. `122325-5A`).

The matching logic is:
1. Parse `clientSampleCode` from the PDF header (e.g. `122325-5A`)
2. Query `emp_swabs` table: `SELECT id FROM emp_swabs WHERE coc_ref = $1`
3. Use the returned `id` as `emp_sample_id` in the results table
4. **Fallback**: If no match found, use the Eurofins sample code (`498-2025-12240272`) as `emp_sample_id` and log a warning

---

## 5. Processor / Orchestrator (`eurofinsProcessor.ts`)

This replaces the Lambda handler. Implement a `processEurofinsEmails()` function:

```typescript
async function processEurofinsEmails(options?: { dryRun?: boolean; sinceDays?: number }): Promise<ProcessingSummary>
```

### Processing Flow

```
1. Initialize processing summary
2. Authenticate with Gmail
3. Search for Eurofins emails (since N days ago)
4. For each email:
   a. Get PDF attachments
   b. For each PDF:
      - Extract text from PDF
      - Parse header (report metadata)
      - Parse test results
      - Validate report (must have reportNumber + eurofinsSampleCode)
      - Look up emp_sample_id via clientSampleCode → emp_swabs.coc_ref
      - Convert to SwabResult objects
   c. If not dry run: mark email as processed (add Gmail label)
5. If not dry run: insert results into database
   - For each result, check for duplicates (emp_sample_id + organism)
   - Skip duplicates, insert new records
6. Return processing summary
```

### Converting Report to SwabResults

For each test result in the parsed report:
```typescript
const swabResult: SwabResult = {
  empSampleId: empSampleId,              // From DB lookup or fallback
  organism: testResult.parameter,         // e.g. "Enterobacteriaceae"
  method1: testResult.method,             // e.g. "AOAC 2003.01"
  resultValue: testResult.resultValue,    // e.g. "Not Detected" (without units)
  units: testResult.units,                // e.g. "per Sponge"
  dateReported: emailReceivedDate || new Date(),
  reviewedBy: '',                         // Empty — assigned manually
  status: 'Pending Review',
  notes: `Report: ${report.header.reportNumber}`
};
```

### Duplicate Detection

Before inserting, check if a record already exists with the same `emp_sample_id` AND `organism`:
```sql
SELECT COUNT(*) FROM emp_swab_results WHERE emp_sample_id = $1 AND LOWER(organism) LIKE '%' || LOWER($2) || '%'
```
If count > 0, skip the insert and increment `resultsSkipped`.

Alternatively, rely on the UNIQUE constraint and catch the conflict:
```sql
INSERT INTO emp_swab_results (...) VALUES (...) ON CONFLICT (emp_sample_id, organism) DO NOTHING
```

---

## 6. Scheduled Job (`eurofinsJob.ts`)

Use `node-cron` to run the processor on a schedule.

### Dependencies
```
npm install node-cron
```

### Configuration
```typescript
import cron from 'node-cron';
import { processEurofinsEmails } from '../services/eurofins/eurofinsProcessor';

// Default: run every 2 hours
const schedule = process.env.EUROFINS_CRON_SCHEDULE || '0 */2 * * *';

export function startEurofinsJob() {
  console.log(`Eurofins job scheduled: ${schedule}`);

  cron.schedule(schedule, async () => {
    console.log('Starting scheduled Eurofins email processing...');
    try {
      const summary = await processEurofinsEmails({
        sinceDays: parseInt(process.env.EUROFINS_SINCE_DAYS || '7'),
        dryRun: process.env.EUROFINS_DRY_RUN === 'true'
      });
      console.log('Eurofins processing complete:', JSON.stringify(summary));
    } catch (error) {
      console.error('Eurofins processing failed:', error);
    }
  });
}
```

Register the job in your Express app startup (e.g. in `server.ts` or `app.ts`):
```typescript
import { startEurofinsJob } from './services/scheduler/eurofinsJob';
startEurofinsJob();
```

---

## 7. (Optional) API Routes (`routes/eurofins.ts`)

For manual triggering and status monitoring:

```typescript
// POST /api/eurofins/process — manually trigger processing
// GET  /api/eurofins/status  — get last processing summary
```

---

## 8. Environment Variables

Add these to your `.env`:

```bash
# Gmail OAuth2 credentials (base64-encoded JSON)
GMAIL_CREDENTIALS_JSON=          # Base64-encoded OAuth2 client credentials JSON
GMAIL_TOKEN_JSON=                # Base64-encoded Gmail token JSON (with refresh_token)

# Eurofins processing config
EUROFINS_CRON_SCHEDULE=0 */2 * * *    # Cron expression (default: every 2 hours)
EUROFINS_SINCE_DAYS=7                  # How many days back to search for emails
EUROFINS_DRY_RUN=false                 # Set to "true" to log without writing to DB

# Logging
LOG_LEVEL=info
```

---

## 9. Error Handling & Edge Cases

### Processing Summary
Every run should produce and log a summary object:
```typescript
{
  timestamp: new Date().toISOString(),
  emailsProcessed: 0,
  pdfsParsed: 0,
  resultsFound: 0,
  resultsUploaded: 0,
  resultsSkipped: 0,
  errors: [],
  dryRun: false,
  success: true
}
```

### Edge Cases to Handle

1. **Duplicate results**: Check before insert (emp_sample_id + organism). Skip and increment `resultsSkipped`.
2. **Missing sample ID**: If `clientSampleCode` doesn't match any `emp_swabs.coc_ref`, fall back to using the Eurofins sample code as `emp_sample_id` and log a warning.
3. **Invalid PDFs**: If a PDF can't be parsed or is missing `reportNumber` + `eurofinsSampleCode`, log the error and continue to the next PDF.
4. **Gmail token refresh**: If the token is expired, automatically refresh using the refresh_token. If refresh fails, log an error and abort.
5. **Email pagination**: Gmail returns max 100 results per page. Paginate using `nextPageToken` until all results are collected.
6. **Dry run mode**: When `EUROFINS_DRY_RUN=true`, do NOT insert records or mark emails as processed. Log what would have been done.
7. **Missing date fields**: If `emailReceivedDate` is null, fall back to `new Date()`.
8. **Case-insensitive matching**: Organism duplicate checks should be case-insensitive.

### Logging
Use your existing logging infrastructure (Winston, Pino, or console). Key log points:
- INFO: Job start, Gmail auth success, emails found count, each PDF parsed, results uploaded count
- WARN: Missing sample ID (fallback used), invalid report, duplicate check failure
- ERROR: Gmail auth failure, PDF parse exception, database insert failure

---

## 10. Testing

Write tests for:
1. **PDF Parser**: Create test fixtures with sample PDF text. Verify all regex patterns extract correct values.
2. **Result conversion**: Verify `resultValue` extraction strips units correctly, `extractUnits` returns correct unit strings.
3. **Date parsing**: Test all three formats (`05Jan2026`, `1/5/2026`, `2026-01-05`).
4. **Duplicate detection**: Verify that duplicate sample+organism pairs are skipped.
5. **End-to-end**: Mock Gmail API, provide a test PDF buffer, verify the correct records would be inserted.

---

## Summary of npm Dependencies to Add

```
googleapis        # Gmail API client
pdf-parse         # PDF text extraction
node-cron         # Scheduled job execution
```

All other dependencies (Express, database client, logging) should already be in the project.
