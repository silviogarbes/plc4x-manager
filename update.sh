#!/bin/bash
# =============================================================
# PLC4X Manager — Update Script
# =============================================================
# Usage:
#   ./update.sh              # Update to latest version
#   ./update.sh v1.2.0       # Update to specific version
#   ./update.sh --check      # Check for updates without applying
#   ./update.sh --rollback   # Rollback to previous version
# =============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

COMPOSE="docker compose"

echo ""
echo -e "${CYAN}=============================================================${NC}"
echo -e "${CYAN}  PLC4X Manager — Update Tool${NC}"
echo -e "${CYAN}=============================================================${NC}"
echo ""

# Get current version
CURRENT_TAG=$(git describe --tags --exact-match 2>/dev/null || echo "untagged")
CURRENT_COMMIT=$(git rev-parse --short HEAD)
echo -e "  Current version: ${GREEN}${CURRENT_TAG}${NC} (${CURRENT_COMMIT})"

# Fetch latest from remote
echo -e "  Fetching updates from GitHub..."
git fetch origin --tags --quiet 2>/dev/null || {
    echo -e "  ${RED}ERROR: Cannot reach GitHub. Check your network.${NC}"
    exit 1
}

# Get latest tag
LATEST_TAG=$(git tag -l 'v*' --sort=-v:refname | head -1)
LATEST_COMMIT=$(git rev-parse --short origin/main 2>/dev/null)

if [ -z "$LATEST_TAG" ]; then
    LATEST_TAG="(no tags)"
fi

echo -e "  Latest release:  ${GREEN}${LATEST_TAG}${NC}"
echo -e "  Latest commit:   ${LATEST_COMMIT}"
echo ""

# ─── Check mode ────────────────────────────────────────
if [ "$1" = "--check" ]; then
    if [ "$CURRENT_COMMIT" = "$LATEST_COMMIT" ]; then
        echo -e "  ${GREEN}You are up to date.${NC}"
    else
        BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo "?")
        echo -e "  ${YELLOW}Update available: ${BEHIND} new commits.${NC}"
        echo ""
        echo "  Recent changes:"
        git log --oneline HEAD..origin/main 2>/dev/null | head -10 | sed 's/^/    /'
    fi
    echo ""
    exit 0
fi

# ─── Rollback mode ─────────────────────────────────────
if [ "$1" = "--rollback" ]; then
    PREV_TAG=$(git tag -l 'v*' --sort=-v:refname | sed -n '2p')
    if [ -z "$PREV_TAG" ]; then
        echo -e "  ${RED}No previous version to rollback to.${NC}"
        exit 1
    fi
    echo -e "  ${YELLOW}Rolling back to ${PREV_TAG}...${NC}"
    echo ""
    read -p "  Continue? [y/N] " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "  Cancelled."
        exit 0
    fi
    git checkout "$PREV_TAG"
    echo ""
    echo -e "  ${YELLOW}Rebuilding containers...${NC}"
    $COMPOSE build
    $COMPOSE up -d
    echo ""
    echo -e "  ${GREEN}Rolled back to ${PREV_TAG}${NC}"
    exit 0
fi

# ─── Update mode ───────────────────────────────────────
TARGET="$1"

if [ -n "$TARGET" ]; then
    # Update to specific version
    if ! git rev-parse "$TARGET" >/dev/null 2>&1; then
        echo -e "  ${RED}Version '${TARGET}' not found.${NC}"
        echo "  Available versions:"
        git tag -l 'v*' --sort=-v:refname | head -10 | sed 's/^/    /'
        exit 1
    fi
    echo -e "  Updating to: ${GREEN}${TARGET}${NC}"
else
    # Update to latest
    TARGET="origin/main"
    if [ "$CURRENT_COMMIT" = "$LATEST_COMMIT" ]; then
        echo -e "  ${GREEN}Already up to date.${NC}"
        echo ""
        exit 0
    fi
    BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo "?")
    echo -e "  ${YELLOW}${BEHIND} new commits available.${NC}"
fi

echo ""
echo "  Changes:"
git log --oneline HEAD.."$TARGET" 2>/dev/null | head -15 | sed 's/^/    /'
echo ""

read -p "  Apply update and rebuild? [y/N] " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "  Cancelled."
    exit 0
fi

# Backup current state
echo ""
echo -e "  ${CYAN}[1/4] Saving current state...${NC}"
BACKUP_TAG="backup-$(date +%Y%m%d-%H%M%S)"
git tag "$BACKUP_TAG" 2>/dev/null || true

# Pull changes
echo -e "  ${CYAN}[2/4] Pulling changes...${NC}"
if echo "$TARGET" | grep -q "^v"; then
    git checkout "$TARGET"
else
    git pull origin main --ff-only || {
        echo -e "  ${RED}Cannot fast-forward. Local changes may conflict.${NC}"
        echo -e "  Run: git stash && ./update.sh && git stash pop"
        exit 1
    }
fi

# Check if .env needs new variables
echo -e "  ${CYAN}[3/4] Checking configuration...${NC}"
if [ -f .env.example ]; then
    MISSING=""
    while IFS= read -r line; do
        key=$(echo "$line" | grep -oP '^[A-Z_]+=')
        if [ -n "$key" ] && ! grep -q "^${key}" .env 2>/dev/null; then
            MISSING="${MISSING}\n    ${line}"
        fi
    done < .env.example
    if [ -n "$MISSING" ]; then
        echo -e "  ${YELLOW}New configuration variables found:${NC}"
        echo -e "$MISSING"
        echo -e "  ${YELLOW}Add them to .env or they will use defaults.${NC}"
        echo ""
    fi
fi

# Rebuild and restart
echo -e "  ${CYAN}[4/4] Rebuilding containers...${NC}"
$COMPOSE build
$COMPOSE up -d

echo ""
echo -e "${GREEN}=============================================================${NC}"
echo -e "${GREEN}  Update complete!${NC}"
NEW_TAG=$(git describe --tags --exact-match 2>/dev/null || echo "$(git rev-parse --short HEAD)")
echo -e "  Version: ${GREEN}${NEW_TAG}${NC}"
echo -e "  Backup:  ${BACKUP_TAG} (use --rollback or: git checkout ${BACKUP_TAG})"
echo -e "${GREEN}=============================================================${NC}"
echo ""
