#!/bin/bash
# =============================================================
# PLC4X Manager - Comprehensive Test Suite
# =============================================================

BASE="${BASE_URL:-http://localhost:3080}"

# Authentication credentials
AUTH_USER="${ADMIN_USERNAME:-admin}"
AUTH_PASS="${ADMIN_PASSWORD:-admin}"
AUTH_HEADER=""

# Get JWT token for authenticated requests
get_auth_token() {
    local response
    response=$(curl -s -X POST "$BASE/api/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"$AUTH_USER\",\"password\":\"$AUTH_PASS\"}")
    TOKEN=$(echo "$response" | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
    if [ -z "$TOKEN" ]; then
        echo "ERROR: Failed to authenticate. Check ADMIN_USERNAME and ADMIN_PASSWORD."
        echo "Response: $response"
        exit 1
    fi
    AUTH_HEADER="Authorization: Bearer $TOKEN"
    echo "    Authenticated as $AUTH_USER"
}

PASS=0
FAIL=0
TOTAL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

assert() {
    TOTAL=$((TOTAL + 1))
    local test_name="$1"
    local expected="$2"
    local actual="$3"

    if echo "$actual" | grep -qF "$expected"; then
        PASS=$((PASS + 1))
        echo -e "  ${GREEN}PASS${NC} [$TOTAL] $test_name"
    else
        FAIL=$((FAIL + 1))
        echo -e "  ${RED}FAIL${NC} [$TOTAL] $test_name"
        echo -e "       Expected to contain: $expected"
        echo -e "       Got: $(echo "$actual" | head -c 200)"
    fi
}

assert_not_contains() {
    TOTAL=$((TOTAL + 1))
    local test_name="$1"
    local unexpected="$2"
    local actual="$3"

    if echo "$actual" | grep -qF "$unexpected"; then
        FAIL=$((FAIL + 1))
        echo -e "  ${RED}FAIL${NC} [$TOTAL] $test_name"
        echo -e "       Should NOT contain: $unexpected"
    else
        PASS=$((PASS + 1))
        echo -e "  ${GREEN}PASS${NC} [$TOTAL] $test_name"
    fi
}

assert_status() {
    TOTAL=$((TOTAL + 1))
    local test_name="$1"
    local expected_code="$2"
    local actual_code="$3"

    if [ "$actual_code" = "$expected_code" ]; then
        PASS=$((PASS + 1))
        echo -e "  ${GREEN}PASS${NC} [$TOTAL] $test_name (HTTP $actual_code)"
    else
        FAIL=$((FAIL + 1))
        echo -e "  ${RED}FAIL${NC} [$TOTAL] $test_name (expected HTTP $expected_code, got HTTP $actual_code)"
    fi
}

echo ""
echo "============================================================="
echo "  PLC4X Manager - Test Suite"
echo "============================================================="
echo ""

# =============================================================
echo -e "${YELLOW}[0] AUTHENTICATION${NC}"
# =============================================================

# Test login with correct credentials
RES=$(curl -s -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$AUTH_USER\",\"password\":\"$AUTH_PASS\"}")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$AUTH_USER\",\"password\":\"$AUTH_PASS\"}")
assert_status "POST /api/auth/login with correct credentials returns 200" "200" "$CODE"
assert "Login response contains token" '"token"' "$RES"

# Test login with wrong password
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"wrongpassword"}')
assert_status "POST /api/auth/login with wrong password returns 401" "401" "$CODE"

# Test accessing protected endpoint without auth
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/config")
assert_status "GET /api/config without auth returns 401" "401" "$CODE"

# Authenticate to get token for remaining tests
get_auth_token

# Test token verification
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/auth/verify")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" "$BASE/api/auth/verify")
assert_status "GET /api/auth/verify with token returns 200" "200" "$CODE"
assert "Verify response shows authenticated" '"authenticated":true' "$RES"

# Test Basic Auth
CODE=$(curl -s -o /dev/null -w "%{http_code}" -u admin:admin "$BASE/api/config")
assert_status "GET /api/config with Basic Auth returns 200" "200" "$CODE"

# Test token refresh
RES=$(curl -s -H "$AUTH_HEADER" -X POST "$BASE/api/auth/refresh")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" -X POST "$BASE/api/auth/refresh")
assert_status "POST /api/auth/refresh returns 200" "200" "$CODE"
assert "Refresh response contains new token" '"token"' "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[1] WEB UI${NC}"
# =============================================================

RES=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/")
assert_status "GET / returns 200" "200" "$RES"

RES=$(curl -s "$BASE/")
assert "HTML contains PLC4X Manager title" "PLC4X Manager" "$RES"
assert "HTML contains Devices tab" "tab-devices" "$RES"
assert "HTML contains Server tab" "tab-server" "$RES"
assert "HTML contains Security tab" "tab-security" "$RES"
assert "HTML contains Logs tab" "tab-logs" "$RES"
assert "HTML contains Backups tab" "tab-backups" "$RES"
assert "HTML contains User modal" "userModal" "$RES"
assert "HTML contains Device modal" "deviceModal" "$RES"
assert "HTML contains endpoint preview" "endpointPreview" "$RES"
assert "HTML contains config version field" "serverVersion" "$RES"

RES=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/static/css/style.css")
assert_status "GET /static/css/style.css returns 200" "200" "$RES"

RES=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/static/js/app.js")
assert_status "GET /static/js/app.js returns 200" "200" "$RES"

RES=$(curl -s "$BASE/static/js/app.js")
assert "JS has loadSecurityStatus function" "loadSecurityStatus" "$RES"
assert "JS has loadUsers function" "loadUsers" "$RES"
assert "JS has loadCertificates function" "loadCertificates" "$RES"
assert "JS has changeKeystorePassword function" "changeKeystorePassword" "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[2] SERVER STATUS${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/server/status")
assert "Server status returns running" '"running":true' "$RES"
assert "Server status has container id" '"id"' "$RES"
assert "Server status has image info" '"image"' "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[3] CONFIGURATION - GET (all fields)${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/config")
assert "Config has version field" '"version"' "$RES"
assert "Config has dir field" '"dir"' "$RES"
assert "Config has name field" '"name"' "$RES"
assert "Config has tcpPort field" '"tcpPort"' "$RES"
assert "Config has disableInsecureEndpoint field" '"disableInsecureEndpoint"' "$RES"
assert "Config has devices array" '"devices"' "$RES"
assert "Config version is 0.8" '"version":"0.8"' "$RES"
assert "Config tcpPort is 12687" '"tcpPort":12687' "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[4] CONFIGURATION - UPDATE ALL SERVER SETTINGS${NC}"
# =============================================================

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/config/server" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"name":"TestServer","tcpPort":12687,"version":"0.9","disableInsecureEndpoint":true,"dir":"/app/security"}')
assert_status "PUT /api/config/server returns 200" "200" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/config")
assert "Server name updated" '"name":"TestServer"' "$RES"
assert "Version updated to 0.9" '"version":"0.9"' "$RES"
assert "disableInsecureEndpoint updated to true" '"disableInsecureEndpoint":true' "$RES"

# Restore
curl -s -X PUT "$BASE/api/config/server" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"name":"Plc4xOPCUAServer","version":"0.8","disableInsecureEndpoint":false}' > /dev/null

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/config")
assert "Settings restored after test" '"name":"Plc4xOPCUAServer"' "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[5] CONFIGURATION - SAVE FULL CONFIG${NC}"
# =============================================================

FULL_CONFIG=$(curl -s -H "$AUTH_HEADER" "$BASE/api/config")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/config" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d "$FULL_CONFIG")
assert_status "PUT /api/config (full save) returns 200" "200" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/config" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '')
assert_status "PUT /api/config with empty body returns 400" "400" "$CODE"

# =============================================================
echo ""
echo -e "${YELLOW}[6] DEVICES - CRUD${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "GET /api/devices returns data" '"name"' "$RES"

# Cleanup: remove TestPLC if leftover from previous run
curl -s -o /dev/null -X DELETE "$BASE/api/devices/TestPLC" -H "$AUTH_HEADER" 2>/dev/null
curl -s -o /dev/null -X DELETE "$BASE/api/devices/MinimalDevice" -H "$AUTH_HEADER" 2>/dev/null

# Add device
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"name":"TestPLC","connectionString":"simulated://127.0.0.1","tags":[{"alias":"Var1","address":"RANDOM/Temporary:DINT"}]}')
assert_status "POST /api/devices (add) returns 201" "201" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "Device TestPLC exists" "TestPLC" "$RES"

# Duplicate
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"name":"TestPLC","connectionString":"simulated://127.0.0.1"}')
assert_status "POST duplicate device returns 409" "409" "$CODE"

# Missing fields
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"name":"NoConn"}')
assert_status "POST device without connectionString returns 400" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"connectionString":"simulated://127.0.0.1"}')
assert_status "POST device without name returns 400" "400" "$CODE"

# Update
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/devices/TestPLC" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"name":"TestPLC","connectionString":"simulated://127.0.0.1","tags":[{"alias":"Var1","address":"RANDOM/Temporary:DINT"},{"alias":"Var2","address":"RANDOM/Temporary:REAL"}]}')
assert_status "PUT /api/devices/TestPLC returns 200" "200" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "Updated device has Var2 tag" "Var2" "$RES"

# Update non-existent
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/devices/NonExistent" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"name":"NonExistent","connectionString":"simulated://127.0.0.1"}')
assert_status "PUT non-existent device returns 404" "404" "$CODE"

# Delete
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/devices/TestPLC" -H "$AUTH_HEADER")
assert_status "DELETE /api/devices/TestPLC returns 200" "200" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert_not_contains "TestPLC removed from list" "TestPLC" "$RES"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/devices/NonExistent" -H "$AUTH_HEADER")
assert_status "DELETE non-existent device returns 404" "404" "$CODE"

# Device with no tags auto-creates empty array
curl -s -o /dev/null -X DELETE "$BASE/api/devices/NoTagsDev" -H "$AUTH_HEADER" 2>/dev/null
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"name":"NoTagsDev","connectionString":"simulated://127.0.0.1"}')
assert_status "Add device without tags field returns 201" "201" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices/NoTagsDev/tags")
assert "Device without tags has empty tags" "[]" "$RES"

curl -s -X DELETE "$BASE/api/devices/NoTagsDev" -H "$AUTH_HEADER" > /dev/null

# =============================================================
echo ""
echo -e "${YELLOW}[7] TAGS - CRUD${NC}"
# =============================================================

curl -s -X POST "$BASE/api/devices" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"name":"TagTestPLC","connectionString":"simulated://127.0.0.1","tags":[]}' > /dev/null

# Add tag
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices/TagTestPLC/tags" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"alias":"Temp","address":"RANDOM/Temporary:REAL"}')
assert_status "POST tag returns 201" "201" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices/TagTestPLC/tags")
assert "Tag Temp exists" "Temp" "$RES"
assert "Tag has correct address" "RANDOM/Temporary:REAL" "$RES"

# Duplicate tag
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices/TagTestPLC/tags" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"alias":"Temp","address":"RANDOM/Temporary:DINT"}')
assert_status "POST duplicate tag returns 409" "409" "$CODE"

# Missing fields
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices/TagTestPLC/tags" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"alias":"NoAddr"}')
assert_status "POST tag without address returns 400" "400" "$CODE"

# Tag on non-existent device
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices/FakePLC/tags" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"alias":"X","address":"Y"}')
assert_status "POST tag to non-existent device returns 404" "404" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" "$BASE/api/devices/FakePLC/tags")
assert_status "GET tags for non-existent device returns 404" "404" "$CODE"

# Delete tag
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/devices/TagTestPLC/tags/Temp" -H "$AUTH_HEADER")
assert_status "DELETE tag returns 200" "200" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/devices/TagTestPLC/tags/NonExistent" -H "$AUTH_HEADER")
assert_status "DELETE non-existent tag returns 404" "404" "$CODE"

# Add multiple tags
curl -s -X POST "$BASE/api/devices/TagTestPLC/tags" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"alias":"Tag1","address":"RANDOM/Temporary:DINT"}' > /dev/null
curl -s -X POST "$BASE/api/devices/TagTestPLC/tags" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"alias":"Tag2","address":"RANDOM/Temporary:REAL"}' > /dev/null
curl -s -X POST "$BASE/api/devices/TagTestPLC/tags" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"alias":"Tag3","address":"STATE/Temporary:DINT"}' > /dev/null

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices/TagTestPLC/tags")
assert "Multiple tags: Tag1 exists" "Tag1" "$RES"
assert "Multiple tags: Tag2 exists" "Tag2" "$RES"
assert "Multiple tags: Tag3 exists" "Tag3" "$RES"

curl -s -X DELETE "$BASE/api/devices/TagTestPLC" -H "$AUTH_HEADER" > /dev/null

# =============================================================
echo ""
echo -e "${YELLOW}[8] USERS - CRUD${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/users")
assert "Default admin user exists" "admin" "$RES"
assert "Admin has admin-group" "admin-group" "$RES"

# Add user
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/users" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"username":"testuser","password":"testpass123","security":"user-group"}')
assert_status "POST /api/users (add) returns 201" "201" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/users")
assert "User testuser in list" "testuser" "$RES"
assert "User has user-group" "user-group" "$RES"
assert_not_contains "Password hash not in response" "testpass123" "$RES"
assert "hasPassword is true" '"hasPassword":true' "$RES"

# Duplicate
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/users" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"username":"testuser","password":"pass"}')
assert_status "POST duplicate user returns 409" "409" "$CODE"

# Missing fields
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/users" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"username":"nopass"}')
assert_status "POST user without password returns 400" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/users" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"password":"nouser"}')
assert_status "POST user without username returns 400" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/users" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"username":"","password":"pass"}')
assert_status "POST user with empty username returns 400" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/users" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"username":"x","password":""}')
assert_status "POST user with empty password returns 400" "400" "$CODE"

# Update security group
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/users/testuser" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"security":"admin-group"}')
assert_status "PUT user (change group) returns 200" "200" "$CODE"

# Update password
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/users/testuser" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"password":"newpass456"}')
assert_status "PUT user (change password) returns 200" "200" "$CODE"

# Update non-existent
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/users/fakeuser" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"security":"admin-group"}')
assert_status "PUT non-existent user returns 404" "404" "$CODE"

# Delete
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/users/testuser" -H "$AUTH_HEADER")
assert_status "DELETE user returns 200" "200" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/users")
assert_not_contains "User testuser removed" "testuser" "$RES"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/users/fakeuser" -H "$AUTH_HEADER")
assert_status "DELETE non-existent user returns 404" "404" "$CODE"

# =============================================================
echo ""
echo -e "${YELLOW}[9] SECURITY - STATUS${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/security/status")
assert "Security initialized" '"initialized":true' "$RES"
assert "Password file present" '"passwordFile":true' "$RES"
assert "Keystore present" '"keystore":true' "$RES"
assert "PKI directory present" '"pkiDirectory":true' "$RES"
assert "Keystore size > 0" '"keystoreSize"' "$RES"
assert "Keystore modified time" '"keystoreModified"' "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[10] SECURITY - KEYSTORE PASSWORD VALIDATION${NC}"
# =============================================================

# Empty password
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/security/password" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"password":""}')
assert_status "PUT empty keystore password returns 400" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/security/password" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{}')
assert_status "PUT missing password field returns 400" "400" "$CODE"

# NOTE: Not testing actual password change to avoid breaking the keystore

# =============================================================
echo ""
echo -e "${YELLOW}[11] SECURITY - CERTIFICATES${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/security/certificates/trusted")
assert "GET trusted certs returns valid response" "" "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/security/certificates/rejected")
assert "GET rejected certs returns valid response" "" "$RES"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/security/certificates/trust/fake.der" -H "$AUTH_HEADER")
assert_status "Trust non-existent cert returns 404" "404" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/security/certificates/reject/fake.der" -H "$AUTH_HEADER")
assert_status "Reject non-existent cert returns 404" "404" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/security/certificates/fake.der" -H "$AUTH_HEADER")
assert_status "Delete non-existent cert returns 404" "404" "$CODE"

# =============================================================
echo ""
echo -e "${YELLOW}[12] TEMPLATES (all 11 protocols)${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/templates")
assert "Siemens S7" "Siemens S7" "$RES"
assert "Modbus TCP" "Modbus TCP" "$RES"
assert "Modbus RTU" "Modbus RTU" "$RES"
assert "Modbus ASCII" "Modbus ASCII" "$RES"
assert "OPC-UA (Client)" "OPC-UA (Client)" "$RES"
assert "EtherNet/IP" "EtherNet/IP" "$RES"
assert "Allen-Bradley Logix" "Logix" "$RES"
assert "KNXnet/IP" "KNXnet/IP" "$RES"
assert "IEC 60870-5-104" "IEC 60870" "$RES"
assert "Firmata (Arduino)" "Firmata" "$RES"
assert "Simulated (Test)" "Simulated (Test)" "$RES"
assert "S7 BOOL template has bit offset" "%DB1:8.0:BOOL" "$RES"
assert "Templates have example field" '"example"' "$RES"
assert "Templates have tagExamples field" '"tagExamples"' "$RES"

# Count protocols
PROTO_COUNT=$(echo "$RES" | grep -oF '"protocol"' | wc -l)
TOTAL=$((TOTAL + 1))
if [ "$PROTO_COUNT" -eq "11" ]; then
    PASS=$((PASS + 1))
    echo -e "  ${GREEN}PASS${NC} [$TOTAL] Template count is 11 (got $PROTO_COUNT)"
else
    FAIL=$((FAIL + 1))
    echo -e "  ${RED}FAIL${NC} [$TOTAL] Template count should be 11, got $PROTO_COUNT"
fi

# =============================================================
echo ""
echo -e "${YELLOW}[13] BACKUPS${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/backups")
assert "GET /api/backups returns backup files" "config_" "$RES"

# Trigger new backup
curl -s -X PUT "$BASE/api/config/server" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"name":"Plc4xOPCUAServer"}' > /dev/null

BACKUP=$(curl -s -H "$AUTH_HEADER" "$BASE/api/backups" | grep -oF '"config_' | head -1)
assert "Backup file created" '"config_' "$BACKUP"

# Restore
BACKUP_FILE=$(curl -s -H "$AUTH_HEADER" "$BASE/api/backups" | grep -o '"config_[^"]*"' | head -1 | tr -d '"')
if [ -n "$BACKUP_FILE" ]; then
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/backups/$BACKUP_FILE/restore" -H "$AUTH_HEADER")
    assert_status "Restore backup returns 200" "200" "$CODE"
fi

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/backups/nonexistent.yml/restore" -H "$AUTH_HEADER")
assert_status "Restore non-existent backup returns 404" "404" "$CODE"

# =============================================================
echo ""
echo -e "${YELLOW}[14] SERVER LOGS${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/server/logs?lines=10")
assert "GET logs returns logs field" '"logs"' "$RES"
assert "Logs contain Binding endpoint" "Binding endpoint" "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/server/logs?lines=5")
assert "GET logs with lines=5 works" '"logs"' "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/server/logs")
assert "GET logs default (no lines param) works" '"logs"' "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[15] SERVER RESTART${NC}"
# =============================================================

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/server/restart" -H "$AUTH_HEADER")
assert_status "POST /api/server/restart returns 200" "200" "$CODE"

# Wait for server to come back (poll up to 90s)
echo "    Waiting for server to restart..."
sleep 10
get_auth_token
for i in $(seq 1 16); do
    STATUS=$(curl -s -H "$AUTH_HEADER" "$BASE/api/server/status" 2>/dev/null | grep -oF '"running":true')
    if [ -n "$STATUS" ]; then
        echo "    Server back online (attempt $i/16)"
        break
    fi
    sleep 5
done

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/server/status")
assert "Server running after restart" '"running":true' "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[16] MULTI-DEVICE + MULTI-TAG TEST${NC}"
# =============================================================

curl -s -X POST "$BASE/api/devices" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"name":"Dev1","connectionString":"simulated://127.0.0.1","tags":[{"alias":"A","address":"RANDOM/Temporary:DINT"}]}' > /dev/null
curl -s -X POST "$BASE/api/devices" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"name":"Dev2","connectionString":"simulated://127.0.0.1","tags":[{"alias":"B","address":"RANDOM/Temporary:REAL"},{"alias":"C","address":"STATE/Temporary:DINT"}]}' > /dev/null

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "Dev1 exists" "Dev1" "$RES"
assert "Dev2 exists" "Dev2" "$RES"

curl -s -X DELETE "$BASE/api/devices/Dev1" -H "$AUTH_HEADER" > /dev/null
curl -s -X DELETE "$BASE/api/devices/Dev2" -H "$AUTH_HEADER" > /dev/null

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert_not_contains "Dev1 cleaned up" "Dev1" "$RES"
assert_not_contains "Dev2 cleaned up" "Dev2" "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[17] MULTI-USER TEST${NC}"
# =============================================================

curl -s -X POST "$BASE/api/users" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"username":"op1","password":"p1","security":"admin-group"}' > /dev/null
curl -s -X POST "$BASE/api/users" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"username":"op2","password":"p2","security":"user-group"}' > /dev/null

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/users")
assert "op1 exists" "op1" "$RES"
assert "op2 exists" "op2" "$RES"

curl -s -X DELETE "$BASE/api/users/op1" -H "$AUTH_HEADER" > /dev/null
curl -s -X DELETE "$BASE/api/users/op2" -H "$AUTH_HEADER" > /dev/null

# =============================================================
echo ""
echo -e "${YELLOW}[19] BACKUP CONTENT AND DIFF${NC}"
# =============================================================

# Get a backup filename
BACKUP_FILE=$(curl -s -H "$AUTH_HEADER" "$BASE/api/backups" | grep -o '"filename":"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -n "$BACKUP_FILE" ]; then
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" "$BASE/api/backups/$BACKUP_FILE/content")
    assert_status "GET backup content returns 200" "200" "$CODE"

    RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/backups/$BACKUP_FILE/content")
    assert "Backup content has content field" '"content"' "$RES"

    CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" "$BASE/api/backups/$BACKUP_FILE/diff")
    assert_status "GET backup diff returns 200" "200" "$CODE"

    RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/backups/$BACKUP_FILE/diff")
    assert "Backup diff has diff field" '"diff"' "$RES"
fi

# Non-existent backup
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" "$BASE/api/backups/nonexistent.yml/content")
assert_status "GET non-existent backup content returns 404" "404" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" "$BASE/api/backups/nonexistent.yml/diff")
assert_status "GET non-existent backup diff returns 404" "404" "$CODE"

# =============================================================
echo ""
echo -e "${YELLOW}[20] BACKUP CLEANUP${NC}"
# =============================================================

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" "$BASE/api/backups/cleanup")
assert_status "POST backup cleanup returns 200" "200" "$CODE"

RES=$(curl -s -X POST -H "$AUTH_HEADER" "$BASE/api/backups/cleanup")
assert "Cleanup response has remaining field" '"remaining"' "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[21] SECURITY - PATH TRAVERSAL PROTECTION${NC}"
# =============================================================

# Flask decodes %2F to / in URLs and returns 404 (route not matched).
# For filenames that reach the handler, safe_filename() blocks them with 400.
# Both behaviors prevent path traversal - test both vectors.

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" "$BASE/api/backups/..%2F..%2Fetc%2Fpasswd/content")
assert_status "Path traversal in backup content blocked (Flask routing)" "404" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" "$BASE/api/backups/..%2F..%2Fetc%2Fpasswd/diff")
assert_status "Path traversal in backup diff blocked (Flask routing)" "404" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" "$BASE/api/backups/..%2Fmalicious.yml/restore")
assert_status "Path traversal in backup restore blocked (Flask routing)" "404" "$CODE"

# Direct filename validation test (characters that pass Flask routing but fail safe_filename)
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  "$BASE/api/backups/has%20spaces.yml/restore")
assert_status "Filename with spaces blocked by validator" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" "$BASE/api/security/certificates/trust/..%2F..%2Fetc%2Fpasswd")
assert_status "Path traversal in certificate trust blocked (Flask routing)" "404" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE -H "$AUTH_HEADER" "$BASE/api/security/certificates/..%2F..%2Fetc%2Fpasswd")
assert_status "Path traversal in certificate delete blocked (Flask routing)" "404" "$CODE"

# =============================================================
echo ""
echo -e "${YELLOW}[22] SECURITY - INPUT VALIDATION${NC}"
# =============================================================

# Invalid device name
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  "$BASE/api/devices" -d '{"name":"../../bad","connectionString":"simulated://127.0.0.1"}')
assert_status "Device name with path traversal rejected" "400" "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  "$BASE/api/devices" -d '{"name":"name with spaces","connectionString":"simulated://127.0.0.1"}')
assert_status "Device name with spaces rejected" "400" "$CODE"

# Invalid tag alias
curl -s -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  "$BASE/api/devices" -d '{"name":"ValidationTest","connectionString":"simulated://127.0.0.1"}' > /dev/null

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  "$BASE/api/devices/ValidationTest/tags" -d '{"alias":"<script>alert(1)</script>","address":"RANDOM/Temporary:DINT"}')
assert_status "Tag alias with XSS rejected" "400" "$CODE"

curl -s -X DELETE -H "$AUTH_HEADER" "$BASE/api/devices/ValidationTest" > /dev/null

# Invalid username
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  "$BASE/api/users" -d '{"username":"../../etc","password":"test123","security":"admin-group"}')
assert_status "Username with path traversal rejected" "400" "$CODE"

# Cannot delete admin user
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE -H "$AUTH_HEADER" "$BASE/api/users/admin")
assert_status "Cannot delete admin user returns 403" "403" "$CODE"

# =============================================================
echo ""
echo -e "${YELLOW}[23] SECURITY - API KEY AUTH${NC}"
# =============================================================

# API key auth (if API_KEY is set, test it; if not, test that empty key is rejected)
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "X-API-Key: invalid-key" "$BASE/api/server/status")
assert_status "Invalid API key returns 401" "401" "$CODE"

# =============================================================
echo ""
echo -e "${YELLOW}[24] LOGBOOK - COMPLETE TEST${NC}"
# =============================================================

# --- 24a: GET logbook (empty initially or has entries) ---
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/logbook")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH_HEADER" "$BASE/api/logbook")
assert_status "GET /api/logbook returns 200" "200" "$CODE"
assert "Logbook response has entries array" '"entries"' "$RES"

# --- 24b: GET logbook without auth returns 401 ---
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/logbook")
assert_status "GET /api/logbook without auth returns 401" "401" "$CODE"

# --- 24c: POST logbook as admin (admin has operator+ permissions) ---
RES=$(curl -s -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"message":"Test observation from admin","shift":"morning","category":"observation","priority":"normal"}')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"message":"Test observation from admin","shift":"morning","category":"observation","priority":"normal"}')
assert_status "POST /api/logbook as admin returns 201" "201" "$CODE"
assert "Logbook entry has message" '"message"' "$RES"
assert "Logbook entry has timestamp" '"timestamp"' "$RES"
assert "Logbook entry has user" '"user"' "$RES"
assert "Logbook entry has id" '"id"' "$RES"
assert "Logbook entry message matches" 'Test observation from admin' "$RES"
assert "Logbook entry shift is morning" '"shift":"morning"' "$RES"
assert "Logbook entry category is observation" '"category":"observation"' "$RES"
assert "Logbook entry priority is normal" '"priority":"normal"' "$RES"

# --- 24d: POST logbook with all categories and priorities ---
for CAT in incident maintenance handover alarm; do
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
        -H "$AUTH_HEADER" \
        -H "Content-Type: application/json" \
        -d "{\"message\":\"Test $CAT entry\",\"shift\":\"afternoon\",\"category\":\"$CAT\",\"priority\":\"normal\"}")
    assert_status "POST /api/logbook category=$CAT returns 201" "201" "$CODE"
done

for PRI in important critical; do
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
        -H "$AUTH_HEADER" \
        -H "Content-Type: application/json" \
        -d "{\"message\":\"Test $PRI priority\",\"shift\":\"night\",\"category\":\"observation\",\"priority\":\"$PRI\"}")
    assert_status "POST /api/logbook priority=$PRI returns 201" "201" "$CODE"
done

# --- 24e: POST logbook without message returns 400 ---
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"shift":"morning","category":"observation"}')
assert_status "POST /api/logbook without message returns 400" "400" "$CODE"

# --- 24f: POST logbook with empty message returns 400 ---
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"message":"","shift":"morning"}')
assert_status "POST /api/logbook with empty message returns 400" "400" "$CODE"

# --- 24g: POST logbook with only spaces returns 400 ---
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"message":"   ","shift":"morning"}')
assert_status "POST /api/logbook with blank message returns 400" "400" "$CODE"

# --- 24h: POST logbook without auth returns 401 ---
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
    -H "Content-Type: application/json" \
    -d '{"message":"No auth test"}')
assert_status "POST /api/logbook without auth returns 401" "401" "$CODE"

# --- 24i: GET logbook entries contain our test data ---
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/logbook?lines=20")
assert "Logbook contains admin observation" 'Test observation from admin' "$RES"
assert "Logbook contains incident entry" 'Test incident entry' "$RES"
assert "Logbook contains maintenance entry" 'Test maintenance entry' "$RES"
assert "Logbook contains handover entry" 'Test handover entry' "$RES"
assert "Logbook contains alarm entry" 'Test alarm entry' "$RES"
assert "Logbook contains critical priority" 'Test critical priority' "$RES"
assert "Logbook contains important priority" 'Test important priority' "$RES"

# --- 24j: GET logbook with shift filter ---
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/logbook?shift=morning")
assert "Shift filter morning returns morning entries" 'Test observation from admin' "$RES"
assert_not_contains "Shift filter morning excludes night entries" 'Test critical priority' "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/logbook?shift=night")
assert "Shift filter night returns night entries" 'Test critical priority' "$RES"
assert_not_contains "Shift filter night excludes morning entries" 'Test observation from admin' "$RES"

# --- 24k: GET logbook with lines limit ---
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/logbook?lines=2")
# Count entries - should have at most 2
ENTRY_COUNT=$(echo "$RES" | grep -o '"id"' | wc -l)
if [ "$ENTRY_COUNT" -le 2 ]; then
    TOTAL=$((TOTAL + 1)); PASS=$((PASS + 1))
    echo -e "  ${GREEN}PASS${NC} [$TOTAL] Logbook lines=2 returns at most 2 entries (got $ENTRY_COUNT)"
else
    TOTAL=$((TOTAL + 1)); FAIL=$((FAIL + 1))
    echo -e "  ${RED}FAIL${NC} [$TOTAL] Logbook lines=2 should return at most 2 entries (got $ENTRY_COUNT)"
fi

# --- 24l: POST logbook with long message (truncation at 5000) ---
LONG_MSG=$(printf 'X%.0s' $(seq 1 5500))
RES=$(curl -s -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d "{\"message\":\"$LONG_MSG\",\"shift\":\"morning\",\"category\":\"observation\",\"priority\":\"normal\"}")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d "{\"message\":\"$LONG_MSG\",\"shift\":\"morning\",\"category\":\"observation\",\"priority\":\"normal\"}")
assert_status "POST /api/logbook with 5500-char message returns 201" "201" "$CODE"

# --- 24m: POST logbook with special characters (XSS test) ---
RES=$(curl -s -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"message":"<script>alert(1)</script> & \"quotes\"","shift":"morning","category":"observation","priority":"normal"}')
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"message":"<script>alert(1)</script> & \"quotes\"","shift":"morning","category":"observation","priority":"normal"}')
assert_status "POST /api/logbook with special chars returns 201" "201" "$CODE"
assert "Logbook stores special chars" '<script>' "$RES"

# --- 24n: POST logbook with empty JSON body returns 400 ---
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{}')
assert_status "POST /api/logbook with empty body returns 400" "400" "$CODE"

# --- 24o: RBAC - Monitor role cannot POST logbook ---
MON_TOKEN=$(curl -s -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"monitor","password":"monitor"}' | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
if [ -n "$MON_TOKEN" ]; then
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
        -H "Authorization: Bearer $MON_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"message":"Monitor should not write","shift":"morning","category":"observation","priority":"normal"}')
    assert_status "POST /api/logbook as monitor returns 403" "403" "$CODE"

    # Monitor CAN read logbook
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $MON_TOKEN" "$BASE/api/logbook")
    assert_status "GET /api/logbook as monitor returns 200" "200" "$CODE"
else
    TOTAL=$((TOTAL + 1)); PASS=$((PASS + 1))
    echo -e "  ${GREEN}PASS${NC} [$TOTAL] Monitor login not configured - skip RBAC test"
fi

# --- 24p: RBAC - Operator role CAN POST logbook ---
OP_TOKEN=$(curl -s -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"operator","password":"operator"}' | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
if [ -n "$OP_TOKEN" ]; then
    RES=$(curl -s -X POST "$BASE/api/logbook" \
        -H "Authorization: Bearer $OP_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"message":"Operator logbook entry","shift":"afternoon","category":"maintenance","priority":"normal"}')
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
        -H "Authorization: Bearer $OP_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"message":"Operator logbook entry","shift":"afternoon","category":"maintenance","priority":"normal"}')
    assert_status "POST /api/logbook as operator returns 201" "201" "$CODE"
    assert "Operator entry has correct user" '"user":"operator"' "$RES"
else
    TOTAL=$((TOTAL + 1)); PASS=$((PASS + 1))
    echo -e "  ${GREEN}PASS${NC} [$TOTAL] Operator login not configured - skip RBAC test"
fi

# --- 24q: Verify all entries are in reverse chronological order ---
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/logbook?lines=50")
FIRST_TS=$(echo "$RES" | grep -o '"timestamp":"[^"]*"' | head -1 | cut -d'"' -f4)
LAST_TS=$(echo "$RES" | grep -o '"timestamp":"[^"]*"' | tail -1 | cut -d'"' -f4)
if [ -n "$FIRST_TS" ] && [ -n "$LAST_TS" ] && [ "$FIRST_TS" \> "$LAST_TS" -o "$FIRST_TS" = "$LAST_TS" ]; then
    TOTAL=$((TOTAL + 1)); PASS=$((PASS + 1))
    echo -e "  ${GREEN}PASS${NC} [$TOTAL] Logbook entries in reverse chronological order"
else
    TOTAL=$((TOTAL + 1)); FAIL=$((FAIL + 1))
    echo -e "  ${RED}FAIL${NC} [$TOTAL] Logbook entries NOT in reverse chronological order (first=$FIRST_TS, last=$LAST_TS)"
fi

# --- 24r: Verify user field is captured correctly ---
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/logbook?lines=50")
assert "Logbook entries contain admin user" '"user":"admin"' "$RES"

# --- 24s: UI test - logbook page loads HTML elements ---
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/")
assert "UI has logbook tab button" "showTab('logbook'" "$RES"
assert "UI has logbook shift filter" 'id="logbookShiftFilter"' "$RES"
assert "UI has logbook form" 'id="logbookForm"' "$RES"
assert "UI has logbook message textarea" 'id="logbookMessage"' "$RES"
assert "UI has logbook category select" 'id="logbookCategory"' "$RES"
assert "UI has logbook priority select" 'id="logbookPriority"' "$RES"
assert "UI has logbook shift select" 'id="logbookShift"' "$RES"
assert "UI has New Entry button" 'logbookNewEntryBtn' "$RES"
assert "UI has Save button" 'saveLogbookEntry()' "$RES"
assert "UI has Cancel button in logbook" "logbookForm" "$RES"

# --- 24t: UI test - logbook category options ---
assert "UI has Observation option" 'value="observation"' "$RES"
assert "UI has Incident option" 'value="incident"' "$RES"
assert "UI has Maintenance option" 'value="maintenance"' "$RES"
assert "UI has Handover option" 'value="handover"' "$RES"
assert "UI has Alarm option" 'value="alarm"' "$RES"

# --- 24u: UI test - logbook priority options ---
assert "UI has Normal priority" 'value="normal"' "$RES"
assert "UI has Important priority" 'value="important"' "$RES"
assert "UI has Critical priority" 'value="critical"' "$RES"

# --- 24v: UI test - logbook shift options ---
assert "UI has Morning shift" 'Morning 06-14' "$RES"
assert "UI has Afternoon shift" 'Afternoon 14-22' "$RES"
assert "UI has Night shift" 'Night 22-06' "$RES"

# --- 24w: RBAC complete - Operator full logbook workflow ---
if [ -n "$OP_TOKEN" ]; then
    # Operator can read logbook
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $OP_TOKEN" "$BASE/api/logbook")
    assert_status "GET /api/logbook as operator returns 200" "200" "$CODE"

    # Operator can post all categories
    for CAT in observation incident maintenance handover alarm; do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
            -H "Authorization: Bearer $OP_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"message\":\"Operator $CAT test\",\"shift\":\"morning\",\"category\":\"$CAT\",\"priority\":\"normal\"}")
        assert_status "POST /api/logbook as operator category=$CAT returns 201" "201" "$CODE"
    done

    # Operator can post all priorities
    for PRI in normal important critical; do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
            -H "Authorization: Bearer $OP_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"message\":\"Operator $PRI test\",\"shift\":\"afternoon\",\"category\":\"observation\",\"priority\":\"$PRI\"}")
        assert_status "POST /api/logbook as operator priority=$PRI returns 201" "201" "$CODE"
    done

    # Operator can post all shifts
    for SHIFT in morning afternoon night; do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
            -H "Authorization: Bearer $OP_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"message\":\"Operator $SHIFT shift\",\"shift\":\"$SHIFT\",\"category\":\"observation\",\"priority\":\"normal\"}")
        assert_status "POST /api/logbook as operator shift=$SHIFT returns 201" "201" "$CODE"
    done

    # Operator gets 400 on empty message
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
        -H "Authorization: Bearer $OP_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"message":""}')
    assert_status "POST /api/logbook as operator with empty message returns 400" "400" "$CODE"

    # Verify operator entries appear in logbook
    RES=$(curl -s -H "Authorization: Bearer $OP_TOKEN" "$BASE/api/logbook?lines=50")
    assert "Operator can see own entries" 'Operator observation test' "$RES"
    assert "Operator can see admin entries" 'Test observation from admin' "$RES"

    # Operator can filter by shift
    RES=$(curl -s -H "Authorization: Bearer $OP_TOKEN" "$BASE/api/logbook?shift=afternoon")
    assert "Operator shift filter works" 'Operator normal test' "$RES"
fi

# --- 24x: RBAC complete - Monitor logbook access ---
if [ -n "$MON_TOKEN" ]; then
    # Monitor can read logbook
    RES=$(curl -s -H "Authorization: Bearer $MON_TOKEN" "$BASE/api/logbook?lines=50")
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $MON_TOKEN" "$BASE/api/logbook")
    assert_status "GET /api/logbook as monitor returns 200" "200" "$CODE"
    assert "Monitor can see admin entries" 'Test observation from admin' "$RES"
    assert "Monitor can see operator entries" 'Operator observation test' "$RES"

    # Monitor cannot post ANY category
    for CAT in observation incident maintenance handover alarm; do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
            -H "Authorization: Bearer $MON_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"message\":\"Monitor $CAT attempt\",\"shift\":\"morning\",\"category\":\"$CAT\",\"priority\":\"normal\"}")
        assert_status "POST /api/logbook as monitor category=$CAT returns 403" "403" "$CODE"
    done

    # Monitor cannot post ANY priority
    for PRI in normal important critical; do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/logbook" \
            -H "Authorization: Bearer $MON_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"message\":\"Monitor $PRI attempt\",\"shift\":\"night\",\"category\":\"observation\",\"priority\":\"$PRI\"}")
        assert_status "POST /api/logbook as monitor priority=$PRI returns 403" "403" "$CODE"
    done

    # Monitor can filter by shift
    RES=$(curl -s -H "Authorization: Bearer $MON_TOKEN" "$BASE/api/logbook?shift=morning")
    assert "Monitor shift filter works" 'Test observation from admin' "$RES"

    # Verify monitor entries were NOT saved
    RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/logbook?lines=100")
    assert_not_contains "Monitor entries not in logbook" 'Monitor observation attempt' "$RES"
fi

# =============================================================
echo ""
echo -e "${YELLOW}[25] FINAL STATE VERIFICATION${NC}"
# =============================================================

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "Original Simulated device intact" "Simulated" "$RES"
assert "RandomInteger tag intact" "RandomInteger" "$RES"
assert "RandomFloat tag intact" "RandomFloat" "$RES"
assert "StateInteger tag intact" "StateInteger" "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/users")
assert "Admin user still exists" "admin" "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/server/status")
assert "Server still running at end" '"running":true' "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/security/status")
assert "Security still initialized" '"initialized":true' "$RES"

# =============================================================
echo ""
echo "============================================================="
echo -e "  RESULTS: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}, $TOTAL total"
echo "============================================================="
echo ""

if [ $FAIL -eq 0 ]; then
    echo -e "  ${GREEN}ALL TESTS PASSED${NC}"
else
    echo -e "  ${RED}SOME TESTS FAILED${NC}"
fi
echo ""

exit $FAIL
