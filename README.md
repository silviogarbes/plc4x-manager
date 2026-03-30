# PLC4X Manager

Web-based management interface for [Apache PLC4X](https://plc4x.apache.org/) OPC-UA Server.

Turn any PLC (Siemens S7, Modbus TCP, OPC-UA, and others) into an OPC-UA server with a simple web interface — no coding required.

## Features

- **Zero-code setup**: configure PLC connections and tags via web browser
- **Official PLC4X**: uses the official Apache PLC4X OPC-UA Server uber-jar
- **Multi-protocol**: Siemens S7, Modbus TCP, OPC-UA client, Simulated, and more
- **Docker-ready**: `docker compose up` and it's running
- **FastAPI + uvicorn**: async Python backend with WebSocket real-time data push
- **SQLite database**: persistent audit trail, alarm history, logbook — no extra DB needed
- **Version management**: update or rollback the PLC4X jar directly from the web UI
- **Auto-backup**: every config change creates a timestamped backup; SQLite backed up every 4 hours
- **Server control**: restart PLC4X server and view logs from the web UI
- **Connection templates**: pre-filled examples for each protocol
- **JWT authentication**: secure API access with login and brute-force lockout
- **HTTPS support**: auto-generated self-signed TLS certificate on port 3443
- **WebSocket live push**: real-time tag values, alarm events, and MQTT messages pushed to the browser
- **HMI / Synoptic**: interactive plant visualization with 16 element types, drag-and-drop editor, zoom/pan, live data, PLC write controls
- **Alarm system**: per-tag thresholds with conditional profiles (per product), virtual tags, sound notification, acknowledge, history
- **OEE dashboard**: Availability x Performance x Quality with SVG gauges and trend chart
- **Multi-plant dashboard**: consolidated view grouped by plant with device status, alarms, OEE per device
- **ML Analytics**: 5 ML modules running automatically every cycle — see ML Analytics section below
- **PDF reports & CSV export**: device status, tag statistics, alarm history, raw data export
- **Shift logbook**: operator observations, incidents, handover notes per shift
- **Inline tag trending**: click any tag value for instant SVG trend chart with period selection
- **Kiosk mode**: auto-cycling screens for monitoring room TV displays (URL param `?kiosk`)
- **PLC write protection**: global read-only mode (`PLC_READONLY=true` by default) — blocks all PLC writes at server level. Requires server access to enable writes (not configurable from UI for safety)
- **RBAC**: 3 roles (admin / operator / monitor) with plant-based access filter per user
- **Audit trail**: automatic logging of all actions with user, timestamp, IP
- **Virtual tags**: manually-set values (no PLC) that flow through the full pipeline (MQTT, InfluxDB, alarms)
- **Calculated tags**: formulas based on other tag values (e.g., efficiency = output / input)

## ML Analytics

The `plc4x-ml` container runs 5 ML modules automatically on every configured interval (default: 5 minutes):

| Module | Library | Output |
|--------|---------|--------|
| **Prophet Forecast** | `prophet` | 2-hour ahead prediction with confidence bands → `plc4x_forecast` in InfluxDB |
| **Anomaly Detection** | `scikit-learn` (Isolation Forest) | Anomaly score + flag → `plc4x_anomaly` |
| **PyOD Ensemble** | `pyod` (ECOD + LOF + IForest) | Multi-algorithm consensus anomaly → `plc4x_ml` |
| **Change Point** | `ruptures` | Detects sudden mean/trend shifts → `plc4x_ml` |
| **Pattern Matching** | `stumpy` | Motif (recurring patterns) and discord (rare anomalies) discovery → `plc4x_ml` |
| **Cross-tag Correlation** | `numpy` | Correlation matrix + broken correlation alerts → `plc4x_ml` |
| **SHAP Explainability** | `shap` | Top contributing tags when an anomaly is detected → `plc4x_ml` |

Results are visualized in Grafana (AI Predictions and Alarms & Anomalies dashboards). A manual "Run Now" button in the web admin triggers an immediate ML cycle without waiting for the next scheduled interval.

## Quick Start

### Production
```bash
# Linux (recommended)
git clone https://github.com/silviogarbes/plc4x-manager.git
cd plc4x-manager && cp .env.example .env
docker compose up -d
```

### Development (Windows)
```powershell
# Docker Desktop required
git clone https://github.com/silviogarbes/plc4x-manager.git
cd plc4x-manager && copy .env.example .env
docker compose up -d
# For PLC access, see docs/INSTALLATION.md (port forwarding required)
```

- **Web Admin**: http://localhost:3080
- **Grafana Dashboards**: http://localhost:3000 (credentials: `admin` / `admin`)
- **OPC-UA Server**: opc.tcp://localhost:12687
- **InfluxDB**: http://localhost:8086
- **MQTT Broker**: mqtt://localhost:1883

### Default Credentials

| Role | Username | Password | Permissions |
|------|----------|----------|-------------|
| Admin | `admin` | `admin` | Full access: configure devices, tags, HMI, alarms, server, users |
| Operator | `operator` | `operator` | View all + write to PLC tags (valves, setpoints) + logbook + acknowledge alarms |
| Monitor | `monitor` | `monitor` | View only + acknowledge alarms + shift logbook (no PLC writes) |

> **WARNING**: Change all default credentials (`ADMIN_PASSWORD`, `OPERATOR_PASSWORD`, `MONITOR_PASSWORD`, `JWT_SECRET`) in `.env` before deploying to production.
>
> **Kiosk mode**: Open `http://localhost:3080?kiosk` for auto-cycling TV display.

For a detailed step-by-step guide including first device setup, OPC-UA client connection, and production deployment, see **[Installation Guide](docs/INSTALLATION.md)**.

## Architecture

```
                     ┌──────────────────────────────────────────────────────────┐
  Browser ──────────►│  plc4x-admin (:3080 / :3443)                            │
  WebSocket ────────►│  FastAPI + uvicorn  │  Web UI + REST API + WebSocket     │
                     │  Config, Live Data, MQTT publisher, InfluxDB writer      │
                     │  Alarm engine, OEE, HMI editor, RBAC, SQLite DB         │
                     └──────────┬────────────┬────────────┬────────────────────┘
                                │            │            │
                     ┌──────────▼──┐  ┌──────▼──┐  ┌─────▼──────┐
  SCADA / HMI ◄──────│ plc4x-server│  │InfluxDB │  │ Mosquitto  │
  OPC-UA Client      │ :12687      │  │ :8086   │  │ :1883 MQTT │
                     └──────┬──────┘  └────┬────┘  └────────────┘
                            │              │
              ┌─────────────┤        ┌─────┼──────────┐
              │             │        │     │          │
  ┌───────────▼┐ ┌─────────▼┐  ┌────▼───┐  ┌────────▼───────────┐
  │ Siemens S7 │ │ Modbus   │  │Grafana │  │ plc4x-ml           │
  │ Logix, EIP │ │ OPC-UA   │  │ :3000  │  │ Prophet forecast   │
  └────────────┘ └──────────┘  └────────┘  │ Isolation Forest   │
                                           │ PyOD ensemble      │
                                           │ Change point       │
                                           │ Pattern matching   │
                                           └────────────────────┘
```

### Containers (6 total)

| Container | Port | Description |
|-----------|------|-------------|
| plc4x-admin | 3080, 3443 | FastAPI (uvicorn) web admin panel, REST API, WebSocket live push, HMI synoptic editor, MQTT publisher, InfluxDB writer, alarm engine, OEE calculation, PDF/CSV reports, shift logbook, audit trail, RBAC, SQLite database |
| plc4x-server | 12687 | Apache PLC4X OPC-UA Server |
| influxdb | 8086 | Time-series database (90-day raw, 2-year hourly, daily forever) |
| grafana | 3000 | 8 dashboards: Overview, Plant Overview, Device Detail, Energy, Predictions, Alarms, Custom Data, System Health |
| mosquitto | 1883 | MQTT broker for real-time tag publishing |
| plc4x-ml | — | ML Analytics: Prophet forecast, Isolation Forest anomaly, PyOD ensemble, change point detection (ruptures), pattern matching (stumpy), cross-tag correlation, SHAP explainability |

## Configuration

### Update PLC4X Version

**From the Web UI** (recommended): go to the **OPC-UA Server** tab, enter the desired version (e.g., `0.13.0`), and click **Apply Version**. The current jar is backed up automatically, and you can undo the change with a single click.

**From `.env`** (rebuild required):

```env
PLC4X_VERSION=0.13.1
```

```bash
docker compose build plc4x-server
docker compose up -d
```

### Ports

| Service         | Default Port | Environment Variable |
|-----------------|-------------|---------------------|
| OPC-UA Server   | 12687       | `OPCUA_PORT`        |
| Web Admin HTTP  | 3080        | `ADMIN_PORT`        |
| Web Admin HTTPS | 3443        | `ADMIN_HTTPS_PORT`  |

### Host Network Mode

If your PLCs are on the local network and not reachable from Docker's default bridge network, change `docker-compose.yml`:

```yaml
plc4x-server:
  network_mode: host
```

## Supported Protocols

| Protocol          | Connection String Example           | Tag Example                 |
|-------------------|-------------------------------------|-----------------------------|
| Siemens S7        | `s7://192.168.1.10`                 | `%DB1:0:REAL`               |
| Modbus TCP        | `modbus-tcp://192.168.1.20:502`     | `holding-register:1`        |
| Modbus RTU        | `modbus-rtu:///dev/ttyUSB0`         | `holding-register:1`        |
| Modbus ASCII      | `modbus-ascii:///dev/ttyUSB0`       | `holding-register:1`        |
| OPC-UA Client     | `opcua:tcp://192.168.1.30:4840`     | `ns=2;i=10`                 |
| EtherNet/IP       | `eip://192.168.1.40`                | `%MyTag:DINT`               |
| Allen-Bradley     | `logix://192.168.1.50`              | `MyTag`                     |
| KNXnet/IP         | `knxnet-ip://192.168.1.60`          | `1/1/1:DPT_Switch`          |
| IEC 60870-5-104   | `iec-60870-5-104://192.168.1.70`    | `M_ME_NC_1:5:20`            |
| Firmata (Arduino) | `firmata:///dev/ttyACM0`            | `digital:13`                |
| Simulated         | `simulated://127.0.0.1`             | `RANDOM/Temporary:DINT`     |

See the full list at [PLC4X Drivers](https://plc4x.apache.org/plc4x/latest/users/protocols/index.html).

## API Reference

Full interactive documentation available at the **API** tab in the web admin, or via Swagger at `/static/swagger.json`. Postman collection at `docs/PLC4X-Manager.postman_collection.json`.

### Authentication
| Method | Endpoint                              | Description                    |
|--------|---------------------------------------|--------------------------------|
| POST   | `/api/auth/login`                     | Login and get JWT token        |

### Configuration
| Method | Endpoint                              | Description                    |
|--------|---------------------------------------|--------------------------------|
| GET    | `/api/config`                         | Get full config                |
| PUT    | `/api/config`                         | Save full config               |
| PUT    | `/api/config/server`                  | Update server settings         |

### Devices & Tags
| Method | Endpoint                              | Description                    |
|--------|---------------------------------------|--------------------------------|
| GET    | `/api/devices`                        | List devices                   |
| POST   | `/api/devices`                        | Add device                     |
| PUT    | `/api/devices/<name>`                 | Update device                  |
| DELETE | `/api/devices/<name>`                 | Remove device                  |
| GET    | `/api/devices/<name>/tags`            | List tags                      |
| POST   | `/api/devices/<name>/tags`            | Add tag                        |
| DELETE | `/api/devices/<name>/tags/<alias>`    | Remove tag                     |
| POST   | `/api/demo/load`                      | Load demo devices              |

### Live Data (Read & Write)
| Method | Endpoint                              | Description                    |
|--------|---------------------------------------|--------------------------------|
| GET    | `/api/live/read`                      | Read all tag values            |
| GET    | `/api/live/read/<device>`             | Read tags for one device       |
| POST   | `/api/live/write`                     | Write a value to a PLC tag     |

### Server Control
| Method | Endpoint                              | Description                    |
|--------|---------------------------------------|--------------------------------|
| GET    | `/api/server/status`                  | Server status                  |
| POST   | `/api/server/restart`                 | Restart server                 |
| GET    | `/api/server/logs?lines=50`           | Server logs                    |

### PLC4X Version Management
| Method | Endpoint                              | Description                    |
|--------|---------------------------------------|--------------------------------|
| GET    | `/api/plc4x/version`                  | Current and backup version     |
| GET    | `/api/plc4x/latest-version`           | Check latest on Maven Central  |
| POST   | `/api/plc4x/update`                   | Update jar to a version        |
| POST   | `/api/plc4x/rollback`                 | Rollback to previous version   |

### Users
| Method | Endpoint                              | Description                    |
|--------|---------------------------------------|--------------------------------|
| GET    | `/api/users`                          | List OPC-UA users              |
| POST   | `/api/users`                          | Add user                       |
| PUT    | `/api/users/<username>`               | Update user                    |
| DELETE | `/api/users/<username>`               | Remove user                    |

### Backups
| Method | Endpoint                              | Description                    |
|--------|---------------------------------------|--------------------------------|
| GET    | `/api/backups`                        | List backups                   |
| POST   | `/api/backups/create`                 | Create manual backup           |
| POST   | `/api/backups/upload`                 | Restore from uploaded file     |
| POST   | `/api/backups/<file>/restore`         | Restore a backup               |
| POST   | `/api/backups/cleanup`                | Cleanup old backups            |
| GET    | `/api/templates`                      | Connection string templates    |
| GET    | `/api/security/status`                | Security initialization status |
| GET    | `/api/security/certificates/trusted`  | List trusted client certs      |
| GET    | `/api/security/certificates/rejected` | List rejected client certs     |

### Alarms
| Method | Endpoint                                          | Description                    |
|--------|---------------------------------------------------|--------------------------------|
| GET    | `/api/alarms`                                     | Active alarms and history      |
| POST   | `/api/alarms/acknowledge`                         | Acknowledge an alarm           |
| POST   | `/api/alarms/acknowledge-all`                     | Acknowledge all alarms         |
| PUT    | `/api/devices/<name>/tags/<alias>/alarms`         | Set alarm thresholds           |

### OEE
| Method | Endpoint                                          | Description                    |
|--------|---------------------------------------------------|--------------------------------|
| GET    | `/api/oee/calculate`                              | Calculate OEE for a device     |
| GET    | `/api/oee/trend`                                  | OEE trend data                 |
| PUT    | `/api/devices/<name>/oee-config`                  | Configure OEE                  |

### Reports
| Method | Endpoint                                          | Description                    |
|--------|---------------------------------------------------|--------------------------------|
| GET    | `/api/export/csv`                                 | Export tag history as CSV      |
| GET    | `/api/export/pdf`                                 | Generate PDF report            |

### Logbook
| Method | Endpoint                                          | Description                    |
|--------|---------------------------------------------------|--------------------------------|
| GET    | `/api/logbook`                                    | List logbook entries           |
| POST   | `/api/logbook`                                    | Add logbook entry              |

### Audit
| Method | Endpoint                                          | Description                    |
|--------|---------------------------------------------------|--------------------------------|
| GET    | `/api/audit`                                      | Audit trail entries            |

### Tag Trending
| Method | Endpoint                                          | Description                    |
|--------|---------------------------------------------------|--------------------------------|
| GET    | `/api/tags/history`                               | Tag value history for charts   |

## License

Apache License 2.0 — same as Apache PLC4X.

This project uses the official Apache PLC4X OPC-UA Server jar, which is downloaded from Maven Central at build time.
