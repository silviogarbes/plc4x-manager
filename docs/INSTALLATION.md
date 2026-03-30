# PLC4X Manager - Installation Guide

Step-by-step guide to install, configure, and run PLC4X Manager.

---

## System Requirements

### Production (recommended)
- **OS:** Ubuntu Server 22.04 LTS or Debian 12 (Linux)
- **RAM:** 8 GB minimum (16 GB recommended for ML analytics)
- **CPU:** 4 cores minimum
- **Disk:** 100 GB SSD
- **Network:** Direct access to PLC network (same subnet or routed)
- **Docker:** Docker Engine 24+ with Docker Compose v2

### Development / Testing (Windows)
- **OS:** Windows 10/11 Pro with Docker Desktop
- **RAM:** 16 GB minimum
- **Note:** Docker Desktop uses NAT — PLCs on external subnets require port forwarding (see below)

---

## Deployment — Linux Production

### 1. Install Docker
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

### 2. Clone and deploy
```bash
git clone https://github.com/silviogarbes/plc4x-manager.git
cd plc4x-manager
cp .env.example .env
# Edit .env: set JWT_SECRET, passwords, PLC4X_VERSION
nano .env
docker compose up -d
```

### 3. Verify
```bash
docker compose ps          # All 6 containers healthy
curl http://localhost:3080/healthz  # {"status":"ok"}
```

Open browser: `http://<server-ip>:3080`

### Network: PLC Access
On Linux, Docker containers access the host network directly. PLCs on the same subnet or reachable via routing work without any extra configuration.

If PLCs are on a different VLAN/subnet, ensure the Linux server has a route:
```bash
# Example: PLC subnet 192.168.1.0/24 via gateway 10.0.0.1
sudo ip route add 192.168.1.0/24 via 10.0.0.1
```

---

## Deployment — Coolify (Self-Hosted PaaS)

PLC4X Manager is fully compatible with [Coolify](https://coolify.io/) for automated deployments from GitHub.

### 1. Create a new resource

In the Coolify dashboard:
1. Click **+ New Resource** → **Docker Compose**
2. Connect your GitHub repository (`silviogarbes/plc4x-manager`)
3. Select the `main` branch

### 2. Configure environment variables

In the **Environment Variables** section, set at minimum:

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=your-random-secret-key
```

All other variables have sensible defaults (see section 11).

### 3. Port configuration

The admin panel defaults to ports **3080** (HTTP) and **3443** (HTTPS) to avoid conflicts with Coolify's Traefik reverse proxy on port 8080.

Coolify's Traefik will route your domain to the container automatically. If you need to override ports, set `ADMIN_PORT` and `ADMIN_HTTPS_PORT` in environment variables.

### 4. Important notes

- **No bind mounts required** — all config files (Grafana provisioning, Mosquitto config, InfluxDB init scripts) are embedded in their respective Docker images via Dockerfiles. This is required because Coolify runs `docker compose` inside a helper container where host-relative bind mounts (`./dir:/path`) do not resolve correctly.
- **Docker socket** — the `plc4x-admin` container mounts `/var/run/docker.sock` for container management (PLC4X server restart, version updates). Ensure Coolify allows Docker socket access.
- **PLC network** — ensure the Coolify host has network access to the PLC subnet. Docker bridge networking is used by default; for PLCs on isolated subnets, add routes on the host (see Linux deployment section above).
- **Persistent data** — Coolify manages Docker named volumes automatically. Configuration, InfluxDB time-series, Grafana dashboards, and MQTT data all persist across redeployments.

---

## Deployment — Windows (Development / Testing)

### 1. Install Docker Desktop
Download from https://www.docker.com/products/docker-desktop/

### 2. Clone and deploy
```powershell
git clone https://github.com/silviogarbes/plc4x-manager.git
cd plc4x-manager
copy .env.example .env
# Edit .env with notepad
docker compose up -d
```

### 3. PLC Network Access (Port Forwarding)
Docker Desktop on Windows uses NAT — containers may not directly reach PLCs on your plant network. Use Windows port forwarding (`netsh`) to bridge the gap.

#### Example: Rockwell PLC at 192.168.1.10

Open **PowerShell as Administrator** and run:

```powershell
# Step 1 — Create port forward from host to PLC (EtherNet/IP uses port 44818)
netsh interface portproxy add v4tov4 listenport=44818 listenaddress=0.0.0.0 connectport=44818 connectaddress=192.168.1.10

# Step 2 — Verify the rule was created
netsh interface portproxy show all

# Expected output:
# Listen on ipv4:             Connect to ipv4:
# Address         Port        Address         Port
# --------------- ----------  --------------- ----------
# 0.0.0.0         44818       192.168.1.10  44818
```

Then configure the device in PLC4X Manager with `host.docker.internal`:
```
eip://host.docker.internal:44818?backplane=1&slot=0
```

#### Multiple PLCs — use different local ports

```powershell
# PLC 1 → local port 44818
netsh interface portproxy add v4tov4 listenport=44818 listenaddress=0.0.0.0 connectport=44818 connectaddress=192.168.1.10

# PLC 2 → local port 44819
netsh interface portproxy add v4tov4 listenport=44819 listenaddress=0.0.0.0 connectport=44818 connectaddress=192.168.1.11

# PLC 3 → local port 44820
netsh interface portproxy add v4tov4 listenport=44820 listenaddress=0.0.0.0 connectport=44818 connectaddress=192.168.1.12
```

Connection strings in PLC4X Manager:
```
eip://host.docker.internal:44818?backplane=1&slot=0    → PLC at 192.168.1.10
eip://host.docker.internal:44819?backplane=1&slot=0    → PLC at 192.168.1.11
eip://host.docker.internal:44820?backplane=1&slot=0    → PLC at 192.168.1.12
```

#### Manage port forwards

```powershell
# List all active port forwards
netsh interface portproxy show all

# Remove a specific port forward
netsh interface portproxy delete v4tov4 listenport=44818 listenaddress=0.0.0.0

# Remove ALL port forwards
netsh interface portproxy reset
```

> **Note:** With Docker Desktop WSL2 backend, containers often have direct access to
> the host network. In that case you can use the PLC IP directly (e.g.,
> `eip://192.168.1.10?backplane=1&slot=0`). The pylogix discovery service always
> connects directly. If the PLC4X Java OPC-UA server cannot reach the PLC but pylogix
> can, try the `host.docker.internal` + portproxy approach above.

> **Production:** On Linux, Docker has direct network access — no port forwarding needed.

---

### Port Reference for Common PLC Protocols

| Protocol | Default Port | Used by |
|----------|-------------|---------|
| EtherNet/IP (CIP) | 44818 | Rockwell Allen-Bradley |
| OPC-UA | 4840 | Generic OPC-UA servers |
| Siemens S7 (ISO-on-TCP) | 102 | Siemens S7-300/400/1200/1500 |
| Modbus TCP | 502 | Modbus devices |
| KNXnet/IP | 3671 | KNX building automation |

For each PLC you want to test from Windows, create a port forward for its protocol port.

---

## 1. Prerequisites

| Requirement | Minimum Version | Check Command |
|-------------|----------------|---------------|
| Docker | 20.10+ | `docker --version` |
| Docker Compose | 2.0+ (V2) | `docker compose version` |
| Git | any | `git --version` |

**System requirements:**
- 2 GB RAM available for Docker containers
- 1 GB disk space (Docker images + backups)
- Network access to the PLCs you want to connect to

**Ports used (configurable):**

| Port | Service | Protocol |
|------|---------|----------|
| 3080 | Web Admin (HTTP) | TCP |
| 3443 | Web Admin (HTTPS) | TCP |
| 12687 | OPC-UA Server | TCP |
| 3000 | Grafana Dashboards | TCP |
| 8086 | InfluxDB | TCP |
| 1883 | MQTT Broker | TCP |

---

## 2. Installation

### 2.1 Clone the repository

```bash
git clone https://github.com/silviogarbes/plc4x-manager.git
cd plc4x-manager
```

### 2.2 Configure environment variables

Copy and edit the `.env` file:

```bash
cp .env.example .env   # if .env doesn't exist yet
```

Edit `.env` with your settings:

```env
# PLC4X version (check https://repo1.maven.org/maven2/org/apache/plc4x/plc4x-opcua-server/)
PLC4X_VERSION=0.13.1

# Ports
OPCUA_PORT=12687
ADMIN_PORT=3080
ADMIN_HTTPS_PORT=3443

# Authentication (CHANGE THESE!)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=change-me-to-a-random-secret-key
JWT_EXPIRY_HOURS=24

# Optional: API key for machine-to-machine access
API_KEY=
```

> **IMPORTANT**: Change `ADMIN_PASSWORD` and `JWT_SECRET` before deploying to production.

### 2.3 Build and start

```bash
docker compose up -d --build
```

This will:
1. Download the PLC4X OPC-UA Server jar from Maven Central
2. Build the admin panel container (Python/FastAPI + uvicorn)
3. Start all 6 containers
4. Auto-generate a self-signed TLS certificate for HTTPS
5. Seed demo devices and logbook entries on first deploy (auto-detected)
6. Initialize the SQLite database for audit trail, alarms, and logbook

First startup takes 1-2 minutes (jar download). Subsequent starts are instant.

### 2.4 Verify it's running

```bash
docker compose ps
```

You should see both containers as `healthy`:

```
NAME           STATUS                    IMAGE
plc4x-admin    Up 30 seconds (healthy)   plc4x-manager-plc4x-admin
plc4x-server   Up 30 seconds (healthy)   plc4x-manager-plc4x-server
```

---

## 3. First Access

### 3.1 Open the web admin

Open your browser and go to:

- **HTTP**: http://localhost:3080
- **HTTPS**: https://localhost:3443 (accept the self-signed certificate warning)

### 3.2 Log in

Use the credentials from your `.env` file:

- **Username**: `admin` (default)
- **Password**: `admin` (default) or whatever you set in `ADMIN_PASSWORD`

### 3.3 Dashboard

After login you will see the Dashboard with:
- **Server status** (Running/Stopped)
- **Device count** and their live status (Online/Error/Disabled)
- **Tag count** across all devices

The configuration starts empty. Go to the **Devices** tab and click **Load Demo Devices** to add a "Demo-Simulated" device for testing.

---

## 4. Add Your First Real Device

### 4.1 Go to the Devices tab

Click **Devices** in the top navigation bar.

### 4.2 Click "+ Add Device"

### 4.3 Select a protocol

Choose from the dropdown (e.g., "Modbus TCP", "Siemens S7"). This fills in the connection string template and example tags.

### 4.4 Fill in the details

**Example: Modbus TCP energy meter**

| Field | Value |
|-------|-------|
| Protocol | Modbus TCP |
| Name | EnergyMeter1 |
| Connection String | `modbus-tcp://192.168.1.20:502` |

### 4.5 Add tags

Click **+ Add Tag** and fill in:

| Alias | Address |
|-------|---------|
| Voltage_L1 | `holding-register:1` |
| Voltage_L2 | `holding-register:2` |
| Voltage_L3 | `holding-register:3` |
| Current_L1 | `holding-register:4` |

> **Note**: Modbus register addresses must be >= 1. The system validates this automatically.

### 4.6 Add calculated tags (optional)

Click **+ Add Calculated Tag** to create virtual tags from formulas:

| Alias | Formula |
|-------|---------|
| VoltageAvg | `avg(Voltage_L1, Voltage_L2, Voltage_L3)` |
| CurrentTotal | `sum(Current_L1, Current_L2, Current_L3)` |

### 4.7 Save

Click **Save**. The configuration is saved and backed up automatically.

### 4.8 Restart the server

Click the **Restart** button in the top right corner. The PLC4X server needs a restart to pick up new devices.

### 4.9 Verify

Go to the **Live Data** tab. Your device should appear with status "Online" if the PLC is reachable, or "Error" if the connection failed (check the IP/port).

---

## 4b. Protocol-Specific Examples

### Allen-Bradley ControlLogix / CompactLogix (EtherNet/IP)

| Field | Value |
|-------|-------|
| Protocol | Allen-Bradley Logix |
| Name | `ControlLogix1` |
| Connection String | `logix://192.168.1.50` |

To specify the processor slot (default is 0):
```
logix://192.168.1.50?slot=0
```

**Tags** use the exact tag name as defined in the PLC program (RSLogix 5000 / Studio 5000):

| Alias | Address | Description |
|-------|---------|-------------|
| Temperature | `Temperature` | Controller-scoped tag |
| MotorSpeed | `MotorSpeed` | Controller-scoped tag |
| RunStatus | `Program:MainProgram.RunStatus` | Program-scoped tag |
| ArrayElement | `MyArray[5]` | Array element |
| UDTField | `MyUDT.Field1` | UDT member access |

**Requirements:**
- PLC accessible on port **44818** (TCP) — EtherNet/IP default, enabled by default on ControlLogix/CompactLogix
- No RSLinx or other middleware needed — PLC4X connects directly to the PLC
- Tags must be **Controller-scoped** for direct access, or use full path `Program:ProgramName.TagName`
- No special PLC configuration required

**Test connectivity** before adding the device:
```bash
docker exec plc4x-server bash -c "timeout 3 bash -c 'echo >/dev/tcp/192.168.1.50/44818' && echo OK || echo FAIL"
```

**Network note:** If the PLC is on the local network and not reachable from Docker's bridge network, add `network_mode: host` to the `plc4x-server` service in `docker-compose.yml` (see section 6.3).

---

### Siemens S7 (S7-300, S7-400, S7-1200, S7-1500)

| Field | Value |
|-------|-------|
| Protocol | Siemens S7 |
| Name | `SiemensS7` |
| Connection String | `s7://192.168.1.10` |

To specify rack and slot (default: rack=0, slot=1):
```
s7://192.168.1.10?remote-rack=0&remote-slot=1
```

Common slot values:
- S7-300/400: slot=2
- S7-1200/1500: slot=1

**Tags** use the S7 addressing format:

| Alias | Address | Description |
|-------|---------|-------------|
| Temperature | `%DB1:0:REAL` | DB1, byte 0, 32-bit float |
| Pressure | `%DB1:4:REAL` | DB1, byte 4, 32-bit float |
| Counter | `%DB1:8:DINT` | DB1, byte 8, 32-bit integer |
| MotorOn | `%DB1:12.0:BOOL` | DB1, byte 12, bit 0 |
| Speed | `%DB1:14:INT` | DB1, byte 14, 16-bit integer |
| StatusWord | `%DB1:16:WORD` | DB1, byte 16, 16-bit word |

**S7-1200/1500 requirement:** The data block must have "Optimized block access" **disabled** (uncheck in DB properties in TIA Portal). Otherwise the byte offsets won't work.

**Test connectivity:**
```bash
docker exec plc4x-server bash -c "timeout 3 bash -c 'echo >/dev/tcp/192.168.1.10/102' && echo OK || echo FAIL"
```

---

### Modbus TCP (Energy meters, VFDs, sensors)

| Field | Value |
|-------|-------|
| Protocol | Modbus TCP |
| Name | `EnergyMeter1` |
| Connection String | `modbus-tcp://192.168.1.20:502` |

To specify the Modbus unit ID (default: 1):
```
modbus-tcp://192.168.1.20:502?unit-identifier=1
```

**Tags** use the Modbus register type and address (starting from 1):

| Alias | Address | Description |
|-------|---------|-------------|
| Voltage_L1 | `holding-register:1` | Holding register 1 |
| Current_L1 | `holding-register:4` | Holding register 4 |
| Coil_0 | `coil:1` | Coil 1 (digital output) |
| Input_0 | `discrete-input:1` | Discrete input 1 |
| AnalogIn | `input-register:1` | Input register 1 |

> **Important:** Modbus register addresses must be **>= 1** (not 0). The system validates this automatically.

**Test connectivity:**
```bash
docker exec plc4x-server bash -c "timeout 3 bash -c 'echo >/dev/tcp/192.168.1.20/502' && echo OK || echo FAIL"
```

---

### OPC-UA Client (connect to another OPC-UA server)

| Field | Value |
|-------|-------|
| Protocol | OPC-UA (Client) |
| Name | `RemoteOpcUa` |
| Connection String | `opcua:tcp://192.168.1.30:4840` |

**Tags** use the OPC-UA node ID format:

| Alias | Address | Description |
|-------|---------|-------------|
| ServerStatus | `ns=0;i=2259` | Server status (standard node) |
| CurrentTime | `ns=0;i=2258` | Server current time |
| MyVariable | `ns=2;s=MyVariable` | String-identified node |
| NumericNode | `ns=2;i=1001` | Numeric-identified node |

> **Important:** PLC4X only supports `ns=N;i=ID` or `ns=N;s=NAME` format. The `nsu=` (namespace URI) format is **not supported** and will crash the server.

---

## 5. Connect an OPC-UA Client

Any OPC-UA client can connect to the PLC4X server to read tag values.

### 5.1 OPC-UA endpoint

```
opc.tcp://<server-ip>:12687/plc4x
```

Replace `<server-ip>` with the IP or hostname of the machine running Docker.

### 5.2 Security modes

| Mode | Description |
|------|-------------|
| None | No encryption, no authentication (for testing) |
| Basic256Sha256 + SignAndEncrypt | Full encryption with certificate |

### 5.3 Authentication

| Method | Credentials |
|--------|-------------|
| Anonymous | No credentials needed |
| Username/Password | Default: `admin` / `password` (change in Security tab) |

### 5.4 Test with UaExpert (free OPC-UA client)

1. Download [UaExpert](https://www.unified-automation.com/products/development-tools/uaexpert.html)
2. Add server: `opc.tcp://<server-ip>:12687/plc4x`
3. Select security: "None" for testing
4. Connect
5. Browse the address space: you should see your devices and tags under the "PLC4X" folder

### 5.5 Other clients

| Client | Type |
|--------|------|
| Ignition | SCADA/HMI |
| Grafana (OPC-UA plugin) | Dashboards |
| Node-RED | Flow automation |
| KEPServerEX | Industrial gateway |
| MATLAB/Simulink | Simulation |

---

## 6. Production Deployment

### 6.1 Change all default passwords

1. **Web admin password**: set `ADMIN_PASSWORD` in `.env`
2. **JWT secret**: set `JWT_SECRET` in `.env` to a random string (e.g., `openssl rand -hex 32`)
3. **OPC-UA user password**: change in the web admin under Security > OPC-UA Users
4. **Keystore password**: change in the web admin under Security > Keystore Password

### 6.2 Use HTTPS

The admin panel auto-generates a self-signed certificate on port 3443. For production, you can mount your own certificate:

```yaml
# docker-compose.yml
plc4x-admin:
  volumes:
    - ./certs/cert.pem:/app/certs/cert.pem:ro
    - ./certs/key.pem:/app/certs/key.pem:ro
```

### 6.3 Network access

If the PLCs are on the host network (not reachable from Docker bridge):

```yaml
# docker-compose.yml
plc4x-server:
  network_mode: host
```

### 6.4 Firewall rules

| Allow | Port | From |
|-------|------|------|
| Web Admin | 3080/3443 | Admin workstations only |
| OPC-UA | 12687 | SCADA/HMI systems |

Block public access to the admin panel. Use a VPN or reverse proxy with authentication.

### 6.5 Resource limits

The default Docker Compose file already sets resource limits:

| Container | Memory Limit | CPU Limit |
|-----------|-------------|-----------|
| plc4x-admin | 512 MB | 0.5 cores |
| plc4x-server | 1 GB | 1.0 cores |
| plc4x-ml | 4 GB | — |
| influxdb | 512 MB | 0.5 cores |
| grafana | 256 MB | 0.5 cores |
| mosquitto | 64 MB | 0.25 cores |

Adjust in `docker-compose.yml` if needed. For large installations (50+ devices), increase the server memory:

```yaml
plc4x-server:
  mem_limit: 2g
  environment:
    - JVM_OPTS=-Xmx1g -Xms256m
```

### 6.6 Backup strategy

Configuration backups are created automatically on every save (max 50 kept). To back up the entire system:

```bash
# Backup config volume
docker run --rm -v plc4x-manager_config-data:/data -v $(pwd):/backup alpine \
  tar czf /backup/plc4x-config-backup.tar.gz -C /data .

# Backup security volume (certificates, users)
docker run --rm -v plc4x-manager_security-data:/data -v $(pwd):/backup alpine \
  tar czf /backup/plc4x-security-backup.tar.gz -C /data .
```

---

## 7. Updating

### 7.1 Update PLC4X version (no rebuild needed)

1. Go to the **OPC-UA Server** tab in the web admin
2. Enter the new version number (e.g., `0.14.0`)
3. Click **Apply Version**
4. Wait for the download and restart

The current version is backed up automatically. Click **Undo Last Change** to rollback.

### 7.2 Update PLC4X Manager itself

```bash
cd plc4x-manager
git pull
docker compose up -d --build
```

This preserves all your configuration and data (stored in Docker named volumes).

---

## 8. Troubleshooting

### Server won't start

```bash
docker logs plc4x-server --tail 30
```

Common causes:
- Invalid tag address (e.g., `holding-register:0` — must be >= 1)
- Invalid YAML in config.yml
- Port already in use

### Can't reach PLCs

- Check if Docker can reach the PLC network: `docker exec plc4x-server ping 192.168.1.20`
- If PLCs are on the host network, use `network_mode: host` (see section 6.3)
- Check PLC firewall allows connections on the required port

### OPC-UA client can't connect

- Verify the server is running: check the Dashboard
- Try security mode "None" first
- Check port 12687 is accessible: `telnet <server-ip> 12687`
- Check server logs for certificate errors

### Keystore password incorrect

The server logs show "keystore password was incorrect". Delete security files and re-bootstrap:

```bash
docker compose stop plc4x-server
docker run --rm -v plc4x-manager_security-data:/data alpine sh -c "rm -rf /data/security /data/pki"
docker compose start plc4x-server
```

> This resets all OPC-UA users and certificates. Default `admin/password` will be recreated.

### Container keeps restarting

The entrypoint retries 5 times with backoff, then waits for a config change. Check logs:

```bash
docker logs --tail 50 plc4x-server
```

| Log message | Cause | Solution |
|-------------|-------|----------|
| YAML parse error | Invalid config.yml | Restore from backup |
| address must be greater than zero | Modbus address 0 | Change to >= 1 |
| PlcInvalidTagException | Bad tag format | Fix tag address |
| NullPointerException on getTag | Unsupported tag for protocol | Disable device or fix tag |
| OutOfMemoryError | Too many devices | Increase JVM_OPTS memory |

### Reset everything

To start fresh (loses all configuration and certificates):

```bash
docker compose down -v
docker compose up -d --build
```

> **Warning**: This deletes all data including user accounts, certificates, and configuration backups.

---

## 9. Security

### Default Credentials

There are two separate sets of credentials:

| What | Username | Password | Where to change |
|------|----------|----------|----------------|
| Web Admin Panel (`:3080`) | `admin` | `admin` | `.env` file (`ADMIN_PASSWORD`) |
| OPC-UA Users (`:12687`) | `admin` | `password` | Security tab in web admin |

### Auto-Bootstrap

On first startup, PLC4X automatically creates:
- PKCS12 keystore with self-signed certificate (`plc4x-opcuaserver.pfx`)
- Password file with default OPC-UA user (`.jibberish`)
- PKI directory for client certificate management

### OPC-UA Security Policies

| Policy | Description |
|--------|-------------|
| None | No encryption (localhost only when `disableInsecureEndpoint: false`) |
| Basic256Sha256 | AES-256 encryption + SHA-256 signing (recommended for production) |

### Authentication Methods

| Method | Description |
|--------|-------------|
| Anonymous | No credentials required |
| Username/Password | Validated against `.jibberish` user database |
| X.509 Certificate | Client cert must be in `pki/trusted/` directory |

---

## 10. Data Persistence

All data is stored in Docker named volumes that survive restarts, rebuilds, and upgrades.

### Volumes

| Volume | Path | Contains |
|--------|------|----------|
| `config-data` | `/app/config` | config.yml, backups, SQLite database (`plc4x_manager.db`) |
| `security-data` | `/app/security` | keystore, users, PKI certificates |
| `influxdb-data` | (internal) | Time-series tag data (90-day raw retention by default) |
| `grafana-data` | (internal) | Grafana dashboards, plugins, sessions |
| `mosquitto-data` | (internal) | MQTT broker persistent data |

### SQLite Database

The admin panel stores structured data in a SQLite database at `/app/config/plc4x_manager.db`:

| Table | Description |
|-------|-------------|
| `audit_entries` | All write operations with user, timestamp, IP |
| `logbook_entries` | Operator shift logbook |
| `alarms` | Currently active alarms |
| `alarm_history` | Historical alarm events with duration |
| `write_log` | PLC write operations |

The database is backed up automatically every 4 hours (max 42 backups kept). The backup files are placed alongside the database in the `config-data` volume.

### What Survives

| Operation | Data preserved? |
|-----------|----------------|
| `docker compose restart` | Yes |
| `docker compose down` + `up` | Yes |
| `docker compose build` + `up` | Yes |
| `docker compose down -v` | **No** (deletes all volumes) |

### Manual Volume Backup

```bash
# Backup
docker run --rm -v plc4x-manager_config-data:/data -v $(pwd):/backup alpine \
  tar czf /backup/config-backup.tar.gz -C /data .
docker run --rm -v plc4x-manager_security-data:/data -v $(pwd):/backup alpine \
  tar czf /backup/security-backup.tar.gz -C /data .

# Restore
docker compose down
docker run --rm -v plc4x-manager_config-data:/data -v $(pwd):/backup alpine \
  tar xzf /backup/config-backup.tar.gz -C /data
docker run --rm -v plc4x-manager_security-data:/data -v $(pwd):/backup alpine \
  tar xzf /backup/security-backup.tar.gz -C /data
docker compose up -d
```

---

## 11. Environment Variables

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `PLC4X_VERSION` | `0.13.1` | PLC4X jar version |
| `OPCUA_PORT` | `12687` | OPC-UA server port |
| `ADMIN_PORT` | `3080` | Web admin HTTP port |
| `ADMIN_HTTPS_PORT` | `3443` | Web admin HTTPS port |
| `UVICORN_PORT` | `8080` | Set automatically by the entrypoint to distinguish the HTTP and HTTPS uvicorn instances (do not set manually) |
| `ADMIN_USERNAME` | `admin` | Web admin login username |
| `ADMIN_PASSWORD` | `admin` | Web admin login password |
| `JWT_SECRET` | (auto) | JWT signing key (auto-generated if empty) |
| `JWT_EXPIRY_HOURS` | `24` | Token expiry in hours |
| `API_KEY` | (empty) | Machine-to-machine API key |
| `PLC_READONLY` | `true` | **Global read-only mode** — blocks ALL writes to ALL PLCs (see below) |
| `JVM_OPTS` | `-Xmx512m -Xms128m` | JVM memory settings for PLC4X |
| `BACKUP_MAX_FILES` | `50` | Max config backup files to keep |

### PLC Write Protection (PLC_READONLY)

The system starts in **read-only mode by default** (`PLC_READONLY=true`). In this mode, all PLC write operations are blocked at both the server and admin layers — no values can be written to any PLC, regardless of user role or device `allowWrite` setting.

This is a **safety feature** for industrial environments:
- **Initial deployment:** test monitoring without risk of accidental PLC writes
- **Audit periods:** guarantee no process interference during audits
- **Maintenance windows:** lock writes while maintenance teams work on equipment
- **Permanent read-only:** some installations only monitor, never write (Rockwell handles operation)

**To enable PLC writes:**

1. Edit `.env` on the server:
   ```
   PLC_READONLY=false
   ```
2. Restart containers:
   ```bash
   docker compose up -d
   ```

**Important:**
- This variable is intentionally **not configurable from the UI** — it requires server access to change, preventing accidental activation of PLC writes by operators
- The setting applies to **both containers**: `plc4x-admin` (API) and `plc4x-server` (pylogix service)
- Even with `PLC_READONLY=false`, individual devices still need `allowWrite=true` in their device settings to accept writes
- The read-only status is visible in the `/healthz` endpoint: `"plcReadOnly": true`

**Write protection layers (defense in depth):**

| Layer | What it blocks | Configured where |
|-------|---------------|-----------------|
| `PLC_READONLY=true` (global) | ALL writes to ALL PLCs | `.env` file (server access required) |
| Device `allowWrite=false` (per device) | Writes to a specific device | UI → Devices → Edit Device |
| Role `monitor` (per user) | User cannot trigger writes | UI → Security → Users |
| Plant filter (per user) | User cannot write to devices outside their plant | `USERS_JSON` in `.env` |

### RBAC

| Variable | Default | Description |
|----------|---------|-------------|
| `OPERATOR_USERNAME` | `operator` | Operator role login username |
| `OPERATOR_PASSWORD` | `operator` | Operator role login password |
| `MONITOR_USERNAME` | `monitor` | Monitor (view-only) role login username |
| `MONITOR_PASSWORD` | `monitor` | Monitor role login password |
| `USERS_JSON` | (empty) | JSON array of additional users with role and optional plant filter — overrides individual OPERATOR_*/MONITOR_* vars |

### Monitoring Stack

| Variable | Default | Description |
|----------|---------|-------------|
| `GRAFANA_PORT` | `3000` | Grafana dashboards port |
| `GRAFANA_ADMIN_PASSWORD` | `admin` | Grafana login password |
| `INFLUXDB_PORT` | `8086` | InfluxDB port |
| `INFLUXDB_TOKEN` | `plc4x-token` | InfluxDB API token |
| `INFLUXDB_ORG` | `plc4x` | InfluxDB organization |
| `INFLUXDB_BUCKET` | `plc4x_raw` | InfluxDB primary bucket |
| `INFLUXDB_RETENTION_DAYS` | `90` | Raw data retention in days |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USERNAME` | `plc4x` | MQTT broker username |
| `MQTT_PASSWORD` | `plc4x` | MQTT broker password |

### ML Analytics

| Variable | Default | Description |
|----------|---------|-------------|
| `ML_INTERVAL_MINUTES` | `5` | How often the ML cycle runs (minutes) |
| `ML_FORECAST_HOURS` | `2` | Prophet forecast horizon (hours ahead) |
| `ML_MIN_POINTS` | `100` | Minimum data points required to run ML on a tag |

---

## 12. RBAC Setup

PLC4X Manager supports three roles: **admin**, **operator**, and **monitor**.

### Roles

| Role | Capabilities |
|------|-------------|
| **admin** | Full access: configure devices, tags, security, manage backups, update PLC4X version, change passwords, view audit trail |
| **operator** | View all data + write PLC tag values + add logbook entries + acknowledge alarms; cannot change configuration |
| **monitor** | View only + acknowledge alarms + add logbook entries; no PLC writes, no configuration changes |

### Environment Variables

Set these in your `.env` file:

```env
# Operator credentials
OPERATOR_USERNAME=operator
OPERATOR_PASSWORD=your-operator-password

# Monitor credentials
MONITOR_USERNAME=monitor
MONITOR_PASSWORD=your-monitor-password

# Advanced: define multiple users as JSON (optional)
# Each user can have an optional "plants" list to restrict access to specific plants.
# USERS_JSON=[{"username":"alice","password":"pw1","role":"operator"},{"username":"bob","password":"pw2","role":"monitor","plants":["PlantA"]}]
```

> **Tip**: Change all default passwords before deploying to production.

### What Each Role Can Do

| Feature | Admin | Operator | Monitor |
|---------|-------|----------|---------|
| View dashboard & live data | Yes | Yes | Yes |
| Acknowledge alarms | Yes | Yes | Yes |
| Add logbook entries | Yes | Yes | Yes |
| Use HMI screens (read) | Yes | Yes | Yes |
| Write PLC values from HMI | Yes | Yes | No |
| Add/edit/delete devices & tags | Yes | No | No |
| Configure alarm thresholds | Yes | No | No |
| Manage backups | Yes | No | No |
| Update PLC4X version | Yes | No | No |
| Change admin password | Yes | No | No |
| View audit trail | Yes | No | No |
| Manage OPC-UA users & security | Yes | No | No |

### Plant-Based Access Filter

You can restrict a user to see only specific plants by adding a `plants` list to their entry in `USERS_JSON`. Users without a `plants` restriction see all plants.

---

## 13. ML Analytics Configuration

The `plc4x-ml` container starts automatically with the stack and runs 7 ML modules on every configured interval.

### Default Behavior

- Runs every **5 minutes** (`ML_INTERVAL_MINUTES`)
- Analyzes tags with at least **100 data points** (`ML_MIN_POINTS`)
- Forecasts **2 hours** ahead (`ML_FORECAST_HOURS`)
- Processes up to **50 tags per cycle**

### Fine-Tuning via config.yml

The ML modules can be enabled/disabled and tuned in the `mlConfig` section of the device configuration (editable from the web admin Config tab):

```yaml
mlConfig:
  enabled: true
  cycleIntervalMinutes: 5
  forecastHours: 2
  minPoints: 100
  anomaly:
    enabled: true
    contamination: 0.05   # expected fraction of anomalies (0–0.5)
    minAgreement: 2       # how many algorithms must agree to flag anomaly
  explainability:
    enabled: true
    topContributors: 5
  correlation:
    enabled: true
    baselineHours: 6
    recentMinutes: 30
    breakThreshold: 0.4   # delta in correlation to trigger a broken-correlation alert
  changepoint:
    enabled: true
    minSegmentSize: 60    # data points per segment
    penalty: 10.0
  pattern:
    enabled: true
    windowSize: 60        # matrix profile window size
    topK: 3              # top motifs/discords to report
```

### Manual Run

Click **Run ML Now** in the web admin (ML tab or OPC-UA Server tab) to trigger an immediate ML cycle without waiting for the next scheduled interval.

### Viewing Results

Results are written to InfluxDB and visualized in Grafana:

| Grafana Dashboard | ML Data |
|------------------|---------|
| AI Predictions | Prophet forecast with confidence band, trend rate |
| Alarms & Anomalies | Anomaly scores, change points, broken correlations |

---

## 14. Kiosk Mode


Kiosk mode turns any browser into a hands-free monitoring display, cycling through screens automatically — ideal for a TV on the factory floor or monitoring room.

### Setup

1. Open a browser on the display machine and navigate to:
   ```
   http://<server-ip>:3080?kiosk
   ```
2. Press **F11** (or browser fullscreen shortcut) to go fullscreen.
3. The browser will cycle through Dashboard, HMI screens, and OEE every **30 seconds** automatically.

### Behavior

- No navigation bar or header controls are shown.
- The cycle order is: Dashboard → HMI screens (all configured) → OEE dashboard → repeat.
- The display logs in automatically using a read-only session (operator role).

### Exit

Press **ESC** to exit kiosk mode and return to the normal interface.

### Recommended Setup for a Permanent Display

1. Use a small PC or Raspberry Pi attached to the TV.
2. Configure the browser to launch in fullscreen on boot pointing to `http://server:3080?kiosk`.
3. Disable the screen saver and power-off timer on the OS.
4. Use a dedicated operator account so the TV display does not block admin access.

---

## 15. Alarm Configuration

Alarms are configured per tag and fire when a value crosses a defined threshold.

### Setting Thresholds

1. Go to the **Devices** tab.
2. Click on a device to expand it, then click on a tag.
3. In the tag detail panel, open the **Alarms** section.
4. Set one or more thresholds:

| Field | Description |
|-------|-------------|
| High High (HH) | Critical high limit — severity: critical |
| High (H) | Warning high limit — severity: warning |
| Low (L) | Warning low limit — severity: warning |
| Low Low (LL) | Critical low limit — severity: critical |
| Deadband | Hysteresis value to prevent alarm chatter |

5. Click **Save Alarms**.

### Conditional Profiles (per product)

Alarm thresholds can change automatically based on the current product being produced:

1. Create a **Virtual Tag** on the device (e.g., `CurrentProduct`) and set its value to the current product code (e.g., `ProductA`).
2. In the tag's alarm configuration, add **Conditional Profiles**.
3. Each profile defines a product code and a set of thresholds that apply when that product is active.
4. When `CurrentProduct` changes, the alarm engine switches to the matching profile automatically.

### Virtual Tags for Product Codes

Virtual tags are manually-set values with no PLC address. They flow through the full pipeline (MQTT, InfluxDB, alarms):

1. Click **+ Add Virtual Tag** on a device.
2. Set the alias (e.g., `CurrentProduct`) and an initial value.
3. Change the value at any time from the Live Data tab or via the API (`POST /api/live/write`).

### Sound Notification

When a new alarm fires, the browser plays an alert sound. To control this:

- The sound plays only when the tab is active and the browser allows audio autoplay.
- Click the bell icon in the header to mute/unmute alarm sounds.
- Acknowledged alarms do not re-trigger sound unless they fire again.

### Acknowledge Alarms

- From the **Alarms** tab: click **Acknowledge** next to an individual alarm, or **Acknowledge All**.
- All acknowledgements are recorded in the audit trail with the user, timestamp, and IP address.

---

## 16. OEE Configuration

OEE (Overall Equipment Effectiveness) = Availability × Performance × Quality.

### Configure a Device for OEE

1. Go to the **Devices** tab and open a device.
2. Click **OEE Configuration**.
3. Map the following fields to tags on the device:

| Field | Tag type | Description |
|-------|----------|-------------|
| Running tag | Boolean | Tag that is `true` when the machine is running |
| Count tag | Integer/Float | Tag that counts produced units (increments each cycle) |
| Reject tag | Integer/Float | Tag that counts rejected/defective units |
| Ideal cycle time | Seconds | Target time per unit (e.g., 30 seconds for 2 parts/min) |
| Planned hours | Hours/day | Scheduled production time per day (e.g., 8 for one shift) |

4. Click **Save OEE Config**.

### Reading OEE Values

- The **OEE** tab shows live gauges for Availability, Performance, Quality, and the combined OEE score.
- The trend chart shows OEE over the last 7 days (or a selected period).
- Use the API to query calculated OEE: `GET /api/oee/calculate?device=<name>&period=shift`

### OEE Periods

| Period | Description |
|--------|-------------|
| `shift` | Current shift (based on shift start time) |
| `day` | Current calendar day |
| `week` | Current ISO week |
| `custom` | Specify `start` and `end` query parameters (ISO 8601) |

---

## Quick Reference

| What | Where |
|------|-------|
| Web Admin | http://localhost:3080 (`admin` / `admin`) |
| Grafana Dashboards | http://localhost:3000 (`admin` / `admin`) |
| OPC-UA Endpoint | opc.tcp://localhost:12687/plc4x |
| InfluxDB | http://localhost:8086 |
| MQTT Broker | mqtt://localhost:1883 |
| HMI Screens | Web Admin > HMI tab, or `?hmi=equip-id` for fullscreen |
| Default OPC-UA login | `admin` / `password` (change in Security tab) |
| API Docs (Swagger) | Web Admin > API tab, or `/static/swagger.json` |
| Postman Collection | `docs/PLC4X-Manager.postman_collection.json` |
| API Reference | `docs/API.md` |
| GitHub | https://github.com/silviogarbes/plc4x-manager |

### Grafana Dashboards

| Dashboard | Description |
|-----------|-------------|
| Overview | All devices status, tag values over time, health metrics, active alerts |
| Plant Overview | All plants at a glance, online/error per plant, latency |
| Device Detail | Select plant/device, tag graphs, current values, history |
| Energy Monitor | Voltage/current/power gauges, trends, power factor |
| AI Predictions | Prophet forecast with confidence band, anomaly score, trend rate |
| Alarms & Anomalies | Active anomalies, anomaly events, trend rates, device health history |
| Custom Data | Quality checks, vision AI, manual readings, production counters |
| System Health | MQTT live stream vs InfluxDB comparison, write rate, bucket sizes |
