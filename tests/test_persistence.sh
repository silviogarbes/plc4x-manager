#!/bin/bash
# =============================================================
# PLC4X Manager - Data Persistence Test Suite
# =============================================================
# This script validates that all data survives container restarts.
# It restarts the plc4x-server container and verifies that
# configuration, users, security, and backups persist.
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
CONTAINER_NAME="plc4x-server"

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

wait_for_server() {
    local max_attempts=$1
    local attempt=0
    echo "    Waiting for PLC4X Manager to become available..."
    while [ $attempt -lt $max_attempts ]; do
        attempt=$((attempt + 1))
        CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$BASE/api/server/status" -H "$AUTH_HEADER" 2>/dev/null)
        if [ "$CODE" = "200" ] || [ "$CODE" = "401" ]; then
            echo "    Server is back online (attempt $attempt/$max_attempts)"
            # Re-authenticate after restart
            get_auth_token
            return 0
        fi
        echo "    Attempt $attempt/$max_attempts - not ready yet..."
        sleep 5
    done
    echo -e "    ${RED}Server did not come back within expected time!${NC}"
    return 1
}

restart_plc4x_server() {
    echo "    Restarting $CONTAINER_NAME container..."
    docker restart "$CONTAINER_NAME" > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        echo -e "    ${RED}Failed to restart $CONTAINER_NAME container!${NC}"
        return 1
    fi
    echo "    Container restart initiated. Waiting for services..."
    sleep 10
    wait_for_server 12
    return $?
}

echo ""
echo "============================================================="
echo "  PLC4X Manager - Data Persistence Test Suite"
echo "============================================================="
echo ""

get_auth_token

# =============================================================
echo -e "${YELLOW}[1] PRE-RESTART: CAPTURE CURRENT STATE${NC}"
# =============================================================

echo "    Saving current configuration snapshot..."

CONFIG_BEFORE=$(curl -s -H "$AUTH_HEADER" "$BASE/api/config")
assert "Pre-restart: config is accessible" '"version"' "$CONFIG_BEFORE"
assert "Pre-restart: config has devices" '"devices"' "$CONFIG_BEFORE"

USERS_BEFORE=$(curl -s -H "$AUTH_HEADER" "$BASE/api/users")
assert "Pre-restart: users are accessible" "admin" "$USERS_BEFORE"

SECURITY_BEFORE=$(curl -s -H "$AUTH_HEADER" "$BASE/api/security/status")
assert "Pre-restart: security is initialized" '"initialized":true' "$SECURITY_BEFORE"

BACKUPS_BEFORE=$(curl -s -H "$AUTH_HEADER" "$BASE/api/backups")
assert "Pre-restart: backups are accessible" "[" "$BACKUPS_BEFORE"

DEVICES_BEFORE=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "Pre-restart: devices are accessible" '"name"' "$DEVICES_BEFORE"

# Save specific values to compare later
CONFIG_NAME_BEFORE=$(echo "$CONFIG_BEFORE" | grep -o '"name":"[^"]*"' | head -1)
CONFIG_VERSION_BEFORE=$(echo "$CONFIG_BEFORE" | grep -o '"version":"[^"]*"' | head -1)
CONFIG_PORT_BEFORE=$(echo "$CONFIG_BEFORE" | grep -o '"tcpPort":[0-9]*' | head -1)

echo "    Captured: $CONFIG_NAME_BEFORE, $CONFIG_VERSION_BEFORE, $CONFIG_PORT_BEFORE"

# =============================================================
echo ""
echo -e "${YELLOW}[2] RESTART PLC4X-SERVER CONTAINER${NC}"
# =============================================================

if ! restart_plc4x_server; then
    echo -e "${RED}ABORTING: Server did not restart properly.${NC}"
    exit 1
fi

# =============================================================
echo ""
echo -e "${YELLOW}[3] POST-RESTART: VERIFY CONFIGURATION PERSISTS${NC}"
# =============================================================

CONFIG_AFTER=$(curl -s -H "$AUTH_HEADER" "$BASE/api/config")
assert "Post-restart: config is accessible" '"version"' "$CONFIG_AFTER"
assert "Post-restart: config has devices" '"devices"' "$CONFIG_AFTER"

CONFIG_NAME_AFTER=$(echo "$CONFIG_AFTER" | grep -o '"name":"[^"]*"' | head -1)
CONFIG_VERSION_AFTER=$(echo "$CONFIG_AFTER" | grep -o '"version":"[^"]*"' | head -1)
CONFIG_PORT_AFTER=$(echo "$CONFIG_AFTER" | grep -o '"tcpPort":[0-9]*' | head -1)

assert "Config name persisted" "$CONFIG_NAME_BEFORE" "$CONFIG_NAME_AFTER"
assert "Config version persisted" "$CONFIG_VERSION_BEFORE" "$CONFIG_VERSION_AFTER"
assert "Config tcpPort persisted" "$CONFIG_PORT_BEFORE" "$CONFIG_PORT_AFTER"

# =============================================================
echo ""
echo -e "${YELLOW}[4] POST-RESTART: VERIFY USERS PERSIST${NC}"
# =============================================================

USERS_AFTER=$(curl -s -H "$AUTH_HEADER" "$BASE/api/users")
assert "Post-restart: admin user still exists" "admin" "$USERS_AFTER"
assert "Post-restart: admin has admin-group" "admin-group" "$USERS_AFTER"

# =============================================================
echo ""
echo -e "${YELLOW}[5] POST-RESTART: VERIFY SECURITY PERSISTS${NC}"
# =============================================================

SECURITY_AFTER=$(curl -s -H "$AUTH_HEADER" "$BASE/api/security/status")
assert "Post-restart: security is initialized" '"initialized":true' "$SECURITY_AFTER"
assert "Post-restart: password file present" '"passwordFile":true' "$SECURITY_AFTER"
assert "Post-restart: keystore present" '"keystore":true' "$SECURITY_AFTER"
assert "Post-restart: PKI directory present" '"pkiDirectory":true' "$SECURITY_AFTER"

# =============================================================
echo ""
echo -e "${YELLOW}[6] POST-RESTART: VERIFY BACKUPS PERSIST${NC}"
# =============================================================

BACKUPS_AFTER=$(curl -s -H "$AUTH_HEADER" "$BASE/api/backups")
assert "Post-restart: backups are accessible" "[" "$BACKUPS_AFTER"

# Count backups before and after
BACKUP_COUNT_BEFORE=$(echo "$BACKUPS_BEFORE" | grep -o '"config_' | wc -l)
BACKUP_COUNT_AFTER=$(echo "$BACKUPS_AFTER" | grep -o '"config_' | wc -l)

TOTAL=$((TOTAL + 1))
if [ "$BACKUP_COUNT_AFTER" -ge "$BACKUP_COUNT_BEFORE" ]; then
    PASS=$((PASS + 1))
    echo -e "  ${GREEN}PASS${NC} [$TOTAL] Backup count preserved (before: $BACKUP_COUNT_BEFORE, after: $BACKUP_COUNT_AFTER)"
else
    FAIL=$((FAIL + 1))
    echo -e "  ${RED}FAIL${NC} [$TOTAL] Backup count decreased (before: $BACKUP_COUNT_BEFORE, after: $BACKUP_COUNT_AFTER)"
fi

# =============================================================
echo ""
echo -e "${YELLOW}[7] POST-RESTART: VERIFY DEVICES PERSIST${NC}"
# =============================================================

DEVICES_AFTER=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "Post-restart: original Simulated device intact" "Simulated" "$DEVICES_AFTER"
assert "Post-restart: RandomInteger tag intact" "RandomInteger" "$DEVICES_AFTER"
assert "Post-restart: RandomFloat tag intact" "RandomFloat" "$DEVICES_AFTER"
assert "Post-restart: StateVariable tag intact" "StateVariable" "$DEVICES_AFTER"

# =============================================================
echo ""
echo -e "${YELLOW}[8] PERSISTENCE ACROSS RESTART: ADD DEVICE, RESTART, VERIFY${NC}"
# =============================================================

echo "    Adding test device PersistTestPLC..."

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/devices" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" \
  -d '{"name":"PersistTestPLC","connectionString":"simulated://127.0.0.1","tags":[{"alias":"PersistVar1","address":"RANDOM/Temporary:DINT"},{"alias":"PersistVar2","address":"STATE/Temporary:REAL"}]}')
assert_status "Add PersistTestPLC device" "201" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "PersistTestPLC exists before restart" "PersistTestPLC" "$RES"
assert "PersistVar1 tag exists before restart" "PersistVar1" "$RES"
assert "PersistVar2 tag exists before restart" "PersistVar2" "$RES"

echo ""
echo "    Restarting server to test device persistence..."

if ! restart_plc4x_server; then
    echo -e "${RED}ABORTING: Server did not restart properly.${NC}"
    # Attempt cleanup before exit
    curl -s -X DELETE "$BASE/api/devices/PersistTestPLC" -H "$AUTH_HEADER" > /dev/null 2>&1
    exit 1
fi

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "PersistTestPLC survives restart" "PersistTestPLC" "$RES"
assert "PersistVar1 tag survives restart" "PersistVar1" "$RES"
assert "PersistVar2 tag survives restart" "PersistVar2" "$RES"
assert "Connection string persists" "simulated://127.0.0.1" "$RES"

# Verify via individual device tags endpoint
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices/PersistTestPLC/tags")
assert "Tags endpoint returns PersistVar1" "PersistVar1" "$RES"
assert "Tags endpoint returns PersistVar2" "PersistVar2" "$RES"
assert "Tag address persists (RANDOM)" "RANDOM/Temporary:DINT" "$RES"
assert "Tag address persists (STATE)" "STATE/Temporary:REAL" "$RES"

# =============================================================
echo ""
echo -e "${YELLOW}[9] CLEANUP${NC}"
# =============================================================

echo "    Removing test device PersistTestPLC..."

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/devices/PersistTestPLC" -H "$AUTH_HEADER")
assert_status "DELETE PersistTestPLC returns 200" "200" "$CODE"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert_not_contains "PersistTestPLC removed after cleanup" "PersistTestPLC" "$RES"

# Verify original system state is intact after all persistence tests
RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/devices")
assert "Original Simulated device still intact after all tests" "Simulated" "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/users")
assert "Admin user still exists after all tests" "admin" "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/server/status")
assert "Server running after all tests" '"running":true' "$RES"

RES=$(curl -s -H "$AUTH_HEADER" "$BASE/api/security/status")
assert "Security initialized after all tests" '"initialized":true' "$RES"

# =============================================================
echo ""
echo "============================================================="
echo -e "  PERSISTENCE RESULTS: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}, $TOTAL total"
echo "============================================================="
echo ""

if [ $FAIL -eq 0 ]; then
    echo -e "  ${GREEN}ALL PERSISTENCE TESTS PASSED${NC}"
else
    echo -e "  ${RED}SOME PERSISTENCE TESTS FAILED${NC}"
fi
echo ""

exit $FAIL
