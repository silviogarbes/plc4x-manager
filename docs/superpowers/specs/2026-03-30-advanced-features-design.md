# PLC4X Manager — Advanced Features Design Spec

**Date:** 2026-03-30
**Version:** 1.0
**Status:** Approved

---

## Overview

Three new features for PLC4X Manager v1.1:

1. **HMI Replay / Time Travel** — DVR industrial for visual playback of historical data
2. **Predictive Maintenance** — Failure history + supervised ML for remaining useful life prediction
3. **NLP Chat** — Natural language queries over plant data via OpenRouter LLM API

All features build on existing infrastructure (InfluxDB, Konva.js HMI, ML engine, WebSocket).

---

## Feature 1: HMI Replay / Time Travel

### Goal

Operator selects a date/time and sees the HMI screen exactly as it was — with historical tag values driving the same Konva.js elements (gauges, tanks, valves, motors). Supports both snapshot (single instant) and DVR playback (animated timeline).

### Architecture

```
[Live Mode]    WebSocket → current tags → Konva render
[Replay Mode]  InfluxDB query → historical tags → same Konva render
```

The HMI rendering is 100% reused. Only the data source changes.

### Backend

**New file: `admin/routes/replay_routes.py`**

#### `GET /api/replay/snapshot`

Returns all tag values for a device at a specific timestamp.

| Param | Type | Description |
|-------|------|-------------|
| `device` | string | Device name |
| `timestamp` | ISO 8601 | Target moment |

Query strategy:
- Flux query on `plc4x_raw` with `|> last()` in a ±5s window around timestamp
- If no data in raw (>90 days), fallback to `plc4x_hourly`

Response:
```json
{
  "device": "CLP-Linha3",
  "timestamp": "2026-03-29T14:32:00Z",
  "actual_timestamp": "2026-03-29T14:32:03Z",
  "tags": [
    {"alias": "Temperature", "value": 72.5},
    {"alias": "Pressure", "value": 3.2},
    {"alias": "MotorOn", "value": true}
  ],
  "source_bucket": "plc4x_raw"
}
```

#### `GET /api/replay/range`

Returns a series of snapshots for DVR playback.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `device` | string | — | Device name |
| `start` | ISO 8601 | — | Range start |
| `end` | ISO 8601 | — | Range end |
| `step` | string | `5s` | Aggregation window |

Query strategy:
- Flux `|> aggregateWindow(every: step, fn: last)` on `plc4x_raw`
- Auto-select bucket based on range duration (same logic as `/api/tags/history`)
- Max 720 points per request (1h at 5s, 12h at 1min, 30d at 1h)

Response:
```json
{
  "device": "CLP-Linha3",
  "start": "2026-03-29T14:00:00Z",
  "end": "2026-03-29T15:00:00Z",
  "step": "5s",
  "frames": [
    {
      "timestamp": "2026-03-29T14:00:00Z",
      "tags": [
        {"alias": "Temperature", "value": 71.2},
        {"alias": "Pressure", "value": 3.1}
      ]
    },
    {
      "timestamp": "2026-03-29T14:00:05Z",
      "tags": [...]
    }
  ],
  "frame_count": 720,
  "source_bucket": "plc4x_raw"
}
```

### Frontend

**Modified file: `admin/static/js/hmi.js`**

New UI components added to the HMI screen view:

1. **Replay button** — Clock icon in the HMI toolbar. Clicking opens the replay panel.

2. **Replay panel** (bottom bar, overlay on HMI screen):
   - Date-time picker for start (and end for range mode)
   - Mode toggle: Snapshot / DVR
   - DVR controls: Play, Pause, Step ← →, Speed (1x, 2x, 5x, 10x)
   - Timeline scrubber bar (draggable)
   - Current timestamp display

3. **Visual indicator**: Red banner "REPLAY MODE — 2026-03-29 14:32:05" at the top of the HMI screen. Prevents confusion with live data.

4. **"Back to Live"** button — returns to real-time WebSocket data.

**Data flow in replay mode:**
- Frontend requests `/api/replay/range` once for the full period
- All frames stored in memory (max 720 frames ≈ 500KB JSON)
- Player iterates frames locally with `setInterval` based on speed
- Each frame updates the Konva elements via existing `hmiUpdateTagValues()` function
- No per-frame API calls — smooth playback

### Data Requirements

| Range | Bucket | Step | Max frames |
|-------|--------|------|-----------|
| ≤ 6h | plc4x_raw | 5s–30s | 720 |
| ≤ 7d | plc4x_hourly | 1h | 168 |
| ≤ 2y | plc4x_daily | 1d | 730 |

### Access Control

- Requires authenticated user (any role)
- Plant filter applied (operator only sees devices in their plant)
- Read-only — replay never writes to PLCs

---

## Feature 2: Predictive Maintenance with Failure History

### Goal

Operators register real failures when they happen. The system correlates sensor data from before each failure to train supervised models that predict future failures: "Bearing failure predicted in ~72h (85% confidence)".

### Problem Statement

Current ML detects anomalies but has no concept of what a "failure" is. Without labeled failure data, it cannot distinguish a harmless anomaly from a pre-failure pattern. This feature closes the loop.

### Database Schema

**New tables in SQLite (`admin/database.py`):**

```sql
-- Catalog of known failure types per equipment class
CREATE TABLE failure_catalog (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,          -- "bearing_failure"
    display_name TEXT NOT NULL,         -- "Bearing Failure"
    description TEXT,                   -- "Excessive vibration leading to bearing damage"
    lookback_hours INTEGER DEFAULT 72,  -- how far back to extract features
    related_tags TEXT,                  -- JSON: ["Vibration", "Temperature", "MotorCurrent"]
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Log of actual failure occurrences
CREATE TABLE failure_log (
    id INTEGER PRIMARY KEY,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,  -- when the failure was reported
    occurred_at TEXT NOT NULL,                  -- when the failure actually happened
    device TEXT NOT NULL,
    equipment TEXT,                             -- HMI equipment name (optional)
    failure_type TEXT NOT NULL,                 -- FK to failure_catalog.name
    severity TEXT DEFAULT 'major',              -- minor, major, critical
    description TEXT,                           -- free-text operator notes
    resolved_at TEXT,                           -- when it was fixed
    reported_by TEXT,                           -- username
    tags_snapshot TEXT                          -- JSON: tag values at moment of failure
);

-- Trained model registry
CREATE TABLE failure_models (
    id INTEGER PRIMARY KEY,
    failure_type TEXT NOT NULL,
    device TEXT NOT NULL,
    trained_at TEXT DEFAULT CURRENT_TIMESTAMP,
    sample_count INTEGER,             -- number of failure events used
    accuracy REAL,                    -- cross-validation score
    model_blob BLOB,                  -- pickled sklearn model
    feature_names TEXT,               -- JSON: feature column names
    status TEXT DEFAULT 'active'      -- active, superseded, failed
);
```

### Backend

**New file: `admin/routes/failure_routes.py`**

#### Failure Catalog CRUD
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/failures/catalog` | List all failure types |
| `POST` | `/api/failures/catalog` | Create failure type (@admin) |
| `PUT` | `/api/failures/catalog/{name}` | Update failure type (@admin) |
| `DELETE` | `/api/failures/catalog/{name}` | Delete failure type (@admin) |

#### Failure Log CRUD
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/failures` | List failures (filterable by device, type, date) |
| `POST` | `/api/failures` | Report a failure (@operator) |
| `PUT` | `/api/failures/{id}` | Update failure (add resolution) (@operator) |
| `DELETE` | `/api/failures/{id}` | Delete failure (@admin) |

#### Model Training & Predictions
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/failures/train` | Train model for a failure type + device (@admin) |
| `GET` | `/api/failures/predictions` | Active predictions with confidence % |
| `GET` | `/api/failures/models` | List trained models with accuracy |

### ML Module

**New file: `ml/predictive_maintenance.py`**

#### Feature Extraction

For each failure in the log, extract features from InfluxDB for the `lookback_hours` window before the failure:

```python
def extract_features(device, tags, end_timestamp, lookback_hours):
    """
    Query InfluxDB for tag data in [end - lookback, end].
    Returns feature vector per tag:
    - mean, std, min, max, median
    - slope (linear regression)
    - rolling_mean_1h, rolling_std_1h
    - delta (last - first value)
    - rate_of_change (delta / hours)
    """
```

Features per tag: 10 statistical features × N related tags = feature vector.

#### Model Training

```python
def train_failure_model(failure_type, device):
    """
    1. Query all failure_log entries for this type + device
    2. For each failure: extract_features() for the lookback window (positive class)
    3. For equal number of random non-failure windows: extract_features() (negative class)
    4. Train GradientBoostingClassifier (sklearn)
    5. 5-fold cross-validation for accuracy
    6. Pickle model + save to failure_models table
    7. Return accuracy score

    Minimum: 5 failure events required. Below that, return error.
    """
```

Model choice: **GradientBoostingClassifier** — works well with small datasets, handles mixed feature types, provides feature importance for explainability.

#### Prediction (runs in predictor.py cycle)

```python
def predict_failures(device, tags_data):
    """
    1. Load active models for this device
    2. Extract features from current data (same window as training)
    3. Run model.predict_proba()
    4. If probability > 0.7: generate alert
    5. Write to plc4x_ml measurement:
       - analysis=predictive_maintenance
       - failure_type, probability, estimated_hours
    """
```

#### Integration with predictor.py

Add to the main ML cycle (after existing analyses):
```python
# Step 7: Predictive Maintenance (if models exist)
from predictive_maintenance import predict_failures
pm_results = predict_failures(device_name, device_tags)
```

### Frontend

**New tab: "Maintenance" in the main navigation**

Sections:
1. **Active Predictions** — Cards showing predicted failures with:
   - Failure type, device, estimated time to failure
   - Confidence percentage (progress bar)
   - Top contributing features (from model feature_importances_)
   - "Acknowledge" button

2. **Failure Log** — Table with filters:
   - Columns: Date, Device, Type, Severity, Description, Status, Reporter
   - "Report Failure" button → modal form
   - Status: Open / Resolved

3. **Failure Catalog** — Admin-only management:
   - List of failure types with related tags
   - "Add Type" / Edit / Delete
   - "Train Model" button per type (shows accuracy after training)

4. **Model Status** — List of trained models:
   - Failure type, device, accuracy, sample count, last trained

### Training Flow

1. Admin creates failure catalog entry: "Bearing Failure" with related tags [Vibration, Temperature, MotorCurrent] and lookback = 72h
2. Operators report failures as they occur over weeks/months
3. When 5+ failures of the same type exist, admin clicks "Train Model"
4. System extracts features, trains model, shows accuracy
5. Model runs automatically every ML cycle (5 min)
6. Alerts appear in Maintenance tab and alarm system

---

## Feature 3: NLP Chat with Plant Data

### Goal

Operator types a question in natural language ("What was the OEE of Line 2 last week?") and gets an answer with real data. Uses OpenRouter API for LLM access with automatic fallback to free models.

### Architecture

```
Operator types question
    ↓
Frontend (chat widget)
    ↓
POST /api/chat/ask {message, conversation_id}
    ↓
Backend builds context:
  - System prompt (role, capabilities, constraints)
  - Available tools (6 data-fetching functions)
  - Conversation history (last 20 messages)
    ↓
OpenRouter API (tool_use / function calling)
  - Primary: openrouter/auto (best cost/quality)
  - Fallback: openrouter/free (zero cost)
  - 402 handler: force openrouter/free on credit exhaustion
    ↓
LLM calls tools → backend executes → returns data to LLM
    ↓
LLM generates final response with real data
    ↓
Frontend renders: text (markdown) + inline chart (if numeric data)
```

### Configuration

**`.env` additions:**
```env
# NLP Chat — OpenRouter (leave CHAT_API_KEY empty to disable chat)
CHAT_API_KEY=
CHAT_API_URL=https://openrouter.ai/api/v1
CHAT_MODEL=openrouter/auto,openrouter/free
CHAT_MAX_TOKENS=2048
```

**`.env.example` presets:**
```env
# --- Option 1: Free only (dev/testing, zero cost) ---
# CHAT_MODEL=openrouter/free

# --- Option 2: Auto with free fallback (recommended for production) ---
# CHAT_MODEL=openrouter/auto,openrouter/free

# --- Option 3: Specific model with free fallback ---
# CHAT_MODEL=anthropic/claude-sonnet-4-20250514,openrouter/free
```

### Backend

**New file: `admin/routes/chat_routes.py`**

#### `POST /api/chat/ask`

Main endpoint. Requires JWT authentication (any role).

Request:
```json
{
  "message": "What was the OEE of Line 2 last week?",
  "conversation_id": "conv_abc123"
}
```

Response:
```json
{
  "reply": "The OEE of Line 2 last week was **78.3%**.\n\n- Availability: 92.1%\n- Performance: 88.5%\n- Quality: 96.2%",
  "data": [
    {"date": "2026-03-24", "oee": 76.1},
    {"date": "2026-03-25", "oee": 79.8},
    ...
  ],
  "chart": {
    "type": "line",
    "title": "OEE Line 2 — Last 7 Days",
    "labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "values": [76.1, 79.8, 81.2, 74.5, 80.1, 77.9, 78.4]
  },
  "model_used": "openrouter/auto",
  "conversation_id": "conv_abc123"
}
```

#### `GET /api/chat/history?conversation_id=X`

Returns conversation messages for context continuity.

#### `GET /api/chat/config` / `PUT /api/chat/config`

Admin-only. Read/update chat configuration (model, max tokens, system prompt override).

#### `GET /api/chat/status`

Returns whether chat is enabled (API key configured) and which model is set.

### OpenRouter Integration

**New file: `admin/chat_llm.py`**

```python
class ChatLLM:
    """OpenRouter LLM client with fallback logic."""

    def __init__(self):
        self.api_key = os.environ.get("CHAT_API_KEY", "")
        self.api_url = os.environ.get("CHAT_API_URL", "https://openrouter.ai/api/v1")
        self.models = os.environ.get("CHAT_MODEL", "openrouter/auto,openrouter/free").split(",")
        self.max_tokens = int(os.environ.get("CHAT_MAX_TOKENS", "2048"))

    async def ask(self, messages, tools):
        """
        Send to OpenRouter with fallback chain.

        1. Try with models list (OpenRouter native fallback)
        2. On HTTP 402 (no credits): retry with openrouter/free only
        3. On any other error: return error message to user
        """

    def _build_request(self, messages, tools, models):
        return {
            "models": models,
            "route": "fallback",
            "messages": messages,
            "tools": tools,
            "max_tokens": self.max_tokens,
            "temperature": 0.3,  # low for factual answers
        }
```

### LLM Tools (Function Calling)

6 tools the LLM can call to fetch real data:

| Tool | Parameters | Returns |
|------|-----------|---------|
| `query_tag_history` | device, tag, hours | Time series [{t, v}] |
| `get_current_values` | device | All tag values + status |
| `get_active_alarms` | (none) | Active alarms with severity |
| `get_oee` | device, hours | OEE breakdown (availability, performance, quality) |
| `get_ml_insights` | device | Anomalies, predictions, change points |
| `get_failure_history` | device, failure_type | Failure log entries (integrates Feature 2) |

Each tool maps directly to existing API endpoints — no new data logic needed.

### System Prompt

```
You are the PLC4X Manager AI assistant for an industrial monitoring system.
You help operators and engineers understand plant data, alarms, and equipment status.

Rules:
- Answer in the same language as the user's question
- When answering with numeric data, always include values and units
- Use the available tools to fetch real data — never invent or estimate values
- If a tool returns no results, say so clearly
- Keep answers concise and actionable
- For time references, use the plant's local timezone
- You can suggest checking specific HMI screens or Grafana dashboards when relevant

Available context:
- Plant devices and their tags (provided in each request)
- Historical data in InfluxDB (up to 90 days raw, 2 years hourly)
- Active alarms and alarm history
- ML analysis results (anomalies, predictions, correlations)
- Failure history and predictive maintenance alerts
- OEE calculations
```

### Database Schema

**New table in SQLite:**

```sql
CREATE TABLE chat_history (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    role TEXT NOT NULL,              -- "user" or "assistant"
    message TEXT NOT NULL,
    tool_calls TEXT,                 -- JSON: tools called by LLM (if any)
    model_used TEXT,                 -- which model responded
    user TEXT                        -- who asked
);
```

Retention: auto-prune conversations older than 30 days (in hourly maintenance job).

### Frontend

**Chat widget in `admin/static/js/app.js`:**

1. **Floating button** — Bottom-right corner, chat bubble icon. Hidden if `CHAT_API_KEY` not configured (checked via `GET /api/chat/status`).

2. **Slide-in panel** (right side, 400px wide):
   - Header: "AI Assistant" + minimize button
   - Message list: User messages (right-aligned), Assistant messages (left-aligned)
   - Assistant messages rendered as markdown (bold, lists, code blocks)
   - Inline mini-chart (Chart.js) when response includes `chart` object
   - "Typing..." indicator while waiting for LLM response
   - Input box at bottom with Send button (Enter to send)

3. **Conversation management:**
   - New conversation on first message or "New Chat" button
   - Last 5 conversations accessible via dropdown
   - Conversation context preserved (last 20 messages sent to LLM)

### Security

- JWT required (any authenticated role can use chat)
- Rate limit: 10 requests/minute per user (enforced in backend)
- API key never exposed to frontend — all LLM calls proxied through backend
- Tool calls restricted to internal read-only APIs (no writes via chat)
- Conversation history filtered by user (operators only see their own chats)
- Admin can see all conversations via `/api/chat/history?all=true`

### Fallback Chain (detailed)

```
Request arrives at /api/chat/ask
    ↓
Build OpenRouter request with models: ["openrouter/auto", "openrouter/free"]
    ↓
Send to OpenRouter API
    ↓
├─ 200 OK → process response (may include tool_calls loop)
├─ 402 No Credits → retry with models: ["openrouter/free"] only
├─ 429 Rate Limit → return "Chat busy, try again in a moment"
├─ 5xx Server Error → retry once, then return error
└─ Timeout (30s) → return "Chat temporarily unavailable"
```

---

## Implementation Order

| Phase | Feature | Estimated Files | Dependencies |
|-------|---------|----------------|-------------|
| **Phase 1** | HMI Replay | 2 new + 2 modified | InfluxDB (existing) |
| **Phase 2** | Predictive Maintenance | 4 new + 3 modified | ML engine (existing) + Phase 1 not required |
| **Phase 3** | NLP Chat | 4 new + 3 modified | OpenRouter API key + benefits from Phase 2 data |

Phases are independent and can be implemented in any order. Recommended: 1 → 2 → 3 because Replay is self-contained, Predictive Maintenance enriches the data layer, and Chat benefits from having all data sources available.

---

## Files Summary

### New Files
| File | Feature | Purpose |
|------|---------|---------|
| `admin/routes/replay_routes.py` | Replay | Snapshot + range endpoints |
| `admin/routes/failure_routes.py` | Pred. Maint. | Failure CRUD + training + predictions |
| `admin/routes/chat_routes.py` | Chat | Ask + history + config endpoints |
| `admin/chat_llm.py` | Chat | OpenRouter client with fallback |
| `ml/predictive_maintenance.py` | Pred. Maint. | Feature extraction + model training + prediction |

### Modified Files
| File | Changes |
|------|---------|
| `admin/database.py` | Add failure_catalog, failure_log, failure_models, chat_history tables |
| `admin/main.py` | Register new route files, add chat status to lifespan |
| `admin/static/js/hmi.js` | Replay mode UI (timeline bar, controls, mode indicator) |
| `admin/static/js/app.js` | Chat widget (floating button, slide-in panel, Chart.js inline) |
| `admin/static/css/style.css` | Chat widget styles |
| `admin/static/css/hmi.css` | Replay bar styles |
| `ml/predictor.py` | Add predictive maintenance step to ML cycle |
| `.env.example` | Add CHAT_* variables |
| `admin/requirements.txt` | Add httpx (async HTTP for OpenRouter) |
