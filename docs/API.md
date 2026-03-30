# PLC4X Manager - API Reference

Base URL: `http://localhost:3080`

All request and response bodies use JSON (`Content-Type: application/json`).

**Interactive docs:** Open the **API** tab in the web admin for Swagger UI with "Try it out". OpenAPI spec available at `/static/swagger.json`. Postman collection at `docs/PLC4X-Manager.postman_collection.json`.

## Authentication

All `/api/*` endpoints (except `/api/auth/login`) require a JWT token in the `Authorization` header:

```
Authorization: Bearer <token>
```

### Login

```
POST /api/auth/login
```

**Request Body**:

```json
{"username": "admin", "password": "admin"}
```

**Response** `200 OK`:

```json
{
  "token": "eyJhbG...",
  "username": "admin",
  "expiresIn": 86400
}
```

Brute-force protection: after 5 failed attempts, the account is locked for 5 minutes.

---

## Configuration

### Get Full Configuration

Returns the complete `config.yml` as JSON.

```
GET /api/config
```

**Response** `200 OK`:

```json
{
  "version": "0.8",
  "dir": "/app/security",
  "name": "Plc4xOPCUAServer",
  "tcpPort": 12687,
  "disableInsecureEndpoint": false,
  "devices": [
    {
      "name": "Demo-Simulated",
      "connectionString": "simulated://127.0.0.1",
      "tags": [
        {"alias": "RandomInteger", "address": "RANDOM/Temporary:DINT"}
      ]
    }
  ]
}
```

**curl**:

```bash
curl http://localhost:3080/api/config
```

---

### Save Full Configuration

Replaces the entire configuration. A backup of the previous configuration is created automatically.

```
PUT /api/config
```

**Request Body**: Complete configuration object (same format as GET response).

**Response** `200 OK`:

```json
{"message": "Configuration saved successfully"}
```

**Error** `400 Bad Request`:

```json
{"error": "Empty payload"}
```

**curl**:

```bash
curl -X PUT http://localhost:3080/api/config \
  -H "Content-Type: application/json" \
  -d '{
    "version": "0.8",
    "dir": "/app/security",
    "name": "Plc4xOPCUAServer",
    "tcpPort": 12687,
    "disableInsecureEndpoint": false,
    "devices": []
  }'
```

---

### Update Server Settings

Updates only the OPC-UA server settings (not devices/tags). Accepts any subset of the server fields.

```
PUT /api/config/server
```

**Request Body** (all fields optional):

```json
{
  "version": "0.8",
  "name": "Plc4xOPCUAServer",
  "tcpPort": 12687,
  "disableInsecureEndpoint": true,
  "dir": "/app/security"
}
```

**Response** `200 OK`:

```json
{"message": "Server configuration updated"}
```

**curl**:

```bash
curl -X PUT http://localhost:3080/api/config/server \
  -H "Content-Type: application/json" \
  -d '{"tcpPort": 12687, "disableInsecureEndpoint": true}'
```

---

## Devices

### List All Devices

```
GET /api/devices
```

**Response** `200 OK`:

```json
[
  {
    "name": "Demo-Simulated",
    "connectionString": "simulated://127.0.0.1",
    "tags": [
      {"alias": "RandomInteger", "address": "RANDOM/Temporary:DINT"}
    ]
  }
]
```

**curl**:

```bash
curl http://localhost:3080/api/devices
```

---

### Add a Device

```
POST /api/devices
```

**Request Body**:

```json
{
  "name": "MyPLC",
  "connectionString": "s7://192.168.1.10",
  "tags": [
    {"alias": "Temperature", "address": "%DB1:0:REAL"}
  ]
}
```

The `tags` field is optional and defaults to an empty list.

**Response** `201 Created`:

```json
{"message": "Device 'MyPLC' added"}
```

**Error** `400 Bad Request`:

```json
{"error": "Fields 'name' and 'connectionString' are required"}
```

**Error** `409 Conflict`:

```json
{"error": "Device 'MyPLC' already exists"}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/devices \
  -H "Content-Type: application/json" \
  -d '{"name": "MyPLC", "connectionString": "s7://192.168.1.10"}'
```

---

### Update a Device

Replaces the entire device configuration.

```
PUT /api/devices/<name>
```

**URL Parameters**: `name` - current device name.

**Request Body**: Complete device object.

```json
{
  "name": "MyPLC",
  "connectionString": "s7://192.168.1.10",
  "tags": [
    {"alias": "Temperature", "address": "%DB1:0:REAL"},
    {"alias": "Pressure", "address": "%DB1:4:REAL"}
  ]
}
```

**Response** `200 OK`:

```json
{"message": "Device 'MyPLC' updated"}
```

**Error** `404 Not Found`:

```json
{"error": "Device 'MyPLC' not found"}
```

**curl**:

```bash
curl -X PUT http://localhost:3080/api/devices/MyPLC \
  -H "Content-Type: application/json" \
  -d '{"name": "MyPLC", "connectionString": "s7://192.168.1.10", "tags": []}'
```

---

### Delete a Device

```
DELETE /api/devices/<name>
```

**URL Parameters**: `name` - device name to delete.

**Response** `200 OK`:

```json
{"message": "Device 'MyPLC' removed"}
```

**Error** `404 Not Found`:

```json
{"error": "Device 'MyPLC' not found"}
```

**curl**:

```bash
curl -X DELETE http://localhost:3080/api/devices/MyPLC
```

---

## Tags

### List Tags for a Device

```
GET /api/devices/<device_name>/tags
```

**URL Parameters**: `device_name` - name of the parent device.

**Response** `200 OK`:

```json
[
  {"alias": "Temperature", "address": "%DB1:0:REAL"},
  {"alias": "Counter", "address": "%DB1:4:DINT"}
]
```

**Error** `404 Not Found`:

```json
{"error": "Device 'MyPLC' not found"}
```

**curl**:

```bash
curl http://localhost:3080/api/devices/MyPLC/tags
```

---

### Add a Tag to a Device

```
POST /api/devices/<device_name>/tags
```

**URL Parameters**: `device_name` - name of the parent device.

**Request Body**:

```json
{"alias": "Temperature", "address": "%DB1:0:REAL"}
```

**Response** `201 Created`:

```json
{"message": "Tag 'Temperature' added"}
```

**Error** `400 Bad Request`:

```json
{"error": "Fields 'alias' and 'address' are required"}
```

**Error** `404 Not Found`:

```json
{"error": "Device 'MyPLC' not found"}
```

**Error** `409 Conflict`:

```json
{"error": "Tag 'Temperature' already exists"}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/devices/MyPLC/tags \
  -H "Content-Type: application/json" \
  -d '{"alias": "Temperature", "address": "%DB1:0:REAL"}'
```

---

### Delete a Tag from a Device

```
DELETE /api/devices/<device_name>/tags/<alias>
```

**URL Parameters**:
- `device_name` - name of the parent device.
- `alias` - tag alias to delete.

**Response** `200 OK`:

```json
{"message": "Tag 'Temperature' removed"}
```

**Error** `404 Not Found`:

```json
{"error": "Tag 'Temperature' not found"}
```

```json
{"error": "Device 'MyPLC' not found"}
```

**curl**:

```bash
curl -X DELETE http://localhost:3080/api/devices/MyPLC/tags/Temperature
```

---

## Users

### List All Users

Returns all OPC-UA users. Password hashes are not included in the response.

```
GET /api/users
```

**Response** `200 OK`:

```json
[
  {
    "username": "admin",
    "security": "admin-group",
    "hasPassword": true
  }
]
```

**curl**:

```bash
curl http://localhost:3080/api/users
```

---

### Add a User

```
POST /api/users
```

**Request Body**:

```json
{
  "username": "operator1",
  "password": "secure-password",
  "security": "admin-group"
}
```

The `security` field is optional and defaults to `"admin-group"`.

**Response** `201 Created`:

```json
{"message": "User 'operator1' added"}
```

**Error** `400 Bad Request`:

```json
{"error": "Fields 'username' and 'password' are required"}
```

```json
{"error": "Username cannot be empty"}
```

```json
{"error": "Password cannot be empty"}
```

**Error** `409 Conflict`:

```json
{"error": "User 'operator1' already exists"}
```

**Error** `503 Service Unavailable`:

```json
{"error": "Security not initialized. Start the server first."}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/users \
  -H "Content-Type: application/json" \
  -d '{"username": "operator1", "password": "secure-password", "security": "admin-group"}'
```

---

### Update a User

Updates the password and/or security group for an existing user. Both fields are optional.

```
PUT /api/users/<username>
```

**URL Parameters**: `username` - user to update.

**Request Body** (all fields optional):

```json
{
  "password": "new-password",
  "security": "operator-group"
}
```

**Response** `200 OK`:

```json
{"message": "User 'operator1' updated"}
```

**Error** `404 Not Found`:

```json
{"error": "User 'operator1' not found"}
```

**curl**:

```bash
curl -X PUT http://localhost:3080/api/users/operator1 \
  -H "Content-Type: application/json" \
  -d '{"password": "new-secure-password"}'
```

---

### Delete a User

```
DELETE /api/users/<username>
```

**URL Parameters**: `username` - user to delete.

**Response** `200 OK`:

```json
{"message": "User 'operator1' removed"}
```

**Error** `404 Not Found`:

```json
{"error": "User 'operator1' not found"}
```

**curl**:

```bash
curl -X DELETE http://localhost:3080/api/users/operator1
```

---

## Security

### Get Security Status

Returns whether security has been initialized (bootstrap completed).

```
GET /api/security/status
```

**Response** `200 OK`:

```json
{
  "initialized": true,
  "passwordFile": true,
  "keystore": true,
  "pkiDirectory": true,
  "keystoreSize": 2574,
  "keystoreModified": 1711234567.89
}
```

When not initialized:

```json
{
  "initialized": false,
  "passwordFile": false,
  "keystore": false,
  "pkiDirectory": false
}
```

**curl**:

```bash
curl http://localhost:3080/api/security/status
```

---

### Change Keystore Password

Updates the keystore security password in the `.jibberish` file. A server restart is required for the change to take effect.

```
PUT /api/security/password
```

**Request Body**:

```json
{"password": "new-keystore-password"}
```

**Response** `200 OK`:

```json
{"message": "Security password updated. Restart the server to apply."}
```

**Error** `400 Bad Request`:

```json
{"error": "Field 'password' is required"}
```

```json
{"error": "Password cannot be empty"}
```

**Error** `503 Service Unavailable`:

```json
{"error": "Security not initialized. Start the server first."}
```

**curl**:

```bash
curl -X PUT http://localhost:3080/api/security/password \
  -H "Content-Type: application/json" \
  -d '{"password": "new-keystore-password"}'
```

---

### List Trusted Certificates

```
GET /api/security/certificates/trusted
```

**Response** `200 OK`:

```json
[
  {
    "filename": "client-cert.der",
    "size": 1234,
    "modified": 1711234567.89
  }
]
```

**curl**:

```bash
curl http://localhost:3080/api/security/certificates/trusted
```

---

### List Rejected Certificates

```
GET /api/security/certificates/rejected
```

**Response** `200 OK`:

```json
[
  {
    "filename": "unknown-client.der",
    "size": 1234,
    "modified": 1711234567.89
  }
]
```

**curl**:

```bash
curl http://localhost:3080/api/security/certificates/rejected
```

---

### Trust a Certificate

Moves a certificate from the `rejected` directory to `trusted`.

```
POST /api/security/certificates/trust/<filename>
```

**URL Parameters**: `filename` - certificate file name.

**Response** `200 OK`:

```json
{"message": "Certificate 'client-cert.der' moved to trusted"}
```

**Error** `404 Not Found`:

```json
{"error": "Certificate 'client-cert.der' not found in rejected"}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/security/certificates/trust/client-cert.der
```

---

### Reject a Certificate

Moves a certificate from the `trusted` directory to `rejected`.

```
POST /api/security/certificates/reject/<filename>
```

**URL Parameters**: `filename` - certificate file name.

**Response** `200 OK`:

```json
{"message": "Certificate 'client-cert.der' moved to rejected"}
```

**Error** `404 Not Found`:

```json
{"error": "Certificate 'client-cert.der' not found in trusted"}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/security/certificates/reject/client-cert.der
```

---

### Delete a Certificate

Deletes a certificate from either `trusted` or `rejected`.

```
DELETE /api/security/certificates/<filename>
```

**URL Parameters**: `filename` - certificate file name.

**Response** `200 OK`:

```json
{"message": "Certificate 'client-cert.der' deleted from trusted"}
```

**Error** `404 Not Found`:

```json
{"error": "Certificate 'client-cert.der' not found"}
```

**curl**:

```bash
curl -X DELETE http://localhost:3080/api/security/certificates/client-cert.der
```

---

## Server Control

### Get Server Status

Returns the current status of the PLC4X server Docker container.

```
GET /api/server/status
```

**Response** `200 OK` (running):

```json
{
  "status": "running",
  "running": true,
  "id": "abc123",
  "image": "plc4x-manager-plc4x-server:latest"
}
```

**Response** `200 OK` (not found):

```json
{
  "status": "not_found",
  "running": false,
  "id": null,
  "image": null
}
```

**Response** `200 OK` (error):

```json
{
  "status": "error",
  "running": false,
  "id": null,
  "image": null,
  "error": "error message"
}
```

**curl**:

```bash
curl http://localhost:3080/api/server/status
```

---

### Restart Server

Restarts the PLC4X server container with a 10-second timeout.

```
POST /api/server/restart
```

**Response** `200 OK`:

```json
{"message": "Server restarted successfully"}
```

**Error** `404 Not Found`:

```json
{"error": "Server container not found"}
```

**Error** `500 Internal Server Error`:

```json
{"error": "error message"}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/server/restart
```

---

### Get Server Logs

Returns the last N lines of the server container logs with timestamps.

```
GET /api/server/logs?lines=<count>
```

**Query Parameters**:
- `lines` (optional, integer, default: `50`) - Number of log lines to return.

**Response** `200 OK`:

```json
{
  "logs": "2024-01-15T10:30:00.000Z [PLC4X Server] Starting OPC-UA server...\n..."
}
```

**Error** `404 Not Found`:

```json
{"error": "Container not found"}
```

**curl**:

```bash
curl "http://localhost:3080/api/server/logs?lines=100"
```

---

## Backups

### List Backups

Returns a list of available configuration backup files, sorted newest first.

```
GET /api/backups
```

**Response** `200 OK`:

```json
[
  "config_20240115_103000.yml",
  "config_20240115_102500.yml",
  "config_20240114_180000.yml"
]
```

**curl**:

```bash
curl http://localhost:3080/api/backups
```

---

### Restore a Backup

Restores a previously saved configuration backup. The current configuration is automatically backed up before the restore.

```
POST /api/backups/<filename>/restore
```

**URL Parameters**: `filename` - backup file name.

**Response** `200 OK`:

```json
{"message": "Backup 'config_20240115_103000.yml' restored"}
```

**Error** `404 Not Found`:

```json
{"error": "Backup not found"}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/backups/config_20240115_103000.yml/restore
```

---

### Cleanup Backups

Manually triggers backup cleanup. Removes old backups, keeping only the most recent `BACKUP_MAX_FILES` (default: 50).

```
POST /api/backups/cleanup
```

**Response** `200 OK`:

```json
{
  "message": "Cleanup complete. Keeping last 50 backups.",
  "remaining": 42
}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/backups/cleanup
```

---

## Templates

### Get Connection Templates

Returns pre-filled connection string templates for all 11 supported PLC4X protocols, including tag examples.

```
GET /api/templates
```

**Response** `200 OK`:

```json
[
  {
    "protocol": "Siemens S7",
    "template": "s7://{{IP}}",
    "example": "s7://192.168.1.10",
    "tagExamples": [
      {"alias": "Temperature", "address": "%DB1:0:REAL"},
      {"alias": "Counter", "address": "%DB1:4:DINT"},
      {"alias": "MotorOn", "address": "%DB1:8.0:BOOL"},
      {"alias": "Speed", "address": "%DB1:10:INT"}
    ]
  },
  {
    "protocol": "Modbus TCP",
    "template": "modbus-tcp://{{IP}}:502",
    "example": "modbus-tcp://192.168.1.20:502",
    "tagExamples": [
      {"alias": "HoldingReg1", "address": "holding-register:1"},
      {"alias": "Coil1", "address": "coil:1"},
      {"alias": "InputReg1", "address": "input-register:1"},
      {"alias": "DiscreteInput1", "address": "discrete-input:1"}
    ]
  }
]
```

The full response includes all 11 protocols: Siemens S7, Modbus TCP, Modbus RTU, Modbus ASCII, OPC-UA (Client), EtherNet/IP, Allen-Bradley Logix, KNXnet/IP, IEC 60870-5-104, Firmata (Arduino), and Simulated (Test).

**curl**:

```bash
curl http://localhost:3080/api/templates
```

---

## Live Data

### Read All Tags

Connects to the OPC-UA server and reads current values for all tags across all devices.

```
GET /api/live/read
```

**Response** `200 OK`:

```json
{
  "server": "opc.tcp://plc4x-server:12687/plc4x",
  "devices": [
    {
      "name": "Demo-Simulated",
      "connectionString": "simulated://127.0.0.1",
      "status": "online",
      "enabled": true,
      "tags": [
        {"alias": "RandomInteger", "address": "RANDOM/Temporary:DINT", "value": 42, "status": "ok", "timestamp": "2026-03-28T12:00:00+00:00"},
        {"alias": "BadTag", "address": "INVALID", "value": null, "status": "read_error", "timestamp": null}
      ]
    }
  ],
  "error": null
}
```

Device status: `online`, `error`, `disabled`, `no_tags`. Tag status: `ok`, `read_error`, `disabled`.

**curl**:

```bash
curl http://localhost:3080/api/live/read \
  -H "Authorization: Bearer <token>"
```

---

### Read Tags for a Specific Device

Reads only the tags for a single device. Much faster than reading all devices.

```
GET /api/live/read/<device_name>
```

Alternative: `GET /api/live/read?device=<device_name>`

**URL Parameters**: `device_name` - name of the device to read.

**Response** `200 OK`: Same format as Read All Tags, but `devices` array contains only the requested device.

**Error** `200 OK` (device not found):

```json
{"devices": [], "error": "Device 'FakeDevice' not found", "server": "opc.tcp://..."}
```

**curl**:

```bash
curl http://localhost:3080/api/live/read/Demo-Simulated \
  -H "Authorization: Bearer <token>"
```

---

### Write a Tag Value

Writes a value to a PLC tag via the OPC-UA server. The value type is automatically cast to match the tag's data type.

```
POST /api/live/write
```

**Request Body**:

```json
{"device": "ModbusMeter", "tag": "SetPoint", "value": 75.5}
```

**Response** `200 OK`:

```json
{"message": "Value 75.5 written to ModbusMeter/SetPoint"}
```

**Error** `400 Bad Request`:

```json
{"error": "Write rejected: The node does not support writing."}
```

Supported value types: integers, floats, booleans (`true`/`false`), strings. The value is automatically cast to match the OPC-UA node's data type (Int32, Float, Boolean, etc.).

> **Note:** Write support depends on the PLC protocol and the tag configuration. Simulated devices are read-only. Real PLCs (Modbus, S7, Logix) support both read and write.

**curl**:

```bash
curl -X POST http://localhost:3080/api/live/write \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"device": "ModbusMeter", "tag": "SetPoint", "value": 75.5}'
```

---

## PLC4X Version Management

### Get Current Version

Returns the current PLC4X jar version and backup version (if available for rollback).

```
GET /api/plc4x/version
```

**Response** `200 OK`:

```json
{
  "currentVersion": "0.13.1",
  "backupVersion": "0.13.0",
  "container": "abc123"
}
```

The `backupVersion` field is `null` when no backup exists (no previous update/rollback has been performed).

**curl**:

```bash
curl http://localhost:3080/api/plc4x/version \
  -H "Authorization: Bearer <token>"
```

---

### Check Latest Version

Checks Maven Central for the latest available PLC4X OPC-UA Server version.

```
GET /api/plc4x/latest-version
```

**Response** `200 OK`:

```json
{
  "latestVersion": "0.13.1",
  "versions": ["0.12.0", "0.13.0", "0.13.1"]
}
```

**curl**:

```bash
curl http://localhost:3080/api/plc4x/latest-version \
  -H "Authorization: Bearer <token>"
```

---

### Update PLC4X Version

Downloads a new PLC4X jar from Maven Central and restarts the server. The current jar and version are backed up automatically for rollback.

```
POST /api/plc4x/update
```

**Request Body**:

```json
{"version": "0.13.0"}
```

**Response** `200 OK`:

```json
{
  "message": "PLC4X updated to version 0.13.0. Server restarting...",
  "version": "0.13.0",
  "jarUrl": "https://repo1.maven.org/maven2/org/apache/plc4x/plc4x-opcua-server/0.13.0/plc4x-opcua-server-0.13.0.jar"
}
```

**Error** `404 Not Found`:

```json
{"error": "Version 0.99.0 not found on Maven Central. Check https://repo1.maven.org/maven2/org/apache/plc4x/plc4x-opcua-server/"}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/plc4x/update \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"version": "0.13.0"}'
```

---

### Rollback PLC4X Version

Swaps the current jar with the backup from the previous update. The server restarts after the rollback.

```
POST /api/plc4x/rollback
```

**Response** `200 OK`:

```json
{"message": "Rolled back to version 0.13.1. Server restarting..."}
```

**Error** `404 Not Found`:

```json
{"error": "No backup jar found. Cannot rollback."}
```

**curl**:

```bash
curl -X POST http://localhost:3080/api/plc4x/rollback \
  -H "Authorization: Bearer <token>"
```

---

## Summary Table

| Method | Endpoint | Description | Status Codes |
|--------|----------|-------------|-------------|
| GET | `/api/config` | Get full configuration | 200 |
| PUT | `/api/config` | Save full configuration | 200, 400 |
| PUT | `/api/config/server` | Update server settings | 200 |
| GET | `/api/devices` | List all devices | 200 |
| POST | `/api/devices` | Add a device | 201, 400, 409 |
| PUT | `/api/devices/<name>` | Update a device | 200, 404 |
| DELETE | `/api/devices/<name>` | Delete a device | 200, 404 |
| GET | `/api/devices/<name>/tags` | List tags for a device | 200, 404 |
| POST | `/api/devices/<name>/tags` | Add a tag to a device | 201, 400, 404, 409 |
| DELETE | `/api/devices/<name>/tags/<alias>` | Delete a tag | 200, 404 |
| GET | `/api/users` | List all users | 200 |
| POST | `/api/users` | Add a user | 201, 400, 409, 503 |
| PUT | `/api/users/<username>` | Update a user | 200, 404 |
| DELETE | `/api/users/<username>` | Delete a user | 200, 404 |
| GET | `/api/security/status` | Security initialization status | 200 |
| PUT | `/api/security/password` | Change keystore password | 200, 400, 503 |
| GET | `/api/security/certificates/trusted` | List trusted certificates | 200 |
| GET | `/api/security/certificates/rejected` | List rejected certificates | 200 |
| POST | `/api/security/certificates/trust/<file>` | Trust a certificate | 200, 404 |
| POST | `/api/security/certificates/reject/<file>` | Reject a certificate | 200, 404 |
| DELETE | `/api/security/certificates/<file>` | Delete a certificate | 200, 404 |
| GET | `/api/server/status` | Get server status | 200 |
| POST | `/api/server/restart` | Restart the server | 200, 404, 500 |
| GET | `/api/server/logs?lines=N` | Get server logs | 200, 404, 500 |
| GET | `/api/backups` | List backups | 200 |
| POST | `/api/backups/<file>/restore` | Restore a backup | 200, 404 |
| POST | `/api/backups/cleanup` | Cleanup old backups | 200 |
| GET | `/api/templates` | Get connection templates | 200 |
| GET | `/api/live/read` | Read all live tag values | 200 |
| GET | `/api/live/read/<device>` | Read tags for a specific device | 200 |
| POST | `/api/live/write` | Write a value to a PLC tag | 200, 400, 404, 504 |
| GET | `/api/plc4x/version` | Get current and backup version | 200 |
| GET | `/api/plc4x/latest-version` | Check latest version on Maven | 200, 502 |
| POST | `/api/plc4x/update` | Update PLC4X jar | 200, 400, 404, 500 |
| POST | `/api/plc4x/rollback` | Rollback to previous version | 200, 404, 500 |
| POST | `/api/auth/login` | Login and get JWT token | 200, 401, 429 |
| POST | `/api/demo/load` | Load demo devices | 200 |
| POST | `/api/backups/create` | Create manual backup | 200 |
| POST | `/api/backups/upload` | Restore from uploaded file | 200, 400 |
| GET | `/api/backups/<file>/changes` | Get backup change summary | 200, 404 |
| GET | `/api/backups/<file>/download` | Download backup file | 200, 404 |
| GET | `/api/backups/<file>/diff` | View diff vs current config | 200, 404 |
