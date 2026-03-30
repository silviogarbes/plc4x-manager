# PLC4X Manager - Testing Documentation

## Prerequisites

Before running the test suite, ensure the following conditions are met:

1. **Docker is running** - The Docker daemon must be active on the host machine.
2. **Containers are up** - Both `plc4x-server` and `plc4x-manager` containers must be running:
   ```bash
   docker ps | grep -E "plc4x-server|plc4x-manager"
   ```
3. **PLC4X Manager is accessible** - The web interface must be reachable at `http://localhost:3080`.
4. **curl is installed** - The test scripts use `curl` for all HTTP requests.
5. **bash is available** - Tests are written as bash scripts.

## How to Run Tests

### Main Test Suite

From the project root directory:

```bash
bash tests/test_all.sh
```

### Persistence Test Suite

To specifically validate data persistence across container restarts:

```bash
bash tests/test_persistence.sh
```

**Note:** The persistence test restarts the `plc4x-server` container and waits for it to come back online. It takes approximately 60-90 seconds to complete.

## Test Categories

The main test suite (`test_all.sh`) is organized into 18 categories:

| # | Category | Description |
|---|----------|-------------|
| 1 | **WEB UI** | Validates the HTML page loads correctly, contains all expected tabs (Devices, Server, Security, Logs, Backups), modals (User, Device), endpoint preview, config version field, and that static assets (CSS, JS) are served. Also verifies key JavaScript functions exist. |
| 2 | **SERVER STATUS** | Checks that the server status API returns running state, container ID, and image information. |
| 3 | **CONFIGURATION - GET (all fields)** | Validates the configuration API returns all expected fields: version, dir, name, tcpPort, disableInsecureEndpoint, and devices. Checks default values (version 0.8, tcpPort 12687). |
| 4 | **CONFIGURATION - UPDATE ALL SERVER SETTINGS** | Tests updating server settings (name, tcpPort, version, disableInsecureEndpoint, dir) via PUT request, verifies changes took effect, then restores original settings. |
| 5 | **CONFIGURATION - SAVE FULL CONFIG** | Tests saving the full configuration object via PUT, and validates that sending an empty body returns HTTP 400. |
| 6 | **DEVICES - CRUD** | Complete Create/Read/Update/Delete lifecycle for devices. Tests adding a device, detecting duplicates (409), missing fields (400), updating a device, updating a non-existent device (404), deleting a device, deleting a non-existent device (404), and auto-creation of empty tags array. |
| 7 | **TAGS - CRUD** | Complete CRUD lifecycle for device tags. Tests adding tags, duplicate detection (409), missing fields (400), tags on non-existent devices (404), deleting tags, deleting non-existent tags (404), and adding multiple tags to a single device. |
| 8 | **USERS - CRUD** | Complete CRUD lifecycle for users. Tests adding users, password hash exclusion from responses, duplicates (409), missing/empty fields (400), updating security groups and passwords, updating non-existent users (404), and deleting users. |
| 9 | **SECURITY - STATUS** | Validates the security status endpoint returns: initialized flag, password file presence, keystore presence, PKI directory presence, keystore size, and keystore modification time. |
| 10 | **SECURITY - KEYSTORE PASSWORD VALIDATION** | Tests keystore password validation: empty password (400), missing password field (400). Does not test actual password change to avoid breaking the keystore. |
| 11 | **SECURITY - CERTIFICATES** | Tests certificate management endpoints: listing trusted and rejected certificates, trusting/rejecting/deleting non-existent certificates (404). |
| 12 | **TEMPLATES (all 11 protocols)** | Validates all 11 protocol templates are present: Siemens S7, Modbus TCP, Modbus RTU, Modbus ASCII, OPC-UA (Client), EtherNet/IP, Allen-Bradley Logix, KNXnet/IP, IEC 60870-5-104, Firmata (Arduino), and Simulated (Test). Also checks that templates include example and tagExamples fields, and validates S7 BOOL bit offset format. |
| 13 | **BACKUPS** | Tests backup listing, triggering a new backup via config save, verifying backup file creation, restoring from a backup, and attempting to restore a non-existent backup (404). |
| 14 | **SERVER LOGS** | Tests the server logs endpoint with different line count parameters (10, 5, default) and verifies log content includes expected entries. |
| 15 | **SERVER RESTART** | Triggers a server restart via the API, waits 20 seconds, then verifies the server is running again. |
| 16 | **MULTI-DEVICE + MULTI-TAG TEST** | Creates multiple devices with multiple tags simultaneously, verifies they all exist, then cleans them up and verifies removal. |
| 17 | **MULTI-USER TEST** | Creates multiple users with different security groups, verifies they exist, then cleans them up. |
| 18 | **FINAL STATE VERIFICATION** | Validates the system is in a clean state after all tests: original simulated device and its tags are intact, admin user exists, server is running, and security is still initialized. |

## Expected Output

A successful test run produces output like:

```
=============================================================
  PLC4X Manager - Test Suite
=============================================================

[1] WEB UI
  PASS [1] GET / returns 200 (HTTP 200)
  PASS [2] HTML contains PLC4X Manager title
  ...

[2] SERVER STATUS
  PASS [17] Server status returns running
  ...

=============================================================
  RESULTS: 97 passed, 0 failed, 97 total
=============================================================

  ALL TESTS PASSED
```

- **PASS** (green) - Test assertion succeeded.
- **FAIL** (red) - Test assertion failed; shows expected vs. actual values.
- The exit code equals the number of failed tests (0 = all passed).

## Test Count

The main test suite (`test_all.sh`) contains approximately **97 test assertions** covering:

- HTTP status code validation (correct response codes for success, client errors, not found)
- Response body content validation (JSON fields, expected values)
- Negative testing (duplicates, missing fields, non-existent resources)
- State management (create, verify, clean up)
- End-to-end workflows (CRUD cycles for devices, tags, users)
- System integrity (final state verification after all tests)

The persistence test suite (`test_persistence.sh`) contains additional assertions focused on data survival across container restarts.

## How to Add New Tests

### 1. Using the existing assertion helpers

The test framework provides three assertion functions:

```bash
# Check that response contains expected string
assert "Test description" "expected_substring" "$response"

# Check that response does NOT contain a string
assert_not_contains "Test description" "unexpected_substring" "$response"

# Check HTTP status code
assert_status "Test description" "expected_code" "$actual_code"
```

### 2. Adding a new test category

To add a new test section, follow this pattern:

```bash
# =============================================================
echo ""
echo -e "${YELLOW}[19] YOUR NEW CATEGORY${NC}"
# =============================================================

# Make API calls with curl
RES=$(curl -s "$BASE/api/your-endpoint")
assert "Description of what you expect" "expected_value" "$RES"

# Check HTTP status codes
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/your-endpoint" \
  -H "Content-Type: application/json" -d '{"key":"value"}')
assert_status "POST returns expected code" "201" "$CODE"
```

### 3. Best practices

- Always **clean up** test data you create (delete test devices, users, etc.).
- Use unique names for test resources to avoid conflicts (e.g., `TestPLC`, `testuser`).
- Test both **happy paths** and **error cases** (missing fields, duplicates, non-existent resources).
- Verify **state changes** after mutations (read back after create/update/delete).
- Update the category number in the section header.
- If your test modifies server settings, **restore original values** afterward.

### 4. Creating a separate test script

For specialized tests (like `test_persistence.sh`), you can create a new file in the `tests/` directory. Include the same assertion helper functions at the top or source them from a shared file.
